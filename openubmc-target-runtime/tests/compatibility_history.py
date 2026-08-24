from __future__ import annotations

from pathlib import Path
import sqlite3

from openubmc_target_runtime.compatibility import (
    SQLiteCompatibilityTelemetryRepository,
)


def seed_compatibility_history(
    database: Path,
    metrics: tuple[tuple[str, str, int, float], ...],
) -> None:
    """Seed pre-retirement rows without restoring a production writer API."""

    SQLiteCompatibilityTelemetryRepository(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO compatibility_telemetry "
            "(metric_kind, metric_name, count, updated_at) "
            "VALUES (?, ?, ?, ?)",
            metrics,
        )
