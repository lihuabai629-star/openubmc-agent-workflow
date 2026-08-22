from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openubmc_target_runtime.compatibility import (  # noqa: E402
    CompatibilityTelemetry,
    SQLiteCompatibilityTelemetryRepository,
)


class CompatibilityTelemetryTests(unittest.TestCase):
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
