from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    AcceptancePlan,
    FilesystemBlobRepository,
    InMemoryBlobRepository,
    InMemoryRuntimeRepository,
    JsonRpcMcpEndpoint,
    OrchestratedMcpBackend,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
    aggregate_case_closeout,
)


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class PlanObservingBackend:
    def __init__(self, repository: InMemoryRuntimeRepository) -> None:
        self.repository = repository
        self.plan_seen_during_debug: dict[str, object] | None = None

    @staticmethod
    def open_task(task_id: str) -> FakeTask:
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

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        case_id = self.repository.case_for_task(task.task_id)
        case = self.repository.load(case_id) if case_id is not None else None
        self.plan_seen_during_debug = (
            dict(case["acceptance_plan"]) if case is not None else None
        )
        return {
            "ok": True,
            "summary": "root cause isolated",
            "target_epoch": 0,
        }


class RuntimeVerificationBackend(PlanObservingBackend):
    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {
            "ok": True,
            "summary": "runtime file replaced and verified",
            "target_epoch": 2,
            "journal": {
                "stage": "verified",
                "epoch_before": 1,
                "epoch_after": 2,
                "expected_checksum": "b" * 64,
                "observed_checksum": "b" * 64,
            },
        }

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {
            "ok": True,
            "summary": "business behavior is healthy",
            "profile": "standard",
            "target_epoch": 2,
            "business_acceptance": "passed",
        }


class StrictRuntimeVerificationBackend(RuntimeVerificationBackend):
    def __init__(
        self,
        repository: InMemoryRuntimeRepository,
        *,
        business_status: str = "passed",
    ) -> None:
        super().__init__(repository)
        self.business_status = business_status

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        metadata = {"mode": "644", "uid": 0, "gid": 0}
        return {
            "ok": True,
            "summary": "runtime file replaced and verified",
            "target_epoch": 2,
            "journal": {
                "stage": "verified",
                "epoch_before": 1,
                "epoch_after": 2,
                "expected_checksum": "b" * 64,
                "observed_checksum": "b" * 64,
                "root_mount_restored": True,
            },
            "mutation": {
                "local_sha256": "b" * 64,
                "remote_after_sha256": "b" * 64,
                "remote_after_metadata": metadata,
                "root_mount_restored": True,
            },
            "verification": {
                "remote_sha256": "b" * 64,
                "remote_metadata": metadata,
                "target_epoch": 2,
            },
        }

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        title = "Redfish health is green"
        return {
            "ok": self.business_status != "failed",
            "summary": "business acceptance evaluated",
            "profile": "standard",
            "target_epoch": 2,
            "business_acceptance": self.business_status,
            "acceptance_results": [
                {"title": title, "status": self.business_status}
            ],
        }


class RecordingTerminalBackend:
    def __init__(self, *, mutation_stage: str = "verified") -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.mutation_stage = mutation_stage
        self.debug_collect_calls = 0

    @staticmethod
    def open_task(task_id: str) -> FakeTask:
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

    def _record(self, name: str, arguments) -> None:
        self.calls.append((name, dict(arguments)))

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self._record("debug_run", arguments)
        return {
            "ok": True,
            "summary": "root cause isolated",
            "root_cause": "stale runtime state",
            "mechanism": "the old process retained the previous implementation",
            "affected_surface": "one bounded service",
            "target_epoch": 0,
        }

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self._record("live_patch_run", arguments)
        stage = self.mutation_stage
        return {
            "ok": stage == "verified",
            "summary": (
                "runtime file replaced and verified"
                if stage == "verified"
                else "live patch requires a new plan"
            ),
            "target_epoch": 8,
            "journal": {
                "stage": stage,
                "epoch_before": 7,
                "epoch_after": 8,
                "expected_checksum": "b" * 64,
                "observed_checksum": "b" * 64,
            },
        }

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self._record("upgrade_run", arguments)
        stage = self.mutation_stage
        return {
            "ok": stage == "verified",
            "summary": (
                "firmware upgraded and version verified"
                if stage == "verified"
                else "upgrade requires a new plan"
            ),
            "target_epoch": 8,
            "journal": {
                "stage": stage,
                "epoch_before": 7,
                "epoch_after": 8,
                "artifact_reference": arguments.get("artifact_path", ""),
            },
            "verification": {
                "installed_version": arguments.get("product_version", ""),
                "target_epoch": 8,
            },
        }

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.debug_collect_calls += 1
        self._record("debug_collect", arguments)
        return {
            "ok": True,
            "summary": "business behavior is healthy after delivery",
            "profile": "standard",
            "target_epoch": 8,
            "business_acceptance": "passed",
        }


class ProductionTopologyBackend:
    def __init__(self, *, mutation_stage: str = "replan_required") -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.mutation_stage = mutation_stage

    @staticmethod
    def open_task(task_id: str) -> FakeTask:
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

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        result = {
            "ok": self.mutation_stage == "verified",
            "summary": "mutation result",
            "journal": {
                "stage": self.mutation_stage,
                "action": "live_patch",
                "effects_started": False,
            },
        }
        if self.mutation_stage == "verified":
            result["epoch_after"] = 8
        return result

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {"ok": True, "summary": "diagnosed", "target_epoch": 0}

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_collect", dict(arguments)))
        return {
            "ok": True,
            "summary": "verified",
            "target_epoch": 8,
            "business_acceptance": "passed",
        }


class CaseCloseoutIntegrationTests(unittest.TestCase):
    @staticmethod
    def _rpc_call(
        endpoint: JsonRpcMcpEndpoint,
        message_id: int,
        name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response is not None
        result = response["result"]
        if result["isError"]:
            raise AssertionError(result["content"][0]["text"])
        return result

    def test_log_analyzer_diagnosis_plan_requires_bundle_evidence(self) -> None:
        plan = AcceptancePlan.freeze(
            {
                "intent": "diagnosis-only",
                "entry_domain": "log_analyzer",
                "final_purpose": "collect the requested diagnostic bundle",
            },
            frozen_at=1.0,
        )

        self.assertEqual(
            [requirement.stage for requirement in plan.requirements],
            ["bundle"],
        )

    def test_closeout_uses_only_the_latest_operation_evidence(self) -> None:
        plan = AcceptancePlan.freeze(
            {
                "intent": "diagnosis-only",
                "final_purpose": "diagnose",
            },
            frozen_at=1.0,
        )
        projection = {
            "case_id": "latest-evidence-case",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 5,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["old", "new"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {"evidence_id": "old", "blob_id": "old"},
                {"evidence_id": "new", "blob_id": "new"},
            ],
        }
        evidence = {
            "old": {"ok": False, "root_cause": "old failed conclusion"},
            "new": {"ok": True, "root_cause": "new reconciled conclusion"},
        }

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: evidence[str(reference["evidence_id"])],
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(diagnosis.status, "completed")
        self.assertEqual(
            diagnosis.facts["root_cause"],
            "new reconciled conclusion",
        )

        corrupt_latest = aggregate_case_closeout(
            projection,
            lambda reference: (
                evidence["old"]
                if reference["evidence_id"] == "old"
                else (_ for _ in ()).throw(OSError("corrupt latest evidence"))
            ),
        )
        degraded = next(
            receipt
            for receipt in corrupt_latest.receipts
            if receipt.stage == "diagnosis"
        )
        self.assertEqual(degraded.status, "partial")
        self.assertNotIn("old failed conclusion", str(degraded.facts))

    def test_multi_target_stage_uses_the_worst_target_result(self) -> None:
        plan = AcceptancePlan.freeze(
            {
                "intent": "diagnosis-only",
                "final_purpose": "compare targets",
            },
            frozen_at=1.0,
        )
        for operations in (
            [
                ("target-a", "failed", "a-evidence"),
                ("target-b", "completed", "b-evidence"),
            ],
            [
                ("target-b", "completed", "b-evidence"),
                ("target-a", "failed", "a-evidence"),
            ],
        ):
            with self.subTest(order=[item[0] for item in operations]):
                projection = {
                    "case_id": "multi-target-case",
                    "acceptance_plan": plan.to_public_dict(),
                    "targets": [
                        {"target_id": "target-a", "address": "192.0.2.31"},
                        {"target_id": "target-b", "address": "192.0.2.32"},
                    ],
                    "operations": [
                        {
                            "operation_id": f"diagnosis-{target_id}",
                            "operation": "debug_run",
                            "status": status,
                            "terminal_revision": index + 1,
                            "inputs": {"target_id": target_id},
                            "evidence_ids": [evidence_id],
                        }
                        for index, (target_id, status, evidence_id) in enumerate(
                            operations
                        )
                    ],
                    "phase_records": [],
                    "evidence_refs": [
                        {"evidence_id": "a-evidence", "blob_id": "a"},
                        {"evidence_id": "b-evidence", "blob_id": "b"},
                    ],
                }
                evidence = {
                    "a-evidence": {"ok": False, "summary": "target A failed"},
                    "b-evidence": {"ok": True, "summary": "target B passed"},
                }
                closeout = aggregate_case_closeout(
                    projection,
                    lambda reference: evidence[str(reference["evidence_id"])],
                )

                self.assertEqual(closeout.closure_status, "failed")
                self.assertEqual(closeout.checks[0].status, "failed")
                self.assertEqual(len(closeout.receipts), 2)

    def test_context_workflow_step_bypasses_legacy_orchestration_and_keeps_authority(self) -> None:
        repository = InMemoryRuntimeRepository()
        domain = ProductionTopologyBackend(mutation_stage="verified")
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {
                    "live_patch_run": domain,
                    "debug_run": domain,
                    "debug_collect": domain,
                }
            ),
            context_repository=repository,
        )
        try:
            result = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.33",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "ssh_password_env": "PASSWORD_A",
                    "force_path": True,
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/srv/diagnostic/unit.lua",
                    "restart_scope": "none",
                    "workflow": {
                        "live_patch": {
                            "intent": "rollback",
                            "delivery_strategy": "source-only",
                            "authorized_exceptions": {"force_path": True},
                            "ip": "192.0.2.99",
                            "ssh_password_env": "PASSWORD_B",
                            "_context_workflow_step": True,
                        },
                    },
                    "deadline": 10,
                },
                task_id="production-topology-task",
                operation_id="production-topology-advance",
            )
        finally:
            service.close()

        self.assertTrue(result["completed"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [name for name, _ in domain.calls],
            ["live_patch_run", "debug_collect"],
        )
        mutation_arguments = domain.calls[0][1]
        self.assertEqual(mutation_arguments["_task_intent"], "live-patch")
        self.assertEqual(
            mutation_arguments["_task_delivery_strategy"],
            "live-patch",
        )
        self.assertFalse(
            mutation_arguments["_task_authorized_exceptions"]["force_path"]
        )
        self.assertFalse(
            mutation_arguments["_task_authorization_policy"][
                "authorized_exceptions"
            ]["force_path"]
        )
        self.assertEqual(mutation_arguments["ip"], "192.0.2.33")
        self.assertEqual(mutation_arguments["ssh_password_env"], "PASSWORD_A")
        self.assertTrue(
            all(
                "_context_workflow_step" not in arguments
                for _, arguments in domain.calls
            )
        )
        self.assertTrue(
            any(
                "未获策略授权" in reason
                for reason in result["closeout"]["reasons"]
            )
        )

    def test_context_replan_required_stops_before_debug_verification(self) -> None:
        domain = ProductionTopologyBackend(mutation_stage="replan_required")
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {
                    "live_patch_run": domain,
                    "debug_run": domain,
                    "debug_collect": domain,
                }
            )
        )
        try:
            result = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.35",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "none",
                    "deadline": 10,
                },
                task_id="context-replan-task",
                operation_id="context-replan-operation",
            )
        finally:
            service.close()

        self.assertFalse(result["completed"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual([name for name, _ in domain.calls], ["live_patch_run"])

    def test_external_context_step_marker_cannot_bypass_orchestration(self) -> None:
        domain = ProductionTopologyBackend(mutation_stage="verified")
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {
                    "live_patch_run": domain,
                    "debug_run": domain,
                    "debug_collect": domain,
                }
            )
        )
        try:
            result = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.34",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "none",
                    "_context_workflow_step": True,
                    "workflow": {
                        "verification": {
                            "_context_workflow_step": True,
                        }
                    },
                    "deadline": 10,
                },
                task_id="external-marker-task",
                operation_id="external-marker-operation",
            )
        finally:
            service.close()

        self.assertEqual(
            result["schema"],
            "openubmc.target-runtime.v1/task-orchestration",
        )
        self.assertEqual(
            [name for name, _ in domain.calls],
            ["live_patch_run", "debug_collect"],
        )
        self.assertTrue(
            all(
                "_context_workflow_step" not in arguments
                for _, arguments in domain.calls
            )
        )

    def test_acceptance_plan_is_frozen_before_the_first_domain_step(self) -> None:
        repository = InMemoryRuntimeRepository()
        backend = PlanObservingBackend(repository)
        service = RuntimeMcpService(backend, context_repository=repository)
        try:
            first = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.40",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "repair the reported behavior",
                    "max_steps": 1,
                    "deadline": 10,
                },
                task_id="closeout-plan-task",
                operation_id="advance-one",
            )
            case_id = first.envelope["case_id"]
            projected = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="closeout-plan-task",
                operation_id="read-plan",
            )
        finally:
            service.close()

        plan = projected["acceptance_plan"]
        self.assertEqual(backend.plan_seen_during_debug, plan)
        self.assertTrue(str(plan["plan_id"]).startswith("acceptance-"))
        self.assertEqual(plan["intent"], "diagnose-and-fix")
        self.assertEqual(plan["delivery_strategy"], "source-only")
        self.assertEqual(
            [item["stage"] for item in plan["requirements"]],
            ["diagnosis", "development"],
        )
        self.assertTrue(all(item["criticality"] == "required" for item in plan["requirements"]))

    def test_terminal_advance_derives_and_persists_closeout_from_case_facts(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            PlanObservingBackend(repository),
            context_repository=repository,
            interface_profile="compatibility",
        )
        try:
            first = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.41",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "repair the reported behavior",
                    "workflow": {
                        "closeout": {
                            "stage_receipts": [
                                {
                                    "stage": "upgrade",
                                    "status": "completed",
                                    "summary": "caller supplied and untrusted",
                                }
                            ]
                        }
                    },
                    "deadline": 10,
                },
                task_id="closeout-terminal-task",
                operation_id="advance-start",
            )
            self.assertEqual(first["status"], "waiting_phase_record")
            case_id = first.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="closeout-terminal-task",
                operation_id="read-before-development",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "development-result",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "implemented the bounded source fix",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["targeted unit regression"],
                },
                task_id="closeout-terminal-task",
                operation_id="record-development",
            )
            final = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "advance-finish",
                    "include_closeout_bundle": True,
                    "deadline": 10,
                },
                task_id="closeout-terminal-task",
                operation_id="advance-finish",
            )
            projected = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="closeout-terminal-task",
                operation_id="read-after-closeout",
            )
        finally:
            service.close()

        self.assertTrue(final["completed"])
        closeout = final["closeout"]
        self.assertEqual(closeout["closure_status"], "completed_in_scope")
        self.assertEqual(closeout["business_acceptance"], "not_applicable")
        self.assertEqual(
            [receipt["stage"] for receipt in closeout["receipts"]],
            ["diagnosis", "development"],
        )
        self.assertNotIn("caller supplied and untrusted", str(closeout))
        self.assertEqual(projected["closeout"], closeout)
        self.assertEqual(projected["closeout_markdown"], final["closeout_markdown"])
        self.assertEqual(projected["closeout_bundle"], final["closeout_bundle"])
        self.assertEqual(
            [item["name"] for item in final["closeout_bundle"]["documents"]],
            ["closeout.json", "closeout.md"],
        )
        self.assertIn("# 问题闭环报告", final["closeout_markdown"])
        self.assertIn("root cause isolated", final["closeout_markdown"])
        self.assertIn("implemented the bounded source fix", final["closeout_markdown"])

    def test_idempotent_replay_refreshes_envelope_from_current_closeout(self) -> None:
        repository = InMemoryRuntimeRepository()
        backend = RecordingTerminalBackend()
        service = RuntimeMcpService(backend, context_repository=repository)
        debug_arguments = {
            "ip": "192.0.2.55",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
            "final_purpose": "repair the reported behavior",
            "idempotency_key": "refresh-debug",
            "deadline": 10,
        }
        try:
            first = service.call_tool(
                "debug_run",
                debug_arguments,
                task_id="refresh-envelope-task",
                operation_id="refresh-debug-first",
            )
            self.assertNotIn("closeout", first)
            case_id = first.envelope["case_id"]
            case = repository.load(case_id)
            assert case is not None
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "refresh-development",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "implemented the source fix",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["targeted regression"],
                },
                task_id="refresh-envelope-task",
                operation_id="refresh-development",
            )
            final = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "refresh-finish",
                    "deadline": 10,
                },
                task_id="refresh-envelope-task",
                operation_id="refresh-finish",
            )
            current = repository.load(case_id)
            assert current is not None
            replay = service.call_tool(
                "debug_run",
                {
                    **debug_arguments,
                    "case_id": case_id,
                    "expected_revision": 1,
                },
                task_id="refresh-envelope-task",
                operation_id="refresh-debug-replay",
            )
        finally:
            service.close()

        self.assertTrue(final["completed"])
        self.assertEqual(replay.envelope["revision"], current["revision"])
        self.assertEqual(
            replay.envelope["closeout_summary"]["fingerprint"],
            current["closeout"]["fingerprint"],
        )
        self.assertEqual(
            replay.envelope["document_refs"],
            current["closeout_bundle"]["documents"],
        )
        self.assertEqual(replay["closeout"], current["closeout"])
        self.assertEqual([name for name, _ in backend.calls], ["debug_run"])

    def test_inline_password_is_redacted_from_case_evidence_and_closeout(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            PlanObservingBackend(repository),
            context_repository=repository,
        )
        try:
            diagnosis = service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.56",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "repair the reported behavior",
                    "deadline": 10,
                },
                task_id="redaction-closeout-task",
                operation_id="redaction-diagnosis",
            )
            case_id = diagnosis.envelope["case_id"]
            projection = repository.load(case_id)
            assert projection is not None
            build = service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": projection["revision"],
                    "idempotency_key": "redaction-build",
                    "phase_type": "build.artifact",
                    "producer_identity": "openubmc-build",
                    "status": "completed",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "built the product image",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "1.2.3",
                    "build_commands": [
                        "OPENUBMC_SSH_PASSWORD=hunter2 make product",
                        "SERVICE_PRIVATE_KEY=private-text-secret package",
                        "SSH_PASSPHRASE=phrase-text-secret sign",
                    ],
                },
                task_id="redaction-closeout-task",
                operation_id="redaction-build",
            )
            projection = repository.load(case_id)
            assert projection is not None
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": projection["revision"],
                    "idempotency_key": "redaction-development",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "implemented the source fix",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["targeted regression"],
                    "design": {
                        "apiKey": "camel-secret",
                        "apikey": "compact-secret",
                        "api_key": "snake-secret",
                        "api-key": "dash-secret",
                        "privateKey": "private-map-secret",
                        "passphrase": "phrase-map-secret",
                    },
                },
                task_id="redaction-closeout-task",
                operation_id="redaction-development",
            )
            final = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "redaction-finish",
                    "deadline": 10,
                },
                task_id="redaction-closeout-task",
                operation_id="redaction-finish",
            )
            projected = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="redaction-closeout-task",
                operation_id="redaction-case-read",
            )
            evidence_ref = build.envelope["evidence_refs"][0]
            evidence = service.call_tool(
                "evidence_read",
                {
                    "case_id": case_id,
                    "evidence_id": evidence_ref["evidence_id"],
                },
                task_id="redaction-closeout-task",
                operation_id="redaction-evidence-read",
            )
        finally:
            service.close()

        durable_output = "\n".join(
            (
                json.dumps(projected, ensure_ascii=False),
                evidence["body"],
                json.dumps(final["closeout"], ensure_ascii=False),
                final["closeout_markdown"],
            )
        )
        self.assertNotIn("hunter2", durable_output)
        for secret in (
            "camel-secret",
            "compact-secret",
            "snake-secret",
            "dash-secret",
            "private-text-secret",
            "phrase-text-secret",
            "private-map-secret",
            "phrase-map-secret",
        ):
            self.assertNotIn(secret, durable_output)
        self.assertNotIn("apiKey", durable_output)
        self.assertIn("OPENUBMC_SSH_PASSWORD=<redacted>", durable_output)
        self.assertIn("SERVICE_PRIVATE_KEY=<redacted>", durable_output)
        self.assertIn("SSH_PASSPHRASE=<redacted>", durable_output)

    def test_runtime_closeout_requires_backend_business_and_freshness_evidence(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            RuntimeVerificationBackend(repository),
            context_repository=repository,
        )
        try:
            final = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.42",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "final_purpose": "restore the expected runtime behavior",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "skynet",
                    "deadline": 10,
                },
                task_id="closeout-runtime-task",
                operation_id="advance-runtime",
            )
        finally:
            service.close()

        closeout = final["closeout"]
        self.assertEqual(closeout["closure_status"], "verified")
        self.assertEqual(closeout["business_acceptance"], "passed")
        self.assertEqual(closeout["identity_status"], "matched")
        self.assertEqual(closeout["freshness_status"], "fresh")
        self.assertEqual(
            [receipt["stage"] for receipt in closeout["receipts"]],
            ["live_patch", "verification"],
        )

    def test_live_patch_acceptance_items_are_frozen_and_individually_evaluated(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            StrictRuntimeVerificationBackend(repository),
            context_repository=repository,
        )
        try:
            final = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.43",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "verification_checks": ["Redfish health is green"],
                },
                task_id="strict-live-patch-acceptance",
                operation_id="strict-live-patch-acceptance",
            )
        finally:
            service.close()

        plan = final["closeout"]["acceptance_plan"]
        items = {
            item["requirement_id"]: item for item in plan["requirements"]
        }
        business_id = next(
            requirement_id
            for requirement_id in items
            if requirement_id.startswith("acceptance.business.")
        )
        self.assertEqual(
            items["acceptance.live-patch.integrity"]["verification_method"],
            "mutation-integrity",
        )
        self.assertTrue(items[business_id]["required"])
        self.assertEqual(items[business_id]["target"], "candidate")
        checks = {
            item["requirement_id"]: item
            for item in final["closeout"]["checks"]
        }
        self.assertEqual(checks["acceptance.live-patch.integrity"]["status"], "passed")
        self.assertEqual(checks["acceptance.live-patch.metadata"]["status"], "passed")
        self.assertEqual(checks[business_id]["status"], "passed")
        self.assertEqual(final["closeout"]["closure_status"], "verified")

    def test_required_business_acceptance_failure_prevents_success(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            StrictRuntimeVerificationBackend(
                repository,
                business_status="failed",
            ),
            context_repository=repository,
        )
        try:
            final = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.44",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "verification_checks": ["Redfish health is green"],
                },
                task_id="failed-live-patch-acceptance",
                operation_id="failed-live-patch-acceptance",
            )
        finally:
            service.close()

        self.assertEqual(final["closeout"]["closure_status"], "failed")
        business_check = next(
            item
            for item in final["closeout"]["checks"]
            if item["requirement_id"].startswith("acceptance.business.")
        )
        self.assertEqual(business_check["status"], "failed")

    def test_build_upgrade_closeout_contains_an_immutable_delivery_record(self) -> None:
        backend = RecordingTerminalBackend()
        service = RuntimeMcpService(backend)
        try:
            waiting = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.48",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "final_purpose": "repair, build, deploy, and verify",
                },
                task_id="delivery-record-task",
                operation_id="delivery-record-start",
            )
            case_id = waiting.envelope["case_id"]
            developer = service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": waiting.envelope["revision"],
                    "idempotency_key": "delivery-record-developer",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "source_revision": "source-abc123",
                    "summary": "source change completed",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["focused test"],
                },
                task_id="delivery-record-task",
                operation_id="delivery-record-developer",
            )
            build = service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": developer.envelope["revision"],
                    "idempotency_key": "delivery-record-build",
                    "phase_type": "build.artifact",
                    "producer_identity": "openubmc-build",
                    "status": "completed",
                    "source_revision": "source-abc123",
                    "summary": "build completed",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "c" * 64,
                    "product_version": "2.0.0",
                },
                task_id="delivery-record-task",
                operation_id="delivery-record-build",
            )
            final = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "expected_revision": build.envelope["revision"],
                },
                task_id="delivery-record-task",
                operation_id="delivery-record-finish",
            )
        finally:
            service.close()

        records = final["closeout"]["delivery_records"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["record_id"].startswith("delivery-"))
        self.assertEqual(record["artifact"]["source_revision"], "source-abc123")
        self.assertEqual(record["artifact"]["sha256"], "c" * 64)
        self.assertEqual(record["deployment"]["requested_version"], "2.0.0")
        self.assertEqual(record["deployment"]["active_version"], "2.0.0")
        self.assertEqual(record["deployment"]["target_epoch"], 8)
        self.assertEqual(record["outcome"]["deployment_integrity"], "passed")
        self.assertEqual(record["outcome"]["active_identity"], "passed")

    def test_public_json_rpc_exposes_terminal_closeout_case_and_evidence(self) -> None:
        repository = InMemoryRuntimeRepository()
        blobs = InMemoryBlobRepository()
        service = RuntimeMcpService(
            PlanObservingBackend(repository),
            context_repository=repository,
            blob_repository=blobs,
            interface_profile="compatibility",
        )
        operator_service = RuntimeMcpService(
            PlanObservingBackend(repository),
            context_repository=repository,
            blob_repository=blobs,
            interface_profile="operator",
        )
        endpoint = JsonRpcMcpEndpoint(
            service,
            session_task_id="jsonrpc-closeout-task",
        )
        operator_endpoint = JsonRpcMcpEndpoint(
            operator_service,
            session_task_id="jsonrpc-closeout-operator",
        )
        try:
            first = self._rpc_call(
                endpoint,
                1,
                "workflow.advance",
                {
                    "ip": "192.0.2.43",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "repair the reported behavior",
                    "deadline": 10,
                },
            )["structuredContent"]
            case_id = first["agent_envelope"]["case_id"]
            before = self._rpc_call(
                operator_endpoint,
                2,
                "case_read",
                {"case_id": case_id},
            )["structuredContent"]
            self._rpc_call(
                endpoint,
                3,
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": before["revision"],
                    "idempotency_key": "jsonrpc-development",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "implemented the bounded source fix",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["targeted unit regression"],
                },
            )
            terminal_result = self._rpc_call(
                endpoint,
                4,
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "jsonrpc-finish",
                    "include_closeout_bundle": True,
                    "deadline": 10,
                },
            )
            terminal = terminal_result["structuredContent"]
            projected = self._rpc_call(
                operator_endpoint,
                5,
                "case_read",
                {"case_id": case_id},
            )["structuredContent"]
            reference = projected["evidence_refs"][0]
            evidence = self._rpc_call(
                operator_endpoint,
                6,
                "evidence_read",
                {
                    "case_id": case_id,
                    "evidence_id": reference["evidence_id"],
                    "limit": 65536,
                },
            )["structuredContent"]
            persisted = repository.load(case_id)
        finally:
            service.close()
            operator_service.close()

        self.assertTrue(terminal["completed"])
        self.assertIn("closeout", terminal)
        self.assertIn("closeout_markdown", terminal)
        self.assertIn("closeout_bundle", terminal)
        self.assertEqual(
            terminal["agent_envelope"]["closeout_summary"]["closure_status"],
            "completed_in_scope",
        )
        self.assertEqual(
            terminal["agent_envelope"]["document_refs"],
            terminal["closeout_bundle"]["documents"],
        )
        self.assertEqual(
            terminal_result["content"][0]["text"],
            terminal["closeout_markdown"].strip(),
        )
        assert persisted is not None
        for key, value in persisted.items():
            self.assertIn(key, projected)
            if key != "last_access":
                self.assertEqual(projected[key], value)
        self.assertIn("capsule", projected)
        self.assertIn("agent_envelope", projected)
        self.assertEqual(projected["closeout"], terminal["closeout"])
        self.assertEqual(projected["closeout_markdown"], terminal["closeout_markdown"])
        self.assertEqual(projected["closeout_bundle"], terminal["closeout_bundle"])
        self.assertGreater(evidence["returned_bytes"], 0)
        self.assertIsInstance(evidence["body"], str)
        self.assertIsInstance(json.loads(evidence["body"]), dict)
        self.assertEqual(
            evidence["evidence"]["evidence_id"],
            reference["evidence_id"],
        )

    def test_missing_or_corrupt_stage_evidence_prevents_a_passing_closeout(self) -> None:
        for failure_mode in ("missing", "corrupt"):
            with self.subTest(failure_mode=failure_mode):
                repository = InMemoryRuntimeRepository()
                blobs = InMemoryBlobRepository()
                service = RuntimeMcpService(
                    PlanObservingBackend(repository),
                    context_repository=repository,
                    blob_repository=blobs,
                )
                try:
                    first = service.call_tool(
                        "workflow.advance",
                        {
                            "ip": "192.0.2.44",
                            "intent": "diagnose-and-fix",
                            "delivery_strategy": "source-only",
                            "final_purpose": "repair the reported behavior",
                            "max_steps": 1,
                            "deadline": 10,
                        },
                        task_id=f"unreadable-{failure_mode}-task",
                        operation_id="advance-one",
                    )
                    case_id = first.envelope["case_id"]
                    case = repository.load(case_id)
                    assert case is not None
                    diagnosis_operation = next(
                        item
                        for item in case["operations"]
                        if item["operation"] == "debug_run"
                    )
                    evidence_id = diagnosis_operation["evidence_ids"][0]
                    reference = next(
                        item
                        for item in case["evidence_refs"]
                        if item["evidence_id"] == evidence_id
                    )
                    blob_id = reference["blob_id"]
                    if failure_mode == "missing":
                        self.assertTrue(blobs.delete(blob_id))
                    else:
                        blobs._blobs[blob_id] = b"corrupt evidence body"
                    service.call_tool(
                        "phase_record",
                        {
                            "case_id": case_id,
                            "expected_revision": case["revision"],
                            "idempotency_key": f"development-{failure_mode}",
                            "phase_type": "developer.change",
                            "producer_identity": "openubmc-developer",
                            "status": "completed",
                            "source_revision": "abc123",
                            "summary": "implemented the bounded source fix",
                            "authored_files": ["src/unit.lua"],
                            "verification_plan": ["targeted unit regression"],
                        },
                        task_id=f"unreadable-{failure_mode}-task",
                        operation_id="record-development",
                    )
                    final = service.call_tool(
                        "workflow.advance",
                        {
                            "case_id": case_id,
                            "idempotency_key": f"finish-{failure_mode}",
                            "deadline": 10,
                        },
                        task_id=f"unreadable-{failure_mode}-task",
                        operation_id="advance-finish",
                    )
                finally:
                    service.close()

                closeout = final["closeout"]
                diagnosis = next(
                    item
                    for item in closeout["receipts"]
                    if item["stage"] == "diagnosis"
                )
                diagnosis_check = next(
                    item
                    for item in closeout["checks"]
                    if item["requirement_id"] == "stage.diagnosis"
                )
                self.assertEqual(closeout["closure_status"], "partial")
                self.assertEqual(diagnosis["status"], "partial")
                self.assertEqual(diagnosis_check["status"], "unverified")
                self.assertNotEqual(diagnosis_check["status"], "passed")

    def test_replan_required_stops_before_debug_collect_and_fails_closeout(self) -> None:
        repository = InMemoryRuntimeRepository()
        backend = RecordingTerminalBackend(mutation_stage="replan_required")
        service = RuntimeMcpService(backend, context_repository=repository)
        try:
            result = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.45",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "final_purpose": "restore the expected runtime behavior",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "skynet",
                    "deadline": 10,
                },
                task_id="replan-required-task",
                operation_id="advance-replan",
            )
            case_id = result.envelope["case_id"]
            projected = repository.load(case_id)
        finally:
            service.close()

        self.assertFalse(result["completed"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["closeout"]["closure_status"], "failed")
        self.assertEqual(backend.debug_collect_calls, 0)
        self.assertEqual([name for name, _ in backend.calls], ["live_patch_run"])
        assert projected is not None
        operations = {
            item["operation"]: item["status"] for item in projected["operations"]
        }
        self.assertEqual(operations["live_patch_run"], "failed")
        self.assertNotIn("debug_collect", operations)
        self.assertEqual(projected["closeout"]["closure_status"], "failed")

    def test_recovery_blocked_produces_a_blocked_closeout(self) -> None:
        backend = RecordingTerminalBackend(mutation_stage="recovery_blocked")
        service = RuntimeMcpService(backend)
        try:
            result = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.54",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "none",
                    "deadline": 10,
                },
                task_id="recovery-blocked-task",
                operation_id="recovery-blocked-operation",
            )
        finally:
            service.close()

        self.assertEqual(result.envelope["status"], "blocked")
        self.assertEqual(result["closeout"]["closure_status"], "blocked")

    def test_direct_terminal_domain_calls_persist_closeout(self) -> None:
        backend = RecordingTerminalBackend()
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(backend, context_repository=repository)
        cases = (
            (
                "debug_run",
                {
                    "ip": "192.0.2.46",
                    "intent": "diagnosis-only",
                    "final_purpose": "diagnose the reported behavior",
                    "deadline": 10,
                },
                "direct-debug-task",
            ),
            (
                "live_patch_run",
                {
                    "ip": "192.0.2.47",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "final_purpose": "restore runtime behavior",
                    "local_path": "/tmp/unit.lua",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "skynet",
                    "deadline": 10,
                },
                "direct-live-patch-task",
            ),
            (
                "upgrade_run",
                {
                    "ip": "192.0.2.48",
                    "intent": "upgrade-and-verify",
                    "delivery_strategy": "build-upgrade",
                    "final_purpose": "install the fixed firmware",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": "c" * 64,
                    "product_version": "1.2.3",
                    "deadline": 10,
                },
                "direct-upgrade-task",
            ),
        )
        try:
            for index, (tool_name, arguments, task_id) in enumerate(cases, start=1):
                with self.subTest(tool=tool_name):
                    result = service.call_tool(
                        tool_name,
                        arguments,
                        task_id=task_id,
                        operation_id=f"direct-{index}",
                    )
                    case_id = result.envelope["case_id"]
                    projected = repository.load(case_id)
                    self.assertIn("closeout", result)
                    self.assertIn("closeout_markdown", result)
                    self.assertIn("closeout_bundle", result)
                    assert projected is not None
                    self.assertEqual(projected["status"], "terminal")
                    self.assertEqual(projected["closeout"], result["closeout"])
                    self.assertEqual(
                        projected["closeout_markdown"],
                        result["closeout_markdown"],
                    )
                    self.assertEqual(
                        projected["closeout_bundle"],
                        result["closeout_bundle"],
                    )
        finally:
            service.close()

    def test_string_false_closeout_bundle_is_rejected_before_domain_execution(self) -> None:
        backend = RecordingTerminalBackend()
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(backend, context_repository=repository)
        try:
            with self.assertRaises(TypeError):
                service.call_tool(
                    "debug_run",
                    {
                        "ip": "192.0.2.49",
                        "intent": "diagnosis-only",
                        "include_closeout_bundle": "false",
                        "deadline": 10,
                    },
                    task_id="invalid-closeout-bundle-task",
                    operation_id="invalid-closeout-bundle",
                )
            case_id = repository.case_for_task("invalid-closeout-bundle-task")
        finally:
            service.close()

        self.assertEqual(backend.calls, [])
        assert case_id is not None
        self.assertIsNone(repository.load(case_id))

    def test_developer_and_build_details_are_rendered_in_closeout(self) -> None:
        repository = InMemoryRuntimeRepository()
        backend = RecordingTerminalBackend()
        service = RuntimeMcpService(backend, context_repository=repository)
        try:
            first = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.50",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "final_purpose": "repair and verify the product",
                    "deadline": 10,
                },
                task_id="rich-closeout-task",
                operation_id="advance-one",
            )
            case_id = first.envelope["case_id"]
            case = repository.load(case_id)
            assert case is not None
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "rich-development",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "source_revision": "deadbeef",
                    "summary": "replaced the stale state transition",
                    "authored_files": ["src/state_machine.lua"],
                    "verification_plan": ["run state-machine regression"],
                    "design": {
                        "what_changed": "centralized the transition guard",
                        "rationale": "prevent stale state reuse",
                        "invariants": ["preserve the public MDB contract"],
                        "tradeoffs": ["one extra local state check"],
                        "rollback": "restore the previous source revision",
                    },
                    "validation_results": [
                        {"suite": "state-machine", "result": "18 passed"}
                    ],
                    "source_delivery": "pull_request",
                },
                task_id="rich-closeout-task",
                operation_id="record-development",
            )
            waiting_build = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "advance-two",
                    "deadline": 10,
                },
                task_id="rich-closeout-task",
                operation_id="advance-two",
            )
            self.assertEqual(waiting_build["required_phase_type"], "build.artifact")
            case = repository.load(case_id)
            assert case is not None
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "rich-build",
                    "phase_type": "build.artifact",
                    "producer_identity": "openubmc-build",
                    "status": "completed",
                    "source_revision": "deadbeef",
                    "summary": "built the product HPM",
                    "artifact_path": "/tmp/openubmc-1.2.3.hpm",
                    "artifact_sha256": "c" * 64,
                    "product_version": "1.2.3",
                    "component_versions": [
                        {
                            "component": "compute_mgmt",
                            "version": "2.4.0",
                            "conan_ref": "compute_mgmt/2.4.0@openubmc/stable",
                        }
                    ],
                    "build_commands": [
                        "conan install . --build=missing",
                        "python build.py --product demo",
                    ],
                    "build_logs": ["/tmp/logs/component.log", "/tmp/logs/product.log"],
                },
                task_id="rich-closeout-task",
                operation_id="record-build",
            )
            final = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "advance-final",
                    "deadline": 10,
                },
                task_id="rich-closeout-task",
                operation_id="advance-final",
            )
        finally:
            service.close()

        self.assertEqual(final["closeout"]["closure_status"], "verified")
        self.assertEqual(final["closeout"]["source_delivery"], "pull_request")
        receipts = {
            item["stage"]: item for item in final["closeout"]["receipts"]
        }
        self.assertEqual(
            receipts["development"]["facts"]["design"]["rationale"],
            "prevent stale state reuse",
        )
        self.assertEqual(
            receipts["development"]["facts"]["validation_results"],
            [{"suite": "state-machine", "result": "18 passed"}],
        )
        self.assertEqual(
            receipts["build"]["facts"]["component_versions"][0]["conan_ref"],
            "compute_mgmt/2.4.0@openubmc/stable",
        )
        self.assertEqual(
            receipts["build"]["facts"]["build_commands"][1],
            "python build.py --product demo",
        )
        markdown = final["closeout_markdown"]
        for expected in (
            "centralized the transition guard",
            "prevent stale state reuse",
            "18 passed",
            "compute_mgmt/2.4.0@openubmc/stable",
            "python build.py --product demo",
            "/tmp/logs/product.log",
            "已创建 PR",
        ):
            self.assertIn(expected, markdown)

    def test_sqlite_and_filesystem_restart_restore_closeout(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository_path = root / "runtime.sqlite3"
            blob_root = root / "blobs"
            first_backend = RecordingTerminalBackend()
            first_service = RuntimeMcpService(
                first_backend,
                context_repository=SQLiteRuntimeRepository(repository_path),
                blob_repository=FilesystemBlobRepository(blob_root),
            )
            arguments = {
                "ip": "192.0.2.51",
                "intent": "diagnosis-only",
                "final_purpose": "diagnose the reported behavior",
                "idempotency_key": "restart-debug",
                "deadline": 10,
            }
            first = first_service.call_tool(
                "debug_run",
                arguments,
                task_id="restart-closeout-task",
                operation_id="first-debug",
            )
            case_id = first.envelope["case_id"]
            expected_closeout = first["closeout"]
            expected_markdown = first["closeout_markdown"]
            expected_bundle = first["closeout_bundle"]
            first_service.close()

            second_backend = RecordingTerminalBackend()
            second_service = RuntimeMcpService(
                second_backend,
                context_repository=SQLiteRuntimeRepository(repository_path),
                blob_repository=FilesystemBlobRepository(blob_root),
            )
            try:
                recovered = second_service.call_tool(
                    "case_read",
                    {"case_id": case_id},
                    task_id="restart-closeout-reader",
                    operation_id="read-closeout",
                )
                replay = second_service.call_tool(
                    "debug_run",
                    {**arguments, "case_id": case_id},
                    task_id="restart-closeout-reader",
                    operation_id="second-debug",
                )
            finally:
                second_service.close()

        self.assertEqual(recovered["closeout"], expected_closeout)
        self.assertEqual(recovered["closeout_markdown"], expected_markdown)
        self.assertEqual(recovered["closeout_bundle"], expected_bundle)
        self.assertEqual(replay["closeout"], expected_closeout)
        self.assertEqual(replay["closeout_markdown"], expected_markdown)
        self.assertEqual(replay["closeout_bundle"], expected_bundle)
        self.assertEqual(second_backend.calls, [])


if __name__ == "__main__":
    unittest.main()
