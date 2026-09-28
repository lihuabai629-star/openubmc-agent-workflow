from __future__ import annotations

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
from tests.compatibility_history import seed_compatibility_history  # noqa: E402


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
        if sys.platform == "win32":
            from openubmc_target_runtime.windows_private import harden_new_file
        with tempfile.TemporaryDirectory() as raw, mock.patch(
            "openubmc_target_runtime.compatibility.sqlite3.connect",
            return_value=connection,
        ):
            database = Path(raw) / "runtime.sqlite3"
            if sys.platform == "win32":
                database.touch()
                harden_new_file(database)
            with self.assertRaisesRegex(OSError, "sqlite pragma failed"):
                SQLiteCompatibilityTelemetryRepository(database)

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
                telemetry.status()

        self.assertEqual(len(closed), 3)

    def test_sqlite_repository_reads_retained_history_without_a_writer_api(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            seed_compatibility_history(
                database,
                (("operation", "debug_run", 40, 1234.5),),
            )
            repository = SQLiteCompatibilityTelemetryRepository(database)
            status = CompatibilityTelemetry(repository).status()

        self.assertFalse(hasattr(repository, "increment"))
        self.assertEqual(status["total_calls"], 40)
        self.assertEqual(status["operation_counts"], {"debug_run": 40})
        self.assertGreater(status["tracking_started_at"], 0)
        self.assertEqual(
            status["last_seen_at"]["operations"]["debug_run"],
            1234.5,
        )


if __name__ == "__main__":
    unittest.main()
