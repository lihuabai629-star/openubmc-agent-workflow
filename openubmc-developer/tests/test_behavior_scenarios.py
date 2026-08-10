from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = ROOT / "evals" / "evals.json"
RUNNER_PATH = ROOT / "tests" / "behavior_eval_runner.py"
CORE_COVERAGE = {
    "authorization.read-only",
    "authorization.explicit-implementation",
    "authorization.material-blocker",
    "authorization.downstream-explicit-only",
    "scope.adjacent-report-only",
    "workspace.dirty-preservation",
    "workspace.handoff-retention",
    "evidence.conflict",
    "structure.responsible-layer",
    "structure.similar-implementation",
    "structure.existing-extension-point",
    "structure.cross-component",
    "structure.single-writer",
    "structure.test-entry-discovery",
    "generation.authored-input",
    "generation.missing-generator-stop",
    "generation.semantic-completeness",
    "compatibility.upgrade-rollback",
    "verification.focused",
    "verification.real-compile",
    "verification.real-runtime",
    "lifecycle.build-handoff",
    "lifecycle.build-upgrade-chain",
    "workflow.lightweight",
    "domain.hardware-vpd",
    "domain.sr-dds-product-records",
    "domain.profile-schema-import-export",
}


def load_runner():
    spec = importlib.util.spec_from_file_location("behavior_eval_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load behavior evaluation runner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_changes(case: dict) -> set[str]:
    value = case["expect"].get("workspace_changes", [])
    if isinstance(value, list):
        return set(value)
    return set(value.get("required", []))


class BehaviorScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
        cls.cases = load_runner().load_cases(EVAL_PATH, None)

    def test_schema_and_case_identity_are_valid(self) -> None:
        self.assertEqual(self.document["schema_version"], 1)
        self.assertEqual(self.document["skill_name"], "openubmc-developer")
        self.assertGreaterEqual(len(self.cases), 15)
        self.assertEqual(len({case["id"] for case in self.cases}), len(self.cases))

    def test_scenarios_cover_the_core_development_capabilities(self) -> None:
        covered = {
            tag
            for case in self.cases
            for tag in case.get("covers", [])
            if isinstance(tag, str)
        }
        self.assertTrue(CORE_COVERAGE <= covered, sorted(CORE_COVERAGE - covered))

    def test_implementation_is_the_majority_without_prescribing_fixture_size(self) -> None:
        implementation = [case for case in self.cases if expected_changes(case)]
        read_only = [case for case in self.cases if not expected_changes(case)]

        self.assertGreaterEqual(len(implementation), 10)
        self.assertGreater(len(implementation), len(read_only))

    def test_implementation_scenarios_have_executable_evidence(self) -> None:
        for case in self.cases:
            if not expected_changes(case):
                continue
            self.assertTrue(case.get("post_checks"), case["id"])
            self.assertTrue(case["expect"].get("command_evidence"), case["id"])

    def test_component_local_implementation_keeps_local_test_ownership(self) -> None:
        implementation = [case for case in self.cases if expected_changes(case)]
        for case in implementation:
            forbidden = set(case["expect"].get("skills", {}).get("forbidden", []))
            self.assertIn("openubmc-dt-testing", forbidden, case["id"])

    def test_structure_scenarios_supply_discovery_context_without_prescribing_paths(
        self,
    ) -> None:
        discovery_tags = {
            "structure.responsible-layer",
            "structure.existing-extension-point",
            "structure.single-writer",
            "structure.test-entry-discovery",
            "structure.product-assembly",
        }
        cases = [
            case
            for case in self.cases
            if discovery_tags.intersection(case.get("covers", []))
        ]

        self.assertGreaterEqual(len(cases), 5)
        prescribed_path = re.compile(
            r"(?:src|components|products|mds|tests)/[A-Za-z0-9_.-]+"
        )
        for case in cases:
            self.assertIsNone(prescribed_path.search(case["prompt"]), case["id"])
            files = case["workspace"].get("files", {})
            self.assertIn("AGENTS.md", files, case["id"])
            self.assertGreaterEqual(len(files), 5, case["id"])
            self.assertTrue(case.get("post_checks"), case["id"])

    def test_changed_files_represent_source_work_not_only_handoffs(self) -> None:
        source_prefixes = ("src/", "components/", "mds/", "products/", "generated/")
        source_cases = [
            case
            for case in self.cases
            if expected_changes(case)
            and any(path.startswith(source_prefixes) for path in expected_changes(case))
        ]
        self.assertGreaterEqual(len(source_cases), 10)

    def test_read_only_and_material_blocker_cases_do_not_edit(self) -> None:
        guarded = [
            case
            for case in self.cases
            if {"authorization.read-only", "authorization.material-blocker"}.intersection(
                case.get("covers", [])
            )
        ]

        self.assertGreaterEqual(len(guarded), 4)
        for case in guarded:
            self.assertEqual(expected_changes(case), set(), case["id"])

    def test_high_risk_implementation_uses_complete_evidence_not_a_count_lock(self) -> None:
        cases = [
            case
            for case in self.cases
            if "authorization.explicit-high-risk-implementation" in case.get("covers", [])
        ]
        self.assertGreaterEqual(len(cases), 1)
        for case in cases:
            self.assertTrue(expected_changes(case), case["id"])
            self.assertTrue(case.get("post_checks"), case["id"])
            forbidden_messages = case["expect"].get("messages", {}).get(
                "forbidden_patterns", []
            )
            self.assertTrue(
                any("批准门" in pattern for pattern in forbidden_messages), case["id"]
            )

    def test_missing_generator_cases_stop_for_concrete_evidence_gaps(self) -> None:
        cases = [
            case
            for case in self.cases
            if "generation.missing-generator-stop" in case.get("covers", [])
        ]
        self.assertGreaterEqual(len(cases), 1)
        for case in cases:
            self.assertEqual(expected_changes(case), set(), case["id"])
            forbidden_messages = case["expect"].get("messages", {}).get(
                "forbidden_patterns", []
            )
            self.assertTrue(any("手工" in pattern for pattern in forbidden_messages))
            self.assertTrue(any("批准门" in pattern for pattern in forbidden_messages))

    def test_reference_use_is_progressive_and_material(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        packaged = {
            relative for relative in manifest["files"] if relative.startswith("references/")
        }
        required = {
            reference
            for case in self.cases
            for reference in case["expect"].get("references", {}).get("required", [])
        }

        self.assertTrue(required <= packaged)
        self.assertGreaterEqual(len(required), 4)
        self.assertLess(len(required), len(packaged))
        self.assertGreaterEqual(
            sum(
                not case["expect"].get("references", {}).get("required")
                for case in self.cases
            ),
            len(self.cases) // 2,
        )

    def test_reference_allowlists_are_not_a_global_constraint(self) -> None:
        unrestricted = [
            case
            for case in self.cases
            if "allowed" not in case["expect"].get("references", {})
        ]
        self.assertGreaterEqual(len(unrestricted), len(self.cases) // 3)

    def test_git_scenarios_are_justified_by_workspace_capabilities(self) -> None:
        git_cases = [case for case in self.cases if "git" in case["workspace"]]
        self.assertGreaterEqual(len(git_cases), 2)
        for case in git_cases:
            covers = set(case.get("covers", []))
            self.assertTrue(
                {"workspace.dirty-preservation", "workspace.handoff-retention"}
                & covers,
                case["id"],
            )

    def test_source_only_work_does_not_imply_downstream_execution(self) -> None:
        cases = [
            case
            for case in self.cases
            if "authorization.downstream-explicit-only" in case.get("covers", [])
            and "lifecycle.build-handoff" not in case.get("covers", [])
        ]
        self.assertGreaterEqual(len(cases), 1)
        self.assertTrue(
            any(
                {
                    "openubmc-build",
                    "openubmc-live-patch",
                    "openubmc-upgrade",
                    "openubmc-publish",
                }
                <= set(case["expect"]["skills"].get("forbidden", []))
                for case in cases
            )
        )

    def test_build_handoff_passes_source_facts_without_forcing_git_identity(self) -> None:
        cases = [
            case
            for case in self.cases
            if "lifecycle.build-handoff" in case.get("covers", [])
        ]
        self.assertGreaterEqual(len(cases), 1)
        for case in cases:
            self.assertIn("git", case["workspace"], case["id"])
            required_references = set(
                case["expect"].get("references", {}).get("required", [])
            )
            self.assertIn(
                "references/downstream-handoffs.md", required_references, case["id"]
            )
            self.assertIsNot(case["expect"].get("git_head_disclosed"), True)
            forbidden_commands = case["expect"].get("commands", {}).get(
                "forbidden_patterns", []
            )
            self.assertTrue(
                any("worktree" in pattern and "clone" in pattern for pattern in forbidden_commands),
                case["id"],
            )
            message_patterns = case["expect"].get("messages", {}).get(
                "required_patterns", []
            )
            self.assertTrue(any("保留" in pattern for pattern in message_patterns))
            self.assertTrue(any("openubmc-upgrade" in pattern for pattern in message_patterns))

    def test_suite_contains_real_compilation_and_runtime_execution(self) -> None:
        cases = [
            case
            for case in self.cases
            if {"verification.real-compile", "verification.real-runtime"}
            <= set(case.get("covers", []))
        ]
        self.assertGreaterEqual(len(cases), 1)
        for case in cases:
            fixture_text = "\n".join(
                specification
                if isinstance(specification, str)
                else specification["content"]
                for specification in case["workspace"].get("files", {}).values()
            )
            self.assertIn("'g++'", fixture_text)
            self.assertIn("subprocess.run([str(binary)]", fixture_text)
            self.assertTrue(case.get("post_checks"), case["id"])

    def test_lightweight_cases_use_qualitative_efficiency_guards(self) -> None:
        cases = [
            case
            for case in self.cases
            if "workflow.lightweight" in case.get("covers", [])
        ]
        self.assertGreaterEqual(len(cases), 3)
        for case in cases:
            self.assertNotIn("max_commands", case["expect"], case["id"])
            self.assertNotIn("max_agent_messages", case["expect"], case["id"])
            self.assertIs(
                case["expect"].get("forbid_exact_command_repeats"),
                True,
                case["id"],
            )
            self.assertIs(
                case["expect"].get("forbid_redundant_unchanged_reads"),
                True,
                case["id"],
            )
            forbidden = case["expect"].get("messages", {}).get(
                "forbidden_patterns", []
            )
            self.assertTrue(any("规格卡" in pattern for pattern in forbidden), case["id"])
            self.assertEqual(
                case["expect"].get("messages", {}).get("scope"),
                "after_target_skill_read",
                case["id"],
            )

    def test_fixtures_are_portable_and_self_contained(self) -> None:
        for case in self.cases:
            workspace = case["workspace"]
            self.assertNotIn("copy_from", workspace)
            for relative in workspace.get("files", {}):
                self.assertFalse(Path(relative).is_absolute(), f"{case['id']}:{relative}")
            self.assertNotRegex(case["prompt"], r"https?://|/mnt/|[A-Za-z]:\\")

if __name__ == "__main__":
    unittest.main()
