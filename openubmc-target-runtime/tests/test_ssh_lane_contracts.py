from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialResolver,
    CredentialSelector,
    OpenUBMCTaskRun,
    OpenSshControlMasterTransport,
    RemoteReadRequest,
    ResolvedSshCredentials,
    TargetSpec,
)


@dataclass
class FakeChannelResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_limit_exceeded: bool = False
    master_lost: bool = False


@dataclass
class FakeMaster:
    identifier: int
    alive: bool = True


class FakeSshTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        self.checks = 0
        self.channel_commands: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.results: list[FakeChannelResult] = []
        self.channel_options: list[dict[str, object]] = []

    def open_master(self, *, target, credentials):
        self.opens += 1
        self.last_target = target
        self.last_credentials = credentials
        return FakeMaster(self.opens)

    def check_master(self, master: FakeMaster) -> bool:
        self.checks += 1
        return master.alive

    def run_channel(self, master: FakeMaster, remote_command: str, **_kwargs):
        self.channel_commands.append(remote_command)
        result = self.results.pop(0) if self.results else FakeChannelResult(
            stdout=f"{remote_command}\n"
        )
        if result.master_lost:
            master.alive = False
        return result

    def channel_lost_master(
        self,
        master: FakeMaster,
        result: FakeChannelResult,
    ) -> bool:
        return result.master_lost or not master.alive

    def upload_file(
        self,
        master: FakeMaster,
        local_path: str,
        remote_path: str,
        **_kwargs,
    ) -> FakeChannelResult:
        self.uploads.append((local_path, remote_path))
        return FakeChannelResult(stdout="uploaded\n")

    def close_master(self, master: FakeMaster) -> None:
        self.closes += 1
        master.alive = False

    def validate_channel_options(self, **options: object) -> None:
        self.channel_options.append(dict(options))


class ConcurrentFakeSshTransport(FakeSshTransport):
    def __init__(self) -> None:
        super().__init__()
        self.active_channels = 0
        self.max_active_channels = 0
        self.activity_lock = threading.Lock()

    def run_channel(self, master: FakeMaster, remote_command: str, **_kwargs):
        with self.activity_lock:
            self.active_channels += 1
            self.max_active_channels = max(
                self.max_active_channels,
                self.active_channels,
            )
        try:
            time.sleep(0.08)
            self.channel_commands.append(remote_command)
            return FakeChannelResult(stdout=f"{remote_command}\n")
        finally:
            with self.activity_lock:
                self.active_channels -= 1


class SshLaneContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader_calls = 0
        self.selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="OPENUBMC_TEST_PASSWORD",
            identity_file="/private/id_ed25519",
            environ={},
        )
        self.target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=self.selector.fingerprint,
        )

        def load_credentials(_selector: CredentialSelector) -> ResolvedSshCredentials:
            self.loader_calls += 1
            return ResolvedSshCredentials(
                user="debug-user",
                password="private-password",
                identity_file="/private/id_ed25519",
            )

        self.task = OpenUBMCTaskRun(
            task_id="task-ssh-lane",
            credential_resolver=CredentialResolver(load_credentials),
        )
        self.transport = FakeSshTransport()

    def lane(self):
        return self.task.ssh_lane(
            target=self.target,
            credential_selector=self.selector,
            lease_name="debug-object-alarm",
            transport=self.transport,
        )

    def test_one_lease_reuses_one_authentication_and_ssh_derived_state(self) -> None:
        lane = self.lane()
        env_loads = 0
        endpoint_loads = 0

        def load_env():
            nonlocal env_loads
            env_loads += 1
            return {
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus",
                "XDG_RUNTIME_DIR": "/run/user/0",
            }

        def load_endpoint():
            nonlocal endpoint_loads
            endpoint_loads += 1
            return {"service": "alarm.service", "path": "/alarm"}

        lane.run_channel("mdbctl lsclass", timeout=2)
        first_env = lane.get_dbus_environment(load_env)
        lane.run_channel("busctl tree", timeout=2)
        second_env = lane.get_dbus_environment(load_env)
        first_endpoint = lane.get_alarm_endpoint(load_endpoint)
        lane.run_channel("busctl call GetAlarmList", timeout=2)
        second_endpoint = lane.get_alarm_endpoint(load_endpoint)

        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.transport.checks, 0)
        self.assertEqual(self.loader_calls, 1)
        self.assertEqual(len(self.transport.channel_commands), 3)
        self.assertEqual(env_loads, 1)
        self.assertEqual(endpoint_loads, 1)
        self.assertIs(first_env, second_env)
        self.assertIs(first_endpoint, second_endpoint)
        self.assertEqual(lane.ssh_epoch, 0)

        status = self.task.runtime_status()
        self.assertEqual(status["metrics"]["ssh_authentications"], 1)
        self.assertEqual(status["metrics"]["ssh_channels"], 3)
        self.assertEqual(
            status["targets"][0]["ssh_leases"]["debug-object-alarm"]["metrics"],
            {
                "authentication_attempts": 1,
                "authentications": 1,
                "channel_requests": 3,
                "reconnects": 0,
                "master_failures": 0,
                "replay_safe_retries": 0,
                "cache_hits": 2,
                "cache_misses": 2,
            },
        )

    def test_channel_timeout_nonzero_and_output_limit_keep_healthy_master(self) -> None:
        lane = self.lane()
        self.transport.results = [
            FakeChannelResult(returncode=124, timed_out=True),
            FakeChannelResult(returncode=7, stderr="remote error"),
            FakeChannelResult(returncode=125, output_limit_exceeded=True),
            FakeChannelResult(stdout="still alive\n"),
        ]

        timeout = lane.run_channel("sleep", timeout=0.05)
        nonzero = lane.run_channel("exit 7", timeout=2)
        overflow = lane.run_channel("large output", timeout=2)
        healthy = lane.run_channel("printf still-alive", timeout=2)

        self.assertTrue(timeout.timed_out)
        self.assertEqual(nonzero.returncode, 7)
        self.assertTrue(overflow.output_limit_exceeded)
        self.assertEqual(healthy.stdout, "still alive\n")
        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.transport.closes, 0)

    def test_alarm_endpoint_cache_can_be_invalidated_without_dropping_master(self) -> None:
        lane = self.lane()
        endpoint_loads = 0

        def load_endpoint():
            nonlocal endpoint_loads
            endpoint_loads += 1
            return {"service": f"alarm.service.{endpoint_loads}"}

        first = lane.get_alarm_endpoint(load_endpoint)
        invalidated = lane.invalidate_alarm_endpoint()
        repeated = lane.invalidate_alarm_endpoint()
        second = lane.get_alarm_endpoint(load_endpoint)

        self.assertEqual(first, {"service": "alarm.service.1"})
        self.assertEqual(second, {"service": "alarm.service.2"})
        self.assertTrue(invalidated)
        self.assertFalse(repeated)
        self.assertEqual(endpoint_loads, 2)
        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.transport.closes, 0)
        self.assertEqual(lane.ssh_epoch, 0)

    def test_run_ssh_adapter_binds_collectors_to_the_lane_target(self) -> None:
        lane = self.lane()

        result = lane.run_ssh(
            "bmc.example",
            "debug-user",
            "private-password",
            "mdbctl lsclass",
            2,
            port=22,
            identity_file="/private/id_ed25519",
            stdout_limit_bytes=4096,
            stderr_limit_bytes=4096,
        )

        self.assertEqual(result.stdout, "mdbctl lsclass\n")
        self.assertEqual(self.transport.opens, 1)
        with self.assertRaisesRegex(ValueError, "different target"):
            lane.run_ssh(
                "other.example",
                "debug-user",
                "private-password",
                "must-not-run",
                2,
                port=22,
                identity_file="/private/id_ed25519",
            )
        self.assertEqual(self.transport.channel_commands, ["mdbctl lsclass"])

    def test_run_ssh_adapter_validates_host_key_options_against_the_lease(self) -> None:
        lane = self.lane()

        lane.run_ssh(
            "bmc.example",
            "debug-user",
            "private-password",
            "mdbctl lsclass",
            2,
            port=22,
            identity_file="/private/id_ed25519",
            host_key_policy="accept-new",
            known_hosts_file="/private/known_hosts",
        )

        self.assertEqual(
            self.transport.channel_options,
            [
                {
                    "target": self.target,
                    "host_key_policy": "accept-new",
                    "known_hosts_file": "/private/known_hosts",
                    "allow_insecure_host_key": False,
                }
            ],
        )

    def test_file_upload_reuses_the_bound_master_without_reauthentication(self) -> None:
        lane = self.lane()

        first = lane.upload_file("/tmp/local.hpm", "/tmp/remote.hpm", timeout=5)
        second = lane.run_channel("sha256sum /tmp/remote.hpm", timeout=2)

        self.assertEqual(first.stdout, "uploaded\n")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(
            self.transport.uploads,
            [("/tmp/local.hpm", "/tmp/remote.hpm")],
        )
        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.loader_calls, 1)

    def test_shared_ssh_epoch_rebuilds_other_lease_and_discards_its_cache(self) -> None:
        first_transport = FakeSshTransport()
        second_transport = FakeSshTransport()
        first = self.task.ssh_lane(
            target=self.target,
            credential_selector=self.selector,
            lease_name="debug-object-alarm",
            transport=first_transport,
        )
        second = self.task.ssh_lane(
            target=self.target,
            credential_selector=self.selector,
            lease_name="log-bundle-ssh",
            transport=second_transport,
        )
        first.get_dbus_environment(lambda: {"DBUS_SESSION_BUS_ADDRESS": "first"})
        second.get_alarm_endpoint(lambda: {"service": "second"})
        first_transport.results = [
            FakeChannelResult(returncode=255, master_lost=True),
            FakeChannelResult(stdout="first-fresh\n"),
        ]

        first.run_channel("lose-first-master", timeout=2)
        first.run_channel("reconnect-first", timeout=2)

        self.assertEqual(first.ssh_epoch, 1)
        self.assertEqual(second.cached_state_keys, ())
        second_result = second.run_channel("second-after-shared-epoch", timeout=2)
        self.assertEqual(second_result.stdout, "second-after-shared-epoch\n")
        self.assertEqual(second_transport.opens, 2)
        self.assertEqual(second.ssh_epoch, 1)

    def test_read_only_channels_can_overlap_on_the_same_healthy_master(self) -> None:
        transport = ConcurrentFakeSshTransport()
        lane = self.task.ssh_lane(
            target=self.target,
            credential_selector=self.selector,
            lease_name="debug-object-alarm",
            transport=transport,
        )
        start = threading.Barrier(3)
        results: list[FakeChannelResult] = []

        def collect(command: str) -> None:
            start.wait()
            results.append(lane.run_channel(command, timeout=2))

        threads = [
            threading.Thread(target=collect, args=("object",)),
            threading.Thread(target=collect, args=("alarm",)),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(len(results), 2)
        self.assertEqual(transport.opens, 1)
        self.assertEqual(transport.max_active_channels, 2)

    def test_target_epoch_advance_never_reuses_the_previous_master(self) -> None:
        lane = self.lane()
        first = lane.run_channel("before-epoch", timeout=2)

        observed_epoch = self.task.ensure_target_epoch(
            self.target,
            1,
            reason="selector-refresh",
        )
        second = lane.run_channel("after-epoch", timeout=2)

        self.assertEqual(first.stdout, "before-epoch\n")
        self.assertEqual(second.stdout, "after-epoch\n")
        self.assertEqual(observed_epoch, 1)
        self.assertEqual(self.transport.opens, 2)
        self.assertEqual(self.transport.closes, 1)

    def test_master_loss_does_not_replay_current_request_and_next_request_reconnects(self) -> None:
        lane = self.lane()
        lane.get_dbus_environment(lambda: {"DBUS_SESSION_BUS_ADDRESS": "old"})
        lane.cache_capability("mdbctl", True)
        lane.get_alarm_endpoint(lambda: {"service": "old"})
        self.transport.results = [
            FakeChannelResult(returncode=255, stderr="master gone", master_lost=True),
            FakeChannelResult(stdout="fresh\n"),
        ]

        failed = lane.run_channel("request-that-must-not-replay", timeout=2)

        self.assertEqual(failed.returncode, 255)
        self.assertEqual(
            self.transport.channel_commands,
            ["request-that-must-not-replay"],
        )
        self.assertEqual(lane.cached_state_keys, ())
        self.assertEqual(self.task.runtime_status()["targets"][0]["epochs"]["target_epoch"], 0)

        fresh = lane.run_channel("next-fresh-request", timeout=2)

        self.assertEqual(fresh.stdout, "fresh\n")
        self.assertEqual(
            self.transport.channel_commands,
            ["request-that-must-not-replay", "next-fresh-request"],
        )
        self.assertEqual(self.transport.opens, 2)
        self.assertEqual(lane.ssh_epoch, 1)
        status = self.task.runtime_status()
        self.assertEqual(status["targets"][0]["epochs"]["target_epoch"], 0)
        self.assertEqual(status["targets"][0]["epochs"]["lanes"]["ssh"]["epoch"], 1)
        self.assertEqual(status["metrics"]["ssh_reconnects"], 1)
        self.assertEqual(status["metrics"]["ssh_master_failures"], 1)

    def test_explicit_replay_safe_read_reconnects_once_after_master_loss(self) -> None:
        lane = self.lane()
        self.transport.results = [
            FakeChannelResult(returncode=255, stderr="master gone", master_lost=True),
            FakeChannelResult(stdout="fresh read\n"),
        ]

        result = lane.run_channel(
            "read-only-query",
            timeout=2,
            replay_safe=True,
        )

        self.assertEqual(result.stdout, "fresh read\n")
        self.assertEqual(
            self.transport.channel_commands,
            ["read-only-query", "read-only-query"],
        )
        self.assertEqual(self.transport.opens, 2)
        status = self.task.runtime_status()
        self.assertEqual(status["metrics"]["ssh_reconnects"], 1)
        self.assertEqual(status["metrics"]["ssh_master_failures"], 1)
        self.assertEqual(status["metrics"]["ssh_replay_safe_retries"], 1)

    def test_replay_safe_read_never_retries_more_than_once(self) -> None:
        lane = self.lane()
        self.transport.results = [
            FakeChannelResult(returncode=255, master_lost=True),
            FakeChannelResult(returncode=255, master_lost=True),
            FakeChannelResult(stdout="next request\n"),
        ]

        failed = lane.run_channel(
            "read-only-query",
            timeout=2,
            replay_safe=True,
        )

        self.assertEqual(failed.returncode, 255)
        self.assertEqual(
            self.transport.channel_commands,
            ["read-only-query", "read-only-query"],
        )
        self.assertEqual(self.transport.opens, 2)

        next_result = lane.run_channel(
            "next-read-only-query",
            timeout=2,
            replay_safe=True,
        )

        self.assertEqual(next_result.stdout, "next request\n")
        self.assertEqual(self.transport.opens, 3)
        status = self.task.runtime_status()
        self.assertEqual(status["metrics"]["ssh_replay_safe_retries"], 1)
        self.assertEqual(status["metrics"]["ssh_master_failures"], 2)

    def test_typed_read_result_reports_the_epoch_used_after_reconnect(self) -> None:
        lane = self.lane()
        self.transport.results = [
            FakeChannelResult(returncode=255, master_lost=True),
            FakeChannelResult(stdout="fresh\n"),
        ]
        lane.run_channel("lose-master", timeout=2)
        request = RemoteReadRequest.create(
            request_id="fresh-after-reconnect",
            target=self.target,
            credential_selector=self.selector,
            collector_name="mdbctl",
            operation={"command": ["lsclass"]},
        )

        result = self.task.run_read(
            request,
            lambda _context: lane.run_channel("fresh-read", timeout=2).stdout,
        )

        self.assertEqual(result.value, "fresh\n")
        self.assertEqual(result.target_epoch, 0)
        self.assertEqual(result.lane_epochs["ssh"], 1)


@unittest.skipIf(sys.platform == "win32", "POSIX OpenSSH ControlMaster only")
class OpenSshEnvironmentContractTests(unittest.TestCase):
    def test_control_master_receives_only_the_selected_password_secret(self) -> None:
        selector = CredentialSelector.for_ssh(
            user="root",
            user_env="",
            password_env="OPENUBMC_SSH_PASSWORD",
            identity_file="/tmp/id_ed25519",
            environ={},
        )
        target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=selector.fingerprint,
        )
        inherited = {
            "OPENUBMC_SSH_PASSWORD": "ssh-secret-from-parent",
            "OPENUBMC_TELNET_PASSWORD": "telnet-secret-from-parent",
            "OPENUBMC_OS_SSH_PASSWORD": "os-secret-from-parent",
            "OPENUBMC_REDFISH_PASSWORD": "redfish-secret-from-parent",
            "REDFISH_PASSWORD": "legacy-redfish-secret-from-parent",
            "SSHPASS": "stale-sshpass",
            "OPENUBMC_TEST_MARKER": "keep-me",
        }

        for credentials in (
            ResolvedSshCredentials(user="root", identity_file="/tmp/id_ed25519"),
            ResolvedSshCredentials(user="root", password="selected-secret"),
        ):
            observed: list[tuple[list[str], dict[str, object]]] = []

            def completed(command, **kwargs):
                observed.append((list(command), dict(kwargs)))
                return subprocess.CompletedProcess(command, 0, "", "")

            transport = OpenSshControlMasterTransport()
            with (
                mock.patch.dict(os.environ, inherited, clear=False),
                mock.patch(
                    "openubmc_target_runtime.openssh.shutil.which",
                    return_value="/usr/bin/tool",
                ),
                mock.patch(
                    "openubmc_target_runtime.openssh.subprocess.run",
                    side_effect=completed,
                ),
                mock.patch.object(transport, "check_master", return_value=True),
            ):
                master = transport.open_master(
                    target=target,
                    credentials=credentials,
                )
                master.closed = True
                master.tempdir.cleanup()

            command, call = observed[0]
            environment = call.get("env")
            self.assertIsNotNone(environment)
            assert environment is not None
            self.assertNotIn("OPENUBMC_TEST_MARKER", environment)
            for name in inherited:
                if name != "OPENUBMC_TEST_MARKER":
                    self.assertNotIn(name, environment)
            if credentials.password:
                self.assertEqual(command[:3], ["sshpass", "-d", "0"])
                self.assertEqual(call.get("input"), "selected-secret\n")
                self.assertNotIn("selected-secret", command)
            else:
                self.assertIsNone(call.get("input"))


if __name__ == "__main__":
    unittest.main()
