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
    DEFAULT_WORKFLOW_REGISTRY,
    InMemorySessionOutcomeRepository,
    SESSION_OUTCOME_LABELS,
    RuntimeMcpService,
    SessionOutcomeError,
    SessionOutcomeService,
    SQLiteSessionOutcomeRepository,
)


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _Backend:
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

    @staticmethod
    def debug_run(_task, _arguments, _context) -> dict[str, object]:
        return {"ok": True}

    @staticmethod
    def debug_collect(_task, _arguments, _context) -> dict[str, object]:
        return {"ok": True}


def replay_bundle() -> CaseReplayBundle:
    definition = DEFAULT_WORKFLOW_REGISTRY.resolve(
        intent="diagnosis-only",
        entry_domain="debug",
        entry_operation="debug_run",
    ).to_public_dict()
    plan = AcceptancePlan.freeze(
        {
            "intent": "diagnosis-only",
            "entry_domain": "debug",
            "delivery_strategy": "",
        },
        frozen_at=1.0,
    ).to_public_dict()
    events = [
        {
            "revision": 1,
            "kind": "CaseOpened",
            "operation_id": "",
            "payload": {
                "intent": "diagnosis-only",
                "entry_domain": "debug",
                "entry_operation": "debug_run",
                "final_purpose": "session outcome replay",
                "targets": [],
                "workflow_inputs": {},
                "workflow_definition": definition,
                "acceptance_plan": plan,
                "workflow_cycle_id": "cycle-1",
                "workflow_cycle_number": 1,
            },
            "created_at": 1.0,
        }
    ]
    return CaseReplayBundle.create(
        case_id="case-session-outcome",
        workflow_definition=definition,
        events=events,
        receipts=[],
        evidence_metadata=[],
        target_epochs={},
        acceptance_plan=plan,
        expected_outcome={"status": "open"},
    )


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


class SessionOutcomeTests(unittest.TestCase):
    def service(self, repository=None) -> SessionOutcomeService:
        return SessionOutcomeService(
            repository or InMemorySessionOutcomeRepository(),
            clock=Clock(),
        )

    def record(self, service: SessionOutcomeService, **overrides):
        bundle = replay_bundle()
        values = {
            "session_id": "session-1",
            "case_id": bundle.case_id,
            "replay_fingerprint": bundle.fingerprint,
            "workflow": "diagnosis-only",
            "domain": "debug",
            "outcome": "failed",
            "summary": "Bearer secret-token caused a failed run",
            "details": {"ssh_password": "secret", "reason": "token abc"},
        }
        values.update(overrides)
        return service.record(**values)

    def approve(self, service: SessionOutcomeService, outcome_id: str) -> None:
        service.transition(outcome_id, action="review", actor="reviewer")
        service.transition(outcome_id, action="approve", actor="approver")

    def test_all_required_labels_are_supported_and_gap_labels_require_type(self) -> None:
        self.assertEqual(
            SESSION_OUTCOME_LABELS,
            {
                "completed",
                "partial",
                "failed",
                "user-corrected",
                "false-success",
                "evidence-gap",
                "contract-gap",
            },
        )
        service = self.service()
        for label in sorted(SESSION_OUTCOME_LABELS - {"evidence-gap", "contract-gap"}):
            with self.subTest(label=label):
                self.record(service, session_id=f"session-{label}", outcome=label)
        with self.assertRaisesRegex(SessionOutcomeError, "requires gap_type"):
            self.record(service, outcome="evidence-gap")

    def test_record_is_redacted_deterministic_and_never_rule_eligible(self) -> None:
        service = self.service()
        first = self.record(service)
        second = self.record(service)

        self.assertEqual(first.outcome_id, second.outcome_id)
        encoded = json.dumps(first.to_public_dict(), sort_keys=True)
        self.assertNotIn("secret-token", encoded)
        self.assertNotIn("\"ssh_password\"", encoded)
        self.assertFalse(first.to_public_dict()["execution_rule_eligible"])
        self.assertEqual(service.summary()["total"], 1)

    def test_repeated_failures_group_by_workflow_domain_outcome_and_gap(self) -> None:
        service = self.service()
        self.record(
            service,
            session_id="session-a",
            outcome="contract-gap",
            gap_type="missing-schema",
        )
        self.record(
            service,
            session_id="session-b",
            outcome="contract-gap",
            gap_type="missing-schema",
        )
        self.record(
            service,
            session_id="session-c",
            outcome="evidence-gap",
            gap_type="stale-evidence",
        )

        groups = service.summary()["groups"]
        contract = next(item for item in groups if item["outcome"] == "contract-gap")
        evidence = next(item for item in groups if item["outcome"] == "evidence-gap")
        self.assertEqual(contract["count"], 2)
        self.assertEqual(contract["gap_type"], "missing-schema")
        self.assertEqual(evidence["count"], 1)

    def test_promotion_requires_independent_review_and_approval(self) -> None:
        service = self.service()
        record = self.record(service)
        with self.assertRaisesRegex(SessionOutcomeError, "reviewed and approved"):
            service.promote(
                record.outcome_id,
                target="knowledge",
                payload={"title": "Failure", "content": "Use the verified path."},
            )
        service.transition(record.outcome_id, action="review", actor="owner")
        with self.assertRaisesRegex(SessionOutcomeError, "independent actor"):
            service.transition(record.outcome_id, action="approve", actor="owner")
        service.transition(record.outcome_id, action="approve", actor="maintainer")
        artifact = service.promote(
            record.outcome_id,
            target="knowledge",
            payload={"title": "Failure", "content": "Use the verified path."},
        )

        self.assertFalse(artifact["executable"])
        self.assertEqual(artifact["execution_rule_effect"], "none")
        self.assertEqual(artifact["case_reference"], f"case://{record.case_id}")
        self.assertEqual(
            artifact["replay_reference"], f"replay://{record.replay_fingerprint}"
        )

    def test_golden_scenario_requires_the_linked_replay_bundle(self) -> None:
        service = self.service()
        bundle = replay_bundle()
        record = self.record(
            service,
            replay_fingerprint=bundle.fingerprint,
            outcome="false-success",
        )
        self.approve(service, record.outcome_id)
        artifact = service.promote(
            record.outcome_id,
            target="golden-scenario",
            payload={"replay_bundle": bundle.to_public_dict()},
        )

        self.assertEqual(artifact["target"], "golden-scenario")
        self.assertEqual(
            artifact["content"]["replay_bundle"]["fingerprint"],
            bundle.fingerprint,
        )

    def test_architecture_conclusion_can_only_be_an_adr_with_case_and_replay(self) -> None:
        service = self.service()
        record = self.record(service, architecture_decision=True)
        self.approve(service, record.outcome_id)
        with self.assertRaisesRegex(SessionOutcomeError, "must be promoted as ADR"):
            service.promote(
                record.outcome_id,
                target="knowledge",
                payload={"title": "Decision", "content": "wrong target"},
            )
        artifact = service.promote(
            record.outcome_id,
            target="adr",
            payload={
                "title": "Keep one workflow kernel",
                "decision": "Use the canonical kernel.",
                "context": "Duplicate state derivation caused drift.",
                "consequences": "All callers use one interface.",
            },
        )

        self.assertEqual(artifact["target"], "adr")
        self.assertTrue(artifact["case_reference"].startswith("case://"))
        self.assertTrue(artifact["replay_reference"].startswith("replay://"))

    def test_sqlite_adapter_preserves_review_state_and_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-outcomes.sqlite3"
            service = self.service(SQLiteSessionOutcomeRepository(path))
            record = self.record(service)
            self.approve(service, record.outcome_id)
            service.promote(
                record.outcome_id,
                target="knowledge",
                payload={"title": "Stable fact", "content": "Reviewed content."},
            )

            reopened = SessionOutcomeService(SQLiteSessionOutcomeRepository(path))
            restored = reopened.repository.get(record.outcome_id)

        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.review_state, "promoted")
        self.assertEqual(restored.promotion["target"], "knowledge")

    def test_mcp_seam_enforces_schema_review_and_inert_promotion(self) -> None:
        service = RuntimeMcpService(_Backend())
        bundle = replay_bundle()
        arguments = {
            "session_id": "session-mcp",
            "case_id": bundle.case_id,
            "replay_fingerprint": bundle.fingerprint,
            "workflow": "diagnosis-only",
            "domain": "debug",
            "outcome": "user-corrected",
            "summary": "User corrected the selected source owner.",
        }
        try:
            with self.assertRaisesRegex(ValueError, "raw_session is unexpected"):
                service.call_tool(
                    "session_outcome_record",
                    {**arguments, "raw_session": "must never become a rule"},
                    task_id="session-mcp",
                    operation_id="record-invalid",
                )
            recorded = service.call_tool(
                "session_outcome_record",
                arguments,
                task_id="session-mcp",
                operation_id="record",
            )
            outcome_id = recorded["outcome_id"]
            for action, actor in (("review", "reviewer"), ("approve", "approver")):
                service.call_tool(
                    "session_outcome_transition",
                    {"outcome_id": outcome_id, "action": action, "actor": actor},
                    task_id="session-mcp",
                    operation_id=action,
                )
            artifact = service.call_tool(
                "session_outcome_promote",
                {
                    "outcome_id": outcome_id,
                    "target": "knowledge",
                    "payload": {
                        "title": "Correct source owner",
                        "content": "Use the reviewed repository evidence.",
                    },
                },
                task_id="session-mcp",
                operation_id="promote",
            )
            summary = service.call_tool(
                "session_outcome_summary",
                {},
                task_id="session-mcp",
                operation_id="summary",
            )
        finally:
            service.close()

        self.assertFalse(artifact["executable"])
        self.assertEqual(summary["total"], 1)


if __name__ == "__main__":
    unittest.main()
