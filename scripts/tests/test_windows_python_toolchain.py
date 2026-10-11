"""Pinned Windows Python supply and fail-closed extraction boundaries."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/setup_windows_python.ps1"
PIN = json.loads((ROOT / "scripts/windows-python.lock.json").read_text())


class WindowsPythonWorkflowTests(unittest.TestCase):
    def test_windows_jobs_use_same_locked_312_supplier_and_linux_pins_stay_unchanged(self):
        for filename, windows_job in [("validate.yml", "windows-plugin"), ("release.yml", "windows-plugin-gate")]:
            workflow = yaml.load((ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader)
            steps = workflow["jobs"][windows_job]["steps"]
            setup = next(step for step in steps if step.get("name") == "Set up locked Windows Python")
            self.assertEqual(setup["shell"], "pwsh")
            self.assertIn("./scripts/setup_windows_python.ps1", setup["run"])
            self.assertIn("-ExportToGitHubActions", setup["run"])
            self.assertFalse(any("setup-python" in step.get("uses", "") for step in steps))
            linux_pins = [step["with"]["python-version"] for name, job in workflow["jobs"].items()
                          if name != windows_job for step in job["steps"]
                          if "setup-python" in step.get("uses", "")]
            self.assertTrue(linux_pins)
            self.assertEqual(set(linux_pins), {"3.12.13"})
        self.assertEqual(PIN["version"], "3.12.15")
        self.assertEqual(PIN["supplier"], "astral-sh/python-build-standalone")
        self.assertIn("/" + PIN["release"] + "/", PIN["url"])
        self.assertIn("cpython-" + PIN["version"] + "+" + PIN["release"] + "-x86_64-pc-windows-msvc-", unquote(PIN["url"]))
        self.assertRegex(PIN["sha256"], r"^[0-9a-f]{64}$")


@unittest.skipUnless(sys.platform == "win32", "requires native Windows PowerShell")
class WindowsPythonSupplyFailureTests(unittest.TestCase):
    def invoke(self, directory, archive):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(shell)
        return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT),
                               "-OutputDirectory", str(directory), "-Archive", str(archive)],
                              capture_output=True, text=True, timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    def test_corrupt_archive_is_rejected_before_extracting_or_executing(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            archive = directory / "tampered.tar.gz"
            archive.write_bytes(b"invalid candidate; must never be extracted")
            target = directory / "toolchain"
            result = self.invoke(target, archive)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stdout + result.stderr)
            self.assertFalse((target / "runtime").exists())

    def test_existing_toolchain_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            sentinel = directory / "keep"
            sentinel.write_bytes(b"existing user toolchain")
            result = self.invoke(directory, directory / "missing-archive")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stdout + result.stderr)
            self.assertEqual(sentinel.read_bytes(), b"existing user toolchain")


if __name__ == "__main__":
    unittest.main()
