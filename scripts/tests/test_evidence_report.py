from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "evidence_report.py"
SPEC = importlib.util.spec_from_file_location("evidence_report", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


class EvidenceReportTests(unittest.TestCase):
    def test_source_commit_accepts_only_the_tested_head(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            _git(repository, "init", "-q")
            _git(repository, "config", "user.name", "Test")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
            _git(repository, "add", "tracked.txt")
            _git(repository, "commit", "-qm", "initial")
            head = _git(repository, "rev-parse", "HEAD")

            self.assertEqual(evidence.source_commit(head, workspace=repository), head)
            with self.assertRaisesRegex(ValueError, "workspace HEAD"):
                evidence.source_commit("a" * 40, workspace=repository)

    def test_release_lock_parent_is_accepted_only_by_its_lock_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repository = Path(raw)
            _git(repository, "init", "-q")
            _git(repository, "config", "user.name", "Test")
            _git(repository, "config", "user.email", "test@example.com")
            (repository / "tracked.txt").write_text("one\n", encoding="utf-8")
            _git(repository, "add", "tracked.txt")
            _git(repository, "commit", "-qm", "source")
            source = _git(repository, "rev-parse", "HEAD")
            (repository / "release-lock.json").write_text(
                json.dumps(
                    {
                        "schema": "openubmc-agent-workflow.release-lock.v1",
                        "source_commit_policy": "lock-finalization-parent-v1",
                        "source_commit": source,
                    }
                ),
                encoding="utf-8",
            )
            _git(repository, "add", "release-lock.json")
            _git(repository, "commit", "-qm", "release lock")

            self.assertEqual(
                evidence.source_commit(source, workspace=repository),
                source,
            )

            (repository / "tracked.txt").write_text("two\n", encoding="utf-8")
            _git(repository, "add", "tracked.txt")
            _git(repository, "commit", "-qm", "after release")
            with self.assertRaisesRegex(ValueError, "release-lock parent"):
                evidence.source_commit(source, workspace=repository)


if __name__ == "__main__":
    unittest.main()
