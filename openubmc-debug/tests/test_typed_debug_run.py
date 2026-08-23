from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _remote_common  # noqa: E402
import _target_runtime_adapter  # noqa: E402
import _workflow_runtime  # noqa: E402
import workflow_remote  # noqa: E402


TEST_IP = "192.0.2.81"


def child_payload(
    tool: str,
    *,
    result: dict[str, object],
    request: dict[str, object] | None = None,
    observed_at: str = "2026-08-01T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "schema_version": "openubmc-debug.v1",
        "tool": tool,
        "ip": TEST_IP,
        "observed_at": observed_at,
        "ok": True,
        "code": "ok",
        "normalized_code": "ok",
        "returncode": 0,
        "warnings": [],
        "error": "",
        "request": request or {},
        "result": result,
    }


def tool_result(
    name: str,
    tool: str,
    result: dict[str, object],
    *,
    request: dict[str, object] | None = None,
    observed_at: str = "2026-08-01T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "name": name,
        "ok": True,
        "code": "ok",
        "returncode": 0,
        "started_at": observed_at,
        "completed_at": observed_at,
        "command": [tool],
        "payload": child_payload(
            tool,
            result=result,
            request=request,
            observed_at=observed_at,
        ),
        "error": "",
    }


class FakeDebugRuntimeLease:
    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def runtime_status(self) -> dict[str, object]:
        return {
            "api_version": "openubmc.target-runtime.v1",
            "task_id": "test-task",
            "metrics": {"credential_resolutions": 1},
            "evidence_ledger": {
                "max_records": 8,
                "record_count": 7,
                "records": [],
            },
            "targets": [],
        }


class FakeTypedToolRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.commands: dict[str, list[str]] = {}

    def __call__(self, name, command, env, timeout, *, deadline=None):
        del env, timeout, deadline
        self.calls.append(name)
        self.commands[name] = list(command)
        script = next(
            (Path(item).stem for item in command if str(item).endswith(".py")),
            name,
        )
        if script == "preflight_remote":
            observed = (
                "2026-08-01T00:01:00+00:00"
                if name == "preflight_end"
                else "2026-08-01T00:00:00+00:00"
            )
            clock = (
                "2026-08-01 00:01:00 +0000"
                if name == "preflight_end"
                else "2026-08-01 00:00:00 +0000"
            )
            return tool_result(
                name,
                script,
                {
                    "capabilities": {
                        "remote_object": True,
                        "remote_log_file": True,
                        "combined_snapshot": True,
                        "ssh_transport": True,
                        "dbus_env": True,
                        "mdbctl": True,
                        "busctl": True,
                        "active_alarm_transport": True,
                        "active_alarms": True,
                    },
                    "checks": {
                        "SSH": {"ok": True, "lines": [clock, "up 1 day"]},
                        "DBUS_ENV": {"ok": True, "lines": []},
                        "MDBCTL": {"ok": True, "lines": ["class"]},
                        "BUSCTL": {"ok": True, "lines": ["/object"]},
                        "TELNET": {"ok": True, "lines": [clock]},
                    },
                },
                observed_at=observed,
            )
        if script == "active_alarms":
            return tool_result(
                name,
                script,
                {
                    "service": "example.Alarm",
                    "path": "/example/alarms",
                    "interface": "example.Alarm",
                    "signature": "a{ss}",
                    "record_count": 0,
                    "records": [],
                },
            )
        if script == "collect_logs":
            return tool_result(name, script, {"entries": []})
        if script == "read_remote_file":
            path = command[command.index("--path") + 1]
            lines = ["100.0 20.0"] if path == "/proc/uptime" else ["v1"]
            if name == "uptime_end":
                lines = ["160.0 20.0"]
            return tool_result(
                name,
                script,
                {
                    "path": path,
                    "lines": lines,
                    "line_count": len(lines),
                    "bytes_returned": len("\n".join(lines).encode("utf-8")),
                    "truncated": False,
                    "content_complete": True,
                },
                request={"path": path},
            )
        return tool_result(name, script, {"stdout_lines": ["ok"]})


class CapabilityReadyFakeRunner(FakeTypedToolRunner):
    def __init__(self) -> None:
        super().__init__()
        self.mdb_started = threading.Event()
        self.release_remaining_capabilities = threading.Event()
        self.capability_preflight_starts = 0

    def start_capability_preflight(
        self,
        command,
        env,
        timeout,
        *,
        deadline=None,
        release_cached_mdb=False,
    ):
        del command, env, timeout, deadline, release_cached_mdb
        self.capability_preflight_starts += 1
        runner = self

        class PreflightRun:
            @staticmethod
            def wait_capability(capability, *, deadline=None):
                del deadline
                if capability == "mdbctl":
                    return True
                if capability == "remote_log_file":
                    return False
                if not runner.release_remaining_capabilities.wait(timeout=2):
                    raise AssertionError("full preflight did not release bus capability")
                return True

            @staticmethod
            def result():
                if not runner.mdb_started.wait(timeout=2):
                    raise AssertionError("MDB collection waited for full preflight")
                runner.release_remaining_capabilities.set()
                return tool_result(
                    "preflight_start",
                    "preflight_remote",
                    {
                        "capabilities": {
                            "remote_object": True,
                            "remote_log_file": False,
                            "combined_snapshot": False,
                            "ssh_transport": True,
                            "dbus_env": True,
                            "mdbctl": True,
                            "busctl": True,
                            "active_alarm_transport": True,
                            "active_alarms": True,
                        },
                        "checks": {
                            "SSH": {"ok": True, "lines": []},
                            "DBUS_ENV": {"ok": True, "lines": []},
                            "MDBCTL": {"ok": True, "lines": []},
                            "BUSCTL": {"ok": True, "lines": []},
                        },
                    },
                )

        return PreflightRun()

    def __call__(self, name, command, env, timeout, *, deadline=None):
        if name.startswith("mdbctl"):
            self.mdb_started.set()
        return super().__call__(
            name,
            command,
            env,
            timeout,
            deadline=deadline,
        )


class FakeSshTransport:
    def __init__(self) -> None:
        self.authentications = 0
        self.channels = 0

    def open_master(self, *, target, credentials):
        self.authentications += 1
        return object()

    def check_master(self, master) -> bool:
        return True

    def run_channel(self, master, remote_command: str, **kwargs):
        self.channels += 1
        return SimpleNamespace(returncode=0, stdout=remote_command, stderr="")

    def channel_lost_master(self, master, result) -> bool:
        return False

    def close_master(self, master) -> None:
        return None


class RecoveringReadSshTransport(FakeSshTransport):
    def open_master(self, *, target, credentials):
        del target, credentials
        self.authentications += 1
        return SimpleNamespace(alive=True)

    def run_channel(self, master, remote_command: str, **kwargs):
        del kwargs
        self.channels += 1
        if self.channels == 1:
            master.alive = False
            return SimpleNamespace(
                returncode=255,
                stdout="",
                stderr="ControlMaster connection lost",
            )
        return SimpleNamespace(returncode=0, stdout=remote_command, stderr="")

    def channel_lost_master(self, master, result) -> bool:
        return not master.alive or result.returncode == 255


class FakeTelnetTransport:
    def __init__(self) -> None:
        self.logins = 0
        self.commands = 0

    def bind_debug_context(self, debug_dumper, debug_label: str) -> None:
        return None

    def open_session(self, *, target, credentials):
        self.logins += 1
        return object()

    def run_command(self, session, command: str, **kwargs):
        self.commands += 1
        return SimpleNamespace(
            stdout=command,
            returncode=0,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            ok=True,
        )

    def command_invalidates_session(self, session, result) -> bool:
        return False

    def close_session(self, session) -> None:
        return None


class HostKeyFailingSshTransport(FakeSshTransport):
    def open_master(self, *, target, credentials):
        del target, credentials
        self.authentications += 1
        completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout="",
            stderr="Host key verification failed.",
        )
        completed.host_key_verification_failed = True
        completed.ssh_host_key_policy = "strict"
        completed.ssh_host_key_policy_source = "default"
        completed.ssh_known_hosts_source = "ssh_default"
        completed.ssh_transport_warnings = []
        raise _remote_common.SshControlMasterOpenError(completed)


class PreflightTelnetTransport(FakeTelnetTransport):
    def run_command(self, session, command: str, **kwargs):
        del session, kwargs
        self.commands += 1
        if command.startswith("date "):
            stdout = "2026-08-02 12:00:00 +0800"
        elif command.startswith("ls -1 /var/log/"):
            stdout = "/var/log/app.log\n/var/log/framework.log"
        else:
            stdout = command
        return SimpleNamespace(
            stdout=stdout,
            returncode=0,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            ok=True,
        )


class TypedDebugRunTests(unittest.TestCase):
    def test_capability_ready_workflow_composes_with_real_typed_runner(self) -> None:
        args = workflow_remote.parse_args(
            [
                "--ip",
                TEST_IP,
                "--mdb-only",
                "--no-freshness",
                "--no-source-correlation",
                "--json",
            ]
        )
        workflow_remote.validate_workflow_inputs(args)
        credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "",
                "password": "",
                "port": 23,
            },
        }
        transport = FakeSshTransport()
        outputs: list[dict[str, object]] = []

        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=credentials,
            task_id="capability-ready-integration",
            ssh_transport_factory=lambda **_kwargs: transport,
        ) as lease:
            runner = workflow_remote.TypedDebugToolRunner(lease)
            returncode = workflow_remote._execute_workflow(
                args,
                source_root_source="none",
                engine="v1",
                env={},
                tool_runner=runner,
                runtime_status=lease.runtime_status,
                parallel_lanes=True,
                emit_output=False,
                output_handler=outputs.append,
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(transport.authentications, 1)
        self.assertEqual(len(outputs), 1)
        result = outputs[0]["result"]
        self.assertTrue(result["preflight_start"]["ok"])
        self.assertTrue(result["lanes"]["ssh"]["mdbctl"]["ok"])
        self.assertEqual(result["lanes"]["ssh"]["busctl"]["code"], "skipped")

    def test_typed_preflight_handle_exposes_ready_capability_before_completion(
        self,
    ) -> None:
        release_preflight = threading.Event()
        recorded_phases: list[str] = []

        def preflight_main(**kwargs) -> int:
            observer = kwargs["_check_observer"]
            observer("SSH", (True, ["ssh ready"]))
            observer("MDBCTL", (True, ["mdb ready"]))
            if not release_preflight.wait(timeout=2):
                raise AssertionError("test did not release preflight completion")
            print(
                json.dumps(
                    child_payload(
                        "preflight_remote",
                        result={
                            "capabilities": {
                                "remote_object": True,
                                "remote_log_file": False,
                                "combined_snapshot": False,
                                "ssh_transport": True,
                                "dbus_env": False,
                                "mdbctl": True,
                                "busctl": False,
                                "active_alarm_transport": False,
                                "active_alarms": False,
                            },
                            "checks": {
                                "SSH": {"ok": True, "lines": ["ssh ready"]},
                                "MDBCTL": {"ok": True, "lines": ["mdb ready"]},
                            },
                        },
                    )
                )
            )
            return 0

        lease = SimpleNamespace(
            ssh_credentials_mapping=lambda: {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            telnet_credentials_mapping=lambda: {
                "user": "",
                "password": "",
                "port": 23,
            },
            object_alarm_lease=SimpleNamespace(),
            telnet_session=None,
            record_phase=recorded_phases.append,
            record_tool_result=lambda _name, _result: None,
        )
        runner = workflow_remote.TypedDebugToolRunner(lease)
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--mdb-only",
            "--json",
            "--compact-json",
        ]
        with mock.patch.object(
            workflow_remote.preflight_remote,
            "main",
            side_effect=preflight_main,
        ):
            handle = runner.start_capability_preflight(
                command,
                {},
                5,
                deadline=_workflow_runtime.WorkflowDeadline(5),
            )
            self.assertTrue(
                handle.wait_capability(
                    "mdbctl",
                    deadline=_workflow_runtime.WorkflowDeadline(1),
                )
            )
            self.assertEqual(recorded_phases, [])
            release_preflight.set()
            result = handle.result()

        self.assertTrue(result["ok"])
        self.assertEqual(recorded_phases, ["preflight_start"])

    def test_fast_snapshot_releases_cached_capability_during_anchor_refresh(
        self,
    ) -> None:
        release_refresh = threading.Event()
        checks = {
            "SSH": {"ok": True, "lines": ["cached ssh"]},
            "DBUS_ENV": {"ok": True, "lines": ["cached dbus"]},
            "MDBCTL": {"ok": True, "lines": ["cached mdb"]},
            "BUSCTL": {"ok": True, "lines": ["cached bus"]},
        }

        def preflight_main(**kwargs) -> int:
            if not release_refresh.wait(timeout=2):
                raise AssertionError("test did not release anchor refresh")
            kwargs["_check_observer"]("SSH", (True, ["fresh ssh"]))
            print(
                json.dumps(
                    child_payload(
                        "preflight_remote",
                        result={
                            "capabilities": {
                                "remote_object": True,
                                "remote_log_file": False,
                                "combined_snapshot": False,
                                "ssh_transport": True,
                                "dbus_env": True,
                                "mdbctl": True,
                                "busctl": True,
                                "active_alarm_transport": True,
                                "active_alarms": True,
                            },
                            "checks": checks,
                        },
                    )
                )
            )
            return 0

        lease = SimpleNamespace(
            ssh_credentials_mapping=lambda: {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            telnet_credentials_mapping=lambda: {
                "user": "",
                "password": "",
                "port": 23,
            },
            object_alarm_lease=SimpleNamespace(),
            telnet_session=None,
            cached_preflight_checks=lambda _args: checks,
            record_phase=lambda _name: None,
            record_tool_result=lambda _name, _result: None,
        )
        runner = workflow_remote.TypedDebugToolRunner(lease)
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--skip-telnet",
            "--json",
            "--compact-json",
        ]
        with mock.patch.object(
            workflow_remote.preflight_remote,
            "main",
            side_effect=preflight_main,
        ):
            handle = runner.start_capability_preflight(
                command,
                {},
                5,
                deadline=_workflow_runtime.WorkflowDeadline(5),
                release_cached_mdb=True,
            )
            self.assertTrue(
                handle.wait_capability(
                    "mdbctl",
                    deadline=_workflow_runtime.WorkflowDeadline(1),
                )
            )
            self.assertFalse(
                handle.wait_capability(
                    "busctl",
                    deadline=_workflow_runtime.WorkflowDeadline(0.05),
                )
            )
            release_refresh.set()
            result = handle.result()

        self.assertTrue(result["ok"])

    def test_workflow_starts_mdb_when_its_capability_is_ready(self) -> None:
        args = workflow_remote.parse_args(
            [
                "--ip",
                TEST_IP,
                "--skip-telnet",
                "--no-freshness",
                "--no-source-correlation",
                "--json",
            ]
        )
        runner = CapabilityReadyFakeRunner()
        outputs: list[dict[str, object]] = []

        returncode = workflow_remote._execute_workflow(
            args,
            source_root_source="none",
            engine="v1",
            env={},
            tool_runner=runner,
            parallel_lanes=True,
            emit_output=False,
            output_handler=outputs.append,
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(runner.capability_preflight_starts, 1)
        self.assertTrue(runner.mdb_started.is_set())
        self.assertNotIn("preflight_start", runner.calls)
        self.assertIn("mdbctl", runner.calls)
        self.assertEqual(len(outputs), 1)
        self.assertTrue(outputs[0]["ok"])

    def test_compact_mdb_lsprop_keeps_properties_beyond_the_line_preview(self) -> None:
        lines = [
            "bmc.kepler.Systems.PowerStrategy",
            *[f"  Filler{index}={index}" for index in range(25)],
            '  PowerWorkingMode="Active/Standby"',
            '  PowerActualWorkingMode="LoadBalancing"',
        ]
        compact = _workflow_runtime.compact_tool_result(
            tool_result(
                "mdbctl",
                "mdbctl_remote",
                {
                    "stdout": "\n".join(lines),
                    "stdout_lines": lines,
                    "stderr": "",
                    "stderr_lines": [],
                },
                request={
                    "command_parts": ["lsprop", "PowerStrategy_1_01010A"]
                },
            )
        )

        self.assertTrue(compact["result"]["stdout_truncated"])
        self.assertEqual(
            compact["result"]["properties"][
                "bmc.kepler.Systems.PowerStrategy"
            ]["PowerWorkingMode"],
            '"Active/Standby"',
        )
        self.assertEqual(compact["result"]["property_count"], 27)

    def test_workflow_rejects_unreviewed_mdb_query_before_collection(self) -> None:
        args = workflow_remote.parse_args(
            ["--ip", TEST_IP, "--mdb-query", "setprop Object Interface Value"]
        )

        with self.assertRaisesRegex(SystemExit, "reviewed read-only grammar"):
            workflow_remote.validate_workflow_inputs(args)

    def test_workflow_rejects_invalid_mdb_expansion_class(self) -> None:
        args = workflow_remote.parse_args(
            ["--ip", TEST_IP, "--mdb-expand-class", "PCIeDevice;setprop"]
        )

        with self.assertRaisesRegex(SystemExit, "valid MDB class token"):
            workflow_remote.validate_workflow_inputs(args)

    def test_mdb_concurrency_auto_bounds_active_queries_without_limiting_count(
        self,
    ) -> None:
        args = workflow_remote.parse_args(["--ip", TEST_IP])
        workflow_remote.validate_workflow_inputs(args)
        self.assertEqual(workflow_remote.mdb_concurrency_budget(args, 10), 4)
        args.mdb_concurrency = "unbounded"
        self.assertEqual(workflow_remote.mdb_concurrency_budget(args, 10), 10)
        args.mdb_concurrency = "3"
        self.assertEqual(workflow_remote.mdb_concurrency_budget(args, 10), 3)

    def test_mdb_expand_class_discovers_then_reads_current_objects(self) -> None:
        args = workflow_remote.parse_args(
            [
                "--ip",
                TEST_IP,
                "--mdb-only",
                "--mdb-expand-class",
                "PCIeDevice",
            ]
        )
        workflow_remote.validate_workflow_inputs(args)

        class ExpansionRunner:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.commands: dict[str, list[str]] = {}

            def __call__(self, name, command, env, timeout, *, deadline=None):
                del env, timeout, deadline
                self.calls.append(name)
                self.commands[name] = list(command)
                command_index = command.index("--compact-json") + 1
                command_parts = command[command_index:]
                if command_parts == ["lsobj", "PCIeDevice"]:
                    lines = ["PCIeDevice_1_01011504", "PCIeDevice_1_01011507"]
                else:
                    lines = [
                        "bmc.kepler.Systems.PCIeDevices.PCIeDevice",
                        f'  ObjectName="{command_parts[-1]}"',
                        "  Bus=95",
                    ]
                return tool_result(
                    name,
                    "mdbctl_remote",
                    {
                        "stdout": "\n".join(lines),
                        "stdout_lines": lines,
                        "stderr": "",
                        "stderr_lines": [],
                    },
                    request={"command_parts": command_parts},
                )

        runner = ExpansionRunner()
        results = workflow_remote.run_ssh_lane(
            args,
            {},
            {
                "remote_object": True,
                "mdbctl": True,
                "busctl": True,
                "active_alarm_transport": True,
            },
            tool_runner=runner,
        )

        first_object = workflow_remote._mdb_expansion_result_name(
            "mdbctl_expand_1",
            "PCIeDevice_1_01011504",
        )
        second_object = workflow_remote._mdb_expansion_result_name(
            "mdbctl_expand_1",
            "PCIeDevice_1_01011507",
        )
        self.assertEqual(runner.calls[0], "mdbctl_expand_1")
        self.assertEqual(
            set(runner.calls[1:]),
            {first_object, second_object},
        )
        self.assertEqual(
            runner.commands[first_object][-2:],
            ["lsprop", "PCIeDevice_1_01011504"],
        )
        self.assertTrue(results[second_object]["ok"])
        self.assertEqual(results["busctl"]["code"], "skipped")

    def test_mdb_only_selects_targeted_queries_without_unrelated_collectors(self) -> None:
        args = workflow_remote.parse_args(
            [
                "--ip",
                TEST_IP,
                "--mdb-only",
                "--mdb-query",
                "lsobj ThresholdSensor",
                "--mdb-query",
                "lsprop ThresholdSensor_SwitchBoardPower1_010115",
            ]
        )
        workflow_remote.validate_workflow_inputs(args)
        runner = FakeTypedToolRunner()

        results = workflow_remote.run_ssh_lane(
            args,
            {},
            {
                "remote_object": True,
                "mdbctl": True,
                "busctl": True,
                "active_alarm_transport": True,
            },
            tool_runner=runner,
        )

        self.assertTrue(args.skip_telnet)
        self.assertEqual(set(runner.calls), {"mdbctl", "mdbctl_2"})
        self.assertEqual(results["busctl"]["code"], "skipped")
        self.assertEqual(results["active_alarms"]["code"], "skipped")

    def test_host_key_failure_keeps_telnet_workflow_lane_available(self) -> None:
        args = workflow_remote.parse_args(
            [
                "--ip",
                TEST_IP,
                "--ssh-user",
                "ssh-user",
                "--telnet-user",
                "telnet-user",
                "--no-freshness",
                "--no-source-correlation",
                "--timeout",
                "5",
                "--deadline",
                "30",
                "--json",
            ]
        )
        credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "telnet-user",
                "password": "telnet-password",
                "port": 23,
            },
        }
        ssh_transport = HostKeyFailingSshTransport()
        telnet_transport = PreflightTelnetTransport()
        output_payloads: list[dict[str, object]] = []

        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=credentials,
            task_id="typed-debug-partial-preflight",
            ssh_transport_factory=lambda **_kwargs: ssh_transport,
            telnet_transport_factory=lambda **_kwargs: telnet_transport,
        ) as lease:
            typed_runner = workflow_remote.TypedDebugToolRunner(lease)
            fallback_runner = FakeTypedToolRunner()
            calls: list[str] = []

            def tool_runner(name, command, env, timeout, *, deadline=None):
                calls.append(name)
                if name == "preflight_start":
                    return typed_runner(
                        name,
                        command,
                        env,
                        timeout,
                        deadline=deadline,
                    )
                return fallback_runner(
                    name,
                    command,
                    env,
                    timeout,
                    deadline=deadline,
                )

            returncode = workflow_remote._execute_workflow(
                args,
                source_root_source="test",
                engine="v1",
                env={},
                tool_runner=tool_runner,
                runtime_status=lease.runtime_status,
                parallel_lanes=False,
                emit_output=False,
                output_handler=output_payloads.append,
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(len(output_payloads), 1)
        payload = output_payloads[0]
        result = payload["result"]
        preflight = result["preflight_start"]
        preflight_result = preflight["payload"]["result"]
        self.assertEqual(preflight["code"], "partial")
        self.assertFalse(preflight_result["capabilities"]["remote_object"])
        self.assertTrue(preflight_result["capabilities"]["remote_log_file"])
        self.assertEqual(
            preflight_result["checks"]["SSH"]["code"],
            "ssh_host_key_verification_failed",
        )
        self.assertEqual(
            preflight_result["checks"]["DBUS_ENV"]["code"],
            "ssh_host_key_verification_failed",
        )
        self.assertIn("logs", calls)
        self.assertIn("file:/etc/version.json", calls)
        self.assertIn("file:/proc/uptime", calls)
        self.assertEqual(telnet_transport.logins, 1)
        self.assertEqual(telnet_transport.commands, 2)

    def test_typed_freshness_uses_light_preflight_refresh(self) -> None:
        checks = {
            "SSH": {"ok": True, "lines": ["2026-08-01 00:00:00 +0000"]},
            "DBUS_ENV": {"ok": True, "lines": []},
        }
        refresh_arguments: list[object] = []

        def preflight_main(**kwargs) -> int:
            refresh_arguments.append(kwargs.get("_refresh_checks"))
            print(
                json.dumps(
                    child_payload(
                        "preflight_remote",
                        result={
                            "capabilities": {"remote_object": True},
                            "checks": checks,
                        },
                    )
                )
            )
            return 0

        lease = SimpleNamespace(
            ssh_credentials_mapping=lambda: {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            telnet_credentials_mapping=lambda: {
                "user": "telnet-user",
                "password": "telnet-password",
                "port": 23,
            },
            object_alarm_lease=SimpleNamespace(),
            telnet_session=SimpleNamespace(),
            record_phase=lambda _name: None,
            record_tool_result=lambda _name, _result: None,
        )
        runner = workflow_remote.TypedDebugToolRunner(lease)
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--json",
            "--compact-json",
        ]
        with mock.patch.object(
            workflow_remote.preflight_remote,
            "main",
            side_effect=preflight_main,
        ):
            start = runner("preflight_start", command, {}, 30)
            end = runner("preflight_end", command, {}, 30)

        self.assertTrue(start["ok"])
        self.assertTrue(end["ok"])
        self.assertIsNone(refresh_arguments[0])
        self.assertEqual(refresh_arguments[1], checks)

    def test_typed_preflight_cache_survives_new_runner_in_same_target_run(self) -> None:
        args = argparse.Namespace(
            ip=TEST_IP,
            ssh_user="ssh-user",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            ssh_host_key_policy="",
            ssh_known_hosts_file="",
            allow_insecure_host_key=False,
            telnet_user="",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            redfish_port=443,
            timeout=30,
            skip_telnet=True,
            mdb_only=True,
        )
        credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "",
                "password": "",
                "port": 23,
            },
        }
        checks = {
            "SSH": {"ok": True, "lines": ["2026-08-01 00:00:00 +0000"]},
            "MDBCTL": {"ok": True, "lines": ["ThresholdSensor"]},
        }
        refresh_arguments: list[object] = []

        def preflight_main(**kwargs) -> int:
            refresh_arguments.append(kwargs.get("_refresh_checks"))
            print(
                json.dumps(
                    child_payload(
                        "preflight_remote",
                        result={
                            "capabilities": {
                                "remote_object": True,
                                "mdbctl": True,
                            },
                            "checks": checks,
                        },
                    )
                )
            )
            return 0

        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--mdb-only",
            "--json",
            "--compact-json",
        ]
        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=credentials,
            task_id="typed-debug-preflight-cache",
            ssh_transport_factory=lambda **_kwargs: FakeSshTransport(),
        ) as lease, mock.patch.object(
            workflow_remote.preflight_remote,
            "main",
            side_effect=preflight_main,
        ):
            first_runner = workflow_remote.TypedDebugToolRunner(lease)
            first_runner("preflight_start", command, {}, 30)
            first_runner("preflight_end", command, {}, 30)
            second_runner = workflow_remote.TypedDebugToolRunner(lease)
            second_runner("preflight_start", command, {}, 30)
            second_runner("preflight_end", command, {}, 30)
            status = lease.runtime_status()
            target_host_key_policy = lease.target.policy.ssh_host_key_policy

        self.assertIsNone(refresh_arguments[0])
        self.assertEqual(refresh_arguments[1], checks)
        self.assertEqual(refresh_arguments[2], checks)
        self.assertEqual(refresh_arguments[3], checks)
        self.assertEqual(
            status["debug_run_metrics"],
            {
                "full_preflight_runs": 1,
                "preflight_refresh_runs": 3,
                "preflight_cache_hits": 1,
                "preflight_cache_misses": 1,
                "preflight_cache_stores": 2,
                "preflight_cache_invalidations": 0,
                "collector_invocations": 0,
            },
        )
        self.assertEqual(status["debug_preflight_cache"]["entry_count"], 1)
        self.assertEqual(target_host_key_policy, "insecure")
        self.assertEqual(
            status["debug_preflight_cache"]["profiles"],
            ["mdb-only"],
        )

    def test_typed_preflight_cache_invalidates_after_target_epoch_change(self) -> None:
        args = argparse.Namespace(
            ip=TEST_IP,
            ssh_user="ssh-user",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            ssh_host_key_policy="",
            ssh_known_hosts_file="",
            allow_insecure_host_key=False,
            telnet_user="",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            redfish_port=443,
            timeout=30,
            skip_telnet=True,
            mdb_only=True,
        )
        credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {"user": "", "password": "", "port": 23},
        }
        checks = {
            "SSH": {"ok": True, "lines": []},
            "MDBCTL": {"ok": True, "lines": []},
        }
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--mdb-only",
            "--json",
            "--compact-json",
        ]

        def preflight_main(**_kwargs) -> int:
            print(
                json.dumps(
                    child_payload(
                        "preflight_remote",
                        result={
                            "capabilities": {"remote_object": True, "mdbctl": True},
                            "checks": checks,
                        },
                    )
                )
            )
            return 0

        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=credentials,
            task_id="typed-debug-preflight-epoch",
            ssh_transport_factory=lambda **_kwargs: FakeSshTransport(),
        ) as lease, mock.patch.object(
            workflow_remote.preflight_remote,
            "main",
            side_effect=preflight_main,
        ):
            workflow_remote.TypedDebugToolRunner(lease)(
                "preflight_start", command, {}, 30
            )
            lease.task_run.advance_target_epoch(lease.target, reason="test-upgrade")
            workflow_remote.TypedDebugToolRunner(lease)(
                "preflight_start", command, {}, 30
            )
            metrics = lease.runtime_status()["debug_run_metrics"]

        self.assertEqual(metrics["full_preflight_runs"], 2)
        self.assertEqual(metrics["preflight_cache_hits"], 0)
        self.assertEqual(metrics["preflight_cache_misses"], 2)
        self.assertEqual(metrics["preflight_cache_invalidations"], 1)

    def test_combined_preflight_refresh_failure_invalidates_cached_checks(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--json",
            "--compact-json",
        ]
        base_checks = {
            "SSH": {"ok": True, "lines": ["ssh clock"]},
            "MDBCTL": {"ok": True, "lines": ["ThresholdSensor"]},
            "TELNET": {"ok": True, "lines": ["telnet clock"]},
        }

        for failed_anchor in ("SSH", "TELNET"):
            with self.subTest(failed_anchor=failed_anchor):
                refreshed_checks = json.loads(json.dumps(base_checks))
                refreshed_checks[failed_anchor] = {
                    "ok": False,
                    "lines": [f"{failed_anchor.lower()} refresh failed"],
                }
                stored: list[object] = []
                invalidated: list[object] = []

                def preflight_main(**kwargs) -> int:
                    self.assertEqual(kwargs.get("_refresh_checks"), base_checks)
                    print(
                        json.dumps(
                            child_payload(
                                "preflight_remote",
                                result={
                                    "capabilities": {
                                        "remote_object": failed_anchor != "SSH",
                                        "remote_log_file": failed_anchor != "TELNET",
                                    },
                                    "checks": refreshed_checks,
                                },
                            )
                        )
                    )
                    return 0

                lease = SimpleNamespace(
                    ssh_credentials_mapping=lambda: {
                        "user": "ssh-user",
                        "password": "ssh-password",
                        "port": 22,
                        "identity_file": "",
                    },
                    telnet_credentials_mapping=lambda: {
                        "user": "telnet-user",
                        "password": "telnet-password",
                        "port": 23,
                    },
                    object_alarm_lease=SimpleNamespace(),
                    telnet_session=SimpleNamespace(),
                    cached_preflight_checks=lambda _args: base_checks,
                    store_preflight_checks=lambda args, checks: stored.append(
                        (args, checks)
                    ),
                    invalidate_preflight_checks=lambda args: invalidated.append(args),
                    record_preflight_phase=lambda _mode: None,
                    record_phase=lambda _name: None,
                    record_tool_result=lambda _name, _result: None,
                )
                runner = workflow_remote.TypedDebugToolRunner(lease)
                with mock.patch.object(
                    workflow_remote.preflight_remote,
                    "main",
                    side_effect=preflight_main,
                ):
                    result = runner("preflight_start", command, {}, 30)

                self.assertTrue(result["ok"])
                self.assertEqual(stored, [])
                self.assertEqual(len(invalidated), 1)

    def test_combined_preflight_end_failure_invalidates_cached_checks(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--json",
            "--compact-json",
        ]
        base_checks = {
            "SSH": {"ok": True, "lines": ["ssh clock"]},
            "MDBCTL": {"ok": True, "lines": ["ThresholdSensor"]},
            "TELNET": {"ok": True, "lines": ["telnet clock"]},
        }

        for failed_anchor in ("SSH", "TELNET"):
            with self.subTest(failed_anchor=failed_anchor):
                calls = 0
                stored: list[object] = []
                invalidated: list[object] = []

                def preflight_main(**kwargs) -> int:
                    nonlocal calls
                    calls += 1
                    self.assertEqual(kwargs.get("_refresh_checks"), base_checks)
                    checks = json.loads(json.dumps(base_checks))
                    if calls == 2:
                        checks[failed_anchor] = {
                            "ok": False,
                            "lines": [f"{failed_anchor.lower()} refresh failed"],
                        }
                    print(
                        json.dumps(
                            child_payload(
                                "preflight_remote",
                                result={
                                    "capabilities": {
                                        "remote_object": failed_anchor != "SSH",
                                        "remote_log_file": failed_anchor != "TELNET",
                                    },
                                    "checks": checks,
                                },
                            )
                        )
                    )
                    return 0

                lease = SimpleNamespace(
                    ssh_credentials_mapping=lambda: {
                        "user": "ssh-user",
                        "password": "ssh-password",
                        "port": 22,
                        "identity_file": "",
                    },
                    telnet_credentials_mapping=lambda: {
                        "user": "telnet-user",
                        "password": "telnet-password",
                        "port": 23,
                    },
                    object_alarm_lease=SimpleNamespace(),
                    telnet_session=SimpleNamespace(),
                    cached_preflight_checks=lambda _args: base_checks,
                    store_preflight_checks=lambda args, checks: stored.append(
                        (args, checks)
                    ),
                    invalidate_preflight_checks=lambda args: invalidated.append(args),
                    record_preflight_phase=lambda _mode: None,
                    record_phase=lambda _name: None,
                    record_tool_result=lambda _name, _result: None,
                )
                runner = workflow_remote.TypedDebugToolRunner(lease)
                with mock.patch.object(
                    workflow_remote.preflight_remote,
                    "main",
                    side_effect=preflight_main,
                ):
                    start = runner("preflight_start", command, {}, 30)
                    end = runner("preflight_end", command, {}, 30)

                self.assertTrue(start["ok"])
                self.assertTrue(end["ok"])
                self.assertEqual(len(stored), 1)
                self.assertEqual(len(invalidated), 1)

    def test_fresh_runner_uses_epoch_valid_cached_preflight_for_refresh(self) -> None:
        command = [
            sys.executable,
            str(SCRIPT_DIR / "preflight_remote.py"),
            "--ip",
            TEST_IP,
            "--check",
            "SSH",
            "--check",
            "MDBCTL",
            "--json",
            "--compact-json",
        ]
        base_checks = {
            "SSH": {"ok": True, "lines": ["ssh clock"]},
            "MDBCTL": {"ok": True, "lines": ["ThresholdSensor"]},
        }
        observed_refreshes: list[object] = []

        def preflight_main(**kwargs) -> int:
            observed_refreshes.append(kwargs.get("_refresh_checks"))
            print(
                json.dumps(
                    child_payload(
                        "preflight_remote",
                        result={
                            "capabilities": {
                                "ssh_transport": True,
                                "mdbctl": True,
                            },
                            "checks": base_checks,
                        },
                    )
                )
            )
            return 0

        lease = SimpleNamespace(
            ssh_credentials_mapping=lambda: {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            telnet_credentials_mapping=lambda: {
                "user": "",
                "password": "",
                "port": 23,
            },
            object_alarm_lease=SimpleNamespace(),
            telnet_session=SimpleNamespace(),
            cached_preflight_checks=lambda _args: base_checks,
            invalidate_preflight_checks=lambda _args: None,
            record_preflight_phase=lambda _mode: None,
            record_phase=lambda _name: None,
            record_tool_result=lambda _name, _result: None,
        )
        runner = workflow_remote.TypedDebugToolRunner(lease)
        args = workflow_remote.preflight_remote.parse_args(
            workflow_remote._command_argv(command)
        )
        self.assertTrue(runner.prepare_assurance_refresh(args))

        with mock.patch.object(
            workflow_remote.preflight_remote,
            "main",
            side_effect=preflight_main,
        ):
            result = runner("preflight_end", command, {}, 30)

        self.assertTrue(result["ok"])
        self.assertEqual(observed_refreshes, [base_checks])

    def test_fresh_runner_rejects_a_missing_epoch_valid_preflight_cache(self) -> None:
        lease = SimpleNamespace(cached_preflight_checks=lambda _args: None)
        runner = workflow_remote.TypedDebugToolRunner(lease)
        args = argparse.Namespace()

        self.assertFalse(runner.prepare_assurance_refresh(args))

    def test_debug_runtime_reuses_both_lanes_and_keeps_fresh_reads(self) -> None:
        args = argparse.Namespace(
            ip=TEST_IP,
            ssh_user="ssh-user",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            ssh_host_key_policy="",
            ssh_known_hosts_file="",
            allow_insecure_host_key=False,
            telnet_user="telnet-user",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            redfish_port=443,
            timeout=30,
            skip_telnet=False,
        )
        credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "telnet-user",
                "password": "telnet-password",
                "port": 23,
            },
        }
        ssh_transport = FakeSshTransport()
        telnet_transport = FakeTelnetTransport()
        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=credentials,
            task_id="typed-debug-test",
            ssh_transport_factory=lambda **_kwargs: ssh_transport,
            telnet_transport_factory=lambda **_kwargs: telnet_transport,
            evidence_max_records=2,
        ) as lease:
            ssh = lease.ssh_credentials_mapping()

            def read(request_id: str, command: str):
                return lease.run_ssh_read(
                    request_id=request_id,
                    collector_name="mdbctl",
                    operation={"command": command},
                    collect=lambda: lease.ssh_runner(
                        TEST_IP,
                        str(ssh["user"]),
                        str(ssh["password"]),
                        command,
                        5,
                        port=22,
                        identity_file="",
                    ).stdout,
                )

            self.assertEqual(read("request-1", "first"), "first")
            self.assertEqual(read("request-2", "second"), "second")
            lease.telnet_session.run_telnet_command("date", timeout=5)
            lease.telnet_session.run_telnet_command("uptime", timeout=5)
            for index in range(3):
                lease.record_tool_result(
                    f"collector-{index}",
                    {"ok": True, "code": "ok", "payload": {"result": {}}},
                )
            status = lease.runtime_status()

        self.assertEqual(ssh_transport.authentications, 1)
        self.assertEqual(ssh_transport.channels, 2)
        self.assertEqual(telnet_transport.logins, 1)
        self.assertEqual(telnet_transport.commands, 2)
        self.assertEqual(status["metrics"]["credential_resolutions"], 1)
        self.assertEqual(status["metrics"]["fresh_requests"], 2)
        self.assertEqual(status["evidence_ledger"]["record_count"], 2)

    def test_debug_runtime_lazily_adds_telnet_without_reauthenticating_ssh(
        self,
    ) -> None:
        args = argparse.Namespace(
            ip=TEST_IP,
            ssh_user="ssh-user",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            ssh_host_key_policy="",
            ssh_known_hosts_file="",
            allow_insecure_host_key=False,
            telnet_user="telnet-user",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            redfish_port=443,
            timeout=30,
            skip_telnet=True,
        )
        ssh_credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {"user": "", "password": "", "port": 23},
        }
        telnet_credentials = {
            "user": "telnet-user",
            "password": "telnet-password",
            "port": 23,
        }
        ssh_transport = FakeSshTransport()
        telnet_transport = FakeTelnetTransport()

        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=ssh_credentials,
            task_id="typed-debug-lazy-telnet",
            ssh_transport_factory=lambda **_kwargs: ssh_transport,
            telnet_transport_factory=lambda **_kwargs: telnet_transport,
        ) as lease:
            ssh = lease.ssh_credentials_mapping()
            lease.ssh_runner(
                TEST_IP,
                str(ssh["user"]),
                str(ssh["password"]),
                "first",
                5,
                port=22,
                identity_file="",
            )
            args.skip_telnet = False
            lease.ensure_telnet(args, telnet_credentials)
            lease.ssh_runner(
                TEST_IP,
                str(ssh["user"]),
                str(ssh["password"]),
                "second",
                5,
                port=22,
                identity_file="",
            )
            lease.telnet_session.run_telnet_command("date", timeout=5)

        self.assertEqual(ssh_transport.authentications, 1)
        self.assertEqual(ssh_transport.channels, 2)
        self.assertEqual(telnet_transport.logins, 1)
        self.assertEqual(telnet_transport.commands, 1)

    def test_debug_read_reconnects_once_when_control_master_is_lost(self) -> None:
        args = argparse.Namespace(
            ip=TEST_IP,
            ssh_user="ssh-user",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            ssh_host_key_policy="",
            ssh_known_hosts_file="",
            allow_insecure_host_key=False,
            telnet_user="telnet-user",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
            redfish_port=443,
            timeout=30,
            skip_telnet=True,
        )
        credentials = {
            "ssh": {
                "user": "ssh-user",
                "password": "ssh-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "telnet-user",
                "password": "telnet-password",
                "port": 23,
            },
        }
        ssh_transport = RecoveringReadSshTransport()

        with _target_runtime_adapter.open_debug_runtime_lease(
            args=args,
            credential_bundle=credentials,
            task_id="typed-debug-replay-safe-read",
            ssh_transport_factory=lambda **_kwargs: ssh_transport,
        ) as lease:
            ssh = lease.ssh_credentials_mapping()
            result = lease.run_ssh_read(
                request_id="read-after-master-loss",
                collector_name="mdbctl",
                operation={"command": "read-only-query"},
                collect=lambda: lease.ssh_runner(
                    TEST_IP,
                    str(ssh["user"]),
                    str(ssh["password"]),
                    "read-only-query",
                    5,
                    port=22,
                    identity_file="",
                ).stdout,
            )
            status = lease.runtime_status()

        self.assertEqual(result, "read-only-query")
        self.assertEqual(ssh_transport.authentications, 2)
        self.assertEqual(ssh_transport.channels, 2)
        self.assertEqual(status["metrics"]["ssh_replay_safe_retries"], 1)

    def test_in_process_json_tool_preserves_child_contract_without_subprocess(self) -> None:
        command = [sys.executable, str(SCRIPT_DIR / "mdbctl_remote.py"), "--ip", TEST_IP]

        def invoke(_command: list[str]) -> int:
            print(json.dumps(child_payload("mdbctl_remote", result={"stdout_lines": ["C"]})))
            return 0

        with mock.patch.object(
            _workflow_runtime.subprocess,
            "run",
            side_effect=AssertionError("helper subprocess must not start"),
        ):
            result = _workflow_runtime.run_python_json_tool(
                "mdbctl",
                command,
                invoke,
                30,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["schema_version"], "openubmc-debug.v1")

    def test_workflow_cli_delegates_to_context_runtime_adapter(self) -> None:
        import target_runtime_cli

        argv = [
            "--ip",
            TEST_IP,
            "--mdb-query",
            "lsobj BusinessConnector",
            "--case-id",
            "case-existing",
            "--json",
        ]
        with mock.patch.object(
            target_runtime_cli,
            "run_legacy",
            return_value=0,
        ) as delegated:
            returncode = workflow_remote.main(argv)

        self.assertEqual(returncode, 0)
        delegated.assert_called_once_with(argv)


if __name__ == "__main__":
    unittest.main()
