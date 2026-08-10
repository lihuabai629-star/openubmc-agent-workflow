from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    TASK_CONTEXT_SCHEMA,
    TASK_CONTEXT_VERSION,
    TaskContextStore,
    TaskContextTooLarge,
)


class FakeWallClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class CountingTaskContextStore(TaskContextStore):
    def __init__(self, root: Path, *, clock: FakeWallClock) -> None:
        super().__init__(root, clock=clock)
        self.write_count = 0

    def _write_atomic(self, path: Path, encoded: bytes) -> None:
        self.write_count += 1
        super()._write_atomic(path, encoded)


class TaskContextStoreTests(unittest.TestCase):
    def test_atomic_round_trip_and_version_zero_migration(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            clock = FakeWallClock()
            store = TaskContextStore(root, clock=clock)
            store.save("task-a", {"target": "192.0.2.10"})
            path = store._path_for("task-a")
            document = json.loads(path.read_text(encoding="utf-8"))
            document["version"] = 0
            document.pop("accessed_at")
            document["updated_at"] = clock()
            path.write_text(json.dumps(document), encoding="utf-8")

            restored = store.load("task-a")
            migrated = json.loads(path.read_text(encoding="utf-8"))
            temporary_files = list(root.glob(".*.tmp"))

        self.assertEqual(restored, {"target": "192.0.2.10"})
        self.assertEqual(migrated["schema"], TASK_CONTEXT_SCHEMA)
        self.assertEqual(migrated["version"], TASK_CONTEXT_VERSION)
        self.assertIn("accessed_at", migrated)
        self.assertEqual(temporary_files, [])

    def test_ttl_and_lru_bound_reclaim_only_old_context_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            clock = FakeWallClock()
            store = TaskContextStore(
                root,
                ttl_seconds=30,
                max_entries=2,
                clock=clock,
            )
            store.save("task-a", {"value": "a"})
            clock.advance(1)
            store.save("task-b", {"value": "b"})
            clock.advance(1)
            self.assertEqual(store.load("task-a"), {"value": "a"})
            clock.advance(1)
            store.save("task-c", {"value": "c"})

            self.assertIsNone(store.load("task-b"))
            self.assertEqual(store.load("task-a"), {"value": "a"})
            self.assertEqual(store.load("task-c"), {"value": "c"})
            clock.advance(31)
            self.assertEqual(store.reap(), 2)
            self.assertEqual(store.status()["entry_count"], 0)

    def test_oversized_context_invalidates_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = TaskContextStore(root, max_state_bytes=1024)
            store.save("task-a", {"value": "small"})

            with self.assertRaises(TaskContextTooLarge):
                store.save("task-a", {"value": "x" * 4096})

            self.assertIsNone(store.load("task-a"))

    def test_reconnect_touches_access_time_only_after_the_write_interval(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            clock = FakeWallClock()
            store = CountingTaskContextStore(Path(raw), clock=clock)
            store.save("task-a", {"target": "192.0.2.10"})

            self.assertEqual(store.load("task-a"), {"target": "192.0.2.10"})
            clock.advance(299)
            self.assertEqual(store.load("task-a"), {"target": "192.0.2.10"})
            self.assertEqual(store.write_count, 1)

            clock.advance(1)
            self.assertEqual(store.load("task-a"), {"target": "192.0.2.10"})
            self.assertEqual(store.write_count, 2)


if __name__ == "__main__":
    unittest.main()
