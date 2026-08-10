from __future__ import annotations

import base64
import gzip
import hashlib
from pathlib import Path
import re
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-live-patch"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-live-patch" / "scripts"))

from openubmc_target_runtime import TelnetCommandResult  # noqa: E402
from openubmc_live_patch.runtime_backend import LivePatchMcpBackend  # noqa: E402
from runtime_cli import (  # type: ignore  # noqa: E402
    RuntimeMutationFailed,
    run_runtime_mutation,
)


TEST_DEADLINE_SECONDS = 30


def decoded_shell_script(command: str) -> str:
    match = re.search(
        r"printf %s '?([A-Za-z0-9+/=]+)'?\|busybox base64 -d",
        command,
    )
    if match is None:
        raise AssertionError("compressed Live Patch script is unavailable")
    return gzip.decompress(base64.b64decode(match.group(1))).decode("utf-8")


class FakeSshTransport:
    def __init__(self, *, upload_returncode: int = 0) -> None:
        self.uploads: list[tuple[str, str]] = []
        self.upload_returncode = upload_returncode

    @staticmethod
    def open_master(*, target, credentials):
        return object()

    @staticmethod
    def check_master(_master) -> bool:
        return True

    def upload_file(self, _master, local_path: str, remote_path: str, **_kwargs):
        self.uploads.append((local_path, remote_path))
        return type(
            "UploadResult",
            (),
            {
                "returncode": self.upload_returncode,
                "stdout": "",
                "stderr": "upload failed" if self.upload_returncode else "",
            },
        )()

    @staticmethod
    def run_channel(_master, _command: str, **_kwargs):
        return type(
            "ChannelResult",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

    @staticmethod
    def channel_lost_master(_master, _result) -> bool:
        return False

    @staticmethod
    def close_master(_master) -> None:
        return None


class FakeTelnetTransport:
    def __init__(
        self,
        digest: str,
        *,
        mount_options: str = "rw,relatime",
        deploy_digest: str = "",
        restore_mount_ok: bool = True,
        path_guard_ok: bool = True,
    ) -> None:
        self.digest = digest
        self.mount_options = mount_options
        self.deploy_digest = deploy_digest or digest
        self.restore_mount_ok = restore_mount_ok
        self.path_guard_ok = path_guard_ok
        self.current_mode = "644"
        self.current_uid = 0
        self.current_gid = 0
        self.commands: list[str] = []

    @staticmethod
    def open_session(*, target, credentials):
        return object()

    def run_command(self, _session, command: str, **_kwargs):
        self.commands.append(command)
        returncode = 0
        if "live_patch_paths_safe" in command:
            if self.path_guard_ok:
                stdout = "live_patch_paths_safe"
            else:
                stdout = "live_patch_paths_unsafe"
                returncode = 1
        elif "live_patch_codec_ready" in command:
            stdout = "live_patch_codec_ready"
        elif "/proc/mounts" in command:
            stdout = self.mount_options
        elif "remount_rw_ok" in command:
            stdout = "remount_rw_ok"
        elif "remount_ro_ok" in command:
            if self.restore_mount_ok:
                stdout = "remount_ro_ok"
            else:
                stdout = "remount_ro_failed"
                returncode = 1
        elif "target_exists" in command:
            stdout = "target_missing"
        elif "p=r;" in command:
            script = decoded_shell_script(command)
            mode = re.search(r"chmod ([0-7]{3,4}) ", script)
            self.current_mode = mode.group(1) if mode else "440"
            self.current_uid = 104
            self.current_gid = 104
            stdout = (
                f"backup_sha256={self.digest}\n"
                f"remote_sha256={self.digest}\n"
                "backup_mode=440\n"
                f"remote_mode={self.current_mode}\n"
                "backup_uid=104\n"
                f"remote_uid={self.current_uid}\n"
                "backup_gid=104\n"
                f"remote_gid={self.current_gid}\nrestore_ok"
            )
        elif "p=i;" in command:
            script = decoded_shell_script(command)
            mode = re.search(r"chmod ([0-7]{3,4}) ", script)
            owner = re.search(r"chown ([0-9]+):([0-9]+) ", script)
            self.current_mode = mode.group(1) if mode else "644"
            if owner:
                self.current_uid = int(owner.group(1))
                self.current_gid = int(owner.group(2))
            stdout = (
                f"remote_sha256={self.deploy_digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\ndeploy_ok"
            )
        elif "verify_sha256" in command:
            stdout = (
                f"remote_sha256={self.digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\nverify_sha256"
            )
        elif "restart_ok" in command:
            stdout = "restart_ok"
        else:
            stdout = "ok"
        return TelnetCommandResult(
            stdout=stdout,
            returncode=returncode,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            raw=stdout.encode(),
        )

    @staticmethod
    def command_invalidates_session(_session, result) -> bool:
        return not result.ok

    @staticmethod
    def close_session(_session) -> None:
        return None


class LivePatchRuntimeCliTests(unittest.TestCase):
    def backend_factory(self, ssh, telnet):
        return lambda store: LivePatchMcpBackend(
            journal_store=store,
            credential_loader=lambda _arguments: {
                "ssh": {"user": "root", "password": "ssh-secret"},
                "telnet": {"user": "root", "password": "telnet-secret"},
            },
            ssh_transport_factory=lambda _arguments: ssh,
            telnet_transport_factory=lambda _arguments: telnet,
        )

    def test_apply_retry_uses_deterministic_identity_and_durable_journal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            arguments = {
                "action": "apply",
                "intent": "live_patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = run_runtime_mutation(
                arguments,
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )
            replayed = run_runtime_mutation(
                arguments,
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )

        self.assertEqual(first["operation_id"], replayed["operation_id"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(len(ssh.uploads), 1)

    def test_read_only_guard_failure_replans_and_retries_same_operation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest, path_guard_ok=False)
            arguments = {
                "action": "apply",
                "intent": "live_patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }

            with self.assertRaises(RuntimeMutationFailed) as captured:
                run_runtime_mutation(
                    arguments,
                    state_dir=root / "state",
                    backend_factory=self.backend_factory(ssh, telnet),
                )

            self.assertEqual(captured.exception.journal["stage"], "replan_required")
            self.assertFalse(captured.exception.journal["effects_started"])
            self.assertEqual(ssh.uploads, [])

            telnet.path_guard_ok = True
            retried = run_runtime_mutation(
                arguments,
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )

        self.assertEqual(retried["journal"]["stage"], "verified")
        self.assertFalse(retried["idempotent_replay"])
        self.assertEqual(len(ssh.uploads), 1)

    def test_ssh_staging_failure_remains_a_blocking_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport(upload_returncode=1)
            telnet = FakeTelnetTransport(digest)

            with self.assertRaises(RuntimeMutationFailed) as captured:
                run_runtime_mutation(
                    {
                        "action": "apply",
                        "intent": "live_patch",
                        "ip": "bmc.example",
                        "local_path": str(local),
                        "remote_path": "/opt/bmc/apps/demo/unit.lua",
                        "restart_scope": "none",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    state_dir=root / "state",
                    backend_factory=self.backend_factory(ssh, telnet),
                )

        self.assertEqual(captured.exception.journal["stage"], "mutation_failed")
        self.assertTrue(captured.exception.journal["effects_started"])
        self.assertEqual(len(ssh.uploads), 1)

    def test_rollback_uses_a_distinct_identity_and_never_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport("b" * 64)
            result = run_runtime_mutation(
                {
                    "action": "rollback",
                    "intent": "live_patch",
                    "ip": "bmc.example",
                    "backup_path": "/tmp/unit.lua.bak.1",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "none",
                    "deadline": TEST_DEADLINE_SECONDS,
                },
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )

        self.assertTrue(result["operation_id"].startswith("live-patch-rollback-"))
        self.assertEqual(result["action"], "rollback")
        self.assertEqual(ssh.uploads, [])

    def test_staging_and_backup_locations_are_part_of_the_operation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            common = {
                "action": "apply",
                "intent": "live_patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = run_runtime_mutation(
                {
                    **common,
                    "staging_path": "/tmp/stage-a",
                    "backup_dir": "/tmp/backups-a",
                },
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )
            different_backup = run_runtime_mutation(
                {
                    **common,
                    "staging_path": "/tmp/stage-a",
                    "backup_dir": "/tmp/backups-b",
                },
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )
            different_staging = run_runtime_mutation(
                {
                    **common,
                    "staging_path": "/tmp/stage-b",
                    "backup_dir": "/tmp/backups-b",
                },
                state_dir=root / "state",
                backend_factory=self.backend_factory(ssh, telnet),
            )

        self.assertEqual(
            len(
                {
                    first["operation_id"],
                    different_backup["operation_id"],
                    different_staging["operation_id"],
                }
            ),
            3,
        )
        self.assertEqual(
            [remote for _local, remote in ssh.uploads],
            ["/tmp/stage-a", "/tmp/stage-a", "/tmp/stage-b"],
        )

    def test_failure_exposes_durable_mount_restoration_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(
                digest,
                mount_options="ro,relatime",
                deploy_digest="0" * 64,
            )
            with self.assertRaises(RuntimeMutationFailed) as captured:
                run_runtime_mutation(
                    {
                        "action": "apply",
                        "intent": "live_patch",
                        "ip": "bmc.example",
                        "local_path": str(local),
                        "remote_path": "/opt/bmc/apps/demo/unit.lua",
                        "restart_scope": "none",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    state_dir=root / "state",
                    backend_factory=self.backend_factory(ssh, telnet),
                )

        self.assertEqual(captured.exception.journal["stage"], "mutation_failed")
        self.assertTrue(captured.exception.journal["root_mount_restored"])
        self.assertTrue(
            any("mount -o remount,ro /" in command for command in telnet.commands)
        )

    def test_mount_restore_failure_is_durable_and_visible_with_the_mutation_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(
                digest,
                mount_options="ro,relatime",
                deploy_digest="0" * 64,
                restore_mount_ok=False,
            )
            with self.assertRaises(RuntimeMutationFailed) as captured:
                run_runtime_mutation(
                    {
                        "action": "apply",
                        "intent": "live_patch",
                        "ip": "bmc.example",
                        "local_path": str(local),
                        "remote_path": "/opt/bmc/apps/demo/unit.lua",
                        "restart_scope": "none",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    state_dir=root / "state",
                    backend_factory=self.backend_factory(ssh, telnet),
                )

        self.assertEqual(captured.exception.journal["stage"], "mutation_failed")
        self.assertIs(captured.exception.journal["root_mount_restored"], False)
        self.assertIn("root mount restoration also failed", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
