from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")


class SkillProgressiveDisclosureTests(unittest.TestCase):
    def test_entrypoint_is_small_and_keeps_the_core_decisions(self) -> None:
        self.assertLessEqual(len(SKILL.encode("utf-8")), 10 * 1024)
        self.assertLessEqual(len(SKILL.splitlines()), 180)
        for concept in (
            "observe",
            "execute",
            "two to four complementary",
            "source-only",
            "live-patch",
            "build-upgrade",
            "strongest evidenced owner",
            "closeout_markdown",
            "Keep all remote actions read-only",
        ):
            self.assertIn(concept, SKILL)

    def test_transport_and_runtime_mechanics_are_disclosed_on_demand(self) -> None:
        for detail in (
            "--mdb-concurrency",
            "TaskContext",
            "bounded LRU",
            "/bmc/kepler/Systems/1/Events",
            "workflow.advance",
            "phase_record",
        ):
            self.assertNotIn(detail, SKILL)
        self.assertIn("references/agent-gateway.md", SKILL)
        self.assertIn("references/remote-automation.md", SKILL)

    def test_every_packaged_reference_is_discoverable_once(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        references = {
            relative
            for relative in manifest["files"]
            if relative.startswith("references/")
        }
        self.assertTrue(references)
        for relative in references:
            self.assertEqual(SKILL.count(relative), 1, relative)

    def test_reference_documents_do_not_create_second_hop_routing(self) -> None:
        manifest = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        packaged_names = {
            Path(relative).name
            for relative in manifest["files"]
            if relative.startswith("references/")
        }
        for path in (ROOT / "references").glob("*.md"):
            content = path.read_text(encoding="utf-8")
            second_hops = sorted(
                name for name in packaged_names - {path.name} if name in content
            )
            self.assertEqual(second_hops, [], path.name)


if __name__ == "__main__":
    unittest.main()
