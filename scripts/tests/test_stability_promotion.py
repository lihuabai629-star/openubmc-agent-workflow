from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.stability_promotion import check_promotion, prepare_candidate, installer


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class StabilityPromotionTests(unittest.TestCase):
    def repo(self, root: Path) -> Path:
        root.mkdir(parents=True)
        git(root, "init", "-b", "main")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "Test")
        for name, relative in installer.TARGET_RUNTIME_SKILL_BUNDLE:
            skill = root / relative
            skill.mkdir()
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nFixture skill.\n")
            (skill / "skill.json").write_text(json.dumps({"name": name, "version": "1.0.0", "manifestVersion": 1, "files": ["SKILL.md", "skill.json"]}))
        package = root / "openubmc-target-runtime/openubmc_target_runtime"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('"""Fixture runtime."""\n')
        (package / "contracts.py").write_text('RUNTIME_API_VERSION = "openubmc.target-runtime.v1"\n')
        entrypoint = root / "openubmc-debug/scripts/target_runtime_mcp.py"
        entrypoint.parent.mkdir()
        entrypoint.write_text('"""Fixture entrypoint."""\n')
        git(root, "add", ".")
        git(root, "commit", "-m", "base")
        return root

    def fixture(self, root: Path):
        center = self.repo(root / "center")
        active = root / "active"
        prepare_candidate(center=center, candidate=active)
        home = root / "home"
        commit = git(active, "rev-parse", "HEAD")
        plan = installer.build_runtime_plan(home, active, source_commit=commit)
        installer.deploy_runtime(plan, False)
        links = {}
        for name, relative in installer.TARGET_RUNTIME_SKILL_BUNDLE:
            link = home / ".codex/skills" / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(active / relative)
            links[str(link)] = str(active / relative)
        state = {
            "version": 1, "source_root": str(active), "source_mode": "linked",
            "source_commit": commit, "resolved_commit": commit,
            "skill_profile": "target-runtime",
            "clients": ["codex"], "runtime": plan, "links": links,
        }
        installer.save_state(home, state, False)
        candidate = root / "candidate"
        prepare_candidate(center=center, candidate=candidate)
        return center, candidate, home, state

    def test_clean_merged_candidate_verifies_real_installer_and_skill_digests(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, state = self.fixture(Path(raw))
            before = installer.state_path(home).read_bytes()
            report = check_promotion(center=center, candidate=candidate, installer_home=home)
            self.assertEqual(report["decision"], "ready", report["blockers"])
            self.assertTrue(report["active_install"]["verified"])
            self.assertEqual(len(report["candidate"]["skills"]), len(installer.TARGET_RUNTIME_SKILL_BUNDLE))
            self.assertEqual(report["candidate"]["runtime_digest"], state["runtime"]["content_digest"])
            self.assertFalse(report["installer_proposal"]["executed"])
            self.assertEqual(installer.state_path(home).read_bytes(), before)

    def test_dirty_development_center_does_not_block_clean_candidate(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, _ = self.fixture(Path(raw))
            (center / "dirty.txt").write_text("development\n")
            report = check_promotion(center=center, candidate=candidate, installer_home=home)
            self.assertEqual(report["decision"], "ready", report["blockers"])
            self.assertFalse(report["center"]["clean"])
            self.assertTrue(report["candidate"]["clean"])

    def test_dirty_center_as_candidate_is_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            center, _, home, _ = self.fixture(Path(raw))
            (center / "dirty.txt").write_text("development\n")
            report = check_promotion(center=center, candidate=center, installer_home=home)
            self.assertIn("candidate worktree is dirty", report["blockers"])

    def test_subdirectory_cannot_impersonate_a_candidate_root(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, _ = self.fixture(Path(raw))
            with self.assertRaisesRegex(ValueError, "worktree root"):
                check_promotion(center=center, candidate=candidate / "openubmc-debug", installer_home=home)

    def test_clean_head_different_from_selected_commit_is_blocked(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, _ = self.fixture(Path(raw))
            prior = git(candidate, "rev-parse", "HEAD")
            (center / "new.txt").write_text("next\n")
            git(center, "add", "."); git(center, "commit", "-m", "next")
            git(candidate, "fetch", "origin"); git(candidate, "checkout", "--detach", "origin/main")
            report = check_promotion(center=center, candidate=candidate, candidate_commit=prior, installer_home=home)
            self.assertIn("candidate HEAD does not equal requested commit", report["blockers"])

    def test_foreign_commit_and_unresolved_main_are_distinct(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, _ = self.fixture(Path(raw))
            git(candidate, "config", "user.email", "test@example.invalid")
            git(candidate, "config", "user.name", "Test")
            (candidate / "foreign.txt").write_text("foreign\n")
            git(candidate, "add", "."); git(candidate, "commit", "-m", "foreign")
            report = check_promotion(center=center, candidate=candidate, installer_home=home, main_ref="missing")
            self.assertIn("candidate commit/tree is not available from center Git objects", report["blockers"])
            self.assertTrue(any("main ref cannot be resolved" in x for x in report["blockers"]))

    def test_prepare_rejects_unmerged_commit_and_does_not_write_center(self):
        with tempfile.TemporaryDirectory() as raw:
            center = self.repo(Path(raw) / "center")
            before = git(center, "status", "--porcelain")
            prepare_candidate(center=center, candidate=Path(raw) / "clean")
            self.assertEqual(git(center, "status", "--porcelain"), before)
            self.assertFalse((center / ".git/worktrees").exists())
            git(center, "checkout", "-b", "topic")
            (center / "topic.txt").write_text("topic\n")
            git(center, "add", "."); git(center, "commit", "-m", "topic")
            with self.assertRaisesRegex(ValueError, "not merged"):
                prepare_candidate(center=center, candidate=Path(raw) / "unmerged")

    def test_runtime_tampering_and_missing_state_cannot_be_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, state = self.fixture(Path(raw))
            (Path(state["runtime"]["package_path"]) / "__init__.py").write_text("tampered\n")
            report = check_promotion(center=center, candidate=candidate, installer_home=home)
            self.assertEqual(report["decision"], "blocked")
            self.assertTrue(any("active Runtime" in x for x in report["blockers"]))
            installer.state_path(home).unlink()
            report = check_promotion(center=center, candidate=candidate, installer_home=home)
            self.assertTrue(any("active installer identity" in x for x in report["blockers"]))

    def test_recorded_active_commit_and_link_drift_block_promotion(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, state = self.fixture(Path(raw))
            state["source_commit"] = "a" * 40
            installer.save_state(home, state, False)
            link = Path(next(iter(state["links"])))
            link.unlink(); link.symlink_to(candidate)
            report = check_promotion(center=center, candidate=candidate, installer_home=home)
            self.assertIn("active source commit does not match installer state", report["blockers"])
            self.assertTrue(any("active Skill link" in x for x in report["blockers"]))

    def test_dirty_candidate_skill_is_blocked_and_digest_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            center, candidate, home, _ = self.fixture(Path(raw))
            before = check_promotion(center=center, candidate=candidate, installer_home=home)
            skill = candidate / "openubmc-debug/SKILL.md"
            skill.write_text(skill.read_text() + "changed\n")
            after = check_promotion(center=center, candidate=candidate, installer_home=home)
            self.assertEqual(after["decision"], "blocked")
            self.assertNotEqual(before["candidate"]["skills"], after["candidate"]["skills"])


if __name__ == "__main__":
    unittest.main()
