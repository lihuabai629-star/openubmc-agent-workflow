from __future__ import annotations

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = ROOT / "SKILL.md"
SKILL = SKILL_PATH.read_text(encoding="utf-8")


class SkillContractTests(unittest.TestCase):
    def test_trigger_is_concise_and_selects_natural_source_work(self) -> None:
        match = re.search(r"(?m)^description:\s*(.+)$", SKILL)
        self.assertIsNotNone(match)
        description = match.group(1).strip().strip("\"'")

        self.assertLessEqual(len(description), 330)
        for pattern in (
            r"Use only for unresolved openUBMC decisions",
            r"source ownership, repository extension patterns",
            r"authored/generated chains",
            r"component lifecycle, persistence, or cross-layer behavior",
            r"Exclude specified edits",
            r"documentation-only",
            r"workflows owned by other Skills",
        ):
            self.assertRegex(description, pattern)
        self.assertNotIn("explicitly invokes", description)

    def test_core_workflow_is_adaptive_structure_first_and_small(self) -> None:
        for concept in (
            "lightest safe path",
            "owns the rule or",
            "nearest comparable implementation",
            "extension point",
            "smallest complete change",
            "Review the completed diff",
        ):
            self.assertIn(concept, SKILL)
        self.assertRegex(SKILL, r"observable\s+outcome")

        self.assertLessEqual(len(SKILL.splitlines()), 210)
        self.assertNotIn("Completion criterion:", SKILL)

    def test_straightforward_changes_do_not_require_a_specification_card(self) -> None:
        self.assertIn("straightforward local change", SKILL)
        self.assertIn("proceed directly after inspection", SKILL)
        self.assertIn("without a\n  separate pre-write ceremony", SKILL)
        self.assertIn("use no intermediate planning artifact", SKILL)
        self.assertIn("ordinary direct edit path", SKILL)
        self.assertIn("Avoid rediscovering the same tree", SKILL)
        self.assertIn("new evidence, workspace state, or\n  risk", SKILL)
        self.assertIn("validator named by the user as the primary\n  check", SKILL)
        self.assertIn("sufficient only when its scope can establish", SKILL)
        self.assertIn("add the smallest relevant check", SKILL)
        self.assertIn("Do not\n  probe unrelated toolchains", SKILL)
        self.assertNotIn("validator named by the\n  user is sufficient", SKILL)
        self.assertIn("report briefly", SKILL)

    def test_lightweight_guidance_does_not_encode_eval_operation_counts(self) -> None:
        for retired_limit in (
            "edit once",
            "Probe Git at most once",
            "One initial update",
            "do not add a second verification pass",
        ):
            self.assertNotIn(retired_limit, SKILL)

    def test_explicit_implementation_stops_only_for_a_material_decision(self) -> None:
        self.assertIn("authorize source edits", SKILL)
        self.assertRegex(SKILL, r"does not need a second approval")
        self.assertRegex(SKILL, r"Pause only when repository evidence")
        self.assertIn("required contract or generator", SKILL)
        self.assertRegex(SKILL, r"behavior-owning module or layer")
        self.assertNotIn("Wait for the user's approval", SKILL)

    def test_authored_configuration_and_generated_ownership_are_generic(self) -> None:
        for concept in (
            "human-maintained models",
            "service metadata",
            "manifests",
            "declarative",
            "Change generated behavior through its authored input",
            "when deferred\n  generation is explicitly supported",
            "report the generation gap",
        ):
            self.assertIn(concept, SKILL)
        self.assertIn("never manually synchronize derivatives", SKILL)

    def test_local_tests_stay_with_source_work_until_shared_testing_is_explicit(self) -> None:
        self.assertIn("focused component-local tests and fixtures", SKILL)
        self.assertIn("Component-local testing remains part of this Skill", SKILL)
        self.assertIn("user explicitly requests shared runners", SKILL)
        self.assertIn("openubmc-dt-testing", SKILL)

    def test_worktree_is_conditional_and_has_a_complete_lifecycle(self) -> None:
        self.assertRegex(SKILL, r"Use the current checkout by default")
        self.assertRegex(SKILL, r"Create a linked worktree when")
        self.assertIn("Do not create either for routine local edits", SKILL)
        self.assertIn("solely\nto manufacture a downstream handoff field", SKILL)
        self.assertIn("only when needed to protect existing work", SKILL)
        self.assertIn("Git identity only when it was already observed", SKILL)
        self.assertIn("do not probe Git solely", SKILL)
        self.assertIn("Build workflow decide whether", SKILL)
        self.assertRegex(SKILL, r"Never remove an\s+isolated workspace automatically")
        self.assertIn("absolute\npath, purpose, and retention intent", SKILL)
        self.assertRegex(SKILL, r"Cleanup requires\s+an explicit request")
        self.assertNotIn("A build handoff must name", SKILL)

    def test_references_are_discovered_dynamically_and_linked_once(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        packaged = {
            Path(relative).name
            for relative in manifest["files"]
            if relative.startswith("references/")
        }
        actual = {path.name for path in (ROOT / "references").glob("*.md")}

        self.assertEqual(actual, packaged)
        for name in actual:
            self.assertEqual(SKILL.count(f"references/{name}"), 1, name)

    def test_reference_loading_is_sequential_and_decision_driven(self) -> None:
        self.assertIn("keywords, file types, directories, or language", SKILL)
        self.assertIn("Read one reference at a time", SKILL)
        self.assertIn("task materially involves its\ncontract", SKILL)
        self.assertIn("separate material decision remains", SKILL)
        self.assertIn("Do not open references merely", SKILL)
        self.assertIn(
            "current source and a direct precedent\nalready resolve the decision",
            SKILL,
        )
        self.assertIn(
            "remains unresolved after\n  inspecting current source and direct precedent",
            SKILL,
        )
        self.assertIn("Do not manufacture a reference decision", SKILL)
        self.assertIn("outside the\nrequested outcome", SKILL)

    def test_external_knowledge_is_discovery_not_authority(self) -> None:
        self.assertIn("openUBMC KB only to discover", SKILL)
        self.assertIn("current source or primary documentation", SKILL)
        self.assertRegex(SKILL, r"history\s+and Obsidian records only when")
        self.assertIn("never use them as proof", SKILL)

    def test_source_verification_prefers_real_execution_without_implying_build(self) -> None:
        self.assertIn("smallest sufficient", SKILL)
        self.assertIn("generation, compilation, unit-test builds", SKILL)
        self.assertIn("Prefer executing changed behavior", SKILL)
        self.assertIn("product or\npackage build", SKILL)

    def test_references_do_not_create_second_hop_reference_routing(self) -> None:
        pointer = re.compile(r"references/[a-z0-9-]+\.md")
        for path in (ROOT / "references").glob("*.md"):
            self.assertIsNone(pointer.search(path.read_text(encoding="utf-8")), path.name)

    def test_persistence_reference_avoids_unversioned_historical_mappings(self) -> None:
        text = (ROOT / "references" / "persistence-compatibility.md").read_text(
            encoding="utf-8"
        )
        for stale_detail in (
            "protect_power_off_retain",
            "protect_temporary_retain",
            "observed C++ remote-persistence implementation",
        ):
            self.assertNotIn(stale_detail, text)
        self.assertIn("target branch", text)
        self.assertIn("current source evidence and provenance", text)

    def test_downstream_handoff_contract_is_explicit_and_non_mutating_by_default(self) -> None:
        text = (ROOT / "references" / "downstream-handoffs.md").read_text(
            encoding="utf-8"
        )
        for concept in (
            "absolute source or component root",
            "files and components changed",
            "observed generation state",
            "component-local compilation",
            "requested next stage or sequence",
            "do not create a\nworktree solely for handoff",
            "Build; Build\nproduces the HPM identity consumed by Upgrade",
        ):
            self.assertIn(concept, text)
        self.assertIn("owns\n  build type", text)
        self.assertIn("owns target, artifact verification", text)
        self.assertNotIn("each component root must", text)
        self.assertNotIn("Pass the exact local artifact", text)
        self.assertIn("does not authorize", SKILL)
        self.assertIn("user explicitly asks to execute", SKILL)

    def test_optional_workflow_state_is_not_a_developer_responsibility(self) -> None:
        for coupling in (
            "Target Runtime Case",
            "phase_record",
            "workflow.advance",
            "phase_type=developer.change",
        ):
            self.assertNotIn(coupling, SKILL)

    def test_profile_schema_reference_requires_target_version_evidence(self) -> None:
        text = (ROOT / "references" / "profile-schema-import-export.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Confirm from target-version source", text)
        self.assertIn("Do not\nassume a read-only directory", text)
        self.assertNotIn("are installed into a read-only product directory", text)

    def test_named_skills_are_only_explicit_testing_or_lifecycle_relationships(self) -> None:
        named = set(re.findall(r"`(openubmc-[a-z0-9-]+)`", SKILL))
        self.assertEqual(
            named,
            {
                "openubmc-dt-testing",
                "openubmc-build",
                "openubmc-live-patch",
                "openubmc-upgrade",
                "openubmc-publish",
            },
        )

    def test_reusable_domain_references_are_packaged_and_routed(self) -> None:
        expected = {
            "hardware-vpd.md": ("bounded byte protocol", "immutable published snapshot"),
            "sr-dds-product-records.md": ("selection chain", "inheritance"),
            "profile-schema-import-export.md": ("public contract", "sensitive values"),
        }
        for name, concepts in expected.items():
            path = ROOT / "references" / name
            self.assertTrue(path.is_file(), name)
            text = path.read_text(encoding="utf-8")
            for concept in concepts:
                self.assertIn(concept, text, name)
            self.assertEqual(SKILL.count(f"references/{name}"), 1, name)

    def test_internal_eval_runner_is_not_packaged(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "tests" / "behavior_eval_runner.py").is_file())
        self.assertNotIn("tests/behavior_eval_runner.py", manifest["files"])


if __name__ == "__main__":
    unittest.main()
