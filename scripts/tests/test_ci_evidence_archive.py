"""Execute the release evidence step without network access or GitHub credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "posix", "Release evidence runs in Bash on Linux")
class CiEvidenceArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-evidence-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()
        workflow = yaml.load(
            (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        self.step = next(
            step for step in workflow["jobs"]["release-gate"]["steps"]
            if step.get("name") == "Upload release evidence"
        )
        self.script = self.root / "evidence.sh"
        self.script.write_text(self.step["run"], encoding="utf-8")
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        fake_gh = self.fake_bin / "gh"
        fake_gh.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['release', 'view']:\n"
            "    print(os.environ['FAKE_DRAFT'])\n"
            "elif args[:2] == ['release', 'upload']:\n"
            "    Path(os.environ['FAKE_UPLOAD_LOG']).write_text(json.dumps(args))\n"
            "    sys.exit(int(os.environ['FAKE_UPLOAD_STATUS']))\n"
            "else:\n"
            "    sys.exit('Unexpected GitHub command')\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o700)
        self.upload_log = self.root / "upload.json"
        self.archive = self.staging / "release-gate-123-2.tar.gz"

    def write(self, relative: str, contents: str = "sanitized evidence") -> Path:
        path = self.staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def run_step(
        self, *, draft: str = "true", upload_status: int = 0,
        evidence_paths: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PATH": str(self.fake_bin) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(self.staging),
            "GITHUB_REPOSITORY": "example/test",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "GH_TOKEN": "",
            "RELEASE_TAG": "v-test",
            "EVIDENCE_PATHS": self.step["env"]["EVIDENCE_PATHS"]
            if evidence_paths is None else evidence_paths,
            "FAKE_DRAFT": draft,
            "FAKE_UPLOAD_LOG": str(self.upload_log),
            "FAKE_UPLOAD_STATUS": str(upload_status),
        }
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(self.script)],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=15,
        )

    def test_only_selected_evidence_and_its_checksum_reach_the_draft(self) -> None:
        self.write("release-gate.json", '{"status":"failed"}')
        self.write("agent-gateway-ab-evidence/summary.json")
        self.write("agent-gateway-ab-evidence/private.txt", "must stay local")
        self.write("credentials.json", "must stay local")
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(self.archive) as archive:
            self.assertEqual(set(archive.getnames()), {
                "release-gate.json", "agent-gateway-ab-evidence/summary.json",
            })
            self.assertEqual(archive.extractfile("release-gate.json").read(), b'{"status":"failed"}')
        checksum = self.archive.with_suffix(".gz.sha256").read_text().split()
        self.assertEqual(checksum, [hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.archive.name])
        uploaded = json.loads(self.upload_log.read_text())
        self.assertEqual(uploaded, [
            "release", "upload", "v-test", "--repo", "example/test",
            self.archive.name, self.archive.name + ".sha256",
        ])
        self.assertNotIn("continue-on-error", self.step)
        self.assertEqual(self.step["if"], "${{ !cancelled() && steps.candidate.outputs.release_tag != '' }}")

    def test_published_release_is_rejected_before_archiving(self) -> None:
        self.write("release-gate.json")
        self.assertNotEqual(self.run_step(draft="false").returncode, 0)
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.upload_log.exists())

    def test_empty_evidence_fails_without_upload(self) -> None:
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No release evidence", result.stderr)
        self.assertFalse(self.upload_log.exists())

    def test_symbolic_links_are_rejected_including_broken_and_parent_links(self) -> None:
        private = self.root / "private"
        private.mkdir()
        (private / "summary.json").write_text("private", encoding="utf-8")
        for kind in ("file", "broken", "parent", "nested"):
            with self.subTest(kind=kind):
                link = self.staging / "agent-gateway-ab-evidence"
                selected = "agent-gateway-ab-evidence"
                if kind == "parent":
                    link.symlink_to(private, target_is_directory=True)
                    selected += "/summary.json"
                elif kind == "nested":
                    link.mkdir()
                    (link / "private.json").symlink_to(private / "summary.json")
                else:
                    link.symlink_to(private / ("summary.json" if kind == "file" else "missing"))
                result = self.run_step(evidence_paths=selected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symbolic links", result.stderr)
                self.assertFalse(self.upload_log.exists())
                if kind == "nested":
                    (link / "private.json").unlink()
                    link.rmdir()
                else:
                    link.unlink()

    def test_paths_cannot_escape_the_staging_directory(self) -> None:
        for path in ("../private", str(self.root / "private")):
            with self.subTest(path=path):
                result = self.run_step(evidence_paths=path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("relative to RUNNER_TEMP", result.stderr)
                self.assertFalse(self.upload_log.exists())

    def test_upload_failure_fails_the_step(self) -> None:
        self.write("release-gate.json")
        self.assertEqual(self.run_step(upload_status=23).returncode, 23)
        self.assertTrue(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
