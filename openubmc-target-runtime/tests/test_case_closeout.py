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


from test_agent_gateway import accepted_diagnosis_payload


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
            "root_cause": "a bounded runtime defect was isolated",
            "observed_at": "2026-08-19T00:00:00Z",
            "freshness": {"status": "fresh"},
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
            "observed_at": "2026-08-19T00:00:00Z",
            "freshness": {"status": "fresh"},
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
            "old": {
                "ok": False,
                "root_cause": "old failed conclusion",
                "observed_at": "2026-08-19T00:00:00Z",
                "freshness": {"status": "fresh"},
            },
            "new": {
                "ok": True,
                "root_cause": "new reconciled conclusion",
                "observed_at": "2026-08-19T00:01:00Z",
                "freshness": {"status": "fresh"},
            },
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

    def test_closeout_rejects_evidence_bound_to_another_operation(self) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        base_projection = {
            "case_id": "operation-bound-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
        }

        for foreign_binding in (
            {"operation": "debug_collect"},
            {"operation_id": "diagnosis-two"},
            {"target_id": "target-2"},
        ):
            with self.subTest(foreign_binding=foreign_binding):
                projection = {
                    **base_projection,
                    "evidence_refs": [
                        {
                            "evidence_id": "diagnosis-evidence",
                            "blob_id": "diagnosis",
                            "operation": "debug_run",
                            "operation_id": "diagnosis-one",
                            "target_id": "target-1",
                            **foreign_binding,
                        }
                    ],
                }
                reads: list[str] = []
                closeout = aggregate_case_closeout(
                    projection,
                    lambda reference: reads.append(str(reference["evidence_id"]))
                    or {"ok": True, "root_cause": "foreign evidence"},
                )
                diagnosis = next(
                    receipt
                    for receipt in closeout.receipts
                    if receipt.stage == "diagnosis"
                )

                self.assertEqual(reads, [])
                self.assertEqual(diagnosis.status, "partial")
                self.assertNotIn("foreign evidence", str(diagnosis.facts))

    def test_closeout_rejects_evidence_when_its_bound_target_no_longer_exists(
        self,
    ) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        projection = {
            "case_id": "replaced-target-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-2", "address": "192.0.2.32"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "diagnosis-evidence",
                    "blob_id": "diagnosis",
                    "target_id": "target-1",
                    "target_address": "192.0.2.31",
                    "operation": "debug_run",
                    "operation_id": "diagnosis-one",
                }
            ],
        }
        reads: list[str] = []

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: reads.append(str(reference["evidence_id"]))
            or {"ok": True, "root_cause": "removed target evidence"},
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(reads, [])
        self.assertEqual(diagnosis.status, "partial")

    def test_closeout_accepts_complete_multi_target_evidence_bindings(self) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        targets = [
            {"target_id": "bmc-a", "address": "192.0.2.33"},
            {"target_id": "bmc-b", "address": "192.0.2.34"},
        ]
        projection = {
            "case_id": "multi-target-bound-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "targets": targets,
            "operations": [
                {
                    "operation_id": "diagnosis-many",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"targets": targets},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "diagnosis-evidence",
                    "blob_id": "diagnosis",
                    "operation": "debug_run",
                    "operation_id": "diagnosis-many",
                    "target_bindings": [
                        {
                            "target_id": "bmc-a",
                            "target_address": "192.0.2.33",
                            "operation_id": "diagnosis-many",
                            "expected_product_version": "",
                            "observed_product_version": "",
                        },
                        {
                            "target_id": "bmc-b",
                            "target_address": "192.0.2.34",
                            "operation_id": "diagnosis-many",
                            "expected_product_version": "",
                            "observed_product_version": "",
                        },
                    ],
                }
            ],
        }
        reads: list[str] = []

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: reads.append(str(reference["evidence_id"]))
            or {"ok": True, "root_cause": "multi-target root cause"},
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(reads, ["diagnosis-evidence"])
        self.assertEqual(diagnosis.status, "blocked")
        self.assertEqual(diagnosis.facts["root_cause"], "multi-target root cause")

    def test_closeout_rejects_evidence_from_a_previous_target_address(self) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        projection = {
            "case_id": "address-bound-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.31"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "diagnosis-evidence",
                    "blob_id": "diagnosis",
                    "target_id": "target-1",
                    "target_address": "192.0.2.30",
                    "operation": "debug_run",
                    "operation_id": "diagnosis-one",
                }
            ],
        }
        reads: list[str] = []

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: reads.append(str(reference["evidence_id"]))
            or {"ok": True, "root_cause": "old target evidence"},
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(reads, [])
        self.assertEqual(diagnosis.status, "partial")
        self.assertNotIn("old target evidence", str(diagnosis.facts))

    def test_closeout_rejects_evidence_for_another_expected_product_version(
        self,
    ) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        projection = {
            "case_id": "version-bound-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "workflow_inputs": {"product_version": "3.2.0"},
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "diagnosis-evidence",
                    "blob_id": "diagnosis",
                    "target_id": "target-1",
                    "target_address": "192.0.2.30",
                    "operation": "debug_run",
                    "operation_id": "diagnosis-one",
                    "expected_product_version": "3.1.0",
                }
            ],
        }
        reads: list[str] = []

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: reads.append(str(reference["evidence_id"]))
            or {"ok": True, "root_cause": "wrong version evidence"},
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(reads, [])
        self.assertEqual(diagnosis.status, "partial")
        self.assertNotIn("wrong version evidence", str(diagnosis.facts))

    def test_closeout_rejects_evidence_observed_for_a_different_version(
        self,
    ) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        projection = {
            "case_id": "observed-version-bound-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "workflow_inputs": {"product_version": "3.2.0"},
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "diagnosis-evidence",
                    "blob_id": "diagnosis",
                    "target_id": "target-1",
                    "target_address": "192.0.2.30",
                    "operation": "debug_run",
                    "operation_id": "diagnosis-one",
                    "expected_product_version": "3.2.0",
                    "observed_product_version": "3.1.0",
                }
            ],
        }
        reads: list[str] = []

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: reads.append(str(reference["evidence_id"]))
            or {"ok": True, "root_cause": "wrong observed version"},
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(reads, [])
        self.assertEqual(diagnosis.status, "partial")

    def test_closeout_rejects_evidence_older_than_its_operation(self) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )
        projection = {
            "case_id": "time-bound-evidence",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-one",
                    "operation": "debug_run",
                    "status": "completed",
                    "started_at": 20.0,
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "diagnosis-evidence",
                    "blob_id": "diagnosis",
                    "target_id": "target-1",
                    "target_address": "192.0.2.30",
                    "operation": "debug_run",
                    "operation_id": "diagnosis-one",
                    "observed_at": 19.0,
                }
            ],
        }
        reads: list[str] = []

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: reads.append(str(reference["evidence_id"]))
            or {"ok": True, "root_cause": "pre-operation evidence"},
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(reads, [])
        self.assertEqual(diagnosis.status, "partial")

    def test_accepted_diagnosis_supersedes_only_its_bound_runtime_receipt(
        self,
    ) -> None:
        plan = AcceptancePlan.freeze(
            {"intent": "diagnosis-only", "final_purpose": "diagnose"},
            frozen_at=1.0,
        )

        def blocked_receipt(receipt_id: str, evidence_id: str) -> dict[str, object]:
            return {
                "receipt_id": receipt_id,
                "operation": "debug_run",
                "status": "blocked",
                "coverage": {
                    "requested": 1,
                    "evaluable": 0,
                    "unavailable": 0,
                    "not_checked": 1,
                    "complete": False,
                },
                "results": [
                    {
                        "result_id": "diagnosis",
                        "kind": "diagnosis",
                        "request": "bounded diagnosis",
                        "status": "not_checked",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "freshness": {"status": "unknown", "observed_at": ""},
                "capabilities": {},
                "truncated": False,
                "content_complete": False,
                "evidence": [{"evidence_id": evidence_id}],
                "gaps": ["diagnostic_result_not_visible"],
            }

        projection = {
            "case_id": "diagnosis-lineage-case",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [
                {"target_id": "target-a", "address": "192.0.2.31"},
                {"target_id": "target-b", "address": "192.0.2.32"},
            ],
            "operations": [
                {
                    "operation_id": "diagnosis-a",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-a"},
                    "evidence_ids": ["a-evidence"],
                    "diagnostic_receipt": blocked_receipt(
                        "diagnostic-a",
                        "a-evidence",
                    ),
                },
                {
                    "operation_id": "diagnosis-b",
                    "operation": "debug_run",
                    "status": "failed",
                    "terminal_revision": 2,
                    "inputs": {"target_id": "target-b"},
                    "evidence_ids": ["b-evidence"],
                    "diagnostic_receipt": blocked_receipt(
                        "diagnostic-b",
                        "b-evidence",
                    ),
                },
            ],
            "phase_records": [
                {
                    "phase_type": "diagnosis.acceptance",
                    "producer_identity": "openubmc-debug",
                    "status": "completed",
                    "summary": "target A diagnosis accepted",
                    "operation_id": "diagnosis-acceptance-a",
                    "native_run_fact": True,
                    "evidence_ids": ["a-evidence"],
                    "root_cause": "target A root cause",
                    "supersedes_diagnostic_receipt_id": "diagnostic-a",
                }
            ],
            "evidence_refs": [
                {"evidence_id": "a-evidence", "blob_id": "a"},
                {"evidence_id": "b-evidence", "blob_id": "b"},
            ],
        }

        closeout = aggregate_case_closeout(
            projection,
            lambda reference: {
                "ok": reference["evidence_id"] == "a-evidence",
                "summary": "diagnostic evidence",
            },
        )
        diagnosis = [
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        ]

        self.assertEqual(
            {(receipt.producer, receipt.status) for receipt in diagnosis},
            {("openubmc-debug", "completed"), ("debug_run", "failed")},
        )
        self.assertNotEqual(closeout.closure_status, "completed_in_scope")

    def test_closeout_requires_a_structured_result_for_each_business_check(
        self,
    ) -> None:
        first_check = "Redfish health is green"
        missing_check = "Drive inventory matches the expected topology"
        plan = AcceptancePlan.freeze(
            {
                "intent": "upgrade-and-verify",
                "delivery_strategy": "build-upgrade",
                "final_purpose": "verify the repaired target",
                "verification_checks": [first_check, missing_check],
            },
            frozen_at=1.0,
        )
        projection = {
            "case_id": "missing-structured-business-check",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.33"}],
            "operations": [
                {
                    "operation_id": "verification-one",
                    "operation": "debug_collect",
                    "status": "completed",
                    "terminal_revision": 1,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["verification-evidence"],
                    "target_epoch": 1,
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {
                    "evidence_id": "verification-evidence",
                    "blob_id": "verification",
                }
            ],
        }

        closeout = aggregate_case_closeout(
            projection,
            lambda _reference: {
                "ok": True,
                "summary": "one required business check was reported",
                "target_epoch": 1,
                "business_acceptance": "passed",
                "acceptance_results": [
                    {"title": first_check, "status": "passed"}
                ],
            },
        )
        missing_requirement = next(
            requirement
            for requirement in plan.requirements
            if requirement.title == missing_check
        )
        missing_result = next(
            check
            for check in closeout.checks
            if check.requirement_id == missing_requirement.requirement_id
        )

        self.assertEqual(missing_result.status, "not_run")
        self.assertEqual(closeout.business_acceptance, "unverified")
        self.assertNotEqual(closeout.closure_status, "verified")

    def test_closeout_uses_the_upgrade_receipt_artifact_identity(self) -> None:
        expected_digest = "a" * 64
        plan = AcceptancePlan.freeze(
            {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "final_purpose": "install the built firmware",
            },
            frozen_at=1.0,
        )
        projection = {
            "case_id": "upgrade-receipt-identity-mismatch",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.34"}],
            "operations": [
                {
                    "operation_id": "upgrade-one",
                    "operation": "upgrade_run",
                    "status": "completed",
                    "terminal_revision": 2,
                    "inputs": {
                        "target_id": "target-1",
                        "artifact_sha256": expected_digest,
                        "product_version": "3.2.0",
                    },
                    "evidence_ids": ["upgrade-evidence"],
                    "target_epoch": 1,
                }
            ],
            "phase_records": [
                {
                    "phase_id": "build-one",
                    "phase_type": "build.artifact",
                    "status": "completed",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": expected_digest,
                    "product_version": "3.2.0",
                }
            ],
            "evidence_refs": [
                {"evidence_id": "upgrade-evidence", "blob_id": "upgrade"}
            ],
        }

        for receipt_identity in (
            {"artifact_sha256": "b" * 64, "product_version": "3.2.0"},
            {"artifact_sha256": expected_digest, "product_version": "9.9.9"},
            {
                "artifact_sha256": expected_digest,
                "product_version": "3.2.0",
                "artifact_ref": {
                    "digest": "sha256:" + "b" * 64,
                    "version": "3.2.0",
                },
            },
            {
                "artifact_sha256": expected_digest,
                "product_version": "3.2.0",
                "journal": {"expected_checksum": "b" * 64},
            },
            {
                "artifact_sha256": expected_digest,
                "product_version": "3.2.0",
                "journal": {"observed_checksum": "b" * 64},
            },
            {
                "artifact_sha256": expected_digest,
                "product_version": "3.2.0",
                "verification": {"remote_sha256": "b" * 64},
            },
            {
                "artifact_sha256": expected_digest,
                "product_version": "3.2.0",
                "artifact_ref": {
                    "digest": "sha256:" + expected_digest,
                    "version": "9.9.9",
                },
            },
        ):
            with self.subTest(receipt_identity=receipt_identity):
                journal = {
                    "stage": "verified",
                    "action": "upgrade",
                    "operation_id": "upgrade-one",
                    "epoch_after": 1,
                    **receipt_identity.get("journal", {}),
                }
                evidence = {
                    "ok": True,
                    "summary": "target reported a different artifact identity",
                    "journal": journal,
                    "verification": {
                        "installed_version": receipt_identity.get(
                            "product_version", "3.2.0"
                        ),
                        "target_epoch": 1,
                        **receipt_identity.get("verification", {}),
                    },
                    **{
                        name: value
                        for name, value in receipt_identity.items()
                        if name not in {"journal", "verification"}
                    },
                }
                closeout = aggregate_case_closeout(
                    projection,
                    lambda _reference: evidence,
                )

                self.assertEqual(closeout.identity_status, "mismatched")
                self.assertNotEqual(closeout.closure_status, "verified")

        omitted_digest_closeout = aggregate_case_closeout(
            projection,
            lambda _reference: {
                "ok": True,
                "summary": "adapter omitted the equivalent digest projection",
                "journal": {
                    "stage": "verified",
                    "action": "upgrade",
                    "operation_id": "upgrade-one",
                    "epoch_after": 1,
                },
                "verification": {
                    "installed_version": "3.2.0",
                    "target_epoch": 1,
                },
            },
        )

        self.assertEqual(omitted_digest_closeout.identity_status, "matched")

    def test_closeout_fails_closed_on_inconsistent_diagnostic_coverage(self) -> None:
        plan = AcceptancePlan.freeze(
            {
                "intent": "diagnosis-only",
                "final_purpose": "diagnose",
            },
            frozen_at=1.0,
        )
        projection = {
            "case_id": "invalid-diagnostic-coverage",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-invalid-coverage",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 5,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                    "diagnostic_receipt": {
                        "receipt_id": "diagnostic-invalid-coverage",
                        "operation": "debug_run",
                        "status": "complete",
                        "coverage": {
                            "requested": 2,
                            "evaluable": 1,
                            "unavailable": 0,
                            "not_checked": 0,
                            "complete": True,
                        },
                        "results": [
                            {
                                "result_id": "version",
                                "kind": "target-version",
                                "request": "/etc/version.json",
                                "status": "available",
                                "value": {"version": "12.08.21.06"},
                            }
                        ],
                        "freshness": {
                            "status": "complete",
                            "observed_at": "2026-08-25T00:00:00Z",
                            "complete": True,
                        },
                        "capabilities": {},
                        "truncated": False,
                        "content_complete": True,
                        "evidence": [],
                        "gaps": [],
                    },
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {"evidence_id": "diagnosis-evidence", "blob_id": "diagnosis"}
            ],
        }

        closeout = aggregate_case_closeout(
            projection,
            lambda _reference: {
                "ok": True,
                "root_cause": "one visible result",
            },
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(diagnosis.status, "partial")
        self.assertNotEqual(closeout.closure_status, "completed_in_scope")

    def test_closeout_requires_complete_visible_diagnostic_coverage(self) -> None:
        plan = AcceptancePlan.freeze(
            {
                "intent": "diagnosis-only",
                "final_purpose": "diagnose",
            },
            frozen_at=1.0,
        )
        projection = {
            "case_id": "compacted-diagnostic-coverage",
            "acceptance_plan": plan.to_public_dict(),
            "targets": [{"target_id": "target-1", "address": "192.0.2.30"}],
            "operations": [
                {
                    "operation_id": "diagnosis-compacted-coverage",
                    "operation": "debug_run",
                    "status": "completed",
                    "terminal_revision": 5,
                    "inputs": {"target_id": "target-1"},
                    "evidence_ids": ["diagnosis-evidence"],
                    "diagnostic_receipt": {
                        "receipt_id": "diagnostic-compacted-coverage",
                        "operation": "debug_run",
                        "status": "complete",
                        "coverage": {
                            "requested": 3,
                            "evaluable": 3,
                            "unavailable": 0,
                            "not_checked": 0,
                            "complete": True,
                            "visible_evaluable": 1,
                            "visible_unavailable": 0,
                            "visible_not_checked": 2,
                            "compacted": 3,
                        },
                        "results": [
                            {
                                "result_id": "version",
                                "kind": "target-version",
                                "request": "/etc/version.json",
                                "status": "available",
                                "value": {"version": "12.08.21.06"},
                            }
                        ],
                        "compacted_results": {
                            "status": "not_checked",
                            "gap": "result_preview_compacted",
                            "result_ids": ["uptime", "target-clock"],
                        },
                        "freshness": {
                            "status": "complete",
                            "observed_at": "2026-08-25T00:00:00Z",
                            "complete": True,
                        },
                        "capabilities": {},
                        "truncated": False,
                        "content_complete": True,
                        "content_compacted": True,
                        "evidence": [],
                        "gaps": ["diagnostic_receipt_compacted"],
                    },
                }
            ],
            "phase_records": [],
            "evidence_refs": [
                {"evidence_id": "diagnosis-evidence", "blob_id": "diagnosis"}
            ],
        }

        closeout = aggregate_case_closeout(
            projection,
            lambda _reference: {
                "ok": True,
                "root_cause": "one visible result",
            },
        )
        diagnosis = next(
            receipt for receipt in closeout.receipts if receipt.stage == "diagnosis"
        )

        self.assertEqual(diagnosis.status, "partial")
        self.assertNotEqual(closeout.closure_status, "completed_in_scope")

        zero_visible_projection = json.loads(json.dumps(projection))
        zero_visible_receipt = zero_visible_projection["operations"][0][
            "diagnostic_receipt"
        ]
        zero_visible_receipt["status"] = "partial"
        zero_visible_receipt["coverage"].update(
            {
                "evaluable": 2,
                "not_checked": 1,
                "complete": False,
                "visible_evaluable": 0,
                "visible_not_checked": 3,
            }
        )
        zero_visible_receipt["results"] = []
        zero_visible_receipt["compacted_results"]["result_ids"] = [
            "version",
            "uptime",
            "target-clock",
        ]

        zero_visible_closeout = aggregate_case_closeout(
            zero_visible_projection,
            lambda _reference: {
                "ok": True,
                "root_cause": "source result is not visible",
            },
        )
        zero_visible_diagnosis = next(
            receipt
            for receipt in zero_visible_closeout.receipts
            if receipt.stage == "diagnosis"
        )

        self.assertEqual(zero_visible_diagnosis.status, "blocked")
        self.assertNotEqual(
            zero_visible_closeout.closure_status,
            "completed_in_scope",
        )

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









    def test_public_json_rpc_exposes_terminal_closeout_case_and_evidence(self) -> None:
        repository = InMemoryRuntimeRepository()
        blobs = InMemoryBlobRepository()
        service = RuntimeMcpService(
            PlanObservingBackend(repository),
            context_repository=repository,
            blob_repository=blobs,
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
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.43",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "purpose": "repair the reported behavior",
                    "deadline": 10,
                },
            )["structuredContent"]
            case_id = first["run_id"]
            first = self._rpc_call(
                endpoint, 10, "execute",
                {
                    "kind": "respond", "run_id": case_id,
                    "gate_id": first["gate"]["gate_id"],
                    "gate_version": first["gate"]["gate_version"],
                    "schema_digest": first["gate"]["schema_digest"],
                    "response": {
                        "status": "completed", "summary": "source diagnosis verified",
                        "payload": accepted_diagnosis_payload([
                            item["evidence_id"] for item in first["diagnostic_receipt"]["evidence"]
                        ]),
                    },
                },
            )["structuredContent"]
            terminal_result = self._rpc_call(
                endpoint,
                2,
                "execute",
                {
                    "kind": "respond",
                    "run_id": case_id,
                    "gate_id": first["gate"]["gate_id"],
                    "gate_version": first["gate"]["gate_version"],
                    "schema_digest": first["gate"]["schema_digest"],
                    "response": {
                        "status": "completed",
                        "summary": "implemented the bounded source fix",
                        "payload": {
                            "source_revision": "abc123",
                            "authored_files": ["src/unit.lua"],
                            "verification_plan": ["targeted unit regression"],
                        },
                    },
                },
            )
            terminal = terminal_result["structuredContent"]
            projected = self._rpc_call(
                operator_endpoint,
                3,
                "case_read",
                {"case_id": case_id},
            )["structuredContent"]
            reference = projected["evidence_refs"][0]
            evidence = self._rpc_call(
                operator_endpoint,
                4,
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

        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(terminal["outcome"]["status"], "completed")
        self.assertNotIn("closeout", terminal)
        assert persisted is not None
        for key, value in persisted.items():
            self.assertIn(key, projected)
            if key != "last_access":
                self.assertEqual(projected[key], value)
        self.assertIn("capsule", projected)
        self.assertIn("agent_envelope", projected)
        self.assertEqual(projected["closeout"]["closure_status"], "completed_in_scope")
        self.assertTrue(projected["closeout_markdown"])
        self.assertIsNone(projected["closeout_bundle"])
        self.assertGreater(evidence["returned_bytes"], 0)
        self.assertIsInstance(evidence["body"], str)
        self.assertIsInstance(json.loads(evidence["body"]), dict)
        self.assertEqual(
            evidence["evidence"]["evidence_id"],
            reference["evidence_id"],
        )



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
