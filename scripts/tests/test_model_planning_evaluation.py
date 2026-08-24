from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from model_planning_evaluation import evaluate, plan_for_objective  # noqa: E402


class ModelPlanningEvaluationTests(unittest.TestCase):
    def test_isolated_candidate_contains_invalid_plans_without_claiming_leverage(self) -> None:
        result = evaluate()

        self.assertTrue(result["invariants_passed"])
        self.assertFalse(result["demonstrated_leverage"])
        self.assertEqual(result["verdict"], "isolate")
        self.assertEqual(result["agent_interface"], ["observe", "execute"])
        self.assertEqual(result["paired_tasks"], 6)
        self.assertEqual(result["static_workflow"]["evaluated"], 6)
        self.assertEqual(result["static_workflow"]["valid"], 6)
        self.assertEqual(result["static_workflow"]["invalid"], 0)
        self.assertEqual(result["isolated_candidate"]["valid_revisions"], 6)
        self.assertEqual(result["isolated_candidate"]["valid_plan_rate"], 1.0)
        self.assertEqual(result["static_workflow"]["valid_plan_rate"], 1.0)
        self.assertEqual(result["containment"]["evaluated"], 4)
        self.assertEqual(result["containment"]["rejected"], 4)
        self.assertEqual(result["containment"]["false_accepts"], 0)
        self.assertTrue(result["input_sensitivity"]["passed"])
        self.assertEqual(
            result["input_sensitivity"]["unrelated_status"],
            "rejected",
        )
        for pair in result["pairs"]:
            with self.subTest(name=pair["name"]):
                self.assertTrue(pair["candidate_used_objective"])
                self.assertEqual(pair["static_steps"], pair["expected_steps"])
                self.assertEqual(pair["candidate_steps"], pair["expected_steps"])

    def test_planner_does_not_treat_negated_keywords_as_an_upgrade_request(self) -> None:
        proposal = plan_for_objective(
            "write firmware release notes; do not build or upgrade"
        )
        actions = [
            node.get("action")
            for node in proposal["nodes"]
            if isinstance(node, dict) and node.get("kind") == "action"
        ]

        self.assertEqual(actions, ["unsupported.objective"])


if __name__ == "__main__":
    unittest.main()
