#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = REPO_ROOT / "scripts" / "validate_workflow.py"
SPEC = importlib.util.spec_from_file_location("openubmc_workflow_validator", VALIDATOR)
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class WorkflowManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        skill = self.root / "example"
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: example-skill\n"
            "description: Example release contract fixture.\n"
            "---\n",
            encoding="utf-8",
        )
        (skill / "agents" / "openai.yaml").write_text(
            "interface:\n"
            "  display_name: 'Example'\n"
            "  short_description: 'Example fixture'\n"
            "  default_prompt: 'Use $example-skill.'\n",
            encoding="utf-8",
        )
        (self.root / "workflow.json").write_text(
            json.dumps(
                {
                    "skills": [
                        {"name": "example-skill", "path": "example"},
                    ],
                    "profiles": {
                        "full": ["example-skill"],
                        "target-runtime": ["example-skill"],
                    },
                }
            ),
            encoding="utf-8",
        )
        installer = self.root / "openubmc-environment-setup" / "scripts"
        installer.mkdir(parents=True)
        (installer / "install_environment.py").write_text(
            "SKILL_BUNDLE = ((\"example-skill\", \"example\"),)\n"
            "TARGET_RUNTIME_SKILL_NAMES = frozenset({\"example-skill\"})\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, *files: str) -> None:
        (self.root / "example" / "skill.json").write_text(
            json.dumps(
                {
                    "manifestVersion": 1,
                    "name": "example-skill",
                    "files": [
                        "SKILL.md",
                        "skill.json",
                        "agents/openai.yaml",
                        *files,
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_every_installable_skill_requires_a_manifest(self) -> None:
        with (
            mock.patch.object(validator, "ROOT", self.root),
            self.assertRaisesRegex(
                SystemExit,
                r"missing skill\.json: example",
            ),
        ):
            validator.validate_manifest()

    def test_manifest_rejects_an_unlisted_runtime_file(self) -> None:
        self.write_manifest()
        scripts = self.root / "example" / "scripts"
        scripts.mkdir()
        (scripts / "required_helper.py").write_text(
            "print('required')\n",
            encoding="utf-8",
        )

        with (
            mock.patch.object(validator, "ROOT", self.root),
            self.assertRaisesRegex(
                SystemExit,
                r"skill\.json omits package files \(scripts/required_helper\.py\): example/skill\.json",
            ),
        ):
            validator.validate_manifest()

    def test_manifest_rejects_duplicate_and_out_of_root_entries(self) -> None:
        for extra, message in (
            ("SKILL.md", "duplicate skill.json files entry"),
            ("../workflow.json", "invalid skill.json file path"),
        ):
            with self.subTest(extra=extra):
                self.write_manifest(extra)
                with (
                    mock.patch.object(validator, "ROOT", self.root),
                    self.assertRaisesRegex(SystemExit, message),
                ):
                    validator.validate_manifest()


class WorkflowStageReportingTests(unittest.TestCase):
    def test_full_validation_labels_each_python_root_and_node_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "alpha/tests/test_alpha.py",
                "beta/tests/test_beta.py",
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            (root / "openubmc-kb-mcp").mkdir()

            with (
                mock.patch.object(validator, "ROOT", root),
                mock.patch.object(validator, "validate_manifest", return_value={}),
                mock.patch.object(validator, "validate_release_metadata"),
                mock.patch.object(validator, "run") as run,
            ):
                self.assertEqual(validator.main([]), 0)

        self.assertEqual(
            [call.kwargs["stage"] for call in run.call_args_list],
            [
                "Python compile",
                "Node dependencies: openubmc-kb-mcp",
                "Python tests: alpha/tests",
                "Python tests: beta/tests",
                "Node tests: openubmc-kb-mcp",
                "Node syntax: openubmc-kb-mcp",
            ],
        )


if __name__ == "__main__":
    unittest.main()
