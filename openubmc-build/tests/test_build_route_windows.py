"""Native Windows is rejected before any build tool lookup."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_route import route_receipt

class NativeWindowsBuildRouteTests(unittest.TestCase):
    def test_native_windows_requires_linux_or_wsl(self):
        for request, argv in (("compile", ["bmcgo", "test", "-ut"]),
                              ("publish package", ["conan", "upload", "synthetic/1.0"])):
            with self.subTest(request=request), patch("sys.platform", "win32"):
                receipt = route_receipt(request, argv=argv)
                self.assertFalse(receipt["ready"])
                self.assertEqual(receipt["tool"], "")
                self.assertEqual(receipt["blocker"]["code"], "unsupported_platform")
                self.assertIn("WSL", receipt["blocker"]["detail"])
