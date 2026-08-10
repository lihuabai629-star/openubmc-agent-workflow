from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _target_runtime_adapter import open_telnet_lease  # noqa: E402
import _target_runtime_adapter as target_runtime_adapter  # noqa: E402
from _telnet_common import (  # noqa: E402
    TelnetCommandResult,
    close_telnet,
    run_cmd_result,
)
import collect_logs  # noqa: E402
import read_remote_file  # noqa: E402


@dataclass
class FakeSession:
    identifier: int
    closed: bool = False


class FakeTelnetSessionTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        self.commands: list[str] = []
        self.results: list[TelnetCommandResult] = []

    def bind_debug_context(self, _debug_dumper, _debug_label: str) -> None:
        return None

    def open_session(self, *, target, credentials) -> FakeSession:
        self.opens += 1
        self.target = target
        self.credentials = credentials
        return FakeSession(self.opens)

    def run_command(self, _session: FakeSession, command: str, **_kwargs):
        self.commands.append(command)
        if self.results:
            return self.results.pop(0)
        return TelnetCommandResult(
            stdout=f"{command}\n",
            returncode=0,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            raw=f"{command}\n".encode(),
        )

    def command_invalidates_session(
        self,
        _session: FakeSession,
        result: TelnetCommandResult,
    ) -> bool:
        return not result.framing_complete

    def close_session(self, session: FakeSession) -> None:
        self.closes += 1
        session.closed = True


class CollectorTelnetTransport(FakeTelnetSessionTransport):
    def run_command(self, _session: FakeSession, command: str, **_kwargs):
        self.commands.append(command)
        if "date +%z" in command:
            output = b"+0000\n"
        elif "/var/log/app.log" in command:
            output = b"2026-08-01 10:00:00 fixture log\n"
        elif "__OPENUBMC_READ_RC__" in command:
            output = b"NAME=openUBMC\n__OPENUBMC_READ_RC__=0\n"
        else:
            raise AssertionError(f"unexpected Telnet command: {command}")
        return TelnetCommandResult(
            stdout=output.decode(),
            returncode=0,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            raw=output,
        )


class TelnetRuntimeAdapterTests(unittest.TestCase):
    def test_proxy_reuses_one_login_and_collector_close_does_not_end_the_lease(self) -> None:
        credential_calls = 0
        transport = FakeTelnetSessionTransport()
        args = argparse.Namespace(
            ip="bmc.example",
            telnet_user="debug-user",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            connect_timeout=2,
            prompt_timeout=2,
        )

        def load_credentials():
            nonlocal credential_calls
            credential_calls += 1
            return {
                "user": "debug-user",
                "password": "private-password",
                "port": 23,
            }

        lease = open_telnet_lease(
            args=args,
            credential_loader=load_credentials,
            lease_name="debug-log-file",
            task_id="telnet-adapter-task",
            transport_factory=lambda **_kwargs: transport,
        )
        try:
            first = run_cmd_result(lease.session_proxy, "read-app", timeout=2)
            second = run_cmd_result(lease.session_proxy, "read-file", timeout=2)
            close_telnet(lease.session_proxy)

            self.assertEqual(first.stdout, "read-app\n")
            self.assertEqual(second.stdout, "read-file\n")
            self.assertEqual(credential_calls, 1)
            self.assertEqual(transport.opens, 1)
            self.assertEqual(transport.commands, ["read-app", "read-file"])
            self.assertEqual(transport.closes, 0)
            self.assertEqual(
                lease.runtime_status()["metrics"]["telnet_commands"],
                2,
            )
        finally:
            lease.close()
        self.assertEqual(transport.closes, 1)

    def _invoke_public_collector(
        self,
        module,
        argv: list[str],
    ) -> tuple[int, dict[str, object], CollectorTelnetTransport, int]:
        transport = CollectorTelnetTransport()
        credentials_mapping = {
            "user": "debug-user",
            "password": "",
            "port": 23,
        }
        stdout = io.StringIO()
        patches = [
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                module,
                "resolve_telnet_credentials",
                return_value=credentials_mapping,
            ),
            mock.patch.object(module, "build_debug_dumper", return_value=None),
            mock.patch.object(
                target_runtime_adapter,
                "TelnetSessionTransport",
                return_value=transport,
            ),
        ]
        with contextlib.ExitStack() as stack:
            entered = [stack.enter_context(patch) for patch in patches]
            credential_mock = entered[1]
            with contextlib.redirect_stdout(stdout):
                returncode = module.main()
        payload = json.loads(stdout.getvalue())
        payload.pop("observed_at", None)
        return returncode, payload, transport, credential_mock.call_count

    def test_collect_logs_public_v1_reuses_one_login_for_multiple_commands(self) -> None:
        argv = [
            "collect_logs.py",
            "--ip",
            "bmc.example",
            "--logs",
            "app.log",
            "--json",
            "--compact-json",
        ]
        result = self._invoke_public_collector(
            collect_logs,
            argv,
        )

        self.assertEqual(result[2].opens, 1)
        self.assertEqual(len(result[2].commands), 2)
        self.assertEqual(result[3], 1)

    def test_read_remote_file_public_v1_preserves_the_cli_contract(self) -> None:
        argv = [
            "read_remote_file.py",
            "--ip",
            "bmc.example",
            "--path",
            "/etc/os-release",
            "--json",
            "--compact-json",
        ]
        result = self._invoke_public_collector(
            read_remote_file,
            argv,
        )

        self.assertEqual(result[2].opens, 1)
        self.assertEqual(len(result[2].commands), 1)
        self.assertEqual(result[3], 1)

    def test_read_only_proxy_reconnects_and_replays_one_incomplete_frame(self) -> None:
        transport = FakeTelnetSessionTransport()
        transport.results = [
            TelnetCommandResult(
                stdout="partial",
                returncode=None,
                framing_complete=False,
                timed_out=True,
                connection_closed=False,
                raw=b"partial",
            ),
            TelnetCommandResult(
                stdout="fresh",
                returncode=0,
                framing_complete=True,
                timed_out=False,
                connection_closed=False,
                raw=b"fresh",
            ),
        ]
        args = argparse.Namespace(
            ip="bmc.example",
            telnet_user="debug-user",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            connect_timeout=2,
            prompt_timeout=2,
        )
        lease = open_telnet_lease(
            args=args,
            credential_loader=lambda: {
                "user": "debug-user",
                "password": "",
                "port": 23,
            },
            lease_name="debug-log-file",
            task_id="telnet-reconnect-task",
            transport_factory=lambda **_kwargs: transport,
        )
        try:
            fresh = run_cmd_result(lease.session_proxy, "fresh", timeout=2)
            self.assertEqual(fresh.stdout, "fresh")
            self.assertEqual(transport.commands, ["fresh", "fresh"])
            self.assertEqual(transport.opens, 2)
            self.assertEqual(lease.telnet_epoch, 1)
            status = lease.task_run.runtime_status()
            self.assertEqual(status["metrics"]["telnet_replay_safe_retries"], 1)
        finally:
            lease.close()


if __name__ == "__main__":
    unittest.main()
