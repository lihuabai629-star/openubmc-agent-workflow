"""Release qualification must attribute Runtime lifecycle through the MCP bootstrap."""
from __future__ import annotations

import unittest
from pathlib import Path
import subprocess
import sys
import threading

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from qualify_plugin import select_runtime_lifecycle, wait_for_process_exit  # noqa: E402


class PluginQualificationLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process-exit evidence')
    def test_delayed_physical_exit_is_observed_before_qualification(self) -> None:
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.15)'])
        reaper = threading.Thread(target=child.wait)
        reaper.start()
        try:
            wait_for_process_exit(child.pid, timeout=2)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.poll() is None:
                child.kill()
            reaper.join(timeout=2)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process-exit evidence')
    def test_live_child_still_blocks_qualification_at_the_deadline(self) -> None:
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            with self.assertRaisesRegex(ValueError, 'survived its Codex parent'):
                wait_for_process_exit(child.pid, timeout=0.02)
            self.assertIsNone(child.poll())
        finally:
            child.kill()
            child.wait(timeout=2)

    def record(self, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "component": "target-runtime",
            "client": "codex",
            "task_id": "plugin-qualification",
            "session_id": "session-one",
            "source_commit": "a" * 40,
            "formal_run": True,
            "parent_pid": 2200,
            "process_id": 2201,
        }
        record.update(overrides)
        return record

    def test_accepts_one_runtime_owned_through_the_bootstrap_supervisor(self) -> None:
        lifecycle = select_runtime_lifecycle(
            [self.record()], session_id="session-one", source_commit="a" * 40
        )

        self.assertEqual(lifecycle["parent_pid"], 2200)
        self.assertEqual(lifecycle["process_id"], 2201)

    def test_rejects_duplicate_runtime_records_for_one_invocation(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            select_runtime_lifecycle(
                [self.record(), self.record(process_id=2202)],
                session_id="session-one",
                source_commit="a" * 40,
            )

    def test_ignores_records_from_another_session_or_source(self) -> None:
        expected = self.record()
        lifecycle = select_runtime_lifecycle(
            [
                self.record(session_id="older-session", process_id=2101),
                self.record(source_commit="b" * 40, process_id=2102),
                expected,
            ],
            session_id="session-one",
            source_commit="a" * 40,
        )

        self.assertIs(lifecycle, expected)


if __name__ == "__main__":
    unittest.main()
