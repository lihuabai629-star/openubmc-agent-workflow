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
from collections.abc import Mapping
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
    ARTIFACT_CAPACITY_BATCH_SIZE,
    ARTIFACT_CAPACITY_RECORDS,
    CAPACITY_BATCH_SIZE,
    CAPACITY_RUNS,
    DUAL_PROJECTION_MIN_PREVIEW_BYTES,
    GATE_WORKERS,
    MAX_CAPACITY_PEAK_PYTHON_BYTES,
    MAX_CAPACITY_PEAK_RSS_BYTES,
    MAX_CAPACITY_SECONDS,
    MAX_CAPACITY_STORAGE_BYTES,
    MAX_ARTIFACT_CAPACITY_SECONDS,
    MAX_ARTIFACT_STORAGE_BYTES,
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
    json_size_bytes,
    projection_measurement,
)

from openubmc_target_runtime.context_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    SQLiteRuntimeRepository,
)
from openubmc_target_runtime.artifact_store import (  # noqa: E402
    LocalArtifactStore,
    SQLiteArtifactRepository,
)
from openubmc_target_runtime.agent_gateway import (  # noqa: E402
    AgentGateway,
    EXECUTE_TEXT_PROJECTION_TARGET_BYTES,
)
from openubmc_target_runtime.diagnostic_receipt import (  # noqa: E402
    DiagnosticReceipt,
)
from openubmc_target_runtime.mcp import (  # noqa: E402
    JsonRpcMcpEndpoint,
    RuntimeMcpService,
)
from openubmc_target_runtime.semantic_runtime import (  # noqa: E402
    CommandConflict,
    Gate,
    Outcome,
    ReferenceViolation,
    RunTurn,
    fingerprint,
)


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
            "root_cause": "the hermetic diagnostic path completed",
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {"status": "fresh"},
        }


DUAL_PROJECTION_TEXT_TARGET_BYTES = EXECUTE_TEXT_PROJECTION_TARGET_BYTES
DUAL_PROJECTION_RESULT_KINDS = (
    "active-alarms",
    "bounded-logs",
    "mdb",
    "service-tree",
    "target-clock",
    "version-file",
)


def _representative_diagnostic_receipt() -> dict[str, object]:
    observed_at = "2026-08-25T04:42:55Z"
    preview_values = {
        "active-alarms": "DiskTimeout alarm " + "a" * 2048,
        "bounded-logs": "mctpd request timeout " + "l" * 2048,
        "mdb": "Drive_1_010102 Health=OK " + "m" * 2048,
        "service-tree": "/bmc/kepler/devmon " + "s" * 2048,
        "target-clock": "2026-08-25 04:42:55 +0000 " + "t" * 2048,
        "version-file": '{"version":"12.08.21.06"}' + "v" * 2048,
    }
    results = []
    evidence = []
    for index, kind in enumerate(DUAL_PROJECTION_RESULT_KINDS):
        result_id = f"projection-{kind}"
        evidence_id = f"evidence-{index}"
        sentinel = f"QUALIFICATION-PREVIEW-{index}-{kind}"
        results.append(
            {
                "result_id": result_id,
                "status": "available",
                "kind": kind,
                "request": f"qualification request for {kind}",
                "observed_at": observed_at,
                "evidence_ids": [evidence_id],
                "value": {
                    "qualification_sentinel": sentinel,
                    "preview": f"{sentinel}::{preview_values[kind]}",
                    "content_complete": True,
                },
            }
        )
        evidence.append(
            {
                "evidence_id": evidence_id,
                "uri": f"qualification://dual-projection/{kind}",
                "kind": kind,
                "observed_at": observed_at,
            }
        )
    return {
        "receipt_id": "diagnostic-dual-projection-qualification",
        "operation": "debug_run",
        "status": "complete",
        "agent_acceptance": "complete",
        "coverage": {
            "requested": len(results),
            "evaluable": len(results),
            "unavailable": 0,
            "not_checked": 0,
            "complete": True,
            "visible_evaluable": len(results),
            "visible_unavailable": 0,
            "visible_not_checked": 0,
        },
        "results": results,
        "freshness": {"status": "fresh", "observed_at": observed_at},
        "capabilities": {
            "ssh": "available",
            "telnet": "available",
            "mdbctl": "available",
            "busctl": "available",
            "dbus": "available",
            "alarms": "available",
        },
        "truncated": False,
        "content_complete": True,
        "evidence": evidence,
        "gaps": [],
    }


class _DualProjectionRuntime:
    def __init__(self) -> None:
        receipt = DiagnosticReceipt.from_public_dict(
            _representative_diagnostic_receipt()
        )
        gate_schema = {
            "type": "object",
            "required": ["status", "summary", "payload"],
        }
        self.turns = {
            "gate": RunTurn(
                run_id="run-dual-projection-qualification",
                state="waiting_response",
                gate=Gate(
                    kind="phase",
                    gate_id="developer.change",
                    version=1,
                    schema_digest=fingerprint(gate_schema),
                    name="Developer change",
                    owner="developer",
                    input_schema=gate_schema,
                ),
                diagnostic_receipt=receipt,
                response_required=True,
            ),
            "terminal": RunTurn(
                run_id="run-dual-projection-qualification",
                state="completed",
                diagnostic_receipt=receipt,
                outcome=Outcome(
                    status="completed",
                    summary="representative source delivery completed",
                    acceptance=[
                        {
                            "requirement_id": "source-delivery",
                            "status": "passed",
                        },
                        {
                            "requirement_id": "diagnostic-evidence",
                            "status": "passed",
                        },
                    ],
                ),
                outcome_recorded=True,
            ),
        }

    def execute(self, command, *, task_id: str, operation_id: str) -> RunTurn:
        del task_id, operation_id
        fixture = str(getattr(command, "purpose", "")).removeprefix(
            "dual-projection-"
        )
        if fixture not in self.turns:
            raise ValueError("dual-projection purpose must select gate or terminal")
        return self.turns[fixture]


class _DualProjectionService:
    def __init__(self) -> None:
        self.runtime = _DualProjectionRuntime()
        self.gateway = AgentGateway(self.runtime)

    def call_exposed_tool(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        if name != "execute":
            raise ValueError("dual-projection qualification exposes execute only")
        return self.gateway.execute(
            arguments,
            task_id=task_id,
            operation_id=operation_id,
        )


def _preview_value_duplicated(
    standard_text: str,
    receipt: Mapping[str, object],
) -> bool:
    raw_results = receipt.get("results", [])
    results = raw_results if isinstance(raw_results, list) else []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        value = item.get("value")
        if not isinstance(value, Mapping):
            continue
        sentinel = str(value.get("qualification_sentinel", ""))
        preview = value.get("preview")
        if sentinel and sentinel in standard_text:
            return True
        if isinstance(preview, str):
            marker = sentinel + "::"
            payload = (
                preview[len(marker) :] if preview.startswith(marker) else preview
            )
            if payload and payload in standard_text:
                return True
    return False


def qualify_dual_projection(
    *,
    text_target_bytes: int = DUAL_PROJECTION_TEXT_TARGET_BYTES,
) -> dict[str, object]:
    service = _DualProjectionService()
    endpoint = JsonRpcMcpEndpoint(
        service,
        session_task_id="dual-projection-qualification",
    )
    results: dict[str, Mapping[str, object]] = {}
    for request_id, fixture in enumerate(("gate", "terminal"), start=1):
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.90",
                        "intent": "diagnosis-only",
                        "purpose": f"dual-projection-{fixture}",
                    },
                },
            }
        )
        result = response.get("result", {})
        results[fixture] = result if isinstance(result, Mapping) else {}

    gate_result = results["gate"]
    terminal_result = results["terminal"]
    gate_structured = gate_result.get("structuredContent", {})
    terminal_structured = terminal_result.get("structuredContent", {})
    gate_turn = gate_structured if isinstance(gate_structured, Mapping) else {}
    terminal_turn = (
        terminal_structured if isinstance(terminal_structured, Mapping) else {}
    )
    gate_receipt = gate_turn.get("diagnostic_receipt", {})
    gate_structured_receipt = (
        gate_receipt if isinstance(gate_receipt, Mapping) else {}
    )
    terminal_receipt = terminal_turn.get("diagnostic_receipt", {})
    terminal_structured_receipt = (
        terminal_receipt if isinstance(terminal_receipt, Mapping) else {}
    )
    raw_results = terminal_structured_receipt.get("results", [])
    receipt_results = raw_results if isinstance(raw_results, list) else []
    result_kinds = sorted(
        str(item.get("kind", ""))
        for item in receipt_results
        if isinstance(item, Mapping) and item.get("kind")
    )
    preview_sentinels = [
        str(value.get("qualification_sentinel", ""))
        for item in receipt_results
        if isinstance(item, Mapping)
        and isinstance((value := item.get("value")), Mapping)
        and value.get("qualification_sentinel")
    ]
    standard_texts = []
    for result in results.values():
        content = result.get("content", [])
        if isinstance(content, list):
            standard_texts.extend(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "text"
            )
    combined_text = "\n".join(standard_texts)
    gate_text = standard_texts[0] if standard_texts else ""
    terminal_text = standard_texts[1] if len(standard_texts) > 1 else ""
    measurements = {
        name: projection_measurement(result)
        for name, result in results.items()
    }
    correctness = {
        "mcp_results_successful": all(
            "isError" not in result or result.get("isError") is False
            for result in results.values()
        ),
        "gate_semantics_complete": all(
            (
                gate_turn.get("state") == "waiting_response",
                isinstance(gate_turn.get("gate"), Mapping),
                "GateBinding" in gate_text,
                '"kind":"respond"' in gate_text,
            )
        ),
        "terminal_semantics_complete": all(
            (
                terminal_turn.get("state") == "completed",
                isinstance(terminal_turn.get("outcome"), Mapping),
                "Outcome status=completed" in terminal_text,
            )
        ),
        "source_completeness_preserved": all(
            (
                gate_structured_receipt.get("status") == "complete",
                gate_structured_receipt.get("content_complete") is True,
                isinstance(gate_structured_receipt.get("coverage"), Mapping),
                gate_structured_receipt.get("coverage", {}).get("complete")
                is True,
                terminal_structured_receipt.get("status") == "complete",
                terminal_structured_receipt.get("content_complete") is True,
                isinstance(
                    terminal_structured_receipt.get("coverage"), Mapping
                ),
                terminal_structured_receipt.get("coverage", {}).get("complete")
                is True,
            )
        ),
        "agent_acceptance_preserved": all(
            (
                gate_structured_receipt.get("agent_acceptance") == "complete",
                terminal_structured_receipt.get("agent_acceptance")
                == "complete",
                "agent_acceptance=complete" in gate_text,
                "agent_acceptance=complete" in terminal_text,
            )
        ),
    }
    correctness["passed"] = all(correctness.values())
    warnings = [
        f"{name}_standard_text_target_exceeded"
        for name, measurement in measurements.items()
        if measurement["standard_text_bytes"] > text_target_bytes
    ]
    evidence = terminal_structured_receipt.get("evidence", [])
    preview_bytes = {
        str(item.get("result_id", "")): len(preview.encode("utf-8"))
        for item in receipt_results
        if isinstance(item, Mapping)
        and item.get("result_id")
        and isinstance((value := item.get("value")), Mapping)
        and isinstance((preview := value.get("preview")), str)
    }
    typed_receipt = service.runtime.turns["terminal"].diagnostic_receipt
    if typed_receipt is None:
        raise RuntimeError("dual-projection fixture lacks a typed receipt")
    expected_receipt = typed_receipt.to_public_dict()
    expected_receipt["agent_acceptance"] = "complete"
    gate_structured_semantics_complete = (
        gate_structured_receipt == expected_receipt
    )
    terminal_structured_semantics_complete = (
        terminal_structured_receipt == expected_receipt
    )
    representative_receipt = {
        "result_count": len(receipt_results),
        "result_kinds": result_kinds,
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
        "structured_semantics_complete": (
            result_kinds == list(DUAL_PROJECTION_RESULT_KINDS)
            and len(preview_sentinels) == len(DUAL_PROJECTION_RESULT_KINDS)
            and len(preview_bytes) == len(DUAL_PROJECTION_RESULT_KINDS)
            and all(
                value >= DUAL_PROJECTION_MIN_PREVIEW_BYTES
                for value in preview_bytes.values()
            )
            and gate_structured_semantics_complete
            and terminal_structured_semantics_complete
        ),
        "gate_structured_semantics_complete": (
            gate_structured_semantics_complete
        ),
        "terminal_structured_semantics_complete": (
            terminal_structured_semantics_complete
        ),
        "preview_sentinel_count": len(preview_sentinels),
        "preview_values_duplicated": _preview_value_duplicated(
            combined_text,
            terminal_structured_receipt,
        ),
        "minimum_preview_bytes": DUAL_PROJECTION_MIN_PREVIEW_BYTES,
        "preview_bytes": preview_bytes,
    }
    correctness["passed"] = bool(
        correctness["passed"]
        and representative_receipt["structured_semantics_complete"]
        and not representative_receipt["preview_values_duplicated"]
    )
    return {
        "status": "passed" if correctness["passed"] else "failed",
        "correctness": correctness,
        "efficiency": {
            "decision": "warning" if warnings else "passed",
            "warnings": warnings,
            "blocks_promotability": False,
            "standard_text_target_bytes": text_target_bytes,
        },
        "measurements": measurements,
        "representative_receipt": representative_receipt,
        "canonical_results": {
            name: json.loads(json.dumps(result))
            for name, result in results.items()
        },
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


def _path_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _artifact_storage_bytes(database: Path, content_root: Path) -> int:
    database_bytes = sum(
        path.stat().st_size
        for path in database.parent.glob(f"{database.name}*")
        if path.is_file()
    )
    return database_bytes + _path_bytes(content_root)


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

    canonical_backend, canonical_agent = _agent(storage)
    try:
        canonical_turn = canonical_agent.call_exposed_tool(
            "execute",
            response,
            task_id="gate-concurrency-canonical",
            operation_id="gate-concurrency-canonical",
        )
    finally:
        canonical_agent.close()

    turns = [turn for turn, _error in results if turn is not None]
    errors = [error for _turn, error in results if error]
    projection, replay = _case_record(
        SQLiteRuntimeRepository(storage.database),
        str(waiting["run_id"]),
    )
    run_ids = {str(turn["run_id"]) for turn in turns}
    unique_turns = len({evidence_fingerprint(turn) for turn in turns})
    turn_states = Counter(str(turn["state"]) for turn in turns)
    canonical_turn_matches = all(
        evidence_fingerprint(turn) == evidence_fingerprint(canonical_turn)
        for turn in turns
    )
    gate_submissions = len(projection["gate_submissions"])
    outcome_events = sum(
        event["kind"] == "RunOutcomeRecorded" for event in replay
    )
    passed = all(
        (
            not errors,
            run_ids == {str(waiting["run_id"])},
            unique_turns == 1,
            turn_states == {"completed": GATE_WORKERS},
            canonical_turn["state"] == "completed",
            canonical_turn_matches,
            canonical_backend.calls == 0,
            gate_submissions == 1,
            outcome_events == 1,
            projection["run_outcome"].get("status") == "completed",
            projection["incidents"] == [],
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "execute_calls": GATE_WORKERS + 1,
        "failed_calls": len(errors),
        "errors": errors,
        "unique_runs": len(run_ids),
        "unique_turns": unique_turns,
        "turn_states": dict(sorted(turn_states.items())),
        "canonical_turn_state": canonical_turn["state"],
        "canonical_turn_matches": canonical_turn_matches,
        "canonical_reattach_backend_calls": canonical_backend.calls,
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
    storage_growth_by_batch = [
        current - previous
        for previous, current in zip(
            [0, *storage_bytes_by_batch[:-1]],
            storage_bytes_by_batch,
        )
    ]
    max_storage_growth_per_batch = (
        MAX_CAPACITY_STORAGE_BYTES
        // (CAPACITY_RUNS // CAPACITY_BATCH_SIZE)
    )
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
            storage_bytes_by_batch[-1] == storage_bytes,
            max(storage_growth_by_batch) <= max_storage_growth_per_batch,
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
        "storage_growth_bytes_by_batch": storage_growth_by_batch,
        "max_events_per_run": max_events_per_run,
        "storage_bytes": storage_bytes,
        "peak_rss_bytes": peak_rss_bytes,
        "peak_python_allocation_bytes": peak_python_bytes,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _artifact_lifecycle(root: Path) -> dict[str, object]:
    database = root / "artifact-capacity.sqlite3"
    content_root = root / "artifact-capacity-content"
    source = root / "artifact-capacity-source.json"
    source.write_text('{"password":"shared-secret","status":"ok"}', encoding="utf-8")
    ephemeral_source = root / "artifact-capacity-ephemeral.txt"
    ephemeral_source.write_text("ephemeral artifact", encoding="utf-8")
    now = [100.0]

    def open_store() -> LocalArtifactStore:
        return LocalArtifactStore(
            content_root=content_root,
            repository=SQLiteArtifactRepository(database),
            clock=lambda: now[0],
            temporary_retention_seconds=10,
        )

    raw_references = []
    records_by_batch: list[int] = []
    storage_bytes_by_batch: list[int] = []
    started = time.monotonic()
    for batch_start in range(
        0,
        ARTIFACT_CAPACITY_RECORDS,
        ARTIFACT_CAPACITY_BATCH_SIZE,
    ):
        store = open_store()
        for index in range(
            batch_start,
            batch_start + ARTIFACT_CAPACITY_BATCH_SIZE,
        ):
            raw_references.append(
                store.put(
                    source,
                    kind="artifact-capacity-raw",
                    provenance="runtime-stability",
                    retention_hint="run-lifetime",
                    target="198.51.100.200",
                    run_id=f"artifact-capacity-{index:03d}",
                    created_by_effect=f"artifact-capacity-create-{index:03d}",
                )
            )
        records_by_batch.append(store.status()["record_count"])
        storage_bytes_by_batch.append(
            _artifact_storage_bytes(database, content_root)
        )

    store = open_store()
    redacted = store.redact(
        raw_references[-1],
        kind="artifact-capacity-redacted",
        provenance="runtime-stability-redaction",
        retention_hint="audit",
        created_by_effect="artifact-capacity-redact",
    )
    ephemeral = store.put(
        ephemeral_source,
        kind="artifact-capacity-ephemeral",
        provenance="runtime-stability",
        retention_hint="temporary",
        target="198.51.100.200",
        run_id="artifact-capacity-ephemeral",
        created_by_effect="artifact-capacity-ephemeral",
    )

    reopened = open_store()
    restart_status = reopened.status()
    restart_resolutions = sum(
        path.is_file()
        for path in (
            reopened.resolve(raw_references[0]),
            reopened.resolve(redacted, require_redacted=True),
        )
    )
    for index in range(ARTIFACT_CAPACITY_RECORDS // 2):
        reopened.release_run(f"artifact-capacity-{index:03d}")
    now[0] = 111.0
    first_gc = reopened.garbage_collect()
    shared_content_preserved = reopened.resolve(raw_references[-1]).is_file()
    expired_resolution_rejected = False
    released_resolution_rejected = False
    try:
        reopened.resolve(ephemeral)
    except ReferenceViolation:
        expired_resolution_rejected = True
    try:
        reopened.resolve(raw_references[0])
    except ReferenceViolation:
        released_resolution_rejected = True

    released_run_records = 0
    for index in range(
        ARTIFACT_CAPACITY_RECORDS // 2,
        ARTIFACT_CAPACITY_RECORDS,
    ):
        released_run_records += reopened.release_run(
            f"artifact-capacity-{index:03d}"
        )
    second_gc = reopened.garbage_collect()
    shared_content_deleted = False
    try:
        reopened.resolve(raw_references[-1])
    except ReferenceViolation:
        shared_content_deleted = True
    final_status = open_store().status()
    final_content_files = sum(
        path.is_file() for path in content_root.rglob("*")
    )
    elapsed_seconds = time.monotonic() - started
    storage_bytes = _artifact_storage_bytes(database, content_root)
    passed = all(
        (
            len(raw_references) == ARTIFACT_CAPACITY_RECORDS,
            len({reference.digest for reference in raw_references}) == 1,
            redacted.digest != raw_references[-1].digest,
            restart_status["record_count"] == ARTIFACT_CAPACITY_RECORDS + 2,
            restart_resolutions == 2,
            first_gc == {
                "deleted_records": ARTIFACT_CAPACITY_RECORDS // 2 + 1,
                "deleted_content": 1,
            },
            shared_content_preserved,
            expired_resolution_rejected,
            released_resolution_rejected,
            released_run_records == ARTIFACT_CAPACITY_RECORDS // 2,
            second_gc == {
                "deleted_records": ARTIFACT_CAPACITY_RECORDS // 2,
                "deleted_content": 1,
            },
            shared_content_deleted,
            final_status["record_count"] == 1,
            final_status["managed_record_count"] == 1,
            final_status["redacted_record_count"] == 1,
            final_status["retention_counts"]["audit"] == 1,
            final_content_files == 1,
            storage_bytes <= MAX_ARTIFACT_STORAGE_BYTES,
            elapsed_seconds <= MAX_ARTIFACT_CAPACITY_SECONDS,
        )
    )
    return {
        "status": "passed" if passed else "failed",
        "created_raw_records": len(raw_references),
        "created_redacted_records": 1,
        "created_ephemeral_records": 1,
        "shared_raw_digests": len({reference.digest for reference in raw_references}),
        "redacted_digest_distinct": redacted.digest != raw_references[-1].digest,
        "records_by_batch": records_by_batch,
        "storage_bytes_by_batch": storage_bytes_by_batch,
        "restart_record_count": restart_status["record_count"],
        "restart_resolutions": restart_resolutions,
        "first_gc_deleted_records": first_gc["deleted_records"],
        "first_gc_deleted_content": first_gc["deleted_content"],
        "shared_content_preserved_after_partial_gc": shared_content_preserved,
        "released_run_records": released_run_records,
        "second_gc_deleted_records": second_gc["deleted_records"],
        "second_gc_deleted_content": second_gc["deleted_content"],
        "shared_content_deleted_after_final_reference": shared_content_deleted,
        "expired_resolution_rejected": expired_resolution_rejected,
        "released_resolution_rejected": released_resolution_rejected,
        "final_record_count": final_status["record_count"],
        "final_managed_record_count": final_status["managed_record_count"],
        "final_redacted_record_count": final_status["redacted_record_count"],
        "final_audit_record_count": final_status["retention_counts"]["audit"],
        "final_content_files": final_content_files,
        "storage_bytes": storage_bytes,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _restart_soak(root: Path) -> dict[str, object]:
    storage = _RuntimeStorage(root / "soak.sqlite3", root / "soak-blobs")
    repository = SQLiteRuntimeRepository(storage.database)
    blobs = FilesystemBlobRepository(storage.blobs)
    run_ids: list[str] = []
    backend_calls = 0
    replay_backend_calls = 0
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
            cycle_actions: list[tuple[str, dict[str, object], dict[str, object]]] = []
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
                    cycle_actions.append((operation_id, action, first))
            finally:
                backend_calls += backend.calls
                agent.close()

            replay_backend, replay_agent = _agent(storage)
            try:
                for operation_id, action, first in cycle_actions:
                    execute_calls += 1
                    try:
                        replayed = replay_agent.call_exposed_tool(
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
                replay_backend_calls += replay_backend.calls
                replay_agent.close()
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
            replay_backend_calls == 0,
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
        "replay_backend_read_calls": replay_backend_calls,
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
            ("artifact_lifecycle", _artifact_lifecycle),
            ("capacity", _capacity),
            ("dual_projection", lambda _root: qualify_dual_projection()),
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
