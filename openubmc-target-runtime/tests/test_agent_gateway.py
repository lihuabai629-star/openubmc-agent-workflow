from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    AgentGateway,
    OBSERVATION_MAX_BYTES,
    TOOLS_LIST_MAX_BYTES,
    TURN_MAX_BYTES,
    JsonRpcMcpEndpoint,
    FilesystemBlobRepository,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
    ScopeContract,
    ScopeViolation,
)


def encoded_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class SemanticBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def open_task(self, task_id: str) -> FakeTask:
        return FakeTask(task_id)

    @staticmethod
    def close_task(_task: FakeTask) -> None:
        return None

    @staticmethod
    def maintain_task(_task: FakeTask) -> int:
        return 0

    @staticmethod
    def task_status(task: FakeTask) -> dict[str, object]:
        return {"task_id": task.task_id}

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_collect", dict(arguments)))
        queries = list(arguments.get("mdb_queries", []))
        ssh: dict[str, object] = {}
        for index, query in enumerate(queries):
            name = "mdbctl" if index == 0 else f"mdbctl_{index + 1}"
            ssh[name] = {
                "ok": True,
                "payload": {
                    "result": {
                        "properties": {
                            f"Object{index}": {"Query": query, "Value": index}
                        }
                    }
                },
            }
        return {
            "ok": True,
            "observed_at": "2026-08-19T00:00:00Z",
            "result": {
                "capabilities": {
                    "ssh_transport": True,
                    "mdbctl": True,
                    "busctl": False,
                    "active_alarm_transport": True,
                    "active_alarm_endpoint_verified": False,
                    "active_alarms": True,
                },
                "lanes": {"ssh": ssh},
            },
        }

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("mdb_only"):
            return self.debug_collect(task, arguments, context)
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "schema": "openubmc-debug.v1",
            "task": task.task_id,
            "summary": "diagnosis completed",
        }

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        return {
            "ok": True,
            "summary": "live patch verified",
            "target_epoch": 1,
            "journal": {"stage": "verified", "action": "live_patch"},
        }

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("upgrade_run", dict(arguments)))
        return {
            "ok": True,
            "summary": "upgrade verified",
            "target_epoch": 1,
            "journal": {"stage": "verified", "action": "upgrade"},
        }


class LargeObservationBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        ssh = value["result"]["lanes"]["ssh"]
        for child in ssh.values():
            child["payload"]["result"]["properties"]["Large"] = {
                "Value": "x" * 12_000
            }
        return value


class OversizedObservationBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value["result"]["runtime"] = {
            "status": {
                "targets": [
                    {
                        "target": {
                            "host": "host-" + "h" * 20_000,
                            "fingerprint": "fingerprint-" + "f" * 20_000,
                        },
                        "epochs": {"target_epoch": 1},
                    }
                ]
            }
        }
        return value


class FailOnceUpgradeSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.upgrade_attempts = 0

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.upgrade_attempts += 1
        self.calls.append(("upgrade_run", dict(arguments)))
        if self.upgrade_attempts == 1:
            raise OSError("upload connection lost")
        return {
            "ok": True,
            "summary": "upgrade reconciled and verified",
            "target_epoch": 1,
            "journal": {"stage": "verified", "action": "upgrade"},
        }


class FailLivePatchSemanticBackend(SemanticBackend):
    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        raise OSError("live patch connection lost")


class AutoAssuranceSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.assurance_calls: list[bool] = []
        self.mdb_collections = 0

    def observe_query(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        assured = bool(arguments.get("assured"))
        prior = arguments.get("prior_observation")
        self.assurance_calls.append(assured)
        if isinstance(prior, dict):
            value = json.loads(json.dumps(prior))
            value["result"]["capabilities"]["remote_log_file"] = True
            value["observed_at"] = "2026-08-19T00:00:01Z"
            return value
        self.mdb_collections += 1
        value = self.debug_collect(task, arguments, context)
        value["result"]["capabilities"].pop("remote_log_file", None)
        return value


class OversizedTurnRuntime:
    def run_operation(self, operation, arguments, *, task_id, operation_id):
        result = {
            "status": "blocked",
            "next_action": "next-" + "n" * 20_000,
        }

        class RuntimeResult(dict):
            pass

        runtime_result = RuntimeResult(result)
        runtime_result.envelope = {
                "case_id": "case-oversized-turn",
                "status": "blocked",
                "facts": [{"value": "f" * 20_000}],
                "gaps": ["gap-" + "g" * 20_000],
        }
        return runtime_result


class AgentGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = SemanticBackend()
        self.service = RuntimeMcpService(self.backend)

    def tearDown(self) -> None:
        self.service.close()

    def test_default_interface_has_two_small_semantic_tools(self) -> None:
        definitions = self.service.tool_definitions()
        self.assertEqual([item["name"] for item in definitions], ["observe", "execute"])
        self.assertLessEqual(encoded_size(definitions), TOOLS_LIST_MAX_BYTES)
        self.assertEqual(self.service.interface_catalog.names(), ("observe", "execute"))
        execute_schema = definitions[1]["inputSchema"]
        self.assertNotIn("max_steps", execute_schema["properties"])

    def test_observe_is_bounded_grounded_and_does_not_open_a_case(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {"id": "caps", "kind": "capability", "names": ["ssh", "telnet", "busctl"]},
                    {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]},
                ],
                "freshness": {"mode": "live", "max_age_seconds": 0},
            },
            task_id="observe-task",
            operation_id="observe-1",
        )

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        states = {
            item["name"]: item["status"]
            for item in receipt["results"]["caps"]["values"]
        }
        self.assertEqual(
            states,
            {"ssh": "available", "telnet": "not_checked", "busctl": "unavailable"},
        )
        self.assertTrue(
            all(claim["receipt_id"] == receipt["receipt_id"] for claim in receipt["claims"])
        )
        self.assertIsNone(
            self.service.context_runtime.repository.case_for_task("observe-task")
        )

    def test_one_receipt_fits_four_capabilities_and_nine_exact_getprop_values(self) -> None:
        queries = [
            f"getprop Drive_1_010102 bmc.kepler.Interface Property{index}"
            for index in range(9)
        ]
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {
                        "id": "caps",
                        "kind": "capability",
                        "names": ["ssh", "telnet", "mdbctl", "busctl"],
                    },
                    {"id": "mdb", "kind": "mdb", "queries": queries},
                ],
            },
            task_id="observe-nine-properties",
            operation_id="observe-nine-properties-1",
        )

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertNotIn("content_compacted", receipt)
        self.assertEqual(receipt["coverage"]["requested"], 13)
        self.assertEqual(len(receipt["results"]["mdb"]["values"]), 9)
        self.assertEqual(len(receipt["claims"]), 2)

    def test_alarm_capability_is_not_checked_until_the_endpoint_is_verified(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {"id": "alarm-capability", "kind": "capability", "names": ["alarms"]}
                ],
            },
            task_id="observe-alarm-capability",
            operation_id="observe-alarm-capability-1",
        )

        self.assertEqual(
            receipt["results"]["alarm-capability"]["values"],
            [{"name": "alarms", "status": "not_checked"}],
        )
        self.assertEqual(receipt["status"], "incomplete")

    def test_observe_compaction_is_incomplete_and_never_exceeds_four_kib(self) -> None:
        service = RuntimeMcpService(LargeObservationBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "large",
                            "kind": "mdb",
                            "queries": [f"lsprop Object{index}" for index in range(16)],
                        }
                    ],
                },
                task_id="observe-large",
                operation_id="observe-large-1",
            )
        finally:
            service.close()

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertFalse(receipt["coverage"]["complete"])
        self.assertTrue(receipt["content_compacted"])

    def test_observe_hard_limit_survives_oversized_target_metadata(self) -> None:
        service = RuntimeMcpService(OversizedObservationBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "caps", "kind": "capability", "names": ["ssh"]}
                    ],
                    "assurance": "fast",
                },
                task_id="observe-oversized-target",
                operation_id="observe-oversized-target-1",
            )
        finally:
            service.close()

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(receipt["content_compacted"])

    def test_observe_hard_limit_survives_maximum_legal_scope_and_large_result(self) -> None:
        service = RuntimeMcpService(LargeObservationBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "t" * 512,
                    "selectors": [
                        {
                            "id": "s" * 64,
                            "kind": "mdb",
                            "queries": ["q" * 1024],
                        }
                    ],
                },
                task_id="observe-maximum-scope",
                operation_id="observe-maximum-scope-1",
            )
        finally:
            service.close()

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(receipt["content_compacted"])

    def test_scope_contract_fails_closed_for_undeclared_surface_or_freshness(self) -> None:
        with self.assertRaises(ScopeViolation):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "shell", "queries": ["id"]}],
                }
            )

        with self.assertRaisesRegex(ValueError, "target"):
            self.service.call_exposed_tool(
                "observe",
                {
                    "target": "x" * 513,
                    "selectors": [
                        {"kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                },
                task_id="oversized-scope",
                operation_id="oversized-scope-1",
            )

    def test_scope_contract_rejects_duplicate_selector_ids(self) -> None:
        with self.assertRaisesRegex(ScopeViolation, "selector ids must be unique"):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "duplicate", "kind": "capability", "names": ["ssh"]},
                        {
                            "id": "duplicate",
                            "kind": "mdb",
                            "queries": ["lsprop Object0"],
                        },
                    ],
                }
            )

    def test_assured_observation_fails_closed_without_a_precise_adapter(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "scope-preserving"):
            self.service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                    "assurance": "assured",
                },
                task_id="assured-without-adapter",
                operation_id="assured-without-adapter-1",
            )

    def test_auto_assurance_upgrades_and_reuses_the_fast_observation(self) -> None:
        backend = AutoAssuranceSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "caps", "kind": "capability", "names": ["telnet"]},
                        {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]},
                    ],
                    "assurance": "auto",
                },
                task_id="auto-assurance",
                operation_id="auto-assurance-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["assurance"], "assured")
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(backend.assurance_calls, [False, True])
        self.assertEqual(backend.mdb_collections, 1)
        with self.assertRaises(ScopeViolation):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "capability", "names": ["ssh"]}],
                    "freshness": {"mode": "cached", "max_age_seconds": 60},
                }
            )

    def test_execute_hides_runtime_mechanics_and_records_terminal_outcome(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "repair the diagnosed source defect",
            },
            task_id="execute-task",
            operation_id="execute-1",
        )
        self.assertEqual(first["state"], "waiting_response")
        self.assertEqual(first["gate"]["name"], "developer.change")
        self.assertLessEqual(encoded_size(first), TURN_MAX_BYTES)
        rendered = json.dumps(first, ensure_ascii=False)
        for hidden in ("phase_record", "workflow.next", "offset"):
            self.assertNotIn(hidden, rendered)

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)

        self.assertNotIn("revision", set(keys(first)))
        self.assertNotIn("attempt", set(keys(first)))

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "abc123",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run regression tests"],
                    },
                },
            },
            task_id="execute-task",
            operation_id="execute-2",
        )
        self.assertEqual(final["state"], "completed")
        self.assertIsNotNone(final["outcome"])
        self.assertTrue(final["outcome_recorded"])
        self.assertLessEqual(encoded_size(final), TURN_MAX_BYTES)
        self.assertEqual(self.service.session_outcome_service.status()["outcome_count"], 1)

    def test_execute_turn_hard_limit_survives_oversized_blocker_details(self) -> None:
        turn = AgentGateway(OversizedTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-turn",
            operation_id="oversized-turn-1",
        )

        self.assertLessEqual(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["run_id"], "case-oversized-turn")
        self.assertEqual(turn["state"], "blocked")
        self.assertEqual(turn["gate"]["kind"], "blocker")
        self.assertTrue(turn["content_compacted"])

    def test_execute_start_reuses_a_complete_observation_receipt(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.40",
                "selectors": [
                    {"id": "facts", "kind": "mdb", "queries": ["lsprop Object0"]}
                ],
            },
            task_id="receipt-reuse",
            operation_id="receipt-observe",
        )

        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.40",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "repair using the grounded observation",
                "observation_receipt": receipt,
            },
            task_id="receipt-reuse",
            operation_id="receipt-execute",
        )

        self.assertEqual(first["state"], "waiting_response")
        self.assertEqual(first["gate"]["name"], "developer.change")
        self.assertEqual(first["observation_receipt_id"], receipt["receipt_id"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_collect"],
        )

    def test_observation_receipt_reconstructs_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "receipt.sqlite3"
            blobs = root / "receipt-blobs"
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                receipt = first.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.42",
                        "selectors": [
                            {"id": "facts", "kind": "mdb", "queries": ["lsprop Object0"]}
                        ],
                    },
                    task_id="receipt-restart-observe",
                    operation_id="receipt-restart-observe-1",
                )
            finally:
                first.close()

            resumed_backend = SemanticBackend()
            second = RuntimeMcpService(
                resumed_backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                turn = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.42",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                        "observation_receipt": receipt,
                    },
                    task_id="receipt-restart-execute",
                    operation_id="receipt-restart-execute-1",
                )
            finally:
                second.close()

        self.assertEqual(turn["gate"]["name"], "developer.change")
        self.assertEqual(resumed_backend.calls, [])

    def test_source_only_failed_phase_never_produces_a_success_outcome(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.41",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="source-failure",
            operation_id="source-failure-start",
        )
        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                "response": {
                    "status": "failed",
                    "summary": "source repair validation failed",
                    "payload": {},
                },
            },
            task_id="source-failure",
            operation_id="source-failure-respond",
        )

        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["outcome"]["status"], "failed")
        self.assertTrue(final["outcome_recorded"])

    def test_control_cancel_without_response_terminates_the_run(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.61",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="cancel-run",
            operation_id="cancel-run-start",
        )

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "control",
                "run_id": first["run_id"],
                "command": "cancel",
            },
            task_id="cancel-run",
            operation_id="cancel-run-control",
        )

        self.assertEqual(final["state"], "cancelled")
        self.assertIsNone(final["gate"])
        self.assertEqual(final["outcome"]["status"], "cancelled")
        self.assertTrue(final["outcome_recorded"])

    def test_execute_live_patch_runs_diagnosis_mutation_and_fresh_verification(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.21",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
                "purpose": "repair and verify the running target",
            },
            task_id="execute-live-patch",
            operation_id="live-patch-start",
        )
        self.assertEqual(first["state"], "waiting_response")
        self.assertEqual(first["gate"]["name"], "developer.change")

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                "response": {
                    "status": "completed",
                    "summary": "source repair is ready for live patching",
                    "payload": {
                        "source_revision": "live-patch-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["fresh target verification"],
                        "artifact_path": "/tmp/fix.lua",
                        "remote_path": "/opt/bmc/apps/fix.lua",
                        "restart_scope": "skynet",
                    },
                },
            },
            task_id="execute-live-patch",
            operation_id="live-patch-respond",
        )

        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_run", "live_patch_run", "debug_collect"],
        )
        verification_arguments = self.backend.calls[-1][1]
        self.assertEqual(verification_arguments["profile"], "standard")
        self.assertFalse(verification_arguments["no_freshness"])

    def test_execute_build_upgrade_runs_both_gates_and_fresh_verification(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.22",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "purpose": "build, deploy, and verify a firmware repair",
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-start",
        )
        self.assertEqual(first["gate"]["name"], "developer.change")

        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "upgrade-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and target verification"],
                    },
                },
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-developer",
        )
        self.assertEqual(build_gate["state"], "waiting_response")
        self.assertEqual(build_gate["gate"]["name"], "build.artifact")

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                "response": {
                    "status": "completed",
                    "summary": "firmware artifact completed",
                    "payload": {
                        "source_revision": "upgrade-source",
                        "artifact_path": "/tmp/product.hpm",
                        "artifact_sha256": "a" * 64,
                        "product_version": "1.2.3",
                    },
                },
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-build",
        )

        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_run", "upgrade_run", "debug_collect"],
        )
        verification_arguments = self.backend.calls[-1][1]
        self.assertEqual(verification_arguments["profile"], "standard")
        self.assertFalse(verification_arguments["no_freshness"])

    def test_execute_reconcile_resumes_the_unknown_mutation_journal(self) -> None:
        backend = FailOnceUpgradeSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.30",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "purpose": "repair and reconcile an interrupted upgrade",
                },
                task_id="execute-reconcile",
                operation_id="reconcile-start",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": first["run_id"],
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "reconcile-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["build and verify"],
                        },
                    },
                },
                task_id="execute-reconcile",
                operation_id="reconcile-developer",
            )
            self.assertEqual(build_gate["gate"]["name"], "build.artifact")
            blocked = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": first["run_id"],
                    "response": {
                        "status": "completed",
                        "summary": "firmware artifact completed",
                        "payload": {
                            "source_revision": "reconcile-source",
                            "artifact_path": "/tmp/product.hpm",
                            "artifact_sha256": "b" * 64,
                            "product_version": "2.0.0",
                        },
                    },
                },
                task_id="execute-reconcile",
                operation_id="reconcile-build",
            )
            self.assertEqual(blocked["state"], "mutation_outcome_unknown")

            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "control",
                    "run_id": first["run_id"],
                    "command": "reconcile",
                },
                task_id="execute-reconcile",
                operation_id="reconcile-control",
            )
        finally:
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(backend.upgrade_attempts, 2)

    def test_execute_workflows_resume_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            source_database = root / "source.sqlite3"
            source_blobs = root / "source-blobs"
            source_first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(source_database),
                blob_repository=FilesystemBlobRepository(source_blobs),
            )
            try:
                source_gate = source_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.51",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id="restart-source",
                    operation_id="restart-source-start",
                )
            finally:
                source_first.close()
            source_second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(source_database),
                blob_repository=FilesystemBlobRepository(source_blobs),
            )
            try:
                source_final = source_second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": source_gate["run_id"],
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed after restart",
                            "payload": {
                                "source_revision": "restart-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["run regression tests"],
                            },
                        },
                    },
                    task_id="restart-source-resumed",
                    operation_id="restart-source-respond",
                )
            finally:
                source_second.close()
            self.assertEqual(source_final["state"], "completed")

            live_database = root / "live.sqlite3"
            live_blobs = root / "live-blobs"
            live_first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(live_database),
                blob_repository=FilesystemBlobRepository(live_blobs),
            )
            try:
                live_gate = live_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.52",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="restart-live",
                    operation_id="restart-live-start",
                )
            finally:
                live_first.close()
            live_second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(live_database),
                blob_repository=FilesystemBlobRepository(live_blobs),
            )
            try:
                live_final = live_second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": live_gate["run_id"],
                        "response": {
                            "status": "completed",
                            "summary": "source repair restored after restart",
                            "payload": {
                                "source_revision": "restart-live",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_path": "/tmp/fix.lua",
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                    task_id="restart-live-resumed",
                    operation_id="restart-live-respond",
                )
            finally:
                live_second.close()
            self.assertEqual(live_final["state"], "completed")

            build_database = root / "build.sqlite3"
            build_blobs = root / "build-blobs"
            build_first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(build_database),
                blob_repository=FilesystemBlobRepository(build_blobs),
            )
            try:
                build_developer = build_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.53",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "build-upgrade",
                    },
                    task_id="restart-build",
                    operation_id="restart-build-start",
                )
                build_gate = build_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": build_developer["run_id"],
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed",
                            "payload": {
                                "source_revision": "restart-build",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["build and verify"],
                            },
                        },
                    },
                    task_id="restart-build",
                    operation_id="restart-build-developer",
                )
            finally:
                build_first.close()
            build_second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(build_database),
                blob_repository=FilesystemBlobRepository(build_blobs),
            )
            try:
                build_final = build_second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": build_gate["run_id"],
                        "response": {
                            "status": "completed",
                            "summary": "artifact restored after restart",
                            "payload": {
                                "source_revision": "restart-build",
                                "artifact_path": "/tmp/product.hpm",
                                "artifact_sha256": "c" * 64,
                                "product_version": "2.0.0",
                            },
                        },
                    },
                    task_id="restart-build-resumed",
                    operation_id="restart-build-respond",
                )
            finally:
                build_second.close()
            self.assertEqual(build_final["state"], "completed")

    def test_live_patch_unknown_mutation_reconciles_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-failure.sqlite3"
            blobs = root / "live-failure-blobs"
            first = RuntimeMcpService(
                FailLivePatchSemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                gate = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.54",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="restart-live-failure",
                    operation_id="restart-live-failure-start",
                )
                blocked = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": gate["run_id"],
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "restart-live-failure",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_path": "/tmp/fix.lua",
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                    task_id="restart-live-failure",
                    operation_id="restart-live-failure-respond",
                )
            finally:
                first.close()
            self.assertEqual(blocked["state"], "mutation_outcome_unknown")

            second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "control",
                        "run_id": gate["run_id"],
                        "command": "reconcile",
                    },
                    task_id="restart-live-failure-reconcile",
                    operation_id="restart-live-failure-control",
                )
            finally:
                second.close()
        self.assertEqual(final["state"], "completed")

    def test_legacy_and_governance_operations_require_explicit_profiles(self) -> None:
        compatibility = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )
        operator = RuntimeMcpService(SemanticBackend(), interface_profile="operator")
        try:
            self.assertIn("debug_run", compatibility.interface_catalog.names())
            self.assertNotIn("evidence_read", compatibility.interface_catalog.names())
            self.assertNotIn("runtime_status", compatibility.interface_catalog.names())
            self.assertIn("evidence_read", operator.interface_catalog.names())
            self.assertNotIn("debug_run", operator.interface_catalog.names())
        finally:
            compatibility.close()
            operator.close()

    def test_agent_endpoint_rejects_raw_evidence_tool(self) -> None:
        endpoint = JsonRpcMcpEndpoint(self.service, session_task_id="agent-session")
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "evidence_read",
                    "arguments": {"case_id": "case-x", "evidence_id": "evidence-x"},
                },
            }
        )
        self.assertTrue(response["result"]["isError"])

    def test_agent_endpoint_renders_observation_values_in_bounded_text_content(self) -> None:
        endpoint = JsonRpcMcpEndpoint(self.service, session_task_id="observe-session")
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "observe",
                    "arguments": {
                        "target": "192.0.2.10",
                        "selectors": [
                            {"kind": "mdb", "queries": ["lsprop Object0"]}
                        ],
                    },
                },
            }
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn("lsprop Object0", text)
        self.assertIn("Value", text)
        self.assertLessEqual(len(text.encode("utf-8")), OBSERVATION_MAX_BYTES)

    def test_legacy_freshness_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "profile"):
            self.service.call_tool(
                "debug_collect",
                {"ip": "192.0.2.10", "profile": "freshness"},
                task_id="legacy-task",
                operation_id="legacy-1",
            )


if __name__ == "__main__":
    unittest.main()
