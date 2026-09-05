#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
PYTHON_LOCK = REPO_ROOT / "requirements-ci.lock"
NODE_PACKAGE = REPO_ROOT / "openubmc-kb-mcp" / "package.json"
NODE_LOCK = REPO_ROOT / "openubmc-kb-mcp" / "package-lock.json"


class ContinuousValidationWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = yaml.load(
            WORKFLOW.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )

    def steps(self) -> list[dict[str, object]]:
        return self.workflow["jobs"]["validate"]["steps"]

    def step(self, name: str) -> dict[str, object]:
        return next(step for step in self.steps() if step.get("name") == name)

    def test_contract_preflight_is_a_separate_required_check(self) -> None:
        jobs = self.workflow["jobs"]
        self.assertEqual(set(jobs), {"ci-contract", "validate"})
        preflight = jobs["ci-contract"]
        self.assertEqual(preflight["name"], "CI contract preflight")
        self.assertEqual(jobs["validate"]["name"], "Complete repository validation")
        self.assertEqual(jobs["validate"]["needs"], "ci-contract")
        self.assertEqual(
            next(
                step["run"]
                for step in preflight["steps"]
                if step.get("name") == "Validate CI contract"
            ),
            "python -m unittest scripts.tests.test_continuous_validation_workflow",
        )

    def test_pull_requests_and_main_pushes_run_validation(self) -> None:
        triggers = self.workflow["on"]
        self.assertIn("pull_request", triggers)
        self.assertEqual(triggers["push"]["branches"], ["main"])
        self.assertEqual(
            self.step("Run complete repository validation")["run"],
            "python scripts/validate_workflow.py",
        )

    def test_stale_runs_are_cancelled_per_branch_or_pull_request(self) -> None:
        concurrency = self.workflow["concurrency"]
        self.assertEqual(
            concurrency["group"],
            "workflow-validation-${{ github.workflow }}-"
            "${{ github.event.pull_request.number || github.ref }}",
        )
        self.assertEqual(concurrency["cancel-in-progress"], "true")

    def test_toolchains_and_dependency_locks_are_explicit(self) -> None:
        checkout = next(
            step for step in self.steps() if step.get("name") == "Check out repository"
        )
        self.assertEqual(checkout["uses"], "actions/checkout@v7")
        python = self.step("Set up Python")
        self.assertEqual(python["uses"], "actions/setup-python@v7")
        self.assertEqual(python["with"]["python-version"], "3.12.13")
        self.assertEqual(
            self.step("Install locked Python validation dependencies")["run"],
            "python -m pip install --only-binary=:all: --require-hashes "
            "--requirement requirements-ci.lock",
        )
        self.assertEqual(
            PYTHON_LOCK.read_text(encoding="utf-8"),
            'attrs==26.1.0 \\\n'
            '    --hash=sha256:c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309\n'
            'cffi==2.1.1 \\\n'
            '    --hash=sha256:c1453022f490d2459a11819d83ad1d586e9ff65a12ac3e705ffebd46d3685dcf\n'
            'cryptography==46.0.5 \\\n'
            '    --hash=sha256:4c3341037c136030cb46e4b1e17b7418ea4cbd9dd207e4a6f3b2b24e0d4ac731\n'
            'jsonschema==4.26.0 \\\n'
            '    --hash=sha256:d489f15263b8d200f8387e64b4c3a75f06629559fb73deb8fdfb525f2dab50ce\n'
            'jsonschema-specifications==2025.9.1 \\\n'
            '    --hash=sha256:98802fee3a11ee76ecaca44429fda8a41bff98b00a0f2838151b113f210cc6fe\n'
            'PyYAML==6.0.3 \\\n'
            '    --hash=sha256:ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc\n'
            'referencing==0.37.0 \\\n'
            '    --hash=sha256:381329a9f99628c9069361716891d34ad94af76e461dcb0335825aecc7692231\n'
            'rpds-py==2026.6.3 \\\n'
            '    --hash=sha256:ecabd69db66de867690f9797f2f8fa27ba501bbc24540cbdbdc649cd15888ba6\n'
            'typing-extensions==4.16.0 \\\n'
            '    --hash=sha256:481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8\n'
            'pycparser==3.0 \\\n'
            '    --hash=sha256:b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992\n',
        )
        node = self.step("Set up Node.js")
        self.assertEqual(node["uses"], "actions/setup-node@v7")
        self.assertEqual(node["with"]["node-version"], "22.23.2")
        self.assertEqual(node["with"]["cache"], "npm")
        self.assertEqual(
            node["with"]["cache-dependency-path"],
            "openubmc-kb-mcp/package-lock.json",
        )

    def test_validation_fetches_historical_release_identity(self) -> None:
        for job_name in ("ci-contract", "validate"):
            checkout = next(
                step
                for step in self.workflow["jobs"][job_name]["steps"]
                if step.get("name") == "Check out repository"
            )
            self.assertEqual(checkout["with"]["fetch-depth"], "0")

    def test_codex_process_probe_dependency_is_locked(self) -> None:
        package = json.loads(NODE_PACKAGE.read_text(encoding="utf-8"))
        lock = json.loads(NODE_LOCK.read_text(encoding="utf-8"))

        self.assertEqual(package["devDependencies"]["@openai/codex"], "0.151.0")
        self.assertEqual(
            lock["packages"][""]["devDependencies"]["@openai/codex"],
            "0.151.0",
        )
        self.assertEqual(
            lock["packages"]["node_modules/@openai/codex"]["version"],
            "0.151.0",
        )

    def test_validation_does_not_request_credentials_or_private_targets(self) -> None:
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for step in self.steps():
            self.assertNotIn("secrets.", str(step))
            self.assertNotIn("BMC_", str(step))
            self.assertNotIn("TARGET_", str(step))


if __name__ == "__main__":
    unittest.main()
