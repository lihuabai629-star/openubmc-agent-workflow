from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openubmc_target_runtime.compatibility import (  # noqa: E402
    CompatibilityTelemetry,
    SQLiteCompatibilityTelemetryRepository,
)


class CompatibilityTelemetryTests(unittest.TestCase):
    def test_sqlite_repository_closes_connection_when_setup_fails(self) -> None:
        class FailingConnection:
            row_factory = None

            def __init__(self) -> None:
                self.closed = False

            def execute(self, _statement: str):
                raise OSError("sqlite pragma failed")

            def close(self) -> None:
                self.closed = True

        connection = FailingConnection()
        with tempfile.TemporaryDirectory() as raw, mock.patch(
            "openubmc_target_runtime.compatibility.sqlite3.connect",
            return_value=connection,
        ):
            with self.assertRaisesRegex(OSError, "sqlite pragma failed"):
                SQLiteCompatibilityTelemetryRepository(
                    Path(raw) / "runtime.sqlite3"
                )

        self.assertTrue(connection.closed)

    def test_sqlite_repository_closes_every_short_lived_connection(self) -> None:
        class TrackingConnection:
            def __init__(self, connection, closed: list[bool]) -> None:
                self.connection = connection
                self.closed = closed

            def __enter__(self):
                self.connection.__enter__()
                return self

            def __exit__(self, *args):
                return self.connection.__exit__(*args)

            def close(self) -> None:
                self.closed.append(True)
                self.connection.close()

            def __getattr__(self, name: str):
                return getattr(self.connection, name)

        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            repository = SQLiteCompatibilityTelemetryRepository(database)
            original_connect = repository._connect
            closed: list[bool] = []

            def tracked_connect():
                return TrackingConnection(original_connect(), closed)

            telemetry = CompatibilityTelemetry(repository)
            with mock.patch.object(
                repository,
                "_connect",
                side_effect=tracked_connect,
            ):
                telemetry.record_operation("debug_run")
                telemetry.status()

        self.assertEqual(len(closed), 4)

    def test_sqlite_counters_are_atomic_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            telemetry = tuple(
                CompatibilityTelemetry(
                    SQLiteCompatibilityTelemetryRepository(database)
                )
                for _index in range(4)
            )

            def record(index: int) -> None:
                telemetry[index % len(telemetry)].record_operation("debug_run")

            with ThreadPoolExecutor(max_workers=4) as executor:
                tuple(executor.map(record, range(40)))

            status = telemetry[0].status()

        self.assertEqual(status["total_calls"], 40)
        self.assertEqual(status["operation_counts"], {"debug_run": 40})
        self.assertGreater(status["tracking_started_at"], 0)
        self.assertGreater(
            status["last_seen_at"]["operations"]["debug_run"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
