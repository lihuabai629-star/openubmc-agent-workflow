from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = RUNTIME_ROOT.parent
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    RELEASE_COMMIT_POLICY,
    RELEASE_LOCK_SCHEMA,
    ReleaseLockError,
    build_release_lock,
    verify_release_lock,
)
from openubmc_target_runtime import release  # noqa: E402


class ReleaseLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()

    def test_same_source_resolves_to_the_same_digest_set(self) -> None:
        first = build_release_lock(REPO_ROOT, source_commit=self.commit)
        second = build_release_lock(REPO_ROOT, source_commit=self.commit)

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], RELEASE_LOCK_SCHEMA)
        self.assertEqual(first["source_commit_policy"], RELEASE_COMMIT_POLICY)
        self.assertEqual(len(first["skills"]), 11)
        self.assertEqual(
            len({item["name"] for item in first["skills"]}),
            len(first["skills"]),
        )
        self.assertTrue(first["runtime"]["content_digest"].startswith("sha256:"))

    def test_lock_verification_reports_the_immutable_release_identity(self) -> None:
        lock = build_release_lock(REPO_ROOT, source_commit=self.commit)
        identity = verify_release_lock(
            REPO_ROOT,
            lock,
            verify_git_topology=True,
        )

        self.assertEqual(identity["source_commit"], self.commit)
        self.assertEqual(identity["lock_digest"], lock["lock_digest"])
        self.assertEqual(
            identity["runtime"]["content_digest"],
            lock["runtime"]["content_digest"],
        )
        self.assertEqual(len(identity["skill_digests"]), 11)

    def test_incompatible_or_tampered_lock_fails_before_install(self) -> None:
        lock = build_release_lock(REPO_ROOT, source_commit=self.commit)
        tampered = json.loads(json.dumps(lock))
        tampered["runtime"]["api_version"] = "openubmc.target-runtime.v0"

        with self.assertRaisesRegex(ReleaseLockError, "does not match"):
            verify_release_lock(
                REPO_ROOT,
                tampered,
                verify_git_topology=False,
            )

    def test_source_commit_must_be_a_full_immutable_commit(self) -> None:
        with self.assertRaisesRegex(ReleaseLockError, "full Git commit"):
            build_release_lock(REPO_ROOT, source_commit="main")

    def test_release_lock_child_must_have_exactly_one_parent(self) -> None:
        source = "a" * 40
        current = "b" * 40
        other_parent = "c" * 40

        def git_result(_root: Path, *arguments: str) -> str:
            if arguments == ("rev-list", "--parents", "-n", "1", "HEAD"):
                return f"{current} {source} {other_parent}"
            if arguments == ("diff", "--name-only", source, current):
                return "release-lock.json"
            self.fail(f"unexpected git arguments: {arguments}")

        with (
            mock.patch.object(release, "repository_commit", return_value=current),
            mock.patch.object(release, "_git", side_effect=git_result),
            self.assertRaisesRegex(ReleaseLockError, "lock-only child"),
        ):
            release._validate_release_commit_topology(REPO_ROOT, source)


if __name__ == "__main__":
    unittest.main()
