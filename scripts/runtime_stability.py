#!/usr/bin/env python3
"""Measure hermetic Runtime stability through Agent and Operator interfaces."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import platform
from pathlib import Path
import resource
import sys
import tempfile
import threading
import time
import tracemalloc
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(ROOT))

from scripts.evidence_report import (  # noqa: E402
    evidence_fingerprint,
    source_commit as selected_source_commit,
)
from scripts.runtime_stability_contract import (  # noqa: E402
    CAPACITY_BATCH_SIZE,
    CAPACITY_RUNS,
    GATE_WORKERS,
    MAX_CAPACITY_PEAK_PYTHON_BYTES,
    MAX_CAPACITY_PEAK_RSS_BYTES,
    MAX_CAPACITY_SECONDS,
    MAX_CAPACITY_STORAGE_BYTES,
    MAX_EVENTS_PER_RUN,
    MAX_SOAK_PEAK_BYTES,
    MAX_SOAK_PEAK_RSS_BYTES,
    MAX_SOAK_SECONDS,
    MAX_SOAK_STORAGE_BYTES,
    SCHEMA,
    SOAK_RESTART_CYCLES,
    SOAK_RUNS_PER_CYCLE,
    STORM_WORKERS,
    ci_parameters,
)

from openubmc_target_runtime.context_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    SQLiteRuntimeRepository,
)
from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from openubmc_target_runtime.semantic_runtime import CommandConflict  # noqa: E402


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _HermeticBackend:
    """Read-only qualification Adapter with no BMC or network dependency."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    @staticmethod
    def open_task(task_id: str) -> _Task:
        return _Task(task_id)

    @staticmethod
    def close_task(_task: _Task) -> None:
        return None

    @staticmethod
    def maintain_task(_task: _Task) -> int:
        return 0

    @staticmethod
    def task_status(task: _Task) -> dict[str, object]:
        return {"task_id": task.task_id}

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        del arguments
        context.raise_if_stopped()
        with self._lock:
            self.calls += 1
        return {
            "ok": True,
            "schema": "openubmc-debug.v1",
            "task": task.task_id,
            "summary": "hermetic diagnosis completed",
        }


class _RuntimeStorage(NamedTuple):
    database: Path
    blobs: Path


def _agent(storage: _RuntimeStorage) -> tuple[_HermeticBackend, RuntimeMcpService]:
    backend = _HermeticBackend()
    return backend, RuntimeMcpService(
        backend,
        context_repository=SQLiteRuntimeRepository(storage.database),
        blob_repository=FilesystemBlobRepository(storage.blobs),
    )


def _case_record(
    repository: SQLiteRuntimeRepository,
    run_id: str,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    projection = repository.load(run_id)
    if projection is None:
        raise RuntimeError(f"Runtime repository lacks Run {run_id}")
    return projection, repository.events(run_id)


def _storage_bytes(
    repository: SQLiteRuntimeRepository,
    blobs: FilesystemBlobRepository,
) -> int:
    return repository.size_bytes() + blobs.size_bytes()


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _duplicate_storm(root: Path) -> dict[str, object]:
    storage = _RuntimeStorage(root / "storm.sqlite3", root / "storm-blobs")
    adapters = [_agent(storage) for _ in range(STORM_WORKERS)]
    barrier = threading.Barrier(STORM_WORKERS)
    action = {
        "kind": "start",
        "target": "192.0.2.91",
        "intent": "diagnosis-only",
        "purpose": "qualify duplicate delivery convergence",
    }

    def execute(index: int) -> tuple[dict[str, object] | None, str]:
        try:
            barrier.wait(timeout=5)
            return (
                adapters[index][1].call_exposed_tool(
                    "execute",
                    action,
                    task_id=f"duplicate-storm-{index}",
                    operation_id="duplicate-storm-start",
                ),
                "",
            )
        except Exception as exc:  # pragma: no cover - reported as evidence
            return None, f"{type(exc).__name__}: {exc}"

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=STORM_WORKERS) as executor:
            results = list(executor.map(execute, range(STORM_WORKERS)))
    finally:
        for _backend, service in adapters:
            service.close()

    final_backend, final_agent = _agent(storage)
    try:
        final_turn = final_agent.call_exposed_tool(
            "execute",
            action,
            task_id="duplicate-storm-final",
            operation_id="duplicate-storm-start",
        )
        conflicting = dict(action)
        conflicting["target"] = "192.0.2.92"
        conflict_rejected = False
        try:
            final_agent.call_exposed_tool(
                "execute",
                conflicting,
                task_id="duplicate-storm-conflict",
                operation_id="duplicate-storm-start",
            )
        except CommandConflict:
            conflict_rejected = True
    finally:
        final_agent.close()

    turns = [turn for turn, _error in results if turn is not None]
    errors = [error for _turn, error in results if error]
    run_ids = {str(turn["run_id"]) for turn in turns}
    run_id = str(final_turn["run_id"])
    projection, replay = _case_record(SQLiteRuntimeRepository(storage.database), run_id)

    states = Counter(str(turn["state"]) for turn in turns)
    outcome_events = sum(
        event["kind"] == "RunOutcomeRecorded" for event in replay
    )
    command_decisions = sum(
        decision.get("command_id") == "duplicate-storm-start"
        for decision in projection["run_decisions"]
    )
    backend_calls = sum(backend.calls for backend, _service in adapters)
    backend_calls += final_backend.calls
    passed = all(
        (
            len(run_ids | {run_id}) == 1,
            not errors,
            set(states).issubset({"running", "completed"}),
            final_turn["state"] == "completed",
            conflict_rejected,
            projection["operation_count"] == 1,
            projection["incidents"] == [],
            outcome_events == 1,
            command_decisions == 1,
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "execute_calls": STORM_WORKERS + 2,
        "failed_calls": len(errors),
        "errors": errors,
        "concurrent_workers": STORM_WORKERS,
        "unique_runs": len(run_ids | {run_id}),
        "turn_states": dict(sorted(states.items())),
        "backend_read_calls": backend_calls,
        "operation_count": projection["operation_count"],
        "command_decisions": command_decisions,
        "outcome_events": outcome_events,
        "open_incidents": len(projection["incidents"]),
        "same_key_different_hash_rejected": conflict_rejected,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _gate_concurrency(root: Path) -> dict[str, object]:
    storage = _RuntimeStorage(root / "gate.sqlite3", root / "gate-blobs")
    _start_backend, starter = _agent(storage)
    try:
        waiting = starter.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.93",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "qualify concurrent Gate submission",
            },
            task_id="gate-concurrency-start",
            operation_id="gate-concurrency-start",
        )
    finally:
        starter.close()

    adapters = [_agent(storage) for _ in range(GATE_WORKERS)]
    barrier = threading.Barrier(GATE_WORKERS)
    response = {
        "kind": "respond",
        "run_id": waiting["run_id"],
        "gate_id": waiting["gate"]["gate_id"],
        "gate_version": waiting["gate"]["gate_version"],
        "schema_digest": waiting["gate"]["schema_digest"],
        "submission_id": "gate-concurrency-submission",
        "response": {
            "status": "completed",
            "summary": "source change completed",
            "payload": {
                "source_revision": "gate-concurrency-source",
                "authored_files": ["src/fix.py"],
                "verification_plan": ["run hermetic checks"],
            },
        },
    }

    def submit(index: int) -> tuple[dict[str, object] | None, str]:
        try:
            barrier.wait(timeout=5)
            return (
                adapters[index][1].call_exposed_tool(
                    "execute",
                    response,
                    task_id=f"gate-concurrency-{index}",
                    operation_id=f"gate-concurrency-{index}",
                ),
                "",
            )
        except Exception as exc:  # pragma: no cover - reported as evidence
            return None, f"{type(exc).__name__}: {exc}"

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=GATE_WORKERS) as executor:
            results = list(executor.map(submit, range(GATE_WORKERS)))
    finally:
        for _backend, service in adapters:
            service.close()

    turns = [turn for turn, _error in results if turn is not None]
    errors = [error for _turn, error in results if error]
    projection, replay = _case_record(
        SQLiteRuntimeRepository(storage.database),
        str(waiting["run_id"]),
    )
    run_ids = {str(turn["run_id"]) for turn in turns}
    gate_submissions = len(projection["gate_submissions"])
    outcome_events = sum(
        event["kind"] == "RunOutcomeRecorded" for event in replay
    )
    passed = all(
        (
            not errors,
            run_ids == {str(waiting["run_id"])},
            gate_submissions == 1,
            outcome_events == 1,
            projection["run_outcome"].get("status") == "completed",
            projection["incidents"] == [],
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "execute_calls": GATE_WORKERS,
        "failed_calls": len(errors),
        "errors": errors,
        "unique_runs": len(run_ids),
        "turn_states": dict(
            sorted(Counter(str(turn["state"]) for turn in turns).items())
        ),
        "gate_submissions": gate_submissions,
        "outcome_events": outcome_events,
        "open_incidents": len(projection["incidents"]),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _capacity(root: Path) -> dict[str, object]:
    storage = _RuntimeStorage(root / "capacity.sqlite3", root / "capacity-blobs")
    repository = SQLiteRuntimeRepository(storage.database)
    blobs = FilesystemBlobRepository(storage.blobs)
    run_ids: list[str] = []
    execute_calls = 0
    failed_calls = 0
    completed_turns = 0
    events_per_batch: list[int] = []
    cumulative_events_by_batch: list[int] = []
    storage_bytes_by_batch: list[int] = []
    total_events = 0
    tracemalloc.start()
    started = time.monotonic()
    try:
        backend, agent = _agent(storage)
        try:
            for batch_start in range(0, CAPACITY_RUNS, CAPACITY_BATCH_SIZE):
                batch_run_ids: list[str] = []
                for index in range(batch_start, batch_start + CAPACITY_BATCH_SIZE):
                    operation_id = f"capacity-{index:04d}"
                    execute_calls += 1
                    try:
                        turn = agent.call_exposed_tool(
                            "execute",
                            {
                                "kind": "start",
                                "target": f"198.51.100.{index % 250 + 1}",
                                "intent": "diagnosis-only",
                                "purpose": "qualify bounded Runtime capacity",
                            },
                            task_id=operation_id,
                            operation_id=operation_id,
                        )
                    except Exception:  # pragma: no cover - counted as evidence
                        failed_calls += 1
                        continue
                    completed_turns += int(turn["state"] == "completed")
                    run_id = str(turn["run_id"])
                    run_ids.append(run_id)
                    batch_run_ids.append(run_id)
                batch_events = sum(
                    len(repository.events(run_id)) for run_id in batch_run_ids
                )
                total_events += batch_events
                events_per_batch.append(batch_events)
                cumulative_events_by_batch.append(total_events)
                storage_bytes_by_batch.append(_storage_bytes(repository, blobs))
        finally:
            agent.close()
        backend_calls = backend.calls
    finally:
        elapsed_seconds = time.monotonic() - started
        _current_bytes, peak_python_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peak_rss_bytes = _peak_rss_bytes()

    invalid_runs = 0
    outcome_events = 0
    open_incidents = 0
    incomplete_operations = 0
    max_events_per_run = 0
    for run_id in run_ids:
        projection, events = _case_record(repository, run_id)
        event_count = len(events)
        max_events_per_run = max(max_events_per_run, event_count)
        outcome_count = sum(
            event["kind"] == "RunOutcomeRecorded" for event in events
        )
        outcome_events += outcome_count
        open_incidents += len(projection["incidents"])
        incomplete = sum(
            operation["status"] != "completed"
            for operation in projection["operations"]
        )
        incomplete_operations += incomplete
        invalid_runs += int(
            projection["run_outcome"].get("status") != "completed"
            or outcome_count != 1
            or projection["incidents"] != []
            or incomplete != 0
        )

    storage_bytes = _storage_bytes(repository, blobs)
    passed = all(
        (
            execute_calls == CAPACITY_RUNS,
            failed_calls == 0,
            completed_turns == CAPACITY_RUNS,
            len(set(run_ids)) == CAPACITY_RUNS,
            invalid_runs == 0,
            outcome_events == CAPACITY_RUNS,
            open_incidents == 0,
            incomplete_operations == 0,
            max_events_per_run <= MAX_EVENTS_PER_RUN,
            total_events <= CAPACITY_RUNS * MAX_EVENTS_PER_RUN,
            storage_bytes <= MAX_CAPACITY_STORAGE_BYTES,
            peak_python_bytes <= MAX_CAPACITY_PEAK_PYTHON_BYTES,
            peak_rss_bytes <= MAX_CAPACITY_PEAK_RSS_BYTES,
            elapsed_seconds <= MAX_CAPACITY_SECONDS,
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "execute_calls": execute_calls,
        "failed_calls": failed_calls,
        "completed_turns": completed_turns,
        "completed_runs": len(set(run_ids)),
        "invalid_runs": invalid_runs,
        "backend_read_calls": backend_calls,
        "outcome_events": outcome_events,
        "open_incidents": open_incidents,
        "incomplete_operations": incomplete_operations,
        "total_events": total_events,
        "events_per_batch": events_per_batch,
        "cumulative_events_by_batch": cumulative_events_by_batch,
        "storage_bytes_by_batch": storage_bytes_by_batch,
        "max_events_per_run": max_events_per_run,
        "storage_bytes": storage_bytes,
        "peak_rss_bytes": peak_rss_bytes,
        "peak_python_allocation_bytes": peak_python_bytes,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _restart_soak(root: Path) -> dict[str, object]:
    storage = _RuntimeStorage(root / "soak.sqlite3", root / "soak-blobs")
    repository = SQLiteRuntimeRepository(storage.database)
    blobs = FilesystemBlobRepository(storage.blobs)
    run_ids: list[str] = []
    backend_calls = 0
    execute_calls = 0
    failed_calls = 0
    completed_turns = 0
    replay_mismatches = 0
    invalid_runs = 0
    outcome_events = 0
    open_incidents = 0
    incomplete_operations = 0
    total_events = 0
    max_events_per_run = 0
    max_run_decisions = 0
    events_per_cycle: list[int] = []
    cumulative_events_by_cycle: list[int] = []
    storage_bytes_by_cycle: list[int] = []
    tracemalloc.start()
    started = time.monotonic()
    try:
        for cycle in range(SOAK_RESTART_CYCLES):
            cycle_start = len(run_ids)
            backend, agent = _agent(storage)
            try:
                for index in range(SOAK_RUNS_PER_CYCLE):
                    operation_id = f"soak-{cycle:02d}-{index:02d}"
                    action = {
                        "kind": "start",
                        "target": f"192.0.2.{100 + index}",
                        "intent": "diagnosis-only",
                        "purpose": "qualify restart and replay stability",
                    }
                    execute_calls += 1
                    try:
                        first = agent.call_exposed_tool(
                            "execute",
                            action,
                            task_id=operation_id,
                            operation_id=operation_id,
                        )
                    except Exception:  # pragma: no cover - counted as evidence
                        failed_calls += 1
                        continue
                    completed_turns += int(first["state"] == "completed")
                    run_ids.append(str(first["run_id"]))
                    execute_calls += 1
                    try:
                        replayed = agent.call_exposed_tool(
                            "execute",
                            action,
                            task_id=f"{operation_id}-replay",
                            operation_id=operation_id,
                        )
                    except Exception:  # pragma: no cover - counted as evidence
                        failed_calls += 1
                        continue
                    completed_turns += int(replayed["state"] == "completed")
                    replay_mismatches += int(first != replayed)
            finally:
                backend_calls += backend.calls
                agent.close()
            cycle_run_ids = run_ids[cycle_start:]
            cycle_events = 0
            for run_id in cycle_run_ids:
                projection, events = _case_record(repository, run_id)
                event_count = len(events)
                cycle_events += event_count
                max_events_per_run = max(max_events_per_run, event_count)
                max_run_decisions = max(
                    max_run_decisions,
                    len(projection["run_decisions"]),
                )
                outcome_count = sum(
                    event["kind"] == "RunOutcomeRecorded" for event in events
                )
                outcome_events += outcome_count
                open_incidents += len(projection["incidents"])
                incomplete = sum(
                    operation["status"] != "completed"
                    for operation in projection["operations"]
                )
                incomplete_operations += incomplete
                valid = all(
                    (
                        projection["run_outcome"].get("status") == "completed",
                        projection["incidents"] == [],
                        outcome_count == 1,
                        incomplete == 0,
                    )
                )
                invalid_runs += 0 if valid else 1
            total_events += cycle_events
            events_per_cycle.append(cycle_events)
            cumulative_events_by_cycle.append(total_events)
            storage_bytes_by_cycle.append(_storage_bytes(repository, blobs))
    finally:
        elapsed_seconds = time.monotonic() - started
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    peak_rss_bytes = _peak_rss_bytes()

    expected_runs = SOAK_RESTART_CYCLES * SOAK_RUNS_PER_CYCLE
    repository_status = repository.status()
    storage_bytes = _storage_bytes(repository, blobs)
    passed = all(
        (
            len(set(run_ids)) == expected_runs,
            int(repository_status["case_count"]) == expected_runs,
            open_incidents == 0,
            incomplete_operations == 0,
            execute_calls == expected_runs * 2,
            failed_calls == 0,
            completed_turns == expected_runs * 2,
            replay_mismatches == 0,
            invalid_runs == 0,
            outcome_events == expected_runs,
            max_events_per_run <= MAX_EVENTS_PER_RUN,
            total_events <= expected_runs * MAX_EVENTS_PER_RUN,
            storage_bytes <= MAX_SOAK_STORAGE_BYTES,
            peak_bytes <= MAX_SOAK_PEAK_BYTES,
            peak_rss_bytes <= MAX_SOAK_PEAK_RSS_BYTES,
            elapsed_seconds <= MAX_SOAK_SECONDS,
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "restart_cycles": SOAK_RESTART_CYCLES,
        "runs_per_cycle": SOAK_RUNS_PER_CYCLE,
        "completed_runs": len(set(run_ids)),
        "execute_calls": execute_calls,
        "failed_calls": failed_calls,
        "completed_turns": completed_turns,
        "replay_mismatches": replay_mismatches,
        "invalid_runs": invalid_runs,
        "backend_read_calls": backend_calls,
        "outcome_events": outcome_events,
        "open_incidents": open_incidents,
        "incomplete_operations": incomplete_operations,
        "total_events": total_events,
        "events_per_cycle": events_per_cycle,
        "cumulative_events_by_cycle": cumulative_events_by_cycle,
        "storage_bytes_by_cycle": storage_bytes_by_cycle,
        "max_events_per_run": max_events_per_run,
        "max_run_decisions": max_run_decisions,
        "storage_bytes": storage_bytes,
        "peak_rss_bytes": peak_rss_bytes,
        "peak_traced_memory_bytes": peak_bytes,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def qualify_runtime_stability(
    workspace: Path = ROOT,
    *,
    source_commit: str = "",
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as raw:
        evidence_root = Path(raw)
        scenarios: dict[str, dict[str, object]] = {}
        for name, scenario in (
            ("capacity", _capacity),
            ("duplicate_storm", _duplicate_storm),
            ("gate_concurrency", _gate_concurrency),
            ("restart_soak", _restart_soak),
        ):
            try:
                scenarios[name] = scenario(evidence_root)
            except Exception as exc:  # pragma: no cover - fail-closed evidence
                scenarios[name] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
    resolved_source_commit = selected_source_commit(
        source_commit,
        workspace=workspace,
    )
    environment = {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": resolved_source_commit,
        "environment": environment,
        "environment_fingerprint": evidence_fingerprint(environment),
        "parameters": ci_parameters(),
        "scenarios": scenarios,
        "promotable": all(
            scenario["status"] == "passed"
            for scenario in scenarios.values()
        ),
    }
    report["evidence_digest"] = evidence_fingerprint(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-commit", default="")
    args = parser.parse_args(argv)
    report = qualify_runtime_stability(
        args.workspace.expanduser().absolute(),
        source_commit=args.source_commit.strip().lower(),
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
