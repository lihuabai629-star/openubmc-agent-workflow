"""Exercise submission invariants using temporary local Git repositories."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_submission import inspect


class SubmissionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "work"
        self.repo.mkdir()
        self.g("init", "-b", "target")
        self.g("config", "user.name", "Test Author")
        self.g("config", "user.email", "test@example.invalid")
        self.commit("base", "base")
        for name in ("upstream", "origin"):
            bare = root / name
            subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                           capture_output=True)
            self.g("remote", "add", name, str(bare))
        self.g("push", "upstream", "HEAD:refs/heads/target")
        self.g("push", "origin", "HEAD:refs/heads/personal")
        self.g("fetch", "upstream")
        self.g("checkout", "-b", "development")
        self.commit("fix", "fix")

    def g(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name, text):
        (self.repo / name).write_text(text + "\n")
        self.g("add", name)
        self.g("commit", "-m", name)

    def check(self, phase="pre-push", verify_remote=True, source="personal"):
        before = self.g("status", "--porcelain")
        head = self.g("rev-parse", "HEAD")
        report = inspect(self.repo, "target", source, phase=phase,
                         verify_remote=verify_remote)
        self.assertEqual(before, self.g("status", "--porcelain"))
        self.assertEqual(head, self.g("rev-parse", "HEAD"))
        return report

    def test_pre_push_and_distinct_branch_roles(self):
        report = self.check()
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual((report["behind"], report["ahead"]), (0, 1))
        self.assertEqual(report["development_branch"], "development")
        self.assertEqual(report["source_branch"], "personal")
        self.assertEqual(report["target_branch"], "target")

    def test_new_personal_branch_is_allowed_before_push(self):
        report = self.check(source="new-branch")
        self.assertTrue(report["checks_passed"], report)
        self.assertIsNone(report["source_remote_sha"])

    def test_post_push_requires_matching_source_sha(self):
        self.assertFalse(self.check("post-push")["checks_passed"])
        self.g("push", "origin", "HEAD:refs/heads/personal")
        report = self.check("post-push")
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["source_remote_sha"], report["head_sha"])

    def test_post_push_missing_branch_fails(self):
        self.assertFalse(self.check("post-push", source="absent")["checks_passed"])

    def test_stale_upstream_reference_fails(self):
        old_baseline = self.g("rev-parse", "refs/remotes/upstream/target")
        self.g("push", "upstream", "HEAD:refs/heads/target")
        # Push refreshes this tracking ref; restore it to model another client's push.
        self.g("update-ref", "refs/remotes/upstream/target", old_baseline)
        report = self.check()
        self.assertFalse(report["checks_passed"])
        self.assertNotEqual(report["baseline_sha"], report["upstream_remote_sha"])

    def test_missing_upstream_commits_fails(self):
        self.g("update-ref", "refs/remotes/upstream/target", "HEAD")
        self.g("reset", "--hard", "HEAD^")
        report = self.check(verify_remote=False)
        self.assertFalse(report["checks_passed"])
        self.assertEqual(report["behind"], 1)

    def test_extra_commits_fail(self):
        self.commit("unrelated", "unrelated")
        report = self.check()
        self.assertFalse(report["checks_passed"])
        self.assertEqual(report["ahead"], 2)

    def test_untracked_and_staged_changes_fail(self):
        p = self.repo / "uncommitted"
        p.write_text("untracked\n")
        self.assertFalse(self.check()["checks_passed"])
        self.g("add", p.name)
        self.assertFalse(self.check()["checks_passed"])

    def test_unstaged_change_fails(self):
        (self.repo / "fix").write_text("changed\n")
        self.assertFalse(self.check()["checks_passed"])

    def test_local_result_does_not_claim_remote_verification(self):
        report = self.check(verify_remote=False)
        self.assertTrue(report["checks_passed"])
        self.assertFalse(report["remote_verified"])
        self.assertEqual(report["scope"], "local")
        self.assertFalse(self.check("post-push", verify_remote=False)["checks_passed"])

    def test_remote_check_uses_push_url_not_fetch_mirror(self):
        push = Path(self.temp.name) / "push"
        subprocess.run(["git", "init", "--bare", str(push)], check=True, capture_output=True)
        self.g("remote", "set-url", "--push", "origin", str(push))
        self.g("push", "origin", "HEAD:refs/heads/personal")
        report = self.check("post-push")
        self.assertTrue(report["checks_passed"], report)
        self.assertEqual(report["source_remote_sha"], report["head_sha"])

    def test_multiple_push_targets_are_rejected(self):
        self.g("remote", "set-url", "--add", "--push", "origin", str(Path(self.temp.name) / "origin"))
        self.g("remote", "set-url", "--add", "--push", "origin", str(Path(self.temp.name) / "upstream"))
        self.assertFalse(self.check()["checks_passed"])

    def test_explicit_community_commit_count(self):
        self.commit("second-fix", "second")
        result = inspect(self.repo, "target", "personal", expected_commits=2)
        self.assertTrue(result["checks_passed"], result)
        self.assertFalse(inspect(self.repo, "target", expected_commits=0)["checks_passed"])


if __name__ == "__main__":
    unittest.main()
