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
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://example/check/2",
                },
            ]
        }

        evidence = github_ci.evaluate_check_runs(
            payload,
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
                },
                {
                    "id": 2,
                    "name": "Complete repository validation",
                    "head_sha": "candidate",
                    "status": "completed",
                    "conclusion": "failure",
                },
            ]
        }

        evidence = github_ci.evaluate_check_runs(
            payload,
            repository="owner/repo",
            commit="candidate",
        )

        self.assertFalse(evidence["promotable"])
        self.assertEqual(
            [item["status"] for item in evidence["required_checks"]],
            ["missing", "failed"],
        )

    def test_collection_uses_github_check_runs_api(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(command, **kwargs):
            calls.append(tuple(command))
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"check_runs": []}),
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


if __name__ == "__main__":
    unittest.main()
