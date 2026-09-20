from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openubmc_target_runtime.context_runtime import project_case  # noqa: E402
from openubmc_target_runtime.agent_gateway import render_execute_turn_text  # noqa: E402
from openubmc_target_runtime.run_engine import RunEngine  # noqa: E402
from openubmc_target_runtime.semantic_runtime import (  # noqa: E402
    Outcome,
    project_run_turn,
)


class PartialRunOutcomeTests(unittest.TestCase):
    def test_partial_outcome_is_durable_and_replayable_with_bound_findings(self) -> None:
        events = [
            {
                "kind": "CaseOpened",
                "revision": 1,
                "created_at": 1.0,
                "payload": {"intent": "diagnose-and-fix"},
            },
            {
                "kind": "RunOutcomeRecorded",
                "revision": 2,
                "created_at": 2.0,
                "payload": {
                    "outcome": {
                        "status": "partial",
                        "summary": "source cause verified; build remains",
                        "acceptance": [],
                        "verified_findings": [
                            {
                                "run_id": "run-partial",
                                "evidence_ids": ["evidence-1"],
                                "summary": "source defect is isolated",
                            }
                        ],
                        "remaining_work": [
                            {
                                "run_id": "run-partial",
                                "evidence_ids": ["evidence-1"],
                                "summary": "build and verify the fix",
                            }
                        ],
                        "blocked_by": [],
                    }
                },
            },
        ]

        projection = project_case("run-partial", events)
        self.assertEqual(projection["status"], "partial")
        self.assertEqual(
            projection["run_outcome"]["verified_findings"][0]["evidence_ids"],
            ["evidence-1"],
        )
        turn = project_run_turn(projection, run_id="run-partial").to_public_dict()
        self.assertEqual(turn["state"], "partial")
        self.assertEqual(turn["outcome"]["remaining_work"][0]["run_id"], "run-partial")

    def test_new_gate_submission_clears_partial_checkpoint_for_deterministic_resume(self) -> None:
        events = [
            {
                "kind": "CaseOpened",
                "revision": 1,
                "created_at": 1.0,
                "payload": {"intent": "diagnose-and-fix"},
            },
            {
                "kind": "RunOutcomeRecorded",
                "revision": 2,
                "created_at": 2.0,
                "payload": {
                    "outcome": {
                        "status": "partial",
                        "summary": "partial",
                        "verified_findings": [],
                        "remaining_work": [],
                        "blocked_by": [],
                    }
                },
            },
            {
                "kind": "RunGateOpened",
                "revision": 3,
                "created_at": 3.0,
                "payload": {
                    "gate": {
                        "gate_id": "gate-1",
                        "gate_version": 1,
                        "workflow_cycle_id": "cycle-1",
                        "workflow_step_id": "step-1",
                    }
                },
            },
            {
                "kind": "RunGateSubmitted",
                "revision": 4,
                "created_at": 4.0,
                "payload": {
                    "gate_id": "gate-1",
                    "gate_version": 1,
                    "submission_id": "submission-1",
                },
            },
        ]
        projection = project_case("run-partial", events)
        self.assertEqual(projection["run_outcome"], {})
        self.assertEqual(projection["status"], "open")

    def test_partial_items_must_bind_to_run_and_known_evidence(self) -> None:
        valid = {
            "verified_findings": [
                {"run_id": "run-1", "evidence_ids": ["e-1"], "summary": "finding"}
            ],
            "remaining_work": [
                {"run_id": "run-1", "evidence_ids": ["e-1"], "summary": "work"}
            ],
            "blocked_by": [],
        }
        RunEngine._validate_partial_outcome_payload(  # noqa: SLF001
            valid, run_id="run-1", evidence_refs=[{"evidence_id": "e-1"}]
        )
        with self.assertRaisesRegex(ValueError, "current Run"):
            RunEngine._validate_partial_outcome_payload(
                {**valid, "verified_findings": [
                    {"run_id": "other", "evidence_ids": ["e-1"], "summary": "finding"}
                ]},
                run_id="run-1",
                evidence_refs=[{"evidence_id": "e-1"}],
            )
        with self.assertRaisesRegex(ValueError, "unknown Evidence"):
            RunEngine._validate_partial_outcome_payload(
                {**valid, "verified_findings": [
                    {"run_id": "run-1", "evidence_ids": ["e-2"], "summary": "finding"}
                ]},
                run_id="run-1",
                evidence_refs=[{"evidence_id": "e-1"}],
            )

    def test_outcome_public_projection_keeps_partial_fields_separate(self) -> None:
        outcome = Outcome(
            status="partial",
            summary="partial",
            verified_findings=({"run_id": "run-1", "evidence_ids": ["e-1"], "summary": "finding"},),
            remaining_work=({"run_id": "run-1", "evidence_ids": ["e-1"], "summary": "work"},),
            blocked_by=(),
        )
        public = outcome.to_public_dict()
        self.assertEqual(public["status"], "partial")
        self.assertIn("verified_findings", public)
        self.assertIn("remaining_work", public)
        self.assertEqual(public["blocked_by"], [])

    def test_partial_text_projection_reports_findings_and_remaining_work_counts(self) -> None:
        text = render_execute_turn_text({
            "state": "partial",
            "outcome": {
                "status": "partial",
                "summary": "partial",
                "verified_findings": [{"summary": "finding"}],
                "remaining_work": [{"summary": "work"}, {"summary": "verify"}],
                "blocked_by": [],
            },
        })
        self.assertIn("verified_findings=1", text)
        self.assertIn("remaining_work=2", text)
        self.assertIn("blocked_by=0", text)


if __name__ == "__main__":
    unittest.main()
