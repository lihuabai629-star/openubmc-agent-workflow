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
    CaseReplayBundle,
    CaseReplayService,
    DEFAULT_WORKFLOW_REGISTRY,
    InMemoryRuntimeRepository,
    PendingCaseEvent,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)


def _event(
    revision: int,
    kind: str,
    payload: dict[str, object],
    operation_id: str = "",
) -> dict[str, object]:
    return {
        "revision": revision,
        "kind": kind,
        "operation_id": operation_id,
        "payload": payload,
        "created_at": float(revision),
    }


def _base_facts(
    *,
    operation: str = "debug_run",
    intent: str = "diagnosis-only",
    entry_domain: str = "debug",
    delivery_strategy: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    definition = DEFAULT_WORKFLOW_REGISTRY.resolve(
        intent=intent,
        entry_domain=entry_domain,
        entry_operation=operation if intent == "diagnosis-only" else "",
        delivery_strategy=delivery_strategy,
    ).to_public_dict()
    plan = AcceptancePlan.freeze(
        {
            "intent": intent,
            "entry_domain": entry_domain,
            "delivery_strategy": delivery_strategy,
        },
        frozen_at=1.0,
    ).to_public_dict()
    return definition, plan


def _base_events(
    *,
    operation: str = "debug_run",
    workflow_target_epoch: int = 0,
    observed_target_epoch: int = 0,
    terminal_status: str = "completed",
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
    definition, plan = _base_facts(operation=operation)
    events = [
        _event(
            1,
            "CaseOpened",
            {
                "intent": "diagnosis-only",
                "entry_domain": "debug",
                "entry_operation": operation,
                "final_purpose": "offline replay",
                "targets": [
                    {
                        "target_id": "target-1",
                        "role": "candidate",
                        "address": "192.0.2.90",
                    }
                ],
                "workflow_inputs": {
                    "problem": "Authorization: Bearer replay-secret",
                    "ssh_password": "never-export-this",
                },
                "workflow_definition": definition,
                "acceptance_plan": plan,
                "workflow_cycle_id": "cycle-1",
                "workflow_cycle_number": 1,
            },
        ),
        _event(
            2,
            "OperationAccepted",
            {
                "operation": operation,
                "idempotency_key": "replay-operation",
                "request_fingerprint": "a" * 64,
                "workflow_cycle_id": "cycle-1",
                "workflow_step_id": f"step-01-{operation}",
                "workflow_step_kind": "operation",
                "workflow_definition_id": definition["definition_id"],
                "workflow_definition_version": definition["version"],
                "workflow_definition_fingerprint": definition["fingerprint"],
                "workflow_execution_id": "step-replay",
                "workflow_attempt": 1,
                "workflow_input_fingerprint": "b" * 64,
                "workflow_target_epoch": workflow_target_epoch,
                "target_version": 1,
                "target_id": "target-1",
            },
            "replay-operation",
        ),
        _event(3, "OperationStarted", {}, "replay-operation"),
        _event(
            4,
            "EvidenceAttached",
            {
                "evidence": {
                    "evidence_id": "evidence-replay",
                    "blob_id": "sha256:" + "c" * 64,
                    "media_type": "application/json",
                    "byte_count": 10,
                    "target_id": "target-1",
                    "generation": str(observed_target_epoch),
                    "provenance": f"{operation}:replay-operation",
                    "observed_at": 4.0,
                    "case_id": "case-replay",
                    "producer": operation,
                    "target_epoch": observed_target_epoch,
                    "parent_evidence_ids": [],
                }
            },
            "replay-operation",
        ),
        _event(
            5,
            "OperationTerminal",
            {
                "status": terminal_status,
                "summary": f"{operation} {terminal_status}",
                "case_status": "open",
                "target_epoch": observed_target_epoch,
            },
            "replay-operation",
        ),
    ]
    return events, definition, plan


def _bundle(
    events: list[dict[str, object]],
    definition: dict[str, object],
    plan: dict[str, object],
    *,
    receipts: list[dict[str, object]] | None = None,
    evidence_metadata: list[dict[str, object]] | None = None,
    expected_outcome: dict[str, object] | None = None,
) -> CaseReplayBundle:
    if evidence_metadata is None:
        evidence_metadata = [
            dict(event["payload"]["evidence"])
            for event in events
            if event["kind"] == "EvidenceAttached"
        ]
    return CaseReplayBundle.create(
        case_id="case-replay",
        workflow_definition=definition,
        events=events,
        receipts=receipts or [],
        evidence_metadata=evidence_metadata,
        target_epochs={"target-1": 0},
        acceptance_plan=plan,
        expected_outcome=expected_outcome or {"status": "open"},
    )


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _NoEffectBackend:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def open_task(task_id: str) -> _Task:
        return _Task(task_id)

    @staticmethod
    def close_task(_task: _Task) -> None:
        return None

    @staticmethod
    def maintain_task(_task: _Task) -> int:
        return 0

    @staticmethod
    def task_status(task: _Task) -> dict[str, object]:
        return {"task_id": task.task_id}

    def debug_run(self, _task, _arguments, _context) -> dict[str, object]:
        self.calls += 1
        return {"ok": True}

    def debug_collect(self, _task, _arguments, _context) -> dict[str, object]:
        self.calls += 1
        return {"ok": True}


class CaseReplayTests(unittest.TestCase):
    def test_diagnostic_receipt_golden_scenarios_are_deterministic(self) -> None:
        fixture = json.loads(
            (RUNTIME_ROOT / "tests/fixtures/diagnostic_receipt_replay.json").read_text()
        )
        for scenario in fixture["scenarios"]:
            with self.subTest(scenario=scenario["name"]):
                events, definition, plan = _base_events()
                events[-1]["payload"]["diagnostic_receipt"] = scenario["receipt"]
                bundle = _bundle(events, definition, plan)

                first = CaseReplayService.replay(bundle)
                second = CaseReplayService.replay(bundle.to_public_dict())

                self.assertEqual(first.status, scenario["expected_status"])
                self.assertIn(
                    scenario["expected_finding"],
                    {item["code"] for item in first.findings},
                )
                self.assertEqual(first.result_fingerprint, second.result_fingerprint)

    def test_replay_rejects_inconsistent_diagnostic_coverage(self) -> None:
        events, definition, plan = _base_events()
        events[-1]["payload"]["diagnostic_receipt"] = {
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
        }

        result = CaseReplayService.replay(_bundle(events, definition, plan))

        self.assertEqual(result.status, "failed")
        self.assertIn(
            "diagnostic_receipt_invalid",
            {item["code"] for item in result.findings},
        )

    def test_repository_export_is_redacted_portable_and_deterministic(self) -> None:
        for repository in (
            InMemoryRuntimeRepository(),
            SQLiteRuntimeRepository(
                Path(tempfile.mkdtemp()) / "replay.sqlite3"
            ),
        ):
            with self.subTest(adapter=repository.status()["adapter"]):
                events, _definition, _plan = _base_events()
                repository.commit(
                    "case-replay",
                    expected_revision=0,
                    events=tuple(
                        PendingCaseEvent(
                            str(event["kind"]),
                            dict(event["payload"]),
                            str(event["operation_id"]),
                        )
                        for event in events
                    ),
                )
                service = CaseReplayService(repository)
                exported = service.export("case-replay")
                first = service.replay(exported)
                second = service.replay(exported.to_public_dict())

                encoded = json.dumps(exported.to_public_dict(), sort_keys=True)
                self.assertNotIn("never-export-this", encoded)
                self.assertNotIn("replay-secret", encoded)
                self.assertIn("<redacted>", encoded)
                self.assertEqual(len(exported.events), 5)
                self.assertEqual(len(exported.evidence_metadata), 1)
                self.assertEqual(first.status, "passed")
                self.assertEqual(first.result_fingerprint, second.result_fingerprint)

    def test_golden_plan_drift_is_detected(self) -> None:
        events, _definition, plan = _base_events()
        drifted = DEFAULT_WORKFLOW_REGISTRY.resolve(
            intent="diagnosis-only",
            entry_domain="log_analyzer",
            entry_operation="log_bundle_collect",
        ).to_public_dict()
        result = CaseReplayService.replay(_bundle(events, drifted, plan))

        self.assertEqual(result.status, "failed")
        self.assertIn(
            "workflow_plan_drift",
            {item["code"] for item in result.findings},
        )

    def test_golden_acceptance_chain_break_is_detected(self) -> None:
        events, definition, plan = _base_events()
        receipt = {
            "receipt_id": "receipt-replay",
            "stage": "diagnosis",
            "producer": "debug_run",
            "status": "completed",
            "evidence_ids": ["evidence-replay"],
        }
        events.append(
            _event(
                6,
                "CloseoutRecorded",
                {
                    "closeout": {
                        "closure_status": "completed",
                        "claim_level": "verified",
                        "business_acceptance": "passed",
                        "receipts": [receipt],
                        "checks": [],
                    }
                },
            )
        )
        result = CaseReplayService.replay(
            _bundle(
                events,
                definition,
                plan,
                receipts=[receipt],
                expected_outcome={
                    "status": "completed",
                    "claim_level": "verified",
                    "business_acceptance": "passed",
                },
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertIn(
            "acceptance_chain_break",
            {item["code"] for item in result.findings},
        )

    def test_golden_stale_receipt_reuse_is_detected(self) -> None:
        events, definition, plan = _base_events(
            workflow_target_epoch=2,
            observed_target_epoch=1,
        )
        result = CaseReplayService.replay(_bundle(events, definition, plan))

        self.assertEqual(result.status, "failed")
        self.assertIn(
            "stale_receipt_reuse",
            {item["code"] for item in result.findings},
        )

    def test_golden_evidence_truncation_uses_complete_index(self) -> None:
        events, definition, plan = _base_events()
        events = events[:3]
        metadata: list[dict[str, object]] = []
        for index in range(300):
            reference = {
                "evidence_id": f"evidence-{index:03d}",
                "blob_id": "sha256:" + f"{index:064x}"[-64:],
                "media_type": "application/json",
                "byte_count": 1,
                "target_id": "target-1",
                "generation": "0",
                "provenance": "debug_run:replay-operation",
                "observed_at": float(index + 4),
                "case_id": "case-replay",
                "producer": "debug_run",
                "target_epoch": 0,
                "parent_evidence_ids": [],
            }
            metadata.append(reference)
            events.append(
                _event(
                    index + 4,
                    "EvidenceAttached",
                    {"evidence": reference},
                    "replay-operation",
                )
            )
        events.append(
            _event(
                len(events) + 1,
                "OperationTerminal",
                {
                    "status": "completed",
                    "summary": "debug_run completed",
                    "case_status": "open",
                    "target_epoch": 0,
                },
                "replay-operation",
            )
        )
        result = CaseReplayService.replay(
            _bundle(
                events,
                definition,
                plan,
                evidence_metadata=metadata,
            )
        )

        self.assertEqual(result.status, "passed")
        self.assertIn(
            "evidence_projection_truncated_preserved",
            {item["code"] for item in result.findings},
        )

    def test_golden_upgrade_unknown_stays_blocked(self) -> None:
        definition, plan = _base_facts(
            operation="upgrade_run",
            intent="diagnosis-only",
            entry_domain="upgrade",
        )
        events, _ignored, _ignored_plan = _base_events(
            operation="upgrade_run",
            terminal_status="mutation_outcome_unknown",
        )
        events[0]["payload"]["workflow_definition"] = definition
        events[0]["payload"]["acceptance_plan"] = plan
        accepted = events[1]["payload"]
        accepted["workflow_definition_id"] = definition["definition_id"]
        accepted["workflow_definition_version"] = definition["version"]
        accepted["workflow_definition_fingerprint"] = definition["fingerprint"]
        result = CaseReplayService.replay(
            _bundle(
                events,
                definition,
                plan,
                expected_outcome={"status": "mutation_outcome_unknown"},
            )
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.outcome["status"], "mutation_outcome_unknown")
        self.assertIn(
            "upgrade_outcome_unknown",
            {item["code"] for item in result.findings},
        )

    def test_mcp_replay_seam_never_invokes_a_domain_backend(self) -> None:
        repository = InMemoryRuntimeRepository()
        events, _definition, _plan = _base_events()
        repository.commit(
            "case-replay",
            expected_revision=0,
            events=tuple(
                PendingCaseEvent(
                    str(event["kind"]),
                    dict(event["payload"]),
                    str(event["operation_id"]),
                )
                for event in events
            ),
        )
        backend = _NoEffectBackend()
        service = RuntimeMcpService(backend, context_repository=repository)
        try:
            exported = service.call_tool(
                "case_replay_export",
                {"case_id": "case-replay"},
                task_id="replay-mcp",
                operation_id="replay-export",
            )
            replayed = service.call_tool(
                "case_replay_run",
                {"bundle": dict(exported)},
                task_id="replay-mcp",
                operation_id="replay-run",
            )
        finally:
            service.close()

        self.assertEqual(replayed["status"], "passed")
        self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()
