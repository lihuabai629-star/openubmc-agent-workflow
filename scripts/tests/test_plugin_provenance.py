"""Offline package, source, dependency and qualification linkage tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.package_plugin import build, canonical
from scripts.plugin_provenance import local_manifest, verify_manifest


ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = ROOT / "scripts" / "plugin_provenance.py"


class PluginProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        names = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        ).decode().split("\0")
        for name in names:
            if name and (ROOT / name).is_file():
                destination = self.source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, destination)
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run([
            "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "-qm", "provenance fixture",
        ], cwd=self.source, check=True)
        self.archive = self.base / "openubmc.tar.gz"
        self.package = build(self.source, "HEAD", self.archive)
        self.qualification = self.base / "qualification.json"
        self.qualification.write_bytes(canonical({
            "schema": "openubmc.codex-plugin.qualification.v1",
            "ok": True,
            "source_commit": self.package["source_commit"],
            "archive_sha256": self.package["archive_sha256"],
            "content_digest": self.package["content_digest"],
            "version": self.package["version"],
            "deterministic_archive": True,
            "native_install": True,
            "native_uninstall": True,
            "reinstall": True,
            "external_state_preserved": True,
        }))
        self.manifest = self.base / "provenance.json"

    def _create(self) -> dict[str, object]:
        document = local_manifest(self.source, self.archive, self.qualification)
        self.manifest.write_bytes(canonical(document))
        return document

    def test_reproducible_offline_link_and_cli_verification(self) -> None:
        first = self._create()
        second = local_manifest(self.source, self.archive, self.qualification)
        self.assertEqual(first, second)
        self.assertEqual(first["claim"], "local-unpublished")
        self.assertEqual(first["source"]["commit"], self.package["source_commit"])
        self.assertEqual(first["package"]["sha256"], self.package["archive_sha256"])
        self.assertEqual(
            first["qualification"]["sha256"],
            hashlib.sha256(self.qualification.read_bytes()).hexdigest(),
        )
        self.assertEqual(len(first["dependencies"]["inventory"]), 2)
        self.assertNotIn(str(self.base), self.manifest.read_text())
        self.assertEqual(
            verify_manifest(self.source, self.archive, self.qualification, self.manifest),
            first,
        )
        cli_manifest = self.base / "provenance-cli.json"
        created = subprocess.run([
            sys.executable, str(PROVENANCE), "create", "--source", str(self.source),
            "--archive", str(self.archive), "--qualification", str(self.qualification),
            "--manifest", str(cli_manifest),
        ], capture_output=True, text=True)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(cli_manifest.read_bytes(), self.manifest.read_bytes())
        result = subprocess.run([
            sys.executable, str(PROVENANCE), "verify", "--source", str(self.source),
            "--archive", str(self.archive), "--qualification", str(self.qualification),
            "--manifest", str(self.manifest),
        ], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_modifying_archive_or_manifest_is_rejected(self) -> None:
        self._create()
        original = self.archive.read_bytes()
        self.archive.write_bytes(original + b"tamper")
        with self.assertRaisesRegex(ValueError, "archive bytes"):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)
        self.archive.write_bytes(original)
        document = json.loads(self.manifest.read_bytes())
        document["dependencies"]["inventory"][0]["sha256"] = "0" * 64
        self.manifest.write_bytes(canonical(document))
        with self.assertRaisesRegex(ValueError, "manifest does not match"):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)

    def test_dirty_and_changed_clean_source_are_rejected(self) -> None:
        self._create()
        source_file = self.source / "requirements-ci.lock"
        source_file.write_bytes(source_file.read_bytes() + b"\nchanged\n")
        with self.assertRaisesRegex(ValueError, "dirty"):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)
        subprocess.run(["git", "add", "requirements-ci.lock"], cwd=self.source, check=True)
        subprocess.run([
            "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "-qm", "changed dependency lock",
        ], cwd=self.source, check=True)
        with self.assertRaisesRegex(ValueError, "archive source commit"):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)

    def test_qualification_must_be_passing_exact_and_present(self) -> None:
        self._create()
        original = self.qualification.read_bytes()
        self.qualification.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "manifest does not match"):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)
        report = json.loads(original)
        report["ok"] = False
        self.qualification.write_bytes(canonical(report))
        with self.assertRaisesRegex(ValueError, "does not qualify"):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)
        self.qualification.unlink()
        with self.assertRaises(FileNotFoundError):
            verify_manifest(self.source, self.archive, self.qualification, self.manifest)


if __name__ == "__main__":
    unittest.main()
