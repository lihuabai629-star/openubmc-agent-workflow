from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = ROOT / "evals" / "trigger-evals.json"
RUNNER_PATH = ROOT / "tests" / "behavior_eval_runner.py"
REQUIRED_COVERAGE = {
    "trigger.structural-positive",
    "trigger.implementation-positive",
    "trigger.natural-source-positive",
    "trigger.generated-chain-positive",
    "trigger.cross-component-positive",
    "trigger.exact-path-judgment-positive",
    "trigger.product-assembly-positive",
    "trigger.tiny-lightweight",
    "trigger.source-exact-negative",
    "trigger.source-mechanical-negative",
    "trigger.mixed-source-build",
    "trigger.build-negative",
    "trigger.runtime-negative",
    "trigger.scaffold-negative",
    "trigger.shared-testing-owner",
    "trigger.deployment-negative",
}


def load_runner():
    spec = importlib.util.spec_from_file_location("behavior_eval_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load behavior evaluation runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TriggerScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
        cls.cases = load_runner().load_cases(EVAL_PATH, None)

    def test_trigger_suite_exercises_automatic_selection(self) -> None:
        self.assertEqual(self.document["schema_version"], 1)
        self.assertEqual(self.document["skill_name"], "openubmc-developer")
        self.assertGreaterEqual(len(self.cases), 15)
        self.assertEqual(len({case["id"] for case in self.cases}), len(self.cases))
        for case in self.cases:
            self.assertNotIn("$openubmc-developer", case["prompt"], case["id"])

    def test_trigger_suite_covers_positive_and_negative_boundaries(self) -> None:
        covered = {
            tag
            for case in self.cases
            for tag in case.get("covers", [])
            if isinstance(tag, str)
        }
        self.assertTrue(REQUIRED_COVERAGE <= covered, sorted(REQUIRED_COVERAGE - covered))

    def test_structural_source_work_requires_developer(self) -> None:
        positive_tags = {
            "trigger.structural-positive",
            "trigger.implementation-positive",
            "trigger.natural-source-positive",
            "trigger.generated-chain-positive",
            "trigger.cross-component-positive",
            "trigger.exact-path-judgment-positive",
            "trigger.product-assembly-positive",
            "trigger.mixed-source-build",
        }
        cases = [
            case
            for case in self.cases
            if positive_tags.intersection(case.get("covers", []))
        ]
        self.assertGreaterEqual(len(cases), 6)
        for case in cases:
            required = set(case["expect"]["skills"].get("required", []))
            self.assertIn("openubmc-developer", required, case["id"])
            allowed = set(case["expect"]["skills"].get("allowed", []))
            if "trigger.mixed-source-build" in case.get("covers", []):
                self.assertEqual(
                    allowed,
                    {"openubmc-developer", "openubmc-build"},
                    case["id"],
                )
            else:
                self.assertEqual(allowed, {"openubmc-developer"}, case["id"])
        implementation = next(
            case
            for case in cases
            if "trigger.implementation-positive" in case.get("covers", [])
        )
        self.assertIs(
            implementation["expect"].get("forbid_exact_command_repeats"), True
        )

    def test_natural_source_trigger_does_not_depend_on_internal_skill_vocabulary(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.natural-source-positive" in case.get("covers", [])
        )
        prompt = case["prompt"].lower()
        for term in (
            "ownership",
            "generator",
            "lifecycle",
            "cross-layer",
            "authored",
        ):
            self.assertNotIn(term, prompt, case["id"])
        self.assertIn(
            "openubmc-developer",
            set(case["expect"]["skills"].get("required", [])),
        )

    def test_exact_path_does_not_bypass_required_repository_judgment(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.exact-path-judgment-positive" in case.get("covers", [])
        )
        self.assertIn("mds/model.json", case["prompt"])
        self.assertIn(
            "openubmc-developer",
            set(case["expect"]["skills"].get("required", [])),
        )
        self.assertEqual(
            set(case["expect"]["skills"].get("allowed", [])),
            {"openubmc-developer"},
        )
        self.assertEqual(case["expect"].get("workspace_changes"), [])

    def test_product_assembly_source_is_developer_work_without_a_build(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.product-assembly-positive" in case.get("covers", [])
        )
        rules = case["expect"]["skills"]
        self.assertIn("openubmc-developer", set(rules.get("required", [])))
        self.assertEqual(set(rules.get("allowed", [])), {"openubmc-developer"})
        self.assertIn("openubmc-build", set(rules.get("forbidden", [])))
        self.assertEqual(case["expect"].get("workspace_changes"), [])

    def test_non_source_stages_forbid_developer_and_select_their_owner(self) -> None:
        negative_tags = {
            "trigger.build-negative",
            "trigger.runtime-negative",
            "trigger.scaffold-negative",
            "trigger.deployment-negative",
        }
        cases = [
            case
            for case in self.cases
            if negative_tags.intersection(case.get("covers", []))
        ]
        for case in cases:
            skill_rules = case["expect"]["skills"]
            self.assertIn(
                "openubmc-developer", set(skill_rules.get("forbidden", [])), case["id"]
            )
            self.assertTrue(skill_rules.get("required"), case["id"])

    def test_tiny_edit_forbids_developer_and_reference_workflow(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.tiny-lightweight" in case.get("covers", [])
        )
        self.assertEqual(case["expect"]["skills"].get("required"), None)
        self.assertEqual(case["expect"]["skills"].get("allowed"), [])
        self.assertIn(
            "openubmc-developer", set(case["expect"]["skills"].get("forbidden", []))
        )
        self.assertEqual(case["expect"]["references"].get("allowed"), [])
        self.assertIs(case["expect"].get("forbid_exact_command_repeats"), True)

    def test_mechanical_source_edit_also_stays_on_the_direct_path(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.source-mechanical-negative" in case.get("covers", [])
        )
        self.assertEqual(case["expect"]["skills"].get("allowed"), [])
        self.assertIn(
            "openubmc-developer",
            set(case["expect"]["skills"].get("forbidden", [])),
        )
        self.assertEqual(case["expect"]["references"].get("allowed"), [])
        self.assertEqual(
            set(case["expect"].get("workspace_changes", [])),
            {"src/defaults.lua"},
        )

    def test_exact_source_edit_stays_on_the_direct_path(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.source-exact-negative" in case.get("covers", [])
        )
        rules = case["expect"]["skills"]
        self.assertEqual(rules.get("allowed"), [])
        self.assertIn("openubmc-developer", set(rules.get("forbidden", [])))
        self.assertEqual(case["expect"]["references"].get("allowed"), [])
        self.assertEqual(
            set(case["expect"].get("workspace_changes", [])),
            {"src/retry_policy.lua", "tests/retry_policy_spec.lua"},
        )
        self.assertIs(case["expect"].get("forbid_exact_command_repeats"), True)

    def test_authored_generation_allows_required_compatibility_references(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.generated-chain-positive" in case.get("covers", [])
        )
        self.assertEqual(
            set(case["expect"]["references"].get("allowed", [])),
            {
                "references/mdb-mds.md",
                "references/persistence-compatibility.md",
            },
        )

    def test_mixed_source_and_build_request_selects_both_stage_owners(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.mixed-source-build" in case.get("covers", [])
        )
        rules = case["expect"]["skills"]
        self.assertEqual(
            set(rules.get("required", [])),
            {"openubmc-developer", "openubmc-build"},
        )
        self.assertEqual(
            set(rules.get("allowed", [])),
            {"openubmc-developer", "openubmc-build"},
        )
        self.assertTrue(
            {"openubmc-upgrade", "openubmc-publish"}
            <= set(rules.get("forbidden", []))
        )

    def test_shared_testing_requires_only_its_owner(self) -> None:
        case = next(
            case
            for case in self.cases
            if "trigger.shared-testing-owner" in case.get("covers", [])
        )
        skill_rules = case["expect"]["skills"]
        self.assertIn("openubmc-dt-testing", set(skill_rules.get("required", [])))
        self.assertEqual(
            set(skill_rules.get("allowed", [])), {"openubmc-dt-testing"}
        )
        self.assertIn("openubmc-developer", set(skill_rules.get("forbidden", [])))
        self.assertEqual(
            set(case["expect"].get("workspace_changes", [])),
            {"test_infra/fixtures.py", "test_infra/runner.py"},
        )
        self.assertTrue(case.get("post_checks"))
        self.assertIs(case["expect"].get("forbid_exact_command_repeats"), True)

if __name__ == "__main__":
    unittest.main()
