#!/usr/bin/env python3
"""Measure hermetic Runtime stability through Agent and Operator interfaces."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.context_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    SQLiteRuntimeRepository,
)
from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from openubmc_target_runtime.semantic_runtime import CommandConflict  # noqa: E402


SCHEMA = "openubmc-agent-workflow.runtime-stability.v1"
STORM_WORKERS = 16
SOAK_RESTART_CYCLES = 4
SOAK_RUNS_PER_CYCLE = 16
MAX_SOAK_SECONDS = 30.0
MAX_SOAK_PEAK_BYTES = 128 * 1024 * 1024
MAX_SOAK_STORAGE_BYTES = 32 * 1024 * 1024
MAX_EVENTS_PER_COMMAND = 16


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


def _fingerprint(value: object) -> str:
    body = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _source_commit(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    value = completed.stdout.strip().lower()
    return value if completed.returncode == 0 and len(value) == 40 else "unknown"


def _agent(database: Path, blobs: Path) -> tuple[_HermeticBackend, RuntimeMcpService]:
    backend = _HermeticBackend()
    return backend, RuntimeMcpService(
        backend,
        context_repository=SQLiteRuntimeRepository(database),
        blob_repository=FilesystemBlobRepository(blobs),
    )


def _operator(database: Path, blobs: Path) -> RuntimeMcpService:
    return RuntimeMcpService(
        _HermeticBackend(),
        context_repository=SQLiteRuntimeRepository(database),
        blob_repository=FilesystemBlobRepository(blobs),
        interface_profile="operator",
    )


def _duplicate_storm(root: Path) -> dict[str, object]:
    database = root / "storm.sqlite3"
    blobs = root / "storm-blobs"
    adapters = [_agent(database, blobs) for _ in range(STORM_WORKERS)]
    barrier = threading.Barrier(STORM_WORKERS)
    action = {
        "kind": "start",
        "target": "192.0.2.91",
        "intent": "diagnosis-only",
        "purpose": "qualify duplicate delivery convergence",
    }

    def execute(index: int) -> dict[str, object]:
        barrier.wait(timeout=5)
        return adapters[index][1].call_exposed_tool(
            "execute",
            action,
            task_id=f"duplicate-storm-{index}",
            operation_id="duplicate-storm-start",
        )

    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=STORM_WORKERS) as executor:
            turns = list(executor.map(execute, range(STORM_WORKERS)))
    finally:
        for _backend, service in adapters:
            service.close()

    final_backend, final_agent = _agent(database, blobs)
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

    operator = _operator(database, blobs)
    try:
        run_ids = {str(turn["run_id"]) for turn in turns}
        run_id = str(final_turn["run_id"])
        projection = operator.call_exposed_tool(
            "case_read",
            {"case_id": run_id},
            task_id="duplicate-storm-operator",
            operation_id="duplicate-storm-read",
        )
        replay = operator.call_exposed_tool(
            "case_replay_export",
            {"case_id": run_id},
            task_id="duplicate-storm-operator",
            operation_id="duplicate-storm-replay",
        )
    finally:
        operator.close()

    states = Counter(str(turn["state"]) for turn in turns)
    outcome_events = sum(
        event["kind"] == "RunOutcomeRecorded" for event in replay["events"]
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
        "deliveries": STORM_WORKERS + 2,
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


def _restart_soak(root: Path) -> dict[str, object]:
    database = root / "soak.sqlite3"
    blobs = root / "soak-blobs"
    run_ids: list[str] = []
    backend_calls = 0
    tracemalloc.start()
    started = time.monotonic()
    try:
        for cycle in range(SOAK_RESTART_CYCLES):
            backend, agent = _agent(database, blobs)
            try:
                for index in range(SOAK_RUNS_PER_CYCLE):
                    operation_id = f"soak-{cycle:02d}-{index:02d}"
                    action = {
                        "kind": "start",
                        "target": f"192.0.2.{100 + index}",
                        "intent": "diagnosis-only",
                        "purpose": "qualify restart and replay stability",
                    }
                    first = agent.call_exposed_tool(
                        "execute",
                        action,
                        task_id=operation_id,
                        operation_id=operation_id,
                    )
                    replayed = agent.call_exposed_tool(
                        "execute",
                        action,
                        task_id=f"{operation_id}-replay",
                        operation_id=operation_id,
                    )
                    if first != replayed or first["state"] != "completed":
                        raise RuntimeError("soak replay did not return the terminal Turn")
                    run_ids.append(str(first["run_id"]))
            finally:
                backend_calls += backend.calls
                agent.close()
    finally:
        elapsed_seconds = time.monotonic() - started
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    operator = _operator(database, blobs)
    max_revision = 0
    max_run_decisions = 0
    outcome_events = 0
    invalid_runs = 0
    try:
        status = operator.call_exposed_tool(
            "runtime_status",
            {},
            task_id="soak-operator",
            operation_id="soak-status",
        )
        for index, run_id in enumerate(run_ids):
            projection = operator.call_exposed_tool(
                "case_read",
                {"case_id": run_id},
                task_id="soak-operator",
                operation_id=f"soak-read-{index}",
            )
            replay = operator.call_exposed_tool(
                "case_replay_export",
                {"case_id": run_id},
                task_id="soak-operator",
                operation_id=f"soak-replay-{index}",
            )
            max_revision = max(max_revision, int(projection["revision"]))
            max_run_decisions = max(
                max_run_decisions,
                len(projection["run_decisions"]),
            )
            outcome_count = sum(
                event["kind"] == "RunOutcomeRecorded"
                for event in replay["events"]
            )
            outcome_events += outcome_count
            valid = all(
                (
                    projection["run_outcome"].get("status") == "completed",
                    projection["incidents"] == [],
                    outcome_count == 1,
                    all(
                        operation["status"] == "completed"
                        for operation in projection["operations"]
                    ),
                )
            )
            invalid_runs += 0 if valid else 1
    finally:
        operator.close()

    expected_runs = SOAK_RESTART_CYCLES * SOAK_RUNS_PER_CYCLE
    context = status["context_runtime"]
    storage_bytes = int(context["storage_bytes"])
    passed = all(
        (
            len(set(run_ids)) == expected_runs,
            int(context["repository"]["case_count"]) == expected_runs,
            status["incident_metrics"]["open"] == 0,
            invalid_runs == 0,
            outcome_events == expected_runs,
            max_revision <= MAX_EVENTS_PER_COMMAND,
            storage_bytes <= MAX_SOAK_STORAGE_BYTES,
            peak_bytes <= MAX_SOAK_PEAK_BYTES,
            elapsed_seconds <= MAX_SOAK_SECONDS,
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "restart_cycles": SOAK_RESTART_CYCLES,
        "runs_per_cycle": SOAK_RUNS_PER_CYCLE,
        "completed_runs": len(set(run_ids)),
        "invalid_runs": invalid_runs,
        "backend_read_calls": backend_calls,
        "outcome_events": outcome_events,
        "open_incidents": status["incident_metrics"]["open"],
        "max_revision_per_command": max_revision,
        "max_run_decisions": max_run_decisions,
        "storage_bytes": storage_bytes,
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
        duplicate_storm = _duplicate_storm(evidence_root)
        restart_soak = _restart_soak(evidence_root)
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": source_commit or _source_commit(workspace),
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "parameters": {
            "storm_workers": STORM_WORKERS,
            "soak_restart_cycles": SOAK_RESTART_CYCLES,
            "soak_runs_per_cycle": SOAK_RUNS_PER_CYCLE,
            "max_soak_seconds": MAX_SOAK_SECONDS,
            "max_soak_peak_bytes": MAX_SOAK_PEAK_BYTES,
            "max_soak_storage_bytes": MAX_SOAK_STORAGE_BYTES,
            "max_events_per_command": MAX_EVENTS_PER_COMMAND,
        },
        "scenarios": {
            "duplicate_storm": duplicate_storm,
            "restart_soak": restart_soak,
        },
        "promotable": all(
            scenario["status"] == "passed"
            for scenario in (duplicate_storm, restart_soak)
        ),
    }
    report["evidence_digest"] = _fingerprint(report)
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
