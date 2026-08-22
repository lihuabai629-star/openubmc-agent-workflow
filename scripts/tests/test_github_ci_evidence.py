from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "github_ci_evidence.py"
SPEC = importlib.util.spec_from_file_location("openubmc_github_ci_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
github_ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(github_ci)


class GitHubCiEvidenceTests(unittest.TestCase):
    @staticmethod
    def workflow_runs(*, check_suite_id: int = 10) -> dict[str, object]:
        return {
            "workflow_runs": [
                {
                    "id": 20,
                    "workflow_id": 30,
                    "name": "Workflow validation",
                    "path": ".github/workflows/validate.yml",
                    "event": "pull_request",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "check_suite_id": check_suite_id,
                }
            ]
        }

    def test_required_checks_must_succeed_on_exact_candidate_commit(self) -> None:
        payload = {
            "check_runs": [
                {
                    "id": 1,
                    "name": "CI contract preflight",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://example/check/1",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://example/check/2",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
            ]
        }

        evidence = github_ci.evaluate_check_runs(
            payload,
            self.workflow_runs(),
            repository="owner/repo",
            commit="candidate",
        )

        self.assertTrue(evidence["promotable"])
        self.assertEqual(
            [item["name"] for item in evidence["required_checks"]],
            list(github_ci.REQUIRED_CHECKS),
        )

    def test_latest_failed_or_wrong_commit_check_blocks_promotion(self) -> None:
        payload = {
            "check_runs": [
                {
                    "id": 1,
                    "name": "CI contract preflight",
                    "head_sha": "other",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "failure",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
            ]
        }

        evidence = github_ci.evaluate_check_runs(
            payload,
            self.workflow_runs(),
            repository="owner/repo",
            commit="candidate",
        )

        self.assertFalse(evidence["promotable"])
        self.assertEqual(
            [item["status"] for item in evidence["required_checks"]],
            ["missing", "failed"],
        )

    def test_untrusted_app_cannot_replace_the_github_actions_check(self) -> None:
        payload = {
            "check_runs": [
                {
                    "id": 1,
                    "name": "CI contract preflight",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "failure",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
                {
                    "id": 99,
                    "name": "CI contract preflight",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 99999, "slug": "untrusted-checks"},
                    "check_suite": {"id": 10},
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
            ]
        }

        evidence = github_ci.evaluate_check_runs(
            payload,
            self.workflow_runs(),
            repository="owner/repo",
            commit="candidate",
        )

        self.assertFalse(evidence["promotable"])
        self.assertEqual(
            [item["status"] for item in evidence["required_checks"]],
            ["failed", "passed"],
        )

    def test_same_app_check_from_another_workflow_cannot_replace_ci(self) -> None:
        payload = {
            "check_runs": [
                {
                    "id": 1,
                    "name": "CI contract preflight",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "failure",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
                {
                    "id": 99,
                    "name": "CI contract preflight",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 11},
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
            ]
        }
        workflow_runs = self.workflow_runs()
        workflow_runs["workflow_runs"].append(
            {
                "id": 21,
                "workflow_id": 31,
                "name": "Impersonating workflow",
                "path": ".github/workflows/other.yml",
                "event": "pull_request",
                "head_sha": "candidate",
                "status": "completed",
                "conclusion": "success",
                "check_suite_id": 11,
            }
        )

        evidence = github_ci.evaluate_check_runs(
            payload,
            workflow_runs,
            repository="owner/repo",
            commit="candidate",
        )

        self.assertFalse(evidence["promotable"])
        self.assertEqual(
            [item["status"] for item in evidence["required_checks"]],
            ["failed", "passed"],
        )

    def test_pending_canonical_workflow_is_pending_instead_of_missing(self) -> None:
        payload = {
            "check_runs": [
                {
                    "id": 1,
                    "name": "CI contract preflight",
                    "head_sha": "candidate",
                    "status": "in_progress",
                    "conclusion": None,
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "queued",
                    "conclusion": None,
                    "app": {"id": 15368, "slug": "github-actions"},
                    "check_suite": {"id": 10},
                },
            ]
        }
        workflow_runs = self.workflow_runs()
        workflow_runs["workflow_runs"][0]["status"] = "in_progress"
        workflow_runs["workflow_runs"][0]["conclusion"] = None

        evidence = github_ci.evaluate_check_runs(
            payload,
            workflow_runs,
            repository="owner/repo",
            commit="candidate",
        )

        self.assertFalse(evidence["promotable"])
        self.assertEqual(
            [item["status"] for item in evidence["required_checks"]],
            ["pending", "pending"],
        )

    def test_collection_uses_github_check_runs_api(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(command, **kwargs):
            calls.append(tuple(command))
            payload = self.workflow_runs()["workflow_runs"] if any(
                "actions/runs" in part for part in command
            ) else []
            return subprocess.CompletedProcess(
                command,
                0,
                "\n".join(json.dumps(item) for item in payload),
                "",
            )

        evidence = github_ci.collect_evidence(
            repository="owner/repo",
            commit="candidate",
            runner=run,
        )

        self.assertFalse(evidence["promotable"])
        self.assertEqual(calls[0][0:2], ("gh", "api"))
        self.assertIn("repos/owner/repo/commits/candidate/check-runs", calls[0])
        self.assertIn("repos/owner/repo/actions/runs", calls[1])

    def test_collection_merges_every_paginated_api_item(self) -> None:
        calls: list[tuple[str, ...]] = []
        checks = [
            {
                "id": index,
                "name": name,
                "head_sha": "candidate",
                "status": "completed",
                "conclusion": "success",
                "html_url": f"https://example/check/{index}",
                "app": {"id": 15368, "slug": "github-actions"},
                "check_suite": {"id": 10},
            }
            for index, name in enumerate(github_ci.REQUIRED_CHECKS, 101)
        ]
        workflow = self.workflow_runs()["workflow_runs"][0]

        def run(command, **kwargs):
            calls.append(tuple(command))
            items = [workflow] if any("actions/runs" in part for part in command) else checks
            return subprocess.CompletedProcess(
                command,
                0,
                "\n".join(json.dumps(item) for item in items) + "\n",
                "",
            )

        evidence = github_ci.collect_evidence(
            repository="owner/repo",
            commit="candidate",
            runner=run,
        )

        self.assertTrue(evidence["promotable"], evidence)
        for call, selector in zip(calls, (".check_runs[]", ".workflow_runs[]")):
            self.assertIn("--paginate", call)
            self.assertIn("--jq", call)
            self.assertIn(selector, call)


if __name__ == "__main__":
    unittest.main()
