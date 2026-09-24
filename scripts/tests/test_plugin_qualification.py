"""Release qualification must attribute Runtime lifecycle through the MCP bootstrap."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from qualify_plugin import select_runtime_lifecycle  # noqa: E402


class PluginQualificationLifecycleTests(unittest.TestCase):
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
