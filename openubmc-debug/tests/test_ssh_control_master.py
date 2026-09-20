from __future__ import annotations

import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
RUNTIME_ROOT = REPO_ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(RUNTIME_ROOT))

from _remote_common import (  # noqa: E402
    OpenSshControlMasterTransport,
    run_ssh,
    ssh_transport_failure_code,
)
from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    OpenUBMCTaskRun,
    ResolvedSshCredentials,
    TargetPolicy,
    TargetSpec,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_checked(command: list[str]) -> None:
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


class EphemeralSshd:
    def __init__(self) -> None:
        self.tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self.port = free_port()
        self.process: subprocess.Popen[str] | None = None
        self.user = pwd.getpwuid(os.getuid()).pw_name

    def __enter__(self) -> "EphemeralSshd":
        if not Path("/usr/sbin/sshd").is_file():
            raise unittest.SkipTest("/usr/sbin/sshd is unavailable")
        self.tempdir = tempfile.TemporaryDirectory(prefix="openubmc-runtime-sshd-")
        self.root = Path(self.tempdir.name)
        host_key = self.root / "host_ed25519"
        client_key = self.root / "client_ed25519"
        authorized_keys = self.root / "authorized_keys"
        known_hosts = self.root / "known_hosts"
        config = self.root / "sshd_config"
        log = self.root / "sshd.log"

        run_checked(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)])
        run_checked(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)])
        authorized_keys.write_text(
            client_key.with_suffix(".pub").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        authorized_keys.chmod(0o600)
        host_fields = host_key.with_suffix(".pub").read_text(encoding="utf-8").split()
        known_hosts.write_text(
            f"[127.0.0.1]:{self.port} {host_fields[0]} {host_fields[1]}\n",
            encoding="utf-8",
        )
        config.write_text(
            "\n".join(
                [
                    f"Port {self.port}",
                    "ListenAddress 127.0.0.1",
                    f"PidFile {self.root / 'sshd.pid'}",
                    f"HostKey {host_key}",
                    f"AuthorizedKeysFile {authorized_keys}",
                    "StrictModes no",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "ChallengeResponseAuthentication no",
                    "UsePAM no",
                    "PubkeyAuthentication yes",
                    "AuthenticationMethods publickey",
                    "PermitRootLogin yes",
                    f"AllowUsers {self.user}",
                    "PrintMotd no",
                    "PrintLastLog no",
                    "PermitTTY no",
                    "AllowTcpForwarding no",
                    "X11Forwarding no",
                    "MaxSessions 32",
                    "LogLevel VERBOSE",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        run_checked(["/usr/sbin/sshd", "-t", "-f", str(config)])
        self.process = subprocess.Popen(
            ["/usr/sbin/sshd", "-D", "-f", str(config), "-E", str(log)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise RuntimeError(f"ephemeral sshd exited: {stderr.strip()}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return self
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("ephemeral sshd did not start")

    @property
    def client_key(self) -> str:
        assert self.root is not None
        return str(self.root / "client_ed25519")

    @property
    def known_hosts(self) -> str:
        assert self.root is not None
        return str(self.root / "known_hosts")

    def authentication_count(self) -> int:
        assert self.root is not None
        log = self.root / "sshd.log"
        if not log.exists():
            return 0
        return len(
            re.findall(
                r"Accepted publickey for ",
                log.read_text(encoding="utf-8", errors="replace"),
            )
        )

    def __exit__(self, *_exc: object) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process is not None and self.process.stderr is not None:
            self.process.stderr.close()
        if self.tempdir is not None:
            self.tempdir.cleanup()


def runtime_target(fixture: EphemeralSshd) -> TargetSpec:
    selector = CredentialSelector.for_ssh(
        user=fixture.user,
        user_env="",
        password_env="",
        identity_file=fixture.client_key,
        environ={},
    )
    return TargetSpec(
        host="127.0.0.1",
        ssh_port=fixture.port,
        credential_selector_fingerprint=selector.fingerprint,
        policy=TargetPolicy(ssh_host_key_policy="strict"),
    )


class ControlMasterTransportTests(unittest.TestCase):
    def test_password_auth_uses_local_input_without_argv_or_environment_secrets(self) -> None:
        secret = "fixture-password-never-in-process-metadata"
        completed = subprocess.CompletedProcess([], 0, "", "")
        target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint="0" * 64,
            policy=TargetPolicy(ssh_host_key_policy="insecure"),
        )
        credentials = ResolvedSshCredentials(user="root", password=secret)
        transport = OpenSshControlMasterTransport()

        with (
            mock.patch("_remote_common.shutil.which", return_value="/usr/bin/tool"),
            mock.patch("_remote_common.subprocess.run", return_value=completed) as run,
            mock.patch.object(transport, "check_master", return_value=True),
        ):
            master = transport.open_master(target=target, credentials=credentials)
            master.closed = True
            master.tempdir.cleanup()

        command = list(run.call_args.args[0])
        call = run.call_args.kwargs
        self.assertEqual(command[:3], ["sshpass", "-d", "0"])
        self.assertNotIn(secret, command)
        self.assertEqual(call.get("input"), secret + "\n")
        environment = call.get("env") or {}
        self.assertFalse(any(secret == value for value in environment.values()))
        self.assertNotIn("SSHPASS", environment)

    def test_channel_host_key_options_must_match_the_authenticated_lease(self) -> None:
        transport = OpenSshControlMasterTransport(
            host_key_policy="strict",
            known_hosts_file="/private/known_hosts",
        )
        target = mock.Mock()
        target.policy.ssh_host_key_policy = "strict"

        transport.validate_channel_options(
            target=target,
            host_key_policy="strict",
            known_hosts_file="/private/known_hosts",
            allow_insecure_host_key=False,
        )
        with self.assertRaisesRegex(ValueError, "bound SSH lease"):
            transport.validate_channel_options(
                target=target,
                host_key_policy="accept-new",
                known_hosts_file="/private/known_hosts",
                allow_insecure_host_key=False,
            )
        with self.assertRaisesRegex(ValueError, "bound SSH lease"):
            transport.validate_channel_options(
                target=target,
                host_key_policy="strict",
                known_hosts_file="/other/known_hosts",
                allow_insecure_host_key=False,
            )

    def test_healthy_channel_results_do_not_spawn_control_checks(self) -> None:
        transport = OpenSshControlMasterTransport()
        master = mock.Mock()
        master.closed = False
        with mock.patch.object(transport, "check_master", return_value=True) as check:
            self.assertFalse(
                transport.channel_lost_master(
                    master,
                    completed_result := subprocess.CompletedProcess(
                        args=["ssh"],
                        returncode=0,
                        stdout="ok\n",
                        stderr="",
                    ),
                )
            )
            completed_result.timed_out = True
            completed_result.returncode = 124
            self.assertFalse(transport.channel_lost_master(master, completed_result))
            completed_result.timed_out = False
            completed_result.output_limit_exceeded = True
            completed_result.returncode = 125
            self.assertFalse(transport.channel_lost_master(master, completed_result))
            check.assert_not_called()

    def test_canonical_lane_and_openssh_transport_compose_as_one_lease(self) -> None:
        with EphemeralSshd() as fixture:
            selector = CredentialSelector.for_ssh(
                user=fixture.user,
                user_env="",
                password_env="",
                identity_file=fixture.client_key,
                environ={},
            )
            target = TargetSpec(
                host="127.0.0.1",
                ssh_port=fixture.port,
                credential_selector_fingerprint=selector.fingerprint,
                policy=TargetPolicy(ssh_host_key_policy="strict"),
            )
            credentials = ResolvedSshCredentials(
                user=fixture.user,
                port=fixture.port,
                identity_file=fixture.client_key,
            )
            task = OpenUBMCTaskRun(
                task_id="integration-object-alarm",
                credential_resolver=CredentialResolver(
                    lambda _selector: credentials
                ),
            )
            transport = OpenSshControlMasterTransport(
                known_hosts_file=fixture.known_hosts,
            )
            lane = task.ssh_lane(
                target=target,
                credential_selector=selector,
                lease_name="debug-object-alarm",
                transport=transport,
            )
            before = fixture.authentication_count()

            first = lane.run_ssh(
                "127.0.0.1",
                fixture.user,
                "",
                "printf 'object\\n'",
                2,
                port=fixture.port,
                identity_file=fixture.client_key,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            second = lane.run_ssh(
                "127.0.0.1",
                fixture.user,
                "",
                "printf 'alarm\\n'",
                2,
                port=fixture.port,
                identity_file=fixture.client_key,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            time.sleep(0.05)

            self.assertEqual(first.stdout, "object\n")
            self.assertEqual(second.stdout, "alarm\n")
            self.assertEqual(fixture.authentication_count() - before, 1)
            self.assertEqual(task.runtime_status()["metrics"]["ssh_channels"], 2)
            lane.close()

    def test_channels_reuse_authentication_and_preserve_bounded_failures(self) -> None:
        with EphemeralSshd() as fixture:
            transport = OpenSshControlMasterTransport(
                known_hosts_file=fixture.known_hosts,
                persist_seconds=60,
            )
            credentials = ResolvedSshCredentials(
                user=fixture.user,
                port=fixture.port,
                identity_file=fixture.client_key,
            )
            before = fixture.authentication_count()
            master = transport.open_master(
                target=runtime_target(fixture),
                credentials=credentials,
            )

            success = transport.run_channel(
                master,
                "printf 'ok\\n'",
                timeout=2,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            nonzero = transport.run_channel(
                master,
                "printf 'error\\n' >&2; exit 7",
                timeout=2,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            timed_out = transport.run_channel(
                master,
                "sleep 0.30",
                timeout=0.05,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            overflow = transport.run_channel(
                master,
                "head -c 4096 /dev/zero | tr '\\000' x",
                timeout=2,
                stdout_limit_bytes=128,
                stderr_limit_bytes=4096,
            )
            after_error = transport.run_channel(
                master,
                "printf 'still-alive\\n'",
                timeout=2,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            time.sleep(0.05)

            self.assertEqual(success.returncode, 0)
            self.assertEqual(success.stdout, "ok\n")
            self.assertEqual(nonzero.returncode, 7)
            self.assertEqual(nonzero.stderr, "error\n")
            self.assertTrue(getattr(timed_out, "timed_out", False))
            self.assertTrue(getattr(overflow, "output_limit_exceeded", False))
            self.assertEqual(after_error.stdout, "still-alive\n")
            self.assertTrue(transport.check_master(master))
            self.assertEqual(fixture.authentication_count() - before, 1)
            for result in (success, nonzero, timed_out, overflow, after_error):
                self.assertFalse(transport.channel_lost_master(master, result))
                self.assertEqual(
                    getattr(result, "ssh_connection_mode", ""),
                    "control-master-channel",
                )
                self.assertIsInstance(getattr(result, "stdout_bytes_captured", None), int)

            transport.close_master(master)

    def test_missing_master_channel_never_silently_reauthenticates(self) -> None:
        with EphemeralSshd() as fixture:
            transport = OpenSshControlMasterTransport(
                known_hosts_file=fixture.known_hosts,
            )
            credentials = ResolvedSshCredentials(
                user=fixture.user,
                port=fixture.port,
                identity_file=fixture.client_key,
            )
            master = transport.open_master(
                target=runtime_target(fixture),
                credentials=credentials,
            )
            time.sleep(0.05)
            authenticated = fixture.authentication_count()
            transport.close_master(master)

            result = transport.run_channel(
                master,
                "printf 'must-not-run\\n'",
                timeout=2,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            time.sleep(0.05)

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("must-not-run", result.stdout or "")
            self.assertEqual(fixture.authentication_count(), authenticated)
            self.assertTrue(transport.channel_lost_master(master, result))

    def test_control_master_channel_matches_direct_transport_metadata(self) -> None:
        with EphemeralSshd() as fixture:
            direct = run_ssh(
                "127.0.0.1",
                fixture.user,
                "",
                "printf 'same\\n'",
                2,
                port=fixture.port,
                identity_file=fixture.client_key,
                host_key_policy="strict",
                known_hosts_file=fixture.known_hosts,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )
            transport = OpenSshControlMasterTransport(
                known_hosts_file=fixture.known_hosts,
            )
            master = transport.open_master(
                target=runtime_target(fixture),
                credentials=ResolvedSshCredentials(
                    user=fixture.user,
                    port=fixture.port,
                    identity_file=fixture.client_key,
                ),
            )
            multiplexed = transport.run_channel(
                master,
                "printf 'same\\n'",
                timeout=2,
                stdout_limit_bytes=4096,
                stderr_limit_bytes=4096,
            )

            self.assertEqual(multiplexed.returncode, direct.returncode)
            self.assertEqual(multiplexed.stdout, direct.stdout)
            self.assertEqual(multiplexed.stderr, direct.stderr)
            self.assertEqual(
                ssh_transport_failure_code(multiplexed),
                ssh_transport_failure_code(direct),
            )
            self.assertEqual(
                getattr(multiplexed, "ssh_host_key_policy", ""),
                getattr(direct, "ssh_host_key_policy", ""),
            )
            transport.close_master(master)


if __name__ == "__main__":
    unittest.main()
