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
            "python -m unittest scripts.tests.test_continuous_validation_workflow scripts.tests.test_ci_evidence_archive",
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
        self.assertRegex(checkout["uses"], r"^actions/checkout@[0-9a-f]{40}$")
        python = self.step("Set up Python")
        self.assertRegex(python["uses"], r"^actions/setup-python@[0-9a-f]{40}$")
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
        self.assertRegex(node["uses"], r"^actions/setup-node@[0-9a-f]{40}$")
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

    def test_workflows_pin_actions_and_do_not_persist_checkout_credentials(self) -> None:
        for path in sorted((REPO_ROOT / ".github/workflows").glob("*.yml")):
            workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            for job in workflow["jobs"].values():
                self.assertIn("timeout-minutes", job, path.name)
                for step in job.get("steps", []):
                    action = step.get("uses", "")
                    if action:
                        self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", path.name)
                    if action.startswith("actions/checkout@"):
                        self.assertEqual(step["with"]["persist-credentials"], "false", path.name)

    def test_required_validation_does_not_depend_on_artifact_storage(self) -> None:
        self.assertFalse(any(
            step.get("uses", "").startswith("actions/upload-artifact@")
            for job in self.workflow["jobs"].values() for step in job["steps"]
        ))
        summary = self.step("Summarize plugin qualification")
        self.assertIn("GITHUB_STEP_SUMMARY", summary["run"])
        self.assertEqual(summary["env"]["QUALIFICATION_OUTCOME"], "${{ steps.qualification.outcome }}")

    def test_validation_does_not_request_credentials_or_private_targets(self) -> None:
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for step in self.steps():
            self.assertNotIn("secrets.", str(step))
            self.assertNotIn("BMC_", str(step))
            self.assertNotIn("TARGET_", str(step))

    def test_native_windows_release_gate_is_retired(self):
        release = yaml.load(RELEASE_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertNotIn("windows-plugin", self.workflow["jobs"])
        self.assertNotIn("windows-plugin-gate", release["jobs"])
        self.assertEqual(release["jobs"]["promote"]["needs"], ["release-gate"])


if __name__ == "__main__":
    unittest.main()
