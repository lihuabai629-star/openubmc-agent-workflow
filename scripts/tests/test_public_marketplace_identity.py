"""The public marketplace payload must be byte-identical to the release archive."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.plugin_archive import canonical
from scripts.verify_public_marketplace import verify_public_marketplace


def fixture(root: Path) -> tuple[Path, Path]:
    files = {
        ".codex-plugin/plugin.json": canonical({"name": "openubmc", "version": "2.1.0"}),
        "payload.txt": b"qualified payload\n",
    }
    lock = {
        "schema": "openubmc.codex-plugin.v1",
        "name": "openubmc",
        "version": "2.1.0",
        "source_commit": "a" * 40,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
        "manifest_digest": hashlib.sha256(files[".codex-plugin/plugin.json"]).hexdigest(),
    }
    lock["content_digest"] = hashlib.sha256(canonical(lock)).hexdigest()
    files["plugin-lock.json"] = canonical(lock)
    archive = root / "openubmc.tar.gz"
    with archive.open("wb") as output:
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as bundle:
                for name, content in files.items():
                    member = tarfile.TarInfo("openubmc/" + name)
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
    marketplace = root / "marketplace"
    plugin = marketplace / "plugins/openubmc"
    for name, content in files.items():
        target = plugin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    manifest = marketplace / ".agents/plugins/marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(canonical({"name": "openubmc-public", "plugins": [
        {"name": "openubmc", "source": {"source": "local", "path": "./plugins/openubmc"}}
    ]}))
    return archive, marketplace


class PublicMarketplaceIdentityTests(unittest.TestCase):
    def test_accepts_the_exact_archive_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, marketplace = fixture(Path(temporary))
            result = verify_public_marketplace(archive, marketplace)
            self.assertEqual(result["version"], "2.1.0")
            self.assertEqual(result["source_commit"], "a" * 40)
            self.assertEqual(result["marketplace"], "openubmc-public")

    def test_rejects_public_payload_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, marketplace = fixture(Path(temporary))
            (marketplace / "plugins/openubmc/payload.txt").write_text("drift\n")
            with self.assertRaisesRegex(ValueError, "inventory"):
                verify_public_marketplace(archive, marketplace)


if __name__ == "__main__":
    unittest.main()
