import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LuaComponentSkillTests(unittest.TestCase):
    def test_is_an_explicit_compatibility_entry(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        interface = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", interface)
        self.assertIn("openubmc-developer", skill)
        self.assertIn("references/lua-component.md", skill)
        self.assertNotIn("openubmc-mdb-interface-dev", skill)

    def test_metadata_packages_only_the_wrapper(self):
        metadata = json.loads((ROOT / "skill.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["relatedSkills"], ["openubmc-developer"])
        self.assertNotIn("references/lua-implementation.md", metadata["files"])
        self.assertEqual([item for item in metadata.get("files", []) if not (ROOT / item).is_file()], [])


if __name__ == "__main__":
    unittest.main()
