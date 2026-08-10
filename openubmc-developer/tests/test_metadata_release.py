from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RELATED = {
    "openubmc-build",
    "openubmc-live-patch",
    "openubmc-upgrade",
    "openubmc-publish",
}


def discover_packaged_files() -> set[str]:
    files = {"SKILL.md", "skill.json", "agents/openai.yaml"}
    files.update(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "evals").glob("*.json")
        if path.is_file()
    )
    files.update(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "references").glob("*")
        if path.is_file()
    )
    return files


class MetadataReleaseTests(unittest.TestCase):
    def test_manifest_matches_the_runtime_package(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "openubmc-developer")
        self.assertEqual(manifest["version"], "5.5.0")
        self.assertEqual(set(manifest["files"]), discover_packaged_files())
        self.assertEqual(len(manifest["files"]), len(set(manifest["files"])))
        for relative in manifest["files"]:
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_internal_eval_runner_is_not_published(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "tests" / "behavior_eval_runner.py").is_file())
        self.assertNotIn("tests/behavior_eval_runner.py", manifest["files"])

    def test_metadata_focuses_on_structural_behavior_work(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        agent = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))

        self.assertIn("Use only for unresolved openUBMC decisions", skill)
        self.assertIn("Exclude specified edits", skill)
        self.assertIn("unresolved openUBMC decisions", agent)
        self.assertIn("repository extension patterns", agent)
        self.assertIn("allow_implicit_invocation: true", agent)
        self.assertIn("Automatically analyze and change", manifest["description"])
        self.assertIn("openUBMC source", manifest["description"])
        self.assertIn("source ownership", manifest["description"])
        self.assertIn("documentation-only", manifest["description"])
        self.assertIn("code-structure", manifest["keywords"])

    def test_only_passive_lifecycle_relationships_are_declared(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["relatedSkills"]), EXPECTED_RELATED)

    def test_keywords_are_not_a_domain_or_routing_menu(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        keywords = set(manifest["keywords"])
        for removed in (
            "lua",
            "mdb",
            "hardware-vpd",
            "driver-abi",
            "profile-schema",
            "worktree",
            "webui",
        ):
            self.assertNotIn(removed, keywords)

    def test_openai_metadata_is_short_and_invokes_the_skill(self) -> None:
        agent = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$openubmc-developer", agent)
        self.assertIn("unresolved openUBMC decisions", agent)
        self.assertIn("allow_implicit_invocation: true", agent)
        self.assertNotIn("explicitly request", agent)

    def test_retired_protocol_terms_are_absent_from_packaged_text(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        combined = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in manifest["files"]
            if Path(relative).suffix in {".md", ".json", ".yaml", ".py"}
        )
        for term in (
            "openubmc-developer.v2",
            "openubmc-developer.v3",
            "Change Closure",
            "Proof Ledger",
            "development_path",
            "path_status",
        ):
            self.assertNotIn(term, combined)
        for removed_domain in ("aspeed", "qemu"):
            self.assertNotIn(removed_domain, combined.lower())


if __name__ == "__main__":
    unittest.main()
