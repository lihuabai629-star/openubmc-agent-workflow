from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "write_artifact_metadata.py"
SPEC = importlib.util.spec_from_file_location("openubmc_artifact_metadata", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
artifact_metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact_metadata)


class ArtifactMetadataTests(unittest.TestCase):
    def test_metadata_binds_version_to_exact_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "product.hpm"
            artifact.write_bytes(b"firmware")

            output = artifact_metadata.write_metadata(
                artifact,
                kind="openubmc-hpm",
                product_version="2.3.4",
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(
            payload,
            {
                "schema": "openubmc-agent-workflow/artifact-metadata-v1",
                "artifact": {
                    "sha256": (
                        "c3bf47ea1f4a4a605470313cacb3a44f4a461f68c6faeab"
                        "07e737610cb5ac835"
                    ),
                    "size": 8,
                    "kind": "openubmc-hpm",
                },
                "product_version": "2.3.4",
            },
        )


if __name__ == "__main__":
    unittest.main()
