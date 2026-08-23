from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"openubmc_debug_mcp_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTaskRun:
    def __init__(self) -> None:
        self.maintenance_runs = 0
        self.minimum_epochs: list[tuple[object, int, str]] = []

    def prune_dead_connections(self) -> int:
        self.maintenance_runs += 1
        return 0

    def ensure_target_epoch(self, target, minimum: int, *, reason: str) -> int:
        self.minimum_epochs.append((target, minimum, reason))
        return minimum


class FakeLease:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.task_run = FakeTaskRun()
        self.target = object()
        self.closed = False
        self.telnet_attachments: list[tuple[object, dict[str, str | int]]] = []

    def ensure_telnet(
        self,
        args,
        credential_bundle: dict[str, str | int],
    ) -> None:
        self.telnet_attachments.append((args, dict(credential_bundle)))

    def runtime_status(self) -> dict[str, object]:
        return {"task_id": self.task_id, "closed": self.closed}

    def close(self) -> None:
        self.closed = True


class RuntimeMcpBackendTests(unittest.TestCase):
    def test_agent_observe_bounds_parallel_selectors_and_reuses_one_lease(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        opened: list[FakeLease] = []
        active = 0
        peak = 0
        lock = threading.Lock()

        def open_lease(**kwargs):
            lease = FakeLease(str(kwargs["task_id"]))
            opened.append(lease)
            return lease

        def runner(name, command, _environment, _timeout, **_kwargs):
            nonlocal active, peak
            if name == "preflight_start":
                return {
                    "name": name,
                    "ok": True,
                    "code": "ok",
                    "returncode": 0,
                    "started_at": "2026-08-23T00:00:00Z",
                    "completed_at": "2026-08-23T00:00:00Z",
                    "payload": {
                        "observed_at": "2026-08-23T00:00:00Z",
                        "result": {
                            "checks": {
                                "SSH": {"ok": True},
                                "MDBCTL": {"ok": True},
                            }
                        },
                    },
                }
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.03)
                return {
                    "name": name,
                    "ok": True,
                    "code": "ok",
                    "returncode": 0,
                    "started_at": "2026-08-23T00:00:01Z",
                    "completed_at": "2026-08-23T00:00:02Z",
                    "payload": {"result": {"stdout_lines": [command[-1]]}},
                }
            finally:
                with lock:
                    active -= 1

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": {"user": "root", "password": "secret", "port": 23},
                },
            ),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=runner,
            ),
        ):
            service = runtime.RuntimeMcpService(
                runtime.OrchestratedMcpBackend(
                    {
                        "debug_collect": module.DebugMcpBackend(),
                        "debug_run": module.DebugMcpBackend(),
                    }
                )
            )
            try:
                receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "mdb-a",
                                "kind": "mdb",
                                "queries": [
                                    "lsprop Object0",
                                    "lsprop Object1",
                                    "lsprop Object2",
                                ],
                            },
                            {
                                "id": "mdb-b",
                                "kind": "mdb",
                                "queries": [
                                    "lsprop Object3",
                                    "lsprop Object4",
                                    "lsprop Object5",
                                ],
                            },
                        ],
                    },
                    task_id="bounded-selector-task",
                    operation_id="bounded-selector-operation",
                )
            finally:
                service.close()

        self.assertEqual(len(opened), 1)
        self.assertGreaterEqual(peak, 2)
        self.assertLessEqual(peak, 4)
        self.assertEqual(list(receipt["results"]), ["mdb-a", "mdb-b"])
        self.assertEqual(receipt["consistency"]["classification"], "coherent")
        self.assertEqual(
            [item["selector_id"] for item in receipt["consistency"]["selectors"]],
            ["mdb-a", "mdb-b"],
        )

    def test_failed_mdb_selector_is_partial_and_not_reusable(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()

        def runner(name, _command, _environment, _timeout, **_kwargs):
            if name == "preflight_start":
                return {
                    "name": name,
                    "ok": True,
                    "code": "ok",
                    "returncode": 0,
                    "started_at": "2026-08-23T00:00:00Z",
                    "completed_at": "2026-08-23T00:00:00Z",
                    "payload": {
                        "observed_at": "2026-08-23T00:00:00Z",
                        "result": {
                            "checks": {
                                "SSH": {"ok": True},
                                "MDBCTL": {"ok": True},
                            }
                        },
                    },
                }
            return {
                "name": name,
                "ok": False,
                "code": "timeout",
                "returncode": 124,
                "started_at": "2026-08-23T00:00:01Z",
                "completed_at": "2026-08-23T00:00:02Z",
                "payload": None,
                "error": "query timed out",
            }

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": {"user": "root", "password": "secret", "port": 23},
                },
            ),
            mock.patch.object(
                module,
                "open_debug_runtime_lease",
                return_value=FakeLease("failed-selector-task"),
            ),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=runner,
            ),
        ):
            service = runtime.RuntimeMcpService(module.DebugMcpBackend())
            try:
                receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "failed-mdb",
                                "kind": "mdb",
                                "queries": ["lsprop Object0"],
                            }
                        ],
                    },
                    task_id="failed-selector-task",
                    operation_id="failed-selector-operation",
                )
            finally:
                service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["consistency"]["classification"], "partial")
        self.assertNotIn("observation_ref", receipt)
        self.assertEqual(
            receipt["results"]["failed-mdb"]["values"][0]["status"],
            "unavailable",
        )

    def test_agent_observe_runs_one_preflight_and_only_exact_mdb_queries(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        calls: list[str] = []
        preflight_scopes: list[tuple[str, ...]] = []

        def runner(name, command, _environment, _timeout, **_kwargs):
            calls.append(name)
            if name in {"preflight_start", "preflight_end"}:
                preflight_scopes.append(
                    tuple(
                        command[index + 1]
                        for index, item in enumerate(command[:-1])
                        if item == "--check"
                    )
                )
                return {
                    "name": name,
                    "ok": True,
                    "code": "ok",
                    "returncode": 0,
                    "payload": {
                        "observed_at": "2026-08-19T00:00:00Z",
                        "result": {
                            "capabilities": {
                                "ssh_transport": True,
                                "remote_log_file": True,
                                "dbus_env": True,
                                "mdbctl": True,
                                "busctl": False,
                                **(
                                    {"active_alarm_endpoint_verified": True}
                                    if name == "preflight_end"
                                    else {}
                                ),
                            }
                        },
                    },
                }
            return {
                "name": name,
                "ok": True,
                "code": "ok",
                "returncode": 0,
                "payload": {
                    "result": {
                        "properties": {name: {"Value": name}},
                    }
                },
            }

        lease = FakeLease("agent-observe-task")
        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": {"user": "root", "password": "secret", "port": 23},
                },
            ),
            mock.patch.object(module, "open_debug_runtime_lease", return_value=lease),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=runner,
            ),
        ):
            backend = module.DebugMcpBackend()
            service = runtime.RuntimeMcpService(
                runtime.OrchestratedMcpBackend(
                    {"debug_collect": backend, "debug_run": backend}
                )
            )
            try:
                receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["ssh", "telnet", "mdbctl", "busctl"],
                            },
                            {
                                "id": "mdb",
                                "kind": "mdb",
                                "queries": ["lsprop Object0", "lsprop Object1"],
                            },
                        ],
                    },
                    task_id="agent-observe-task",
                    operation_id="agent-observe-operation",
                )
                combined_calls = list(calls)
                combined_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                mdb_only_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "mdb",
                                "kind": "mdb",
                                "queries": ["lsprop Object2"],
                            }
                        ],
                    },
                    task_id="agent-observe-mdb-only",
                    operation_id="agent-observe-mdb-only-operation",
                )
                mdb_only_calls = list(calls)
                mdb_only_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                capability_only_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["ssh", "telnet", "mdbctl", "busctl"],
                            }
                        ],
                    },
                    task_id="agent-observe-capability-only",
                    operation_id="agent-observe-capability-only-operation",
                )
                capability_only_calls = list(calls)
                capability_only_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                legacy_assurance_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["ssh", "busctl"],
                            },
                            {
                                "id": "mdb",
                                "kind": "mdb",
                                "queries": ["lsprop Object3"],
                            },
                        ],
                        "assurance": "assured",
                    },
                    task_id="agent-observe-assured",
                    operation_id="agent-observe-assured-operation",
                )
                legacy_assurance_calls = list(calls)
                legacy_assurance_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                ssh_only_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["ssh"],
                            }
                        ],
                    },
                    task_id="agent-observe-ssh-only",
                    operation_id="agent-observe-ssh-only-operation",
                )
                ssh_only_calls = list(calls)
                ssh_only_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                telnet_only_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["telnet"],
                            }
                        ],
                    },
                    task_id="agent-observe-telnet-only",
                    operation_id="agent-observe-telnet-only-operation",
                )
                telnet_only_calls = list(calls)
                telnet_only_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                dbus_only_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["dbus"],
                            }
                        ],
                    },
                    task_id="agent-observe-dbus-only",
                    operation_id="agent-observe-dbus-only-operation",
                )
                dbus_only_calls = list(calls)
                dbus_only_scopes = list(preflight_scopes)
                calls.clear()
                preflight_scopes.clear()
                automatic_receipt = service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.30",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["alarms"],
                            }
                        ],
                    },
                    task_id="agent-observe-automatic-assurance",
                    operation_id="agent-observe-automatic-assurance-operation",
                )
                automatic_calls = list(calls)
                automatic_scopes = list(preflight_scopes)
                case_ids = [
                    service._test.context_runtime.repository.case_for_task(task_id)
                    for task_id in (
                        "agent-observe-task",
                        "agent-observe-mdb-only",
                        "agent-observe-capability-only",
                        "agent-observe-assured",
                        "agent-observe-ssh-only",
                        "agent-observe-telnet-only",
                        "agent-observe-dbus-only",
                        "agent-observe-automatic-assurance",
                    )
                ]
            finally:
                service.close()

        self.assertEqual(
            case_ids,
            [None, None, None, None, None, None, None, None],
        )
        self.assertEqual(
            set(combined_calls), {"preflight_start", "mdbctl", "mdbctl_2"}
        )
        self.assertEqual(len(combined_calls), 3)
        self.assertEqual(mdb_only_calls, ["preflight_start", "mdbctl"])
        self.assertEqual(capability_only_calls, ["preflight_start"])
        self.assertEqual(legacy_assurance_calls, ["preflight_start", "mdbctl"])
        self.assertEqual(ssh_only_calls, ["preflight_start"])
        self.assertEqual(telnet_only_calls, ["preflight_start"])
        self.assertEqual(dbus_only_calls, ["preflight_start"])
        self.assertEqual(automatic_calls, ["preflight_start", "preflight_end"])
        self.assertEqual(
            combined_scopes,
            [("BUSCTL", "DBUS_ENV", "MDBCTL", "SSH", "TELNET")],
        )
        self.assertEqual(mdb_only_scopes, [("MDBCTL", "SSH")])
        self.assertEqual(
            capability_only_scopes,
            [("BUSCTL", "DBUS_ENV", "MDBCTL", "SSH", "TELNET")],
        )
        self.assertEqual(
            legacy_assurance_scopes,
            [("BUSCTL", "DBUS_ENV", "MDBCTL", "SSH")],
        )
        self.assertEqual(ssh_only_scopes, [("SSH",)])
        self.assertEqual(telnet_only_scopes, [("TELNET",)])
        self.assertEqual(dbus_only_scopes, [("DBUS_ENV", "SSH")])
        self.assertEqual(
            automatic_scopes,
            [
                ("BUSCTL", "DBUS_ENV", "SSH"),
                ("BUSCTL", "DBUS_ENV", "SSH"),
            ],
        )
        states = {
            item["name"]: item["status"]
            for item in receipt["results"]["caps"]["values"]
        }
        self.assertEqual(
            states,
            {
                "ssh": "available",
                "telnet": "available",
                "mdbctl": "available",
                "busctl": "unavailable",
            },
        )
        self.assertTrue(receipt["coverage"]["complete"])
        self.assertTrue(mdb_only_receipt["coverage"]["complete"])
        self.assertTrue(capability_only_receipt["coverage"]["complete"])
        self.assertNotIn("assurance", legacy_assurance_receipt)
        self.assertTrue(legacy_assurance_receipt["coverage"]["complete"])
        self.assertTrue(ssh_only_receipt["coverage"]["complete"])
        self.assertTrue(telnet_only_receipt["coverage"]["complete"])
        self.assertTrue(dbus_only_receipt["coverage"]["complete"])
        self.assertEqual(
            dbus_only_receipt["results"]["caps"]["values"],
            [{"name": "dbus", "status": "available"}],
        )
        self.assertNotIn("assurance", automatic_receipt)
        self.assertTrue(automatic_receipt["coverage"]["complete"])

    def test_workflow_argv_accepts_runtime_direct_passwords_without_legacy_projection(
        self,
    ) -> None:
        module = load_script("target_runtime_mcp")

        argv = module._workflow_argv(
            {
                "ip": "target.example",
                "ssh_password": "ssh-secret",
                "telnet_password": "telnet-secret",
            }
        )
        parsed = module.workflow_remote.parse_args(argv)

        self.assertEqual(parsed.ssh_password, "ssh-secret")
        self.assertEqual(parsed.telnet_password, "telnet-secret")

        legacy_args = module.workflow_remote.build_parser().parse_args(
            [
                "--ip",
                "target.example",
                "--ssh-password",
                "ssh-secret",
                "--telnet-password",
                "telnet-secret",
            ]
        )
        projected = module.workflow_arguments_from_namespace(legacy_args)
        self.assertNotIn("ssh_password", projected)
        self.assertNotIn("telnet_password", projected)

    def test_orchestrated_debug_restores_direct_passwords_for_case_continuation(
        self,
    ) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        runtime_mcp = sys.modules[runtime.OrchestratedMcpBackend.__module__]
        observed_passwords: list[tuple[str, str]] = []

        def execute(args, **kwargs):
            observed_passwords.append(
                (str(args.ssh_password), str(args.telnet_password))
            )
            kwargs["output_handler"](
                {
                    "schema": "openubmc-debug.v1",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "result": {},
                }
            )
            return 0

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "ssh-secret", "port": 22},
                    "telnet": None,
                },
            ),
            mock.patch.object(
                module,
                "open_debug_runtime_lease",
                return_value=FakeLease("direct-password-case-task"),
            ),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
            mock.patch.object(
                runtime_mcp,
                "load_selected_credentials_file",
                return_value={},
            ),
        ):
            service = runtime.RuntimeMcpService(
                runtime.OrchestratedMcpBackend(
                    {"debug_run": module.DebugMcpBackend()}
                )
            )
            try:
                first = service.call_tool(
                    "debug_run",
                    {
                        "ip": "target.example",
                        "ssh_user": "root",
                        "ssh_password": "ssh-secret",
                        "telnet_user": "root",
                        "telnet_password": "telnet-secret",
                        "deadline": 2,
                        "mdb_only": True,
                    },
                    task_id="direct-password-case-task",
                    operation_id="first",
                )
                case_id = str(first.envelope["case_id"])
                second = service.call_tool(
                    "debug_run",
                    {
                        "case_id": case_id,
                        "deadline": 2,
                        "mdb_only": True,
                    },
                    task_id="direct-password-case-task",
                    operation_id="continue",
                )
            finally:
                service.close()

        self.assertEqual(
            observed_passwords,
            [
                ("ssh-secret", "telnet-secret"),
                ("ssh-secret", "telnet-secret"),
            ],
        )
        self.assertEqual(second.envelope["case_id"], case_id)
        public_result = json.dumps(
            [first.envelope, second.envelope], ensure_ascii=False
        )
        self.assertNotIn("ssh-secret", public_result)
        self.assertNotIn("telnet-secret", public_result)

    def test_runtime_entrypoint_selects_default_credentials_file(self) -> None:
        module = load_script("target_runtime_mcp")
        with tempfile.TemporaryDirectory() as raw:
            config_root = Path(raw) / "config"
            credentials = config_root / "openubmc" / "credentials.env"
            credentials.parent.mkdir(parents=True)
            credentials.write_text(
                "OPENUBMC_SSH_USER=root\nOPENUBMC_SSH_PASSWORD=secret\n",
                encoding="utf-8",
            )
            credentials.chmod(0o600)
            with mock.patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(config_root)},
                clear=True,
            ):
                selected = module.select_default_credentials_file()
                exported = os.environ.get("OPENUBMC_CREDENTIALS_FILE")

        self.assertEqual(selected, str(credentials))
        self.assertEqual(exported, str(credentials))

    def test_orchestrated_debug_compares_two_roleless_targets_symmetrically(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        runtime_mcp = sys.modules[runtime.OrchestratedMcpBackend.__module__]
        observed_ips: list[str] = []

        def execute(args, **kwargs):
            observed_ips.append(str(args.ip))
            kwargs["output_handler"](
                {
                    "schema_version": "openubmc-debug.v1",
                    "tool": "workflow_remote",
                    "ip": str(args.ip),
                    "observed_at": "2026-08-04T00:00:00+00:00",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "normalized_code": "ok",
                    "warnings": [],
                    "error": "",
                    "request": {},
                    "result": {"capabilities": {}},
                }
            )
            return 0

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": None,
                },
            ),
            mock.patch.object(
                module,
                "open_debug_runtime_lease",
                side_effect=lambda **kwargs: FakeLease(str(kwargs["task_id"])),
            ),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
            mock.patch.object(
                runtime_mcp,
                "load_selected_credentials_file",
                return_value={},
            ),
        ):
            service = runtime.RuntimeMcpService(
                runtime.OrchestratedMcpBackend(
                    {"debug_run": module.DebugMcpBackend()}
                )
            )
            try:
                result = service.call_tool(
                    "debug_run",
                    {
                        "intent": "diagnosis-only",
                        "targets": [
                            {"ip": "target-a.example"},
                            {"ip": "target-b.example"},
                        ],
                        "deadline": 2,
                        "mdb_only": True,
                    },
                    task_id="symmetric-comparison-task",
                    operation_id="1",
                )
            finally:
                service.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "symmetric")
        self.assertEqual(
            [target["role"] for target in result["targets"]],
            ["target-a", "target-b"],
        )
        self.assertCountEqual(
            observed_ips,
            ["target-a.example", "target-b.example"],
        )

    def test_orchestrated_debug_projects_only_supported_connection_arguments(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        runtime_mcp = sys.modules[runtime.OrchestratedMcpBackend.__module__]
        captured_ports: list[tuple[int, int]] = []

        def execute(args, **kwargs):
            captured_ports.append((int(args.ssh_port), int(args.telnet_port)))
            kwargs["output_handler"](
                {
                    "schema": "openubmc-debug.v1",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "result": {},
                }
            )
            return 0

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": None,
                },
            ),
            mock.patch.object(
                module,
                "open_debug_runtime_lease",
                return_value=FakeLease("orchestrated-debug-task"),
            ),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
            mock.patch.object(
                runtime_mcp,
                "load_selected_credentials_file",
                return_value={},
            ),
        ):
            service = runtime.RuntimeMcpService(
                runtime.OrchestratedMcpBackend(
                    {"debug_run": module.DebugMcpBackend()}
                )
            )
            try:
                result = service.call_tool(
                    "debug_run",
                    {
                        "intent": "diagnosis-only",
                        "ip": "target.example",
                        "ssh_port": 2201,
                        "telnet_port": 2301,
                        "redfish_port": 8443,
                        "redfish_user_env": "ISOLATED_REDFISH_USER",
                        "redfish_password_env": "ISOLATED_REDFISH_PASSWORD",
                        "redfish_password": "isolated-test-secret",
                        "password": "isolated-test-secret",
                        "allow_insecure_tls": True,
                        "problem": "explicit target switch smoke",
                        "deadline": 2,
                        "mdb_only": True,
                    },
                    task_id="orchestrated-debug-task",
                    operation_id="1",
                )
            finally:
                service.close()

        self.assertTrue(result["ok"])
        self.assertEqual(captured_ports, [(2201, 2301)])

    def test_object_alarm_collect_uses_one_fast_live_snapshot(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        captured_modes: list[tuple[bool, bool, bool, bool]] = []

        def execute(args, **kwargs):
            captured_modes.append(
                (
                    bool(args.skip_telnet),
                    bool(args.no_freshness),
                    bool(args.no_source_correlation),
                    bool(args.fast_snapshot),
                )
            )
            kwargs["output_handler"](
                {
                    "schema": "openubmc-debug.v1",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "result": {},
                }
            )
            return 0

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": None,
                },
            ),
            mock.patch.object(
                module,
                "open_debug_runtime_lease",
                return_value=FakeLease("fast-object-alarm-task"),
            ),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
        ):
            service = runtime.RuntimeMcpService(module.DebugMcpBackend())
            try:
                result = service.call_tool(
                    "debug_collect",
                    {
                        "ip": "target.example",
                        "deadline": 2,
                        "profile": "object-alarm",
                    },
                    task_id="fast-object-alarm-task",
                    operation_id="1",
                )
            finally:
                service.close()

        self.assertTrue(result["ok"])
        self.assertEqual(captured_modes, [(True, True, True, True)])

    def test_mdb_collect_profile_uses_one_fast_live_snapshot(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        captured_modes: list[tuple[bool, bool, bool, bool, bool]] = []

        def execute(args, **kwargs):
            captured_modes.append(
                (
                    bool(args.mdb_only),
                    bool(args.skip_telnet),
                    bool(args.no_freshness),
                    bool(args.no_source_correlation),
                    bool(args.fast_snapshot),
                )
            )
            kwargs["output_handler"](
                {
                    "schema": "openubmc-debug.v1",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "result": {},
                }
            )
            return 0

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": None,
                },
            ),
            mock.patch.object(
                module,
                "open_debug_runtime_lease",
                return_value=FakeLease("fast-mdb-task"),
            ),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
        ):
            service = runtime.RuntimeMcpService(module.DebugMcpBackend())
            try:
                result = service.call_tool(
                    "debug_collect",
                    {
                        "ip": "target.example",
                        "deadline": 2,
                        "profile": "mdb",
                    },
                    task_id="fast-mdb-task",
                    operation_id="1",
                )
            finally:
                service.close()

        self.assertTrue(result["ok"])
        self.assertEqual(captured_modes, [(True, True, True, True, True)])

    def test_debug_tools_reuse_one_lease_and_one_credential_resolution(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        leases: list[FakeLease] = []
        credential_calls = 0
        workflow_calls: list[str] = []
        parallel_lane_calls: list[bool] = []
        mdb_query_calls: list[list[list[str]]] = []
        mdb_expand_calls: list[list[str]] = []
        mdb_concurrency_calls: list[str] = []
        mdb_only_calls: list[bool] = []
        fast_snapshot_calls: list[bool] = []

        def resolve_credentials(_args, *, include_telnet: bool):
            nonlocal credential_calls
            credential_calls += 1
            return {
                "ssh": {"user": "root", "password": "<secret>", "port": 22},
                "telnet": {
                    "user": "root",
                    "password": "<secret>",
                    "port": 23,
                },
            }

        def open_lease(*, args, credential_bundle, task_id: str):
            del args, credential_bundle
            lease = FakeLease(task_id)
            leases.append(lease)
            return lease

        def execute(args, **kwargs):
            workflow_calls.append(str(kwargs["engine"]))
            parallel_lane_calls.append(bool(kwargs["parallel_lanes"]))
            mdb_query_calls.append(list(args.mdb_queries))
            mdb_expand_calls.append(list(args.mdb_expand_classes))
            mdb_concurrency_calls.append(str(args.mdb_concurrency))
            mdb_only_calls.append(bool(args.mdb_only))
            fast_snapshot_calls.append(bool(getattr(args, "fast_snapshot", False)))
            kwargs["output_handler"](
                {
                    "schema": "openubmc-debug.v1",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "result": {
                        "runtime": {
                            "engine": kwargs["engine"],
                            "status": {
                                "targets": [
                                    {"epochs": {"target_epoch": 3}}
                                ]
                            },
                        }
                    },
                }
            )
            return 0

        with (
            mock.patch.object(module, "resolve_debug_credentials", resolve_credentials),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
        ):
            service = runtime.RuntimeMcpService(module.DebugMcpBackend())
            try:
                first = service.call_tool(
                    "debug_run",
                    {
                        "ip": "target.example",
                        "deadline": 2,
                        "mdb_queries": [
                            "lsobj BusinessConnector",
                            "lsobj PcieAddrInfo",
                        ],
                        "mdb_expand_classes": ["PCIeDevice"],
                        "mdb_concurrency": 3,
                        "mdb_only": True,
                    },
                    task_id="codex-task-a",
                    operation_id="1",
                )
                second = service.call_tool(
                    "debug_collect",
                    {
                        "ip": "target.example",
                        "deadline": 2,
                        "_minimum_target_epoch": 3,
                        "mdb_only": True,
                    },
                    task_id="codex-task-a",
                    operation_id="2",
                )
            finally:
                service.close()

        self.assertEqual(first["schema"], "openubmc-debug.v1")
        self.assertEqual(second["schema"], "openubmc-debug.v1")
        self.assertEqual(credential_calls, 1)
        self.assertEqual(len(leases), 1)
        self.assertEqual(workflow_calls, ["mcp", "mcp"])
        self.assertEqual(parallel_lane_calls, [True, True])
        self.assertEqual(
            mdb_query_calls,
            [
                [["lsobj", "BusinessConnector"], ["lsobj", "PcieAddrInfo"]],
                [],
            ],
        )
        self.assertEqual(mdb_expand_calls, [["PCIeDevice"], []])
        self.assertEqual(mdb_concurrency_calls, ["3", "auto"])
        self.assertEqual(mdb_only_calls, [True, True])
        self.assertEqual(fast_snapshot_calls, [False, True])
        self.assertEqual(leases[0].task_run.maintenance_runs, 1)
        self.assertEqual(
            leases[0].task_run.minimum_epochs,
            [(leases[0].target, 3, "orchestrated-fresh-verification")],
        )
        self.assertTrue(leases[0].closed)

    def test_evidence_profile_expansion_reuses_ssh_lease_and_lazily_adds_telnet(
        self,
    ) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        leases: list[FakeLease] = []
        credential_modes: list[bool] = []

        def resolve_credentials(_args, *, include_telnet: bool):
            credential_modes.append(include_telnet)
            return {
                "ssh": {
                    "user": "root",
                    "password": "<secret>",
                    "port": 22,
                    "identity_file": "",
                },
                "telnet": {
                    "user": "root" if include_telnet else "",
                    "password": "<secret>" if include_telnet else "",
                    "port": 23,
                },
            }

        def open_lease(**kwargs):
            lease = FakeLease(str(kwargs["task_id"]))
            leases.append(lease)
            return lease

        def execute(_args, **kwargs):
            kwargs["output_handler"](
                {
                    "schema_version": "openubmc-debug.v1",
                    "returncode": 0,
                    "ok": True,
                    "code": "ok",
                    "result": {},
                }
            )
            return 0

        with (
            mock.patch.object(module, "resolve_debug_credentials", resolve_credentials),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
            mock.patch.object(module, "resolve_source_root", return_value=(None, "none")),
            mock.patch.object(module.workflow_remote, "_execute_workflow", execute),
            mock.patch.object(
                module.workflow_remote,
                "build_typed_debug_tool_runner",
                return_value=object(),
            ),
        ):
            service = runtime.RuntimeMcpService(module.DebugMcpBackend())
            try:
                service.call_tool(
                    "debug_run",
                    {
                        "ip": "target.example",
                        "deadline": 2,
                        "mdb_only": True,
                    },
                    task_id="profile-expansion-task",
                    operation_id="1",
                )
                service.call_tool(
                    "debug_run",
                    {
                        "ip": "target.example",
                        "deadline": 2,
                    },
                    task_id="profile-expansion-task",
                    operation_id="2",
                )
            finally:
                service.close()

        self.assertEqual(len(leases), 1)
        self.assertEqual(credential_modes, [False, True])
        self.assertEqual(len(leases[0].telnet_attachments), 1)
        self.assertEqual(
            leases[0].telnet_attachments[0][1]["user"],
            "root",
        )

    def test_ssh_transport_policy_change_rebuilds_debug_lease(self) -> None:
        module = load_script("target_runtime_mcp")
        task = module.DebugMcpTask("transport-policy-task")
        opened: list[tuple[str, str, bool]] = []

        def open_lease(**kwargs):
            args = kwargs["args"]
            opened.append(
                (
                    str(args.ssh_host_key_policy),
                    str(args.ssh_known_hosts_file),
                    bool(args.allow_insecure_host_key),
                )
            )
            return FakeLease(str(kwargs["task_id"]))

        def args_for(policy: str, known_hosts: str):
            args = module.workflow_remote.parse_args(
                ["--ip", "target.example", "--json"]
            )
            args.ssh_host_key_policy = policy
            args.ssh_known_hosts_file = known_hosts
            args.allow_insecure_host_key = False
            return args

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {
                        "user": "root",
                        "password": "<secret>",
                        "port": 22,
                        "identity_file": "",
                    },
                    "telnet": {
                        "user": "root",
                        "password": "<secret>",
                        "port": 23,
                    },
                },
            ),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
        ):
            first = task.lease_for(args_for("strict", "/tmp/known-hosts-a"))
            same = task.lease_for(args_for("strict", "/tmp/known-hosts-a"))
            changed_policy = task.lease_for(
                args_for("accept-new", "/tmp/known-hosts-a")
            )
            changed_known_hosts = task.lease_for(
                args_for("accept-new", "/tmp/known-hosts-b")
            )

        self.assertIs(first, same)
        self.assertIsNot(first, changed_policy)
        self.assertIsNot(changed_policy, changed_known_hosts)
        self.assertEqual(
            opened,
            [
                ("strict", "/tmp/known-hosts-a", False),
                ("accept-new", "/tmp/known-hosts-a", False),
                ("accept-new", "/tmp/known-hosts-b", False),
            ],
        )

    def test_debug_lease_is_not_reused_after_credentials_change_or_close(self) -> None:
        module = load_script("target_runtime_mcp")
        task = module.DebugMcpTask("credential-bound-lease-task")
        opened: list[FakeLease] = []

        def open_lease(**kwargs):
            lease = FakeLease(str(kwargs["task_id"]))
            opened.append(lease)
            return lease

        args = module.workflow_remote.parse_args(
            ["--ip", "target.example", "--json"]
        )
        args.ssh_host_key_policy = "disabled"
        args.ssh_known_hosts_file = ""
        args.allow_insecure_host_key = False
        args.ssh_password_env = "OPENUBMC_ROTATING_TEST_PASSWORD"
        credentials_a = {
            "ssh_password": "secret-a",
            "telnet_password": "telnet-a",
        }
        credentials_b = {
            "ssh_password": "secret-b",
            "telnet_password": "telnet-b",
        }

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "secret", "port": 22},
                    "telnet": {"user": "root", "password": "secret", "port": 23},
                },
            ),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
        ):
            first = task.lease_for(args, credential_values=credentials_a)
            same = task.lease_for(args, credential_values=credentials_a)
            changed = task.lease_for(args, credential_values=credentials_b)
            changed.closed = True
            reopened = task.lease_for(args, credential_values=credentials_b)
            with mock.patch.dict(
                os.environ,
                {"OPENUBMC_ROTATING_TEST_PASSWORD": "environment-secret-a"},
            ):
                environment_first = task.lease_for(args)
            with mock.patch.dict(
                os.environ,
                {"OPENUBMC_ROTATING_TEST_PASSWORD": "environment-secret-b"},
            ):
                environment_changed = task.lease_for(args)
            with tempfile.TemporaryDirectory() as raw:
                identity = Path(raw) / "id_ed25519"
                identity.write_text("private-key-a", encoding="utf-8")
                args.ssh_identity_file = str(identity)
                identity_first = task.lease_for(args)
                identity.write_text("private-key-b", encoding="utf-8")
                identity_changed = task.lease_for(args)

        self.assertIs(first, same)
        self.assertIsNot(first, changed)
        self.assertIsNot(changed, reopened)
        self.assertIsNot(environment_first, environment_changed)
        self.assertIsNot(identity_first, identity_changed)
        self.assertEqual(len(opened), 7)

    def test_debug_lease_cache_is_bounded_without_limiting_target_count(self) -> None:
        module = load_script("target_runtime_mcp")
        task = module.DebugMcpTask("bounded-lease-task", max_cached_leases=2)
        opened: list[FakeLease] = []

        def open_lease(**kwargs):
            lease = FakeLease(str(kwargs["task_id"]))
            opened.append(lease)
            return lease

        def args_for(ip: str):
            args = module.workflow_remote.parse_args(["--ip", ip, "--json"])
            args.ssh_host_key_policy = "disabled"
            args.ssh_known_hosts_file = ""
            args.allow_insecure_host_key = False
            return args

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "x", "port": 22},
                    "telnet": {"user": "root", "password": "x", "port": 23},
                },
            ),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
        ):
            for ip in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
                task.lease_for(args_for(ip))
        status = task.status()
        self.assertEqual(status["debug_run_count"], 2)
        self.assertEqual(status["debug_lease_cache_limit"], 2)
        self.assertEqual(status["debug_lease_evictions"], 1)
        self.assertTrue(opened[0].closed)

    def test_active_debug_lease_is_not_evicted_during_parallel_collection(self) -> None:
        module = load_script("target_runtime_mcp")
        task = module.DebugMcpTask("active-lease-task", max_cached_leases=1)
        opened: list[FakeLease] = []

        def open_lease(**kwargs):
            lease = FakeLease(str(kwargs["task_id"]))
            opened.append(lease)
            return lease

        def args_for(ip: str):
            args = module.workflow_remote.parse_args(["--ip", ip, "--json"])
            args.ssh_host_key_policy = "disabled"
            args.ssh_known_hosts_file = ""
            args.allow_insecure_host_key = False
            return args

        with (
            mock.patch.object(
                module,
                "resolve_debug_credentials",
                return_value={
                    "ssh": {"user": "root", "password": "x", "port": 22},
                    "telnet": {"user": "root", "password": "x", "port": 23},
                },
            ),
            mock.patch.object(module, "open_debug_runtime_lease", open_lease),
            task.lease_scope(args_for("192.0.2.10")) as first,
        ):
            with task.lease_scope(args_for("192.0.2.11")) as second:
                self.assertFalse(first.closed)
                self.assertFalse(second.closed)
                self.assertEqual(task.status()["active_debug_leases"], 2)

        self.assertFalse(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual(task.status()["debug_run_count"], 1)

    def test_mcp_tasks_receive_the_durable_mutation_store(self) -> None:
        module = load_script("target_runtime_mcp")
        runtime = module._load_runtime_module()
        with tempfile.TemporaryDirectory() as raw:
            store = runtime.MutationJournalStore(Path(raw) / "mutations")
            backend = module.DebugMcpBackend(mutation_journal_store=store)
            task = backend.open_task("persisted-task")
            args = module.workflow_remote.parse_args(
                ["--ip", "target.example", "--json"]
            )
            captured: list[object] = []

            def open_lease(**kwargs):
                captured.append(kwargs.get("mutation_journal_store"))
                return FakeLease("persisted-task")

            with (
                mock.patch.object(
                    module,
                    "resolve_debug_credentials",
                    return_value={
                        "ssh": {"user": "root", "password": "secret", "port": 22},
                        "telnet": {
                            "user": "root",
                            "password": "secret",
                            "port": 23,
                        },
                    },
                ),
                mock.patch.object(module, "open_debug_runtime_lease", open_lease),
            ):
                task.lease_for(args)

        self.assertEqual(captured, [store])

    def test_stdio_entrypoint_lists_tools_without_opening_a_target(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        with tempfile.TemporaryDirectory() as raw_state:
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "target_runtime_mcp.py")],
                input="".join(json.dumps(item) + "\n" for item in requests),
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CODEX_TASK_ID": "entrypoint-task",
                    "OPENUBMC_TARGET_RUNTIME_STATE_DIR": raw_state,
                },
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = {
            response["id"]: response
            for response in (
                json.loads(line)
                for line in completed.stdout.splitlines()
                if line.strip()
            )
        }
        self.assertEqual(set(responses), {1, 2})
        self.assertEqual(
            [tool["name"] for tool in responses[2]["result"]["tools"]],
            ["observe", "execute"],
        )

    def test_stdio_validation_failure_returns_an_error_and_keeps_serving(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "debug_run",
                    "arguments": {
                        "ip": "target.example",
                        "mdb_queries": ["setprop Bad Value"],
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/list",
                "params": {},
            },
        ]
        with tempfile.TemporaryDirectory() as raw_state:
            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "target_runtime_mcp.py")],
                input="".join(json.dumps(item) + "\n" for item in requests),
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CODEX_TASK_ID": "validation-failure-task",
                    "OPENUBMC_TARGET_RUNTIME_STATE_DIR": raw_state,
                    "OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE": "compatibility",
                },
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = {
            response["id"]: response
            for response in (
                json.loads(line)
                for line in completed.stdout.splitlines()
                if line.strip()
            )
        }
        failed = responses[2]["result"]
        self.assertTrue(failed["isError"])
        self.assertEqual(failed["structuredContent"]["status"], "failed")
        self.assertEqual(
            failed["structuredContent"]["canonical_error"]["code"],
            "ValueError",
        )
        exposed_tools = [
            tool["name"] for tool in responses[3]["result"]["tools"]
        ]
        self.assertIn("debug_run", exposed_tools)
        self.assertNotIn("runtime_status", exposed_tools)


if __name__ == "__main__":
    unittest.main()
