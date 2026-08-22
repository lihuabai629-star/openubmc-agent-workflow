"""Anonymous persistent telemetry for compatibility retirement decisions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
import time
from typing import Protocol


_LEGACY_OPERATION_FEATURES = frozenset({"phase_record", "workflow.next"})


@dataclass(frozen=True)
class CompatibilityMetric:
    count: int
    last_seen_at: float


class CompatibilityTelemetryRepository(Protocol):
    """Persist anonymous metric counters shared by Runtime instances."""

    def increment(self, metric_kind: str, metric_name: str) -> None: ...

    def metrics(self, metric_kind: str) -> Mapping[str, CompatibilityMetric]: ...

    def tracking_started_at(self) -> float: ...


class InMemoryCompatibilityTelemetryRepository:
    def __init__(self) -> None:
        self._metrics: dict[tuple[str, str], CompatibilityMetric] = {}
        self._tracking_started_at = time.time()
        self._lock = threading.RLock()

    def increment(self, metric_kind: str, metric_name: str) -> None:
        identity = (metric_kind, metric_name)
        with self._lock:
            prior = self._metrics.get(identity)
            self._metrics[identity] = CompatibilityMetric(
                count=(prior.count if prior is not None else 0) + 1,
                last_seen_at=time.time(),
            )

    def metrics(self, metric_kind: str) -> Mapping[str, CompatibilityMetric]:
        with self._lock:
            return {
                name: metric
                for (kind, name), metric in self._metrics.items()
                if kind == metric_kind
            }

    def tracking_started_at(self) -> float:
        return self._tracking_started_at


class SQLiteCompatibilityTelemetryRepository:
    """Atomic counters stored beside the SQLite Runtime repository."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS compatibility_telemetry (
                    metric_kind TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (metric_kind, metric_name)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS compatibility_telemetry_meta (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO compatibility_telemetry_meta "
                "(key, value) VALUES ('tracking_started_at', ?)",
                (time.time(),),
            )

    def increment(self, metric_kind: str, metric_name: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO compatibility_telemetry
                    (metric_kind, metric_name, count, updated_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(metric_kind, metric_name) DO UPDATE SET
                    count = compatibility_telemetry.count + 1,
                    updated_at = excluded.updated_at
                """,
                (metric_kind, metric_name, time.time()),
            )

    def metrics(self, metric_kind: str) -> Mapping[str, CompatibilityMetric]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT metric_name, count, updated_at "
                "FROM compatibility_telemetry "
                "WHERE metric_kind = ? ORDER BY metric_name",
                (metric_kind,),
            ).fetchall()
        return {
            str(row["metric_name"]): CompatibilityMetric(
                count=int(row["count"]),
                last_seen_at=float(row["updated_at"]),
            )
            for row in rows
        }

    def tracking_started_at(self) -> float:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM compatibility_telemetry_meta "
                "WHERE key = 'tracking_started_at'"
            ).fetchone()
        if row is None:
            raise RuntimeError("compatibility telemetry start time is unavailable")
        return float(row["value"])


class CompatibilityTelemetry:
    """Recognize compatibility usage and expose one anonymous status shape."""

    def __init__(self, repository: CompatibilityTelemetryRepository) -> None:
        self.repository = repository

    def record_operation(self, name: str) -> None:
        self.repository.increment("operation", name)
        if name in _LEGACY_OPERATION_FEATURES:
            self.repository.increment("feature", name)

    def record_agent_input(
        self,
        operation: str,
        arguments: Mapping[str, object],
    ) -> None:
        if operation == "observe" and "assurance" in arguments:
            self.repository.increment("feature", "observe.assurance")
            return
        if operation != "execute":
            return
        if isinstance(arguments.get("observation_receipt"), Mapping):
            self.repository.increment(
                "feature", "execute.observation_receipt"
            )
        if (
            str(arguments.get("kind", "")).strip().lower() == "control"
            and str(arguments.get("command", "")).strip().lower() == "continue"
        ):
            self.repository.increment("feature", "execute.control_continue")

    def status(self) -> dict[str, object]:
        operations = dict(sorted(self.repository.metrics("operation").items()))
        features = dict(sorted(self.repository.metrics("feature").items()))
        operation_counts = {
            name: metric.count for name, metric in operations.items()
        }
        feature_counts = {
            name: metric.count for name, metric in features.items()
        }
        return {
            "tracking_started_at": self.repository.tracking_started_at(),
            "total_calls": sum(operation_counts.values()),
            "operation_counts": operation_counts,
            "total_features": sum(feature_counts.values()),
            "feature_counts": feature_counts,
            "last_seen_at": {
                "operations": {
                    name: metric.last_seen_at
                    for name, metric in operations.items()
                },
                "features": {
                    name: metric.last_seen_at
                    for name, metric in features.items()
                },
            },
        }
