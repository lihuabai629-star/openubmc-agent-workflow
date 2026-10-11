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
