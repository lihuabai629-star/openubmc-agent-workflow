from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from model_planning_evaluation import (  # noqa: E402
    evaluate,
    has_demonstrated_leverage,
    plan_for_objective,
)


class ModelPlanningEvaluationTests(unittest.TestCase):
    def test_isolated_candidate_contains_invalid_plans_without_claiming_leverage(self) -> None:
        result = evaluate()

        self.assertTrue(result["invariants_passed"])
        self.assertFalse(result["demonstrated_leverage"])
        self.assertFalse(result["turn_leverage"])
        self.assertFalse(result["validity_leverage"])
        self.assertEqual(result["verdict"], "isolate")
        self.assertEqual(result["agent_interface"], ["observe", "execute"])
        self.assertEqual(result["paired_tasks"], 6)
        self.assertEqual(result["static_workflow"]["evaluated"], 6)
        self.assertEqual(result["static_workflow"]["valid"], 6)
        self.assertEqual(result["static_workflow"]["invalid"], 0)
        self.assertEqual(result["isolated_candidate"]["valid_revisions"], 6)
        self.assertEqual(result["isolated_candidate"]["valid_plan_rate"], 1.0)
        self.assertEqual(result["static_workflow"]["valid_plan_rate"], 1.0)
        self.assertEqual(result["static_workflow"]["agent_gate_turns"], 7)
        self.assertEqual(result["isolated_candidate"]["agent_gate_turns"], 7)
        self.assertEqual(result["containment"]["evaluated"], 5)
        self.assertEqual(result["containment"]["rejected"], 5)
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
                self.assertEqual(
                    pair["candidate_agent_gate_turns"],
                    pair["static_agent_gate_turns"],
                )
                self.assertEqual(
                    pair["candidate_gate_schemas"],
                    pair["static_gate_schemas"],
                )

        live_patch = next(
            pair for pair in result["pairs"] if pair["name"] == "live-patch"
        )
        self.assertEqual(live_patch["candidate_agent_gate_turns"], 2)
        self.assertEqual(
            live_patch["candidate_compensations"],
            [
                {
                    "action": "live_patch_run",
                    "compensation": "live_patch.rollback",
                }
            ],
        )
        build_upgrade = next(
            pair for pair in result["pairs"] if pair["name"] == "build-upgrade"
        )
        self.assertEqual(build_upgrade["candidate_agent_gate_turns"], 3)
        self.assertEqual(
            [item["kind"] for item in build_upgrade["candidate_semantics"]],
            ["action", "gate", "gate", "gate", "action", "action"],
        )
        gate_as_action = next(
            case
            for case in result["containment"]["cases"]
            if case["name"] == "gate-as-action"
        )
        self.assertEqual(gate_as_action["candidate_status"], "rejected")

    def test_planner_does_not_treat_negated_keywords_as_an_upgrade_request(self) -> None:
        objectives = (
            "write firmware release notes; do not build or upgrade",
            "write firmware release notes; don't build or upgrade",
            "write firmware release notes; never build or upgrade",
            "write firmware release notes; no build or upgrade",
            "write firmware release notes without building or upgrading",
        )

        for objective in objectives:
            with self.subTest(objective=objective):
                proposal = plan_for_objective(objective)
                actions = [
                    node.get("action")
                    for node in proposal["nodes"]
                    if isinstance(node, dict) and node.get("kind") == "action"
                ]

                self.assertEqual(actions, ["unsupported.objective"])

    def test_leverage_accepts_either_better_validity_or_fewer_gate_turns(self) -> None:
        self.assertTrue(
            has_demonstrated_leverage(
                static_valid_plan_rate=0.8,
                candidate_valid_plan_rate=1.0,
                static_gate_turns=4,
                candidate_gate_turns=4,
            )
        )
        self.assertTrue(
            has_demonstrated_leverage(
                static_valid_plan_rate=1.0,
                candidate_valid_plan_rate=1.0,
                static_gate_turns=4,
                candidate_gate_turns=3,
            )
        )
        self.assertFalse(
            has_demonstrated_leverage(
                static_valid_plan_rate=1.0,
                candidate_valid_plan_rate=1.0,
                static_gate_turns=4,
                candidate_gate_turns=4,
            )
        )
        self.assertFalse(
            has_demonstrated_leverage(
                static_valid_plan_rate=1.0,
                candidate_valid_plan_rate=0.8,
                static_gate_turns=4,
                candidate_gate_turns=3,
            )
        )


if __name__ == "__main__":
    unittest.main()
