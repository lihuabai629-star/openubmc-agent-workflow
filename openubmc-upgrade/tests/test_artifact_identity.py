from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "artifact_identity.py"


class ArtifactIdentityCliTests(unittest.TestCase):
    def test_release_provenance_is_reported_without_blocking_matching_existing_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "fixture.hpm"
            content = b"synthetic existing firmware"
            artifact.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            metadata = {
                "artifact": {"sha256": digest, "size": len(content)},
                "product_version": "12.08.21.06", "upgrade_eligible": False,
                "package_binding": "unverified",
            }
            sidecar = Path(str(artifact) + ".metadata.json")
            sidecar.write_text(json.dumps(metadata))
            argv = [sys.executable, "-B", str(SCRIPT), "--path", str(artifact),
                    "--expected-sha256", digest, "--product-version", "12.08.21.06"]
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["metadata"]["provenance_warnings"],
                             ["build_upgrade_eligibility_unverified", "build_package_binding_unverified"])
            self.assertEqual(json.loads(sidecar.read_text()), metadata)
            metadata["artifact"]["sha256"] = "0" * 64
            sidecar.write_text(json.dumps(metadata))
            mismatch = subprocess.run(argv, capture_output=True, text=True, check=False)
            self.assertEqual(mismatch.returncode, 1)
            self.assertIn("metadata SHA-256 does not match", mismatch.stderr)

    def test_existing_hpm_is_accepted_when_its_path_and_handle_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "fixture.hpm"
            artifact.write_bytes(b"synthetic firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--path", str(artifact),
                 "--expected-sha256", digest],
                capture_output=True, text=True, check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sha256"], digest)


if __name__ == "__main__":
    unittest.main()
