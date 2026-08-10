from __future__ import annotations

from dataclasses import dataclass
import sys
import threading
import time
import unittest
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CredentialSelector,
    OpenUBMCTaskRun,
    ResolvedSshCredentials,
    ResolvedTelnetCredentials,
    TargetSpec,
)
from openubmc_target_runtime.runtime import CredentialResolver  # noqa: E402


@dataclass
class FakeTelnetResult:
    stdout: str = ""
    returncode: int | None = 0
    framing_complete: bool = True
    timed_out: bool = False
    connection_closed: bool = False


@dataclass
class FakeSession:
    identifier: int
    closed: bool = False


class FakeTelnetTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        self.commands: list[str] = []
        self.results: list[FakeTelnetResult | BaseException] = []
        self.active_commands = 0
        self.max_active_commands = 0
        self.activity_lock = threading.Lock()

    def open_session(self, *, target, credentials) -> FakeSession:
        self.opens += 1
        self.target = target
        self.credentials = credentials
        return FakeSession(self.opens)

    def run_command(self, session: FakeSession, command: str, **_kwargs):
        with self.activity_lock:
            self.active_commands += 1
            self.max_active_commands = max(
                self.max_active_commands,
                self.active_commands,
            )
        try:
            time.sleep(0.04)
            self.commands.append(command)
            result = self.results.pop(0) if self.results else FakeTelnetResult(
                stdout=f"{command}\n"
            )
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            with self.activity_lock:
                self.active_commands -= 1

    def command_invalidates_session(
        self,
        _session: FakeSession,
        result: FakeTelnetResult,
    ) -> bool:
        return bool(
            not result.framing_complete
            or result.timed_out
            or result.connection_closed
        )

    def close_session(self, session: FakeSession) -> None:
        self.closes += 1
        session.closed = True


class FakeSshTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.commands: list[str] = []

    def open_master(self, *, target, credentials) -> object:
        self.opens += 1
        return object()

    def run_channel(self, _master: object, command: str, **_kwargs):
        self.commands.append(command)
        return FakeTelnetResult(stdout=f"{command}\n")

    def channel_lost_master(self, _master: object, _result: object) -> bool:
        return False

    def close_master(self, _master: object) -> None:
        return None


class TelnetLaneContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selector = CredentialSelector.for_telnet(
            user="debug-user",
            user_env="",
            password_env="OPENUBMC_TEST_TELNET_PASSWORD",
            environ={},
        )
        self.target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=self.selector.fingerprint,
        )
        self.task = OpenUBMCTaskRun(
            task_id="task-telnet-lane",
            credential_resolver=CredentialResolver(
                lambda _selector: ResolvedSshCredentials(user="unused")
            ),
        )
        self.credentials = ResolvedTelnetCredentials(
            user="debug-user",
            password="private-password",
            port=23,
        )
        self.transport = FakeTelnetTransport()

    def lane(self, lease_name: str = "debug-log-file"):
        return self.task.telnet_lane(
            target=self.target,
            credentials=self.credentials,
            lease_name=lease_name,
            transport=self.transport,
        )

    def test_one_domain_lease_logs_in_once_and_strictly_serializes_commands(self) -> None:
        lane = self.lane()
        start = threading.Barrier(3)
        results: list[FakeTelnetResult] = []

        def collect(command: str) -> None:
            start.wait()
            results.append(lane.run_command(command, timeout=2))

        threads = [
            threading.Thread(target=collect, args=("read-app-log",)),
            threading.Thread(target=collect, args=("read-framework-log",)),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(len(results), 2)
        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.transport.max_active_commands, 1)
        self.assertEqual(sorted(self.transport.commands), [
            "read-app-log",
            "read-framework-log",
        ])
        status = self.task.runtime_status()
        self.assertEqual(status["metrics"]["telnet_logins"], 1)
        self.assertEqual(status["metrics"]["telnet_commands"], 2)

    def test_incomplete_command_is_not_replayed_and_next_request_reconnects(self) -> None:
        lane = self.lane()
        self.transport.results = [
            FakeTelnetResult(
                returncode=None,
                framing_complete=False,
                timed_out=True,
            ),
            FakeTelnetResult(stdout="fresh\n"),
        ]

        failed = lane.run_command("request-that-must-not-replay", timeout=1)

        self.assertTrue(failed.timed_out)
        self.assertEqual(
            self.transport.commands,
            ["request-that-must-not-replay"],
        )
        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.transport.closes, 1)
        self.assertEqual(lane.telnet_epoch, 0)

        fresh = lane.run_command("next-fresh-request", timeout=1)

        self.assertEqual(fresh.stdout, "fresh\n")
        self.assertEqual(
            self.transport.commands,
            ["request-that-must-not-replay", "next-fresh-request"],
        )
        self.assertEqual(self.transport.opens, 2)
        self.assertEqual(lane.telnet_epoch, 1)

    def test_explicit_replay_safe_read_reconnects_once_after_incomplete_frame(self) -> None:
        lane = self.lane()
        self.transport.results = [
            FakeTelnetResult(
                returncode=None,
                framing_complete=False,
                connection_closed=True,
            ),
            FakeTelnetResult(stdout="fresh\n"),
        ]

        result = lane.run_command(
            "read-only-log-snapshot",
            timeout=2,
            replay_safe=True,
        )

        self.assertEqual(result.stdout, "fresh\n")
        self.assertEqual(
            self.transport.commands,
            ["read-only-log-snapshot", "read-only-log-snapshot"],
        )
        self.assertEqual(self.transport.opens, 2)
        self.assertEqual(self.transport.closes, 1)
        self.assertEqual(lane.telnet_epoch, 1)
        status = self.task.runtime_status()
        self.assertEqual(status["metrics"]["telnet_reconnects"], 1)
        self.assertEqual(status["metrics"]["telnet_replay_safe_retries"], 1)

    def test_output_failure_discards_the_session_without_replaying(self) -> None:
        lane = self.lane()
        self.transport.results = [RuntimeError("telnet output limit")]

        with self.assertRaisesRegex(RuntimeError, "output limit"):
            lane.run_command("bounded-read", timeout=1)

        self.assertEqual(self.transport.commands, ["bounded-read"])
        self.assertEqual(self.transport.opens, 1)
        self.assertEqual(self.transport.closes, 1)
        self.assertFalse(lane.connected)

    def test_debug_and_mutation_leases_never_share_a_session(self) -> None:
        debug_transport = FakeTelnetTransport()
        mutation_transport = FakeTelnetTransport()
        debug = self.task.telnet_lane(
            target=self.target,
            credentials=self.credentials,
            lease_name="debug-log-file",
            transport=debug_transport,
        )
        mutation = self.task.telnet_lane(
            target=self.target,
            credentials=self.credentials,
            lease_name="live-patch",
            transport=mutation_transport,
        )

        debug.run_command("read-only", timeout=1)
        mutation.run_command("mutation", timeout=1)

        self.assertEqual(debug_transport.opens, 1)
        self.assertEqual(mutation_transport.opens, 1)
        self.assertIsNot(debug.session, mutation.session)

    def test_telnet_failure_does_not_invalidate_a_healthy_ssh_lane(self) -> None:
        ssh_selector = CredentialSelector.for_ssh(
            user="debug-user",
            user_env="",
            password_env="",
            identity_file="",
            environ={},
        )
        shared_target = TargetSpec(
            host="bmc.example",
            credential_selector_fingerprint=ssh_selector.fingerprint,
        )
        ssh_transport = FakeSshTransport()
        ssh_lane = self.task.ssh_lane(
            target=shared_target,
            credential_selector=ssh_selector,
            lease_name="debug-object-alarm",
            transport=ssh_transport,
        )
        telnet_lane = self.task.telnet_lane(
            target=shared_target,
            credentials=self.credentials,
            lease_name="debug-log-file",
            transport=self.transport,
        )
        ssh_lane.run_channel("object-read", timeout=1)
        self.transport.results = [
            FakeTelnetResult(
                returncode=None,
                framing_complete=False,
                connection_closed=True,
            )
        ]

        telnet_lane.run_command("log-read", timeout=1)
        follow_up = ssh_lane.run_channel("alarm-read", timeout=1)

        self.assertEqual(follow_up.stdout, "alarm-read\n")
        self.assertEqual(ssh_transport.opens, 1)
        epochs = self.task.runtime_status()["targets"][0]["epochs"]
        self.assertEqual(epochs["target_epoch"], 0)
        self.assertEqual(epochs["lanes"]["ssh"]["status"], "ready")
        self.assertEqual(epochs["lanes"]["telnet"]["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
