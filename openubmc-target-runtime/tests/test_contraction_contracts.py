from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "openubmc-target-runtime"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RuntimeContractionContracts(unittest.TestCase):
    def test_runtime_operation_contracts_are_the_single_domain_metadata_source(self) -> None:
        mcp_source = (
            RUNTIME_ROOT / "openubmc_target_runtime/mcp.py"
        ).read_text(encoding="utf-8")
        workflow_source = (
            RUNTIME_ROOT / "openubmc_target_runtime/workflow.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("_OPERATION_BINDINGS", mcp_source)
        self.assertNotIn("_CAPABILITY_CONTRACTS", mcp_source)
        self.assertIn("DEFAULT_OPERATION_CONTRACTS", mcp_source)
        self.assertIn(
            "operation_owners=DEFAULT_OPERATION_CONTRACTS.operation_owners()",
            workflow_source,
        )

    def test_workflow_definitions_expose_one_deterministic_cursor(self) -> None:
        sys.path.insert(0, str(RUNTIME_ROOT))
        try:
            from openubmc_target_runtime.workflow import (
                DEFAULT_WORKFLOW_DEFINITIONS,
            )

            projection = {
                "intent": "diagnose-and-fix",
                "entry_domain": "debug",
                "entry_operation": "debug_run",
                "delivery_strategy": "source-only",
                "workflow_cycle_id": "cycle-1",
                "target_version": 1,
            }
            nodes = [{"step_id": "step-01-debug-run", "status": "completed"}]
            first = DEFAULT_WORKFLOW_DEFINITIONS.semantic_cursor(
                projection,
                nodes=nodes,
                acceptance_plan_id="acceptance-1",
            )
            replayed = DEFAULT_WORKFLOW_DEFINITIONS.semantic_cursor(
                projection,
                nodes=nodes,
                acceptance_plan_id="acceptance-1",
            )
            changed = DEFAULT_WORKFLOW_DEFINITIONS.semantic_cursor(
                projection,
                nodes=[{"step_id": "step-01-debug-run", "status": "failed"}],
                acceptance_plan_id="acceptance-1",
            )
        finally:
            sys.path.remove(str(RUNTIME_ROOT))

        self.assertEqual(replayed, first)
        self.assertNotEqual(changed, first)

    def test_agent_gateway_routes_requests_through_typed_runtime_values(self) -> None:
        sys.path.insert(0, str(RUNTIME_ROOT))
        try:
            from openubmc_target_runtime import (
                AgentGateway,
                ObservationQuery,
                ObservationRef,
                ObservationResult,
                RunTurn,
                StartRun,
            )

            class TypedRuntime:
                observed = None
                executed = None

                def observe(self, query, *, task_id, operation_id):
                    self.observed = query
                    return ObservationResult(
                        query=query,
                        raw={
                            "observed_at": "2026-08-20T00:00:00Z",
                            "observation_timing": {
                                "started_at": "2026-08-20T00:00:00Z",
                                "completed_at": "2026-08-20T00:00:00Z",
                                "selectors": [
                                    {
                                        "selector_id": "selector-1",
                                        "kind": "capability",
                                        "started_at": "2026-08-20T00:00:00Z",
                                        "completed_at": "2026-08-20T00:00:00Z",
                                        "status": "observed",
                                    }
                                ],
                                "classification": "coherent",
                                "max_skew_seconds": 5.0,
                                "observed_skew_seconds": 0.0,
                                "reusable": True,
                                "gaps": [],
                            },
                            "result": {
                                "capabilities": {"ssh_transport": True},
                                "lanes": {"ssh": {}},
                            },
                        },
                        assurance="fast",
                        observation_ref=ObservationRef(
                            handle="blob://" + "1" * 64,
                            digest="1" * 64,
                            size=1,
                            provenance="runtime-observation",
                            retention_hint="run-lifetime",
                            kind="observation",
                            target=query.target,
                            scope_digest="2" * 64,
                            observed_at="2026-08-20T00:00:00Z",
                        ),
                    )

                def execute(self, command, *, task_id, operation_id):
                    self.executed = command
                    return RunTurn(run_id="run-typed", state="running")

            runtime = TypedRuntime()
            gateway = AgentGateway(runtime)
            receipt = gateway.observe(
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"kind": "capability", "names": ["ssh"]}
                    ],
                },
                task_id="typed-observe",
                operation_id="typed-observe-1",
            )
            turn = gateway.execute(
                {"kind": "start", "target": "192.0.2.10"},
                task_id="typed-execute",
                operation_id="typed-execute-1",
            )
        finally:
            sys.path.remove(str(RUNTIME_ROOT))

        self.assertIsInstance(runtime.observed, ObservationQuery)
        self.assertIsInstance(runtime.executed, StartRun)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(turn["run_id"], "run-typed")

    def test_live_patch_clis_route_mutations_through_runtime(self) -> None:
        for name in ("deploy_live_file.py", "rollback_live_file.py"):
            source = (
                REPO_ROOT / "openubmc-live-patch" / "scripts" / name
            ).read_text(encoding="utf-8")

            self.assertNotIn("OPENUBMC_DEBUG_HELPERS", source)
            self.assertNotIn("from _cli_common import", source)
            self.assertNotIn("from _telnet_common import", source)
            self.assertIn("from runtime_cli import", source)
            self.assertIn("run_runtime_mutation(", source)

    def test_debug_mcp_loads_the_public_log_analyzer_backend(self) -> None:
        source = (
            REPO_ROOT / "openubmc-debug/scripts/target_runtime_mcp.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("spec_from_file_location", source)
        self.assertNotIn("scripts/target_runtime_adapter.py", source)
        self.assertIn("openubmc_log_analyzer.runtime_backend", source)

    def test_log_analyzer_declares_its_public_backend_in_the_package(self) -> None:
        manifest = json.loads(
            (REPO_ROOT / "openubmc-log-analyzer/skill.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn("openubmc_log_analyzer/__init__.py", manifest["files"])
        self.assertIn(
            "openubmc_log_analyzer/runtime_backend.py", manifest["files"]
        )

    def test_log_analyzer_uses_one_multi_selector_target_spec(self) -> None:
        source = (
            REPO_ROOT
            / "openubmc-log-analyzer/openubmc_log_analyzer/runtime_backend.py"
        ).read_text(encoding="utf-8")

        self.assertIn("TargetSpec.for_credential_selectors", source)
        self.assertIn("self.redfish_target = self.target", source)
        self.assertIn("self.ssh_target = self.target", source)
        self.assertNotIn("self.redfish_target = runtime.TargetSpec(", source)
        self.assertNotIn("self.ssh_target = runtime.TargetSpec(", source)

    def test_upgrade_credentials_are_the_runtime_public_type(self) -> None:
        sys.path.insert(0, str(RUNTIME_ROOT))
        try:
            from openubmc_target_runtime import ResolvedRedfishCredentials

            module = load_module(
                "upgrade_redfish_credentials_contract",
                REPO_ROOT / "openubmc-upgrade/scripts/redfish_credentials.py",
            )
            with tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / "credentials"
                path.write_text(
                    "REDFISH_USERNAME=Administrator\n"
                    "REDFISH_PASSWORD=<runtime-secret>\n",
                    encoding="utf-8",
                )
                path.chmod(0o600)
                credentials = module.load_credentials(path)
        finally:
            sys.path.remove(str(RUNTIME_ROOT))

        self.assertIsInstance(credentials, ResolvedRedfishCredentials)
        self.assertEqual(credentials.username, "Administrator")

    def test_combined_debug_v1_core_has_no_helper_subprocess_fanout(self) -> None:
        source = (
            REPO_ROOT / "openubmc-debug/scripts/workflow_remote.py"
        ).read_text(encoding="utf-8")
        core = source.split("def _execute_workflow(", 1)[1].split(
            "\ndef main()", 1
        )[0]

        self.assertNotIn("subprocess.run", core)
        self.assertNotIn("run_json_tool(", core)
        self.assertIn("tool_runner(", core)

    def test_qemu_monitor_and_formal_redfish_sessions_remain_independent(self) -> None:
        protected_roots = (
            REPO_ROOT / "qemu-testing",
            REPO_ROOT / "openubmc-qemu-arm-kvm",
            REPO_ROOT / "openubmc-redfish-testing",
        )
        offenders = []
        for root in protected_roots:
            for path in root.rglob("*.py"):
                if "openubmc_target_runtime" in path.read_text(
                    encoding="utf-8", errors="ignore"
                ):
                    offenders.append(str(path.relative_to(REPO_ROOT)))

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
