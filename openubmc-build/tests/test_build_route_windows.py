"""Native Windows routing keeps build prerequisites separate from device work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROUTE = Path(__file__).resolve().parents[1] / "scripts" / "build_route.py"


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class NativeWindowsBuildRouteTests(unittest.TestCase):
    def test_missing_bingo_returns_typed_build_environment_blocker(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            root = Path(raw)
            workspace = root / "component"
            (workspace / "mds").mkdir(parents=True)
            (workspace / "mds" / "service.json").write_text("{}\n")
            empty_path = root / "empty-path"
            empty_path.mkdir()
            result = subprocess.run(
                [sys.executable, "-B", str(ROUTE), "--request", "run bingo build",
                 "--workspace", str(workspace), "--argv", "bingo", "build"],
                env={**os.environ, "PATH": str(empty_path)},
                capture_output=True, text=True, timeout=10,
            )

        receipt = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(receipt["owner"], "openubmc-bingo-build")
        self.assertFalse(receipt["ready"])
        self.assertEqual(receipt["blocker"]["code"], "build_environment_unavailable")
        self.assertEqual(receipt["blocker"]["execution_host"], "windows-native")
        self.assertEqual(receipt["blocker"]["missing_tools"], ["bingo"])

    def test_missing_conan_publish_returns_typed_build_environment_blocker(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            empty_path = Path(raw) / "empty-path"
            empty_path.mkdir()
            result = subprocess.run(
                [sys.executable, "-B", str(ROUTE), "--request", "publish package",
                 "--argv", "conan", "upload", "synthetic/1.0"],
                env={**os.environ, "PATH": str(empty_path)},
                capture_output=True, text=True, timeout=10,
            )

        receipt = json.loads(result.stdout)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(receipt["owner"], "openubmc-publish")
        self.assertFalse(receipt["ready"])
        self.assertEqual(receipt["blocker"]["code"], "build_environment_unavailable")
        self.assertEqual(receipt["blocker"]["missing_tools"], ["conan"])


if __name__ == "__main__":
    unittest.main()
