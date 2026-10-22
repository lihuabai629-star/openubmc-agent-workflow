from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = load("community_checks", ROOT / "scripts/community_checks.py")
coverage = load("interface_coverage", ROOT / "scripts/interface_coverage.py")
redfish = load("offline_redfish", ROOT / "resources/community/redfish-diff/scripts/analyze_redfish_diff.py")


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class CommunityChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_compliance_distinguishes_findings_clean_and_skip(self):
        bad = 'json/intf/mdb/bmc/kepler/bad.json'
        good = 'intf/mdb/bmc/kepler/good.json'
        self.write(bad, json.dumps({"bmc.kepler.systems": {"properties": {"P": {"baseType": "Strring"}}}}))
        self.write(good, json.dumps({"bmc.kepler.Systems.Power": {"properties": {"P": {"baseType": "U8"}}}}))
        self.write("README.md", "# Notes\n")
        findings = checks.run_check("compliance", self.root, [bad])
        clean = checks.run_check("compliance", self.root, [good])
        skipped = checks.run_check("compliance", self.root, ["README.md"])
        self.assertEqual(findings["status"], "findings")
        self.assertTrue(findings["result"]["findings"])
        self.assertEqual(clean["status"], "completed")
        self.assertFalse(clean["result"]["findings"])
        self.assertEqual(skipped["status"], "skipped")

    def test_missing_file_or_node_cannot_pass(self):
        with self.assertRaises(FileNotFoundError):
            checks.run_check("compliance", self.root, ["missing.json"])
        name = "intf/mdb/good.json"
        self.write(name, "{}")
        with mock.patch.object(checks.shutil, "which", return_value=None):
            result = checks.run_check("compliance", self.root, [name])
        self.assertEqual(result["status"], "incomplete")

    def test_scope_cannot_escape_repository(self):
        outside = self.write("outside.json", "{}")
        source = self.root / "source"
        source.mkdir()
        (source / "link.json").symlink_to(outside)
        with self.assertRaises(ValueError):
            checks.run_check("compliance", source, ["link.json"])

    def test_uncovered_documentation_is_distinct_from_successful_execution(self):
        request = {"changedFiles": ["interface_config/redfish/mapping_config/Systems/Bios/config.json"],
                   "headSha": "source-revision", "docsTree": [], "docsRevision": "docs-revision"}
        path = self.write("coverage.json", json.dumps(request))
        result = coverage.check_interface_coverage(path)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["conclusion"], "uncovered")
        request["docsTree"] = None
        path.write_text(json.dumps(request))
        self.assertEqual(coverage.check_interface_coverage(path)["status"], "incomplete")

    def test_redfish_directory_comparison_preserves_inputs_and_old_report(self):
        relative = "interface_config/redfish/mapping_config/Test/Test.json"
        old = self.root / "old"
        new = self.root / "new"
        for name, value in [("old", "before"), ("new", "after")]:
            self.write(f"{name}/{relative}", json.dumps({"Resources": [{"Uri": "/redfish/v1/Test", "Interfaces": [
                {"Type": "GET", "RspBody": {"Name": value}}]}]}))
        before = {path: path.read_bytes() for path in (old / relative, new / relative)}
        output = self.root / "report"
        result = checks.run_check("redfish-diff", self.root, old_dir=old, new_dir=new, output=output)
        self.assertEqual(result["status"], "completed", result)
        report = output / "redfish_interface_change_report.md"
        self.assertTrue(report.is_file())
        original_report = report.read_bytes()
        for path, expected in before.items():
            self.assertEqual(path.read_bytes(), expected)
        with self.assertRaises(ValueError):
            checks.run_check("redfish-diff", self.root, old_dir=old, new_dir=new, output=output)
        self.assertEqual(report.read_bytes(), original_report)

    def test_offline_refs_do_not_read_credentials_or_create_worktrees(self):
        source = self.root / "repo"
        subprocess.run(["git", "init", "--quiet", "--template=", str(source)], check=True)
        path = source / "interface_config/redfish/mapping_config/Test/Test.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"Resources": [{"Uri": "/redfish/v1/Test", "Interfaces": [{"Type": "GET"}]}]}))
        env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
               "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
        subprocess.run(["git", "-C", str(source), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(source), "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "fixture"], check=True, env=env)
        path.write_text("local dirty source\n")
        output = self.root / "offline-report"
        arguments = ["analyzer", "--repo", str(source), "--old-ref", "HEAD", "--new-ref", "HEAD",
                     "--out-dir", str(output), "--offline"]
        with mock.patch("sys.argv", arguments), mock.patch.object(Path, "home", side_effect=AssertionError("credential lookup")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("network request")), \
                contextlib.redirect_stdout(io.StringIO()):
            redfish.main()
        self.assertEqual(path.read_text(), "local dirty source\n")
        self.assertFalse((source / ".git/worktrees").exists())
        self.assertTrue((output / "redfish_interface_change_report.md").is_file())


if __name__ == "__main__":
    unittest.main()
