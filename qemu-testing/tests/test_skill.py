import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class QemuTestingSkillTests(unittest.TestCase):
    def setUp(self):
        self.skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.metadata = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        self.docs = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [ROOT / "SKILL.md", *sorted((ROOT / "references").glob("*.md"))]
        )

    def test_owner_covers_identity_and_smoke_classification(self):
        for phrase in (
            "launcher",
            "PID identity",
            "serial",
            "port mapping",
            "image identity",
            "smoke classification",
        ):
            self.assertIn(phrase, self.docs)

    def test_unsafe_shortcuts_are_absent(self):
        for forbidden in ("killall qemu", "pkill qemu", "curl -k", "--insecure", "/tmp/qemu"):
            self.assertNotIn(forbidden, self.docs)

    def test_metadata_and_references_are_current(self):
        self.assertNotIn("contractCompatibility", self.metadata)
        self.assertIn("agents/openai.yaml", self.metadata["files"])
        self.assertEqual([item for item in self.metadata["files"] if not (ROOT / item).is_file()], [])
        targets = set(re.findall(r"`(references/[^`]+\.md)`", self.skill))
        self.assertEqual({target for target in targets if not (ROOT / target).is_file()}, set())


if __name__ == "__main__":
    unittest.main()
