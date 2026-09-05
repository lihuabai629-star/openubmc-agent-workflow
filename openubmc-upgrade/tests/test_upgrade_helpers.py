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

from artifact_identity import validate_artifact_metadata  # noqa: E402
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

    def test_skill_resolves_helpers_relative_to_its_release(self) -> None:
        text = (UPGRADE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("<skill-dir>", text)
        self.assertNotIn("/mnt/c/Users/", text)
        self.assertNotIn("$HOME/.agents/skills/openubmc-upgrade", text)
        self.assertNotIn("OPENUBMC_UPGRADE_SKILL_ROOT", text)

    def test_skill_documents_legacy_multipart_and_activation_reconnect(self) -> None:
        text = (UPGRADE_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("legacy compatibility shape", text)
        self.assertIn("activation_connection_lost", text)
        self.assertIn("mutation transaction", text)

    def test_skill_package_includes_the_preflight_script(self) -> None:
        package = json.loads((UPGRADE_ROOT / "skill.json").read_text(encoding="utf-8"))
        self.assertIn("scripts/preflight_upgrade.py", package["files"])

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

    def test_artifact_metadata_and_versioned_filename_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "rootfs_openUBMC_12.00.05.03_release.hpm"
            content = b"firmware"
            artifact.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            metadata = artifact.with_name(artifact.name + ".metadata.json")
            metadata.write_text(
                json.dumps(
                    {
                        "artifact": {"sha256": digest, "size": len(content)},
                        "product_version": "12.00.05.03",
                    }
                ),
                encoding="utf-8",
            )
            result = validate_artifact_metadata(
                artifact,
                expected_sha256=digest,
                product_version="12.00.05.03",
                actual_size=len(content),
            )
            self.assertEqual(result["sidecar_path"], str(metadata))
            with self.assertRaisesRegex(ValueError, "filename product version"):
                validate_artifact_metadata(
                    artifact,
                    expected_sha256=digest,
                    product_version="12.00.05.15",
                    actual_size=len(content),
                )
            metadata.write_text(
                json.dumps(
                    {
                        "artifact": {"sha256": digest, "size": len(content)},
                        "product_version": "12.00.05.15",
                    }
                ),
                encoding="utf-8",
            )
            generic = Path(raw) / "rootfs_openUBMC.hpm"
            generic.write_bytes(content)
            generic_metadata = Path(f"{generic}.metadata.json")
            generic_metadata.write_bytes(metadata.read_bytes())
            with self.assertRaisesRegex(ValueError, "metadata product version"):
                validate_artifact_metadata(
                    generic,
                    expected_sha256=digest,
                    product_version="12.00.05.03",
                    actual_size=len(content),
                )

    def test_version_immediately_before_hpm_suffix_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "openUBMC_12.08.21.14.hpm"
            content = b"firmware"
            artifact.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            result = validate_artifact_metadata(
                artifact, expected_sha256=digest, product_version="12.08.21.14",
                actual_size=len(content),
            )
            self.assertEqual(result["filename_product_version"], "12.08.21.14")
            with self.assertRaisesRegex(ValueError, "filename product version"):
                validate_artifact_metadata(
                    artifact, expected_sha256=digest, product_version="12.08.21.10",
                    actual_size=len(content),
                )

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
