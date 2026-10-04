"""Native Windows process ownership through the Runtime lifecycle interface."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class WindowsMcpLifecycleTests(unittest.TestCase):
    def test_process_probe_observes_a_live_owned_child_without_terminating_it(self):
        from openubmc_target_runtime.mcp_lifecycle import _default_process_alive, _default_process_identity
        from openubmc_target_runtime.context_runtime import _process_owner_is_active, _process_start_marker

        child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertTrue(_default_process_alive(child.pid))
            self.assertIsNone(child.poll())
            self.assertNotEqual(_default_process_identity(child.pid), "unknown")
            marker = _process_start_marker(child.pid)
            self.assertEqual(marker, _default_process_identity(child.pid))
            self.assertTrue(_process_owner_is_active(child.pid, marker))
            self.assertFalse(_process_owner_is_active(child.pid, "wrong-owner-identity"))
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(_default_process_alive(child.pid))
        self.assertFalse(_process_owner_is_active(child.pid, marker))


if __name__ == "__main__":
    unittest.main()
