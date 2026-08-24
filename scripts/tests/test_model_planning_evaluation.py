from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from model_planning_evaluation import evaluate  # noqa: E402


class ModelPlanningEvaluationTests(unittest.TestCase):
    def test_isolated_candidate_contains_invalid_plans_without_claiming_leverage(self) -> None:
        result = evaluate()

        self.assertTrue(result["invariants_passed"])
        self.assertFalse(result["demonstrated_leverage"])
        self.assertEqual(result["verdict"], "isolate")
        self.assertEqual(result["agent_interface"], ["observe", "execute"])
        self.assertEqual(result["isolated_candidate"]["false_accepts"], 0)
        self.assertEqual(result["isolated_candidate"]["false_rejects"], 0)
        self.assertEqual(result["static_workflow"]["evaluated"], 6)
        self.assertEqual(result["static_workflow"]["valid"], 6)
        self.assertEqual(result["static_workflow"]["invalid"], 0)
        self.assertEqual(result["isolated_candidate"]["valid_revisions"], 2)
        self.assertLess(
            result["isolated_candidate"]["valid_plan_rate"],
            result["static_workflow"]["valid_plan_rate"],
        )


if __name__ == "__main__":
    unittest.main()
