from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "restore_ab_bundle.py"
SPEC = importlib.util.spec_from_file_location("restore_ab_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


class RestoreAbBundleTests(unittest.TestCase):
    @staticmethod
    def write_bundle(path: Path, *, symlink_name: str = "") -> None:
        with tarfile.open(path, "w:xz") as archive:
            for name in restore.EXPECTED_MEMBERS:
                info = tarfile.TarInfo(name)
                if name == symlink_name:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/tmp/untrusted-ab-evidence"
                    archive.addfile(info)
                    continue
                content = b"{}\n"
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def test_restores_exact_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "evidence.tar.xz"
            destination = root / "restored"
            self.write_bundle(archive)

            restore.restore_bundle(archive, destination)

            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                list(restore.EXPECTED_MEMBERS),
            )

    def test_rejects_a_symlink_with_an_expected_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "evidence.tar.xz"
            destination = root / "restored"
            self.write_bundle(archive, symlink_name="summary.json")

            with self.assertRaisesRegex(ValueError, "regular files"):
                restore.restore_bundle(archive, destination)

            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
