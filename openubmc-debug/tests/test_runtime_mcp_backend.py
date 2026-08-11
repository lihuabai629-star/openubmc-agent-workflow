from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "runtime_status", "arguments": {}},
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
        self.assertEqual(set(responses), {1, 2, 3})
        self.assertEqual(
            [tool["name"] for tool in responses[2]["result"]["tools"]],
            [
                "debug_run",
                "debug_collect",
                "log_bundle_collect",
                "live_patch_run",
                "upgrade_run",
                "case_read",
                "evidence_read",
                "case_close",
                "case_forget",
                "phase_record",
                "workflow.advance",
                "workflow.next",
                "runtime_status",
            ],
        )
        status_envelope = responses[3]["result"]["structuredContent"]
        self.assertEqual(status_envelope["operation"]["name"], "runtime_status")
        self.assertEqual(status_envelope["status"], "completed")

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
                "method": "tools/call",
                "params": {"name": "runtime_status", "arguments": {}},
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
        self.assertEqual(
            responses[3]["result"]["structuredContent"]["status"],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
