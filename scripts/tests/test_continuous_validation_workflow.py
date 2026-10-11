#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import json
import unittest

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "validate.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
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
        self.assertEqual(set(jobs), {"ci-contract", "validate", "windows-plugin"})
        preflight = jobs["ci-contract"]
        self.assertEqual(preflight["name"], "CI contract preflight")
        self.assertEqual(jobs["validate"]["name"], "Complete repository validation")
        self.assertEqual(jobs["validate"]["needs"], "ci-contract")
        self.assertEqual(jobs["windows-plugin"]["name"], "Windows marketplace bootstrap")
        self.assertEqual(jobs["windows-plugin"]["runs-on"], "windows-2025")
        self.assertEqual(jobs["windows-plugin"]["needs"], "ci-contract")
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
        lines = PYTHON_LOCK.read_text(encoding="utf-8").splitlines()
        pinned = [line for line in lines if line and not line.startswith(" ")]
        self.assertEqual(
            [line.split("==", 1)[0] for line in pinned],
            [
                "attrs", "cffi", "cryptography", "jsonschema",
                "jsonschema-specifications", "PyYAML", "referencing",
                "rpds-py", "typing-extensions", "pycparser",
                "paramiko", "bcrypt", "PyNaCl",
            ],
        )
        self.assertTrue(all("==" in line and line.endswith("\\") for line in pinned))
        self.assertTrue(all(
            line.startswith("    --hash=sha256:")
            and len(line.removeprefix("    --hash=sha256:").rstrip(" \\")) == 64
            for line in lines if line.startswith("    --hash=")
        ))
        self.assertGreaterEqual(sum(line.startswith("    --hash=") for line in lines), len(pinned))
        node = self.step("Set up Node.js")
        self.assertEqual(node["uses"], "actions/setup-node@v7")
        self.assertEqual(node["with"]["node-version"], "22.23.2")
        self.assertEqual(node["with"]["cache"], "npm")
        self.assertEqual(
            node["with"]["cache-dependency-path"],
            "openubmc-kb-mcp/package-lock.json",
        )

    def test_validation_fetches_historical_release_identity(self) -> None:
        for job_name in ("ci-contract", "validate", "windows-plugin"):
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

    def test_windows_ci_and_release_use_one_qualification_entrypoint(self) -> None:
        release = yaml.load(RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        validate_windows = self.workflow["jobs"]["windows-plugin"]
        release_windows = release["jobs"]["windows-plugin-gate"]
        for job in (validate_windows, release_windows):
            commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
            self.assertIn("scripts/qualify_windows_plugin.ps1", commands)
        public_checkout = next(
            step for step in release_windows["steps"]
            if step.get("name") == "Check out public marketplace release"
        )
        self.assertEqual(public_checkout["with"]["repository"], "lihuabai629-star/openubmc-codex-plugins")
        self.assertEqual(public_checkout["with"]["ref"], "${{ needs.release-gate.outputs.release_tag }}")


if __name__ == "__main__":
    unittest.main()
