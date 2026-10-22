"""Platform selection from effective Git remotes, without contacting a host."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from submission_route import route, endpoint
from check_submission import CheckError


class SubmissionRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "-q")
        self.git("remote", "add", "personal", "git@gitcode.com:person/component.git")
        self.git("remote", "add", "base", "https://gitcode.com/openUBMC/component.git")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              check=True, capture_output=True, text=True).stdout

    def select(self, hosts=()):
        before = (self.repo / ".git/config").read_bytes()
        result = route(self.repo, "personal", "base", hosts)
        self.assertEqual(before, (self.repo / ".git/config").read_bytes())
        return result

    def test_gitcode_ssh_and_https_roles(self):
        result = self.select()
        self.assertEqual(result["platform"], "gitcode")
        self.assertEqual(result["source_push"]["project"], "person/component")
        self.assertFalse(result["remote_identity_verified"])

    def test_internal_platform_requires_verified_host(self):
        self.git("remote", "set-url", "personal", "ssh://git@code.internal.test:8122/person/component.git")
        self.git("remote", "set-url", "base", "https://code.internal.test/team/component.git")
        self.assertIsNone(self.select()["platform"])
        self.assertEqual(self.select(["code.internal.test"])["platform"], "gitlab")

    def test_fetch_mirror_does_not_override_push_platform(self):
        self.git("remote", "set-url", "personal", "https://github.com/person/component.git")
        self.git("remote", "set-url", "--push", "personal", "git@gitcode.com:person/component.git")
        self.assertEqual(self.select()["platform"], "gitcode")

    def test_cross_platform_and_multiple_targets_fail(self):
        self.git("remote", "set-url", "--push", "personal", "git@gitlab.internal.test:person/component.git")
        self.assertIsNone(self.select(["gitlab.internal.test"])["platform"])
        self.git("remote", "set-url", "--add", "--push", "personal", "git@gitcode.com:person/component.git")
        self.assertEqual(self.select()["status"], "needs_input")

    def test_same_project_cannot_be_treated_as_fork(self):
        self.git("remote", "set-url", "personal", "git@gitcode.com:openUBMC/component.git")
        self.assertIsNone(self.select()["platform"])

    def test_unknown_host_is_not_implicitly_gitlab(self):
        self.git("remote", "set-url", "personal", "git@gitcode.com.invalid:person/component.git")
        self.git("remote", "set-url", "base", "git@gitcode.com.invalid:team/component.git")
        self.assertIsNone(self.select()["platform"])

    def test_urls_never_reflect_credentials(self):
        value = endpoint("https://user:synthetic-secret@gitcode.com/person/component.git?token=synthetic-secret")
        self.assertNotIn("synthetic-secret", json.dumps(value))
        self.assertEqual(value["host"], "gitcode.com")
        with self.assertRaisesRegex(CheckError, "invalid remote"):
            endpoint("https://synthetic-secret@[invalid")


if __name__ == "__main__":
    unittest.main()
