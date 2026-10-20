"""Native Windows process ownership through the Runtime lifecycle interface."""
from __future__ import annotations

import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class WindowsMcpLifecycleTests(unittest.TestCase):
    def test_repeated_process_probes_do_not_retain_ctypes_types(self):
        from openubmc_target_runtime.mcp_lifecycle import _windows_process_state

        process_id = os.getpid()
        expected = _windows_process_state(process_id)
        self.assertTrue(expected[0])
        self.assertNotEqual(expected[1], "unknown")
        for _ in range(25):
            _windows_process_state(process_id)

        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        try:
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(3000):
                self.assertEqual(_windows_process_state(process_id), expected)
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0] - before
        finally:
            if not already_tracing:
                tracemalloc.stop()

        self.assertLess(retained, 1024 * 1024, f"probes retained {retained} bytes")

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
