#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


UPGRADE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = UPGRADE_ROOT / "scripts"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS))

from redfish_credentials import load_credentials  # noqa: E402


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class UpgradeHelperTests(unittest.TestCase):
    def test_skill_accepts_existing_hpm_and_keeps_debug_optional(self) -> None:
        text = (UPGRADE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("already-built HPM", text)
        self.assertIn("only when the caller", text)
        self.assertNotIn("starts only after Build", text)

    def test_skill_uses_the_canonical_installed_path(self) -> None:
        text = (UPGRADE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("$HOME/.agents/skills/openubmc-upgrade", text)
        self.assertNotIn("OPENUBMC_UPGRADE_SKILL_ROOT", text)

    def test_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "firmware.hpm"
            artifact.write_bytes(b"firmware")
            result = run(
                str(SCRIPTS / "artifact_identity.py"),
                "--path",
                str(artifact),
                "--expected-sha256",
                hashlib.sha256(b"firmware").hexdigest(),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["path"], str(artifact))

    def test_credentials_file_does_not_echo_secret(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            credentials = Path(raw) / "credentials"
            credentials.write_text(
                "REDFISH_USERNAME=admin\nREDFISH_PASSWORD=not-for-output\n",
                encoding="utf-8",
            )
            credentials.chmod(0o600)
            result = run(
                str(SCRIPTS / "redfish_credentials.py"),
                "--target",
                "https://10.0.0.1",
                "--credentials-file",
                str(credentials),
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("not-for-output", result.stdout)
            self.assertTrue(json.loads(result.stdout)["password_present"])

            credentials.chmod(0o644)
            rejected = run(
                str(SCRIPTS / "redfish_credentials.py"),
                "--target",
                "https://10.0.0.1",
                "--credentials-file",
                str(credentials),
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_credentials_file_accepts_debug_keys_but_reads_only_redfish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            credentials = Path(raw) / "credentials"
            credentials.write_text(
                "OPENUBMC_SSH_USER=debug-user\n"
                "OPENUBMC_SSH_PASSWORD=debug-secret\n"
                "REDFISH_USERNAME=admin\n"
                "REDFISH_PASSWORD='redfish-secret'\n",
                encoding="utf-8",
            )
            credentials.chmod(0o600)
            result = run(
                str(SCRIPTS / "redfish_credentials.py"),
                "--target",
                "https://10.0.0.1",
                "--credentials-file",
                str(credentials),
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("debug-secret", result.stdout)
            self.assertNotIn("redfish-secret", result.stdout)
            loaded = load_credentials(credentials)
            self.assertEqual(loaded.password, "redfish-secret")

    def test_credentials_file_rejects_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            credentials = Path(raw) / "credentials"
            credentials.write_text(
                "REDFISH_USERNAME=admin\n"
                "REDFISH_PASSWORD=secret\n"
                "UNKNOWN_SECRET=value\n",
                encoding="utf-8",
            )
            credentials.chmod(0o600)
            result = run(
                str(SCRIPTS / "redfish_credentials.py"),
                "--target",
                "https://10.0.0.1",
                "--credentials-file",
                str(credentials),
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
