#!/usr/bin/env python3
"""Qualify the public observe-to-diagnosis-to-development Runtime chain."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
DEFAULT_FIXTURE = (
    ROOT
    / "scripts"
    / "fixtures"
    / "observation-ba1a3b5277447c57cf972ee2.json"
)
SCHEMA = "openubmc-agent-workflow.diagnosis-chain-qualification.v1"


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _FixtureBackend:
    """Hermetic Adapter that reproduces the observed public semantic shape."""

    def __init__(self, fixture: Mapping[str, object], calls: Counter[str]) -> None:
        self.fixture = fixture
        self.calls = calls

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

    def debug_collect(
        self,
        task: _Task,
        arguments: Mapping[str, object],
        context: object,
    ) -> dict[str, object]:
        del task
        context.raise_if_stopped()
        self.calls["observe"] += 1
        observed_at = str(self.fixture["observed_at"])
        queries = [
            str(query)
            for selector in self.fixture["selectors"]
            if isinstance(selector, Mapping)
            for query in selector.get("queries", [])
        ]
        return {
            "ok": True,
            "observed_at": observed_at,
            "result": {
                "completed_at": observed_at,
                "capabilities": dict(self.fixture["capabilities"]),
                "lanes": {
                    "ssh": {
                        "mdbctl": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "payload": {
                                "result": {
                                    "properties": {
                                        "Drive_1_010102": {
                                            "Protocol": "SATA",
                                            "Health": "OK",
                                            "Query": queries[0] if queries else "",
                                        }
                                    },
                                    "content_complete": True,
                                },
                            },
                        }
                    }
                },
            },
        }

    def observe_query(
        self,
        task: _Task,
        arguments: Mapping[str, object],
        context: object,
    ) -> dict[str, object]:
        value = self.debug_collect(task, arguments, context)
        observed_at = str(self.fixture["observed_at"])
        value["observation_timing"] = {
            "started_at": observed_at,
            "completed_at": observed_at,
            "selectors": [
                {
                    "selector_id": str(selector.get("id", "")),
                    "kind": str(selector.get("kind", "")),
                    "started_at": observed_at,
                    "completed_at": observed_at,
                    "status": "observed",
                }
                for selector in arguments.get("selectors", [])
                if isinstance(selector, Mapping)
            ],
        }
        return value

    def debug_run(
        self,
        task: _Task,
        arguments: Mapping[str, object],
        context: object,
    ) -> dict[str, object]:
        del task, arguments
        context.raise_if_stopped()
        self.calls["debug_run"] += 1
        return {
            "ok": True,
            "summary": "Domain operation completed without a visible diagnosis",
        }


def _runtime_types(runtime_root: Path) -> tuple[type, type, type]:
    selected = str(runtime_root.expanduser().absolute())
    if selected not in sys.path:
        sys.path.insert(0, selected)
    from openubmc_target_runtime.context_runtime import (  # noqa: PLC0415
        FilesystemBlobRepository,
        SQLiteRuntimeRepository,
    )
    from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: PLC0415

    return RuntimeMcpService, SQLiteRuntimeRepository, FilesystemBlobRepository


def _fixture(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != (
        "openubmc-agent-workflow.diagnosis-chain-fixture.v1"
    ):
        raise ValueError("invalid diagnosis-chain fixture")
    required = (
        "source_receipt_id",
        "target",
        "source_observed_at",
        "freshness",
        "selectors",
        "capabilities",
        "diagnosis_gap",
    )
    if any(not value.get(name) for name in required):
        raise ValueError("incomplete diagnosis-chain fixture")
    return value


def _qualification_observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _gate_binding(turn: Mapping[str, object]) -> dict[str, object]:
    gate = turn.get("gate")
    if not isinstance(gate, Mapping):
        return {}
    return {
        "gate_id": gate.get("gate_id"),
        "gate_version": gate.get("gate_version"),
        "schema_digest": gate.get("schema_digest"),
    }


def _gate_binding_complete(binding: Mapping[str, object]) -> bool:
    gate_id = binding.get("gate_id")
    gate_version = binding.get("gate_version")
    schema_digest = binding.get("schema_digest")
    return (
        isinstance(gate_id, str)
        and bool(gate_id.strip())
        and isinstance(gate_version, int)
        and not isinstance(gate_version, bool)
        and gate_version >= 1
        and isinstance(schema_digest, str)
        and bool(schema_digest.strip())
    )


def _gate_name(turn: Mapping[str, object]) -> str:
    gate = turn.get("gate")
    return str(gate.get("name") or "") if isinstance(gate, Mapping) else ""


def _diagnostic_receipt_id(turn: Mapping[str, object]) -> str:
    receipt = turn.get("diagnostic_receipt")
    return (
        str(receipt.get("receipt_id") or "")
        if isinstance(receipt, Mapping)
        else ""
    )


def _diagnostic_evidence_ids(turn: Mapping[str, object]) -> tuple[str, ...]:
    receipt = turn.get("diagnostic_receipt")
    if not isinstance(receipt, Mapping):
        return ()
    raw_evidence = receipt.get("evidence", [])
    return tuple(
        str(item.get("evidence_id"))
        for item in raw_evidence
        if isinstance(item, Mapping) and item.get("evidence_id")
    ) if isinstance(raw_evidence, list) else ()


def _evidence_ids_complete(evidence_ids: object) -> bool:
    return (
        isinstance(evidence_ids, (list, tuple))
        and bool(evidence_ids)
        and all(
            isinstance(evidence_id, str) and bool(evidence_id.strip())
            for evidence_id in evidence_ids
        )
    )


def _events_include_development(events: object) -> bool:
    if not isinstance(events, (list, tuple)):
        return False
    for event in events:
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event.get("kind") == "RunGateOpened":
            gate = payload.get("gate")
            if isinstance(gate, Mapping) and gate.get("name") == "developer.change":
                return True
        if event.get("kind") == "RunGateSubmitted":
            phase = payload.get("phase")
            if (
                isinstance(phase, Mapping)
                and phase.get("phase_type") == "developer.change"
            ):
                return True
    return False


def _events_have_single_outcome(events: object, status: str) -> bool:
    if not isinstance(events, (list, tuple)):
        return False
    outcomes = [
        event
        for event in events
        if isinstance(event, Mapping)
        and event.get("kind") == "RunOutcomeRecorded"
    ]
    return (
        len(outcomes) == 1
        and isinstance(outcomes[0].get("payload"), Mapping)
        and isinstance(outcomes[0]["payload"].get("outcome"), Mapping)
        and outcomes[0]["payload"]["outcome"].get("status") == status
    )


def _acceptance(outcome: Mapping[str, object]) -> dict[str, str]:
    raw = outcome.get("acceptance", [])
    values = {
        str(item.get("requirement_id")): str(item.get("status"))
        for item in raw
        if isinstance(item, Mapping)
    } if isinstance(raw, list) else {}
    return {
        name: values.get(name, "")
        for name in ("stage.development", "stage.diagnosis")
    }


def qualification_violations(report: Mapping[str, object]) -> list[str]:
    violations: list[str] = []
    fixture = report.get("fixture")
    if not isinstance(fixture, Mapping) or fixture.get("semantic_match") is not True:
        violations.append("fixture did not reproduce a complete live ObservationRef")
    blocked = report.get("blocked_path")
    blocked = blocked if isinstance(blocked, Mapping) else {}
    blocked_gate = blocked.get("gate")
    if blocked_gate == "developer.change":
        violations.append("blocked diagnosis exposed developer.change")
    elif blocked_gate != "diagnosis.acceptance":
        violations.append("blocked diagnosis did not expose diagnosis.acceptance")
    if blocked.get("diagnostic_status") != "blocked":
        violations.append("diagnosis was not classified as blocked")
    if blocked.get("gap_present") is not True:
        violations.append("diagnostic_result_not_visible gap was not preserved")
    if blocked.get("gate_binding_complete") is not True:
        violations.append("diagnosis Gate binding is incomplete")
    if blocked.get("evidence_present") is not True:
        violations.append("diagnostic evidence identities are missing")
    if blocked.get("same_gate_after_resume") is not True:
        violations.append("unchanged resume did not return the same diagnosis Gate")
    if blocked.get("same_gate_after_restart") is not True:
        violations.append("restart did not preserve the same diagnosis Gate")
    if blocked.get("same_evidence_after_resume") is not True:
        violations.append("resume changed diagnostic evidence identities")
    if blocked.get("same_evidence_after_restart") is not True:
        violations.append("restart changed diagnostic evidence identities")
    recovery = report.get("recovery_path")
    recovery = recovery if isinstance(recovery, Mapping) else {}
    if recovery.get("gate") != "developer.change":
        violations.append("accepted diagnosis did not expose developer.change")
    if recovery.get("gate_binding_complete") is not True:
        violations.append("developer Gate binding is incomplete")
    if recovery.get("accepted_receipt_present") is not True:
        violations.append("accepted diagnosis receipt identity is missing")
    if recovery.get("accepted_diagnosis_survived_restart") is not True:
        violations.append("accepted diagnosis did not survive restart and replay")
    if recovery.get("outcome") != "completed":
        violations.append("terminal Outcome is not completed")
    acceptance = recovery.get("acceptance")
    acceptance = acceptance if isinstance(acceptance, Mapping) else {}
    for requirement in ("stage.diagnosis", "stage.development"):
        if acceptance.get(requirement) != "passed":
            violations.append(f"{requirement} acceptance did not pass")
    terminal = report.get("terminal_paths")
    terminal = terminal if isinstance(terminal, Mapping) else {}
    for status in ("failed", "cancelled"):
        if terminal.get(status) is not True:
            violations.append(f"{status} diagnosis advanced to development")
    metrics = report.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    if metrics.get("observe_calls") != 1:
        violations.append("qualification repeated Observation collection")
    if metrics.get("target_calls_after_observe") != 0:
        violations.append("resume or restart repeated target diagnosis work")
    if metrics.get("duplicate_diagnosis_gates") != 0:
        violations.append("duplicate diagnosis.acceptance Gate was created")
    if metrics.get("duplicate_development_gates") != 0:
        violations.append("duplicate developer.change Gate was created")
    if metrics.get("duplicate_development_work") != 0:
        violations.append("duplicate developer.change work was recorded")
    if metrics.get("durable_outcome_records") != 1:
        violations.append("terminal Outcome was not recorded exactly once")
    if metrics.get("durable_outcome_completed") is not True:
        violations.append("durable terminal Outcome is not completed")
    execution_error = report.get("execution_error")
    if execution_error:
        violations.append(str(execution_error))
    return list(dict.fromkeys(violations))


def _finalize(report: dict[str, object]) -> dict[str, object]:
    violations = qualification_violations(report)
    report["violations"] = violations
    report["promotable"] = not violations
    return report


def _runtime_service(
    runtime_types: tuple[type, type, type],
    fixture: Mapping[str, object],
    calls: Counter[str],
    database: Path,
    blobs: Path,
):
    RuntimeMcpService, SQLiteRuntimeRepository, FilesystemBlobRepository = (
        runtime_types
    )
    return RuntimeMcpService(
        _FixtureBackend(fixture, calls),
        context_repository=SQLiteRuntimeRepository(database),
        blob_repository=FilesystemBlobRepository(blobs),
    )


def _worker_accept_diagnosis(
    runtime_types: tuple[type, type, type],
    fixture: Mapping[str, object],
    database: Path,
    blobs: Path,
    state: Mapping[str, object],
) -> dict[str, object]:
    calls: Counter[str] = Counter()
    service = _runtime_service(runtime_types, fixture, calls, database, blobs)
    try:
        restarted = service.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": state["run_id"]},
            task_id="diagnosis-chain-run",
            operation_id="diagnosis-chain-restart-resume",
        )
        evidence_ids = list(_diagnostic_evidence_ids(restarted))
        development = service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": restarted["run_id"],
                **_gate_binding(restarted),
                "response": {
                    "status": "completed",
                    "summary": "the fixture isolates a bounded source defect",
                    "payload": {
                        "root_cause": "fixture drive identity uses the wrong scope",
                        "evidence_ids": evidence_ids,
                        "known_gaps": ["representative NVMe hardware is pending"],
                    },
                },
            },
            task_id="diagnosis-chain-run",
            operation_id="diagnosis-chain-accept-diagnosis",
        )
    finally:
        service.close()
    return {
        "pid": os.getpid(),
        "execute_calls": 2,
        "debug_run_calls": calls["debug_run"],
        "restart_gate_binding": _gate_binding(restarted),
        "restart_evidence_ids": list(_diagnostic_evidence_ids(restarted)),
        "development_run_id": development["run_id"],
        "development_gate": _gate_name(development),
        "development_gate_binding": _gate_binding(development),
        "accepted_receipt_id": _diagnostic_receipt_id(development),
    }


def _worker_complete_development(
    runtime_types: tuple[type, type, type],
    fixture: Mapping[str, object],
    database: Path,
    blobs: Path,
    state: Mapping[str, object],
) -> dict[str, object]:
    calls: Counter[str] = Counter()
    terminal_paths: dict[str, bool] = {}
    terminal_run_ids: dict[str, str] = {}
    service = _runtime_service(runtime_types, fixture, calls, database, blobs)
    try:
        replayed = service.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": state["run_id"]},
            task_id="diagnosis-chain-run",
            operation_id="diagnosis-chain-development-restart",
        )
        final = service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": replayed["run_id"],
                **_gate_binding(replayed),
                "response": {
                    "status": "completed",
                    "summary": "bounded source delivery completed",
                    "payload": {
                        "source_revision": "qualification-source",
                        "authored_files": ["src/qualification.lua"],
                        "verification_plan": ["run focused regression tests"],
                        "known_gaps": ["official dependency validation is pending"],
                    },
                },
            },
            task_id="diagnosis-chain-run",
            operation_id="diagnosis-chain-development",
        )
        for status in ("failed", "cancelled"):
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": fixture["target"],
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "observation_ref": state["observation_ref"],
                },
                task_id=f"diagnosis-chain-{status}",
                operation_id=f"diagnosis-chain-{status}-start",
            )
            terminal = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **_gate_binding(waiting),
                    "response": {
                        "status": status,
                        "summary": f"fixture diagnosis {status}",
                        "payload": {},
                    },
                },
                task_id=f"diagnosis-chain-{status}",
                operation_id=f"diagnosis-chain-{status}-respond",
            )
            terminal_run_ids[status] = str(waiting["run_id"])
            terminal_paths[status] = (
                terminal.get("state") == status
                and terminal.get("outcome", {}).get("status") == status
                and _gate_name(terminal) == ""
            )
    finally:
        service.close()
    outcome = final.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    return {
        "pid": os.getpid(),
        "execute_calls": 6,
        "debug_run_calls": calls["debug_run"],
        "replayed_gate": _gate_name(replayed),
        "replayed_gate_binding": _gate_binding(replayed),
        "replayed_receipt_id": _diagnostic_receipt_id(replayed),
        "outcome": outcome.get("status", ""),
        "acceptance": _acceptance(outcome),
        "terminal_paths": terminal_paths,
        "terminal_run_ids": terminal_run_ids,
    }


def _worker_result(
    phase: str,
    *,
    runtime_root: Path,
    fixture_path: Path,
    database: Path,
    blobs: Path,
    state_path: Path,
) -> dict[str, object]:
    fixture = _fixture(fixture_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("worker state is not an object")
    fixture = {**fixture, "observed_at": state["qualification_observed_at"]}
    runtime_types = _runtime_types(runtime_root)
    if phase == "accept-diagnosis":
        return _worker_accept_diagnosis(
            runtime_types,
            fixture,
            database,
            blobs,
            state,
        )
    if phase == "complete-development":
        return _worker_complete_development(
            runtime_types,
            fixture,
            database,
            blobs,
            state,
        )
    raise ValueError(f"unknown diagnosis-chain worker phase: {phase}")


def _run_worker(
    phase: str,
    *,
    runtime_root: Path,
    fixture_path: Path,
    database: Path,
    blobs: Path,
    root: Path,
    state: Mapping[str, object],
) -> dict[str, object]:
    state_path = root / f"{phase}-state.json"
    output = root / f"{phase}-output.json"
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            phase,
            "--runtime-root",
            str(runtime_root),
            "--fixture",
            str(fixture_path),
            "--database",
            str(database),
            "--blobs",
            str(blobs),
            "--state",
            str(state_path),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    value = json.loads(output.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"{phase} worker output is not an object")
    return value


def qualify_diagnosis_chain(
    *,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    fixture_path: Path = DEFAULT_FIXTURE,
) -> dict[str, object]:
    source_fixture = _fixture(fixture_path)
    qualification_observed_at = _qualification_observed_at()
    fixture = {**source_fixture, "observed_at": qualification_observed_at}
    runtime_types = _runtime_types(runtime_root)
    calls: Counter[str] = Counter()
    execute_calls = 0
    started = time.monotonic()
    report: dict[str, object] = {
        "schema": SCHEMA,
        "runtime_root": str(runtime_root.expanduser().absolute()),
        "fixture": {
            "source_receipt_id": source_fixture["source_receipt_id"],
            "source_observed_at": source_fixture["source_observed_at"],
            "qualification_observed_at": qualification_observed_at,
            "semantic_match": False,
        },
        "blocked_path": {},
        "recovery_path": {},
        "terminal_paths": {"failed": False, "cancelled": False},
        "metrics": {
            "correctness_is_primary": True,
            "token_measurement": "not_collected",
            "target_calls_after_observe": -1,
            "duplicate_diagnosis_gates": -1,
            "duplicate_development_gates": -1,
            "duplicate_development_work": -1,
            "durable_outcome_records": -1,
            "durable_outcome_completed": False,
        },
    }
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        database = root / "runtime.sqlite3"
        blobs = root / "blobs"
        first = _runtime_service(runtime_types, fixture, calls, database, blobs)
        try:
            observation = first.call_exposed_tool(
                "observe",
                {
                    "target": fixture["target"],
                    "selectors": fixture["selectors"],
                    "freshness": fixture["freshness"],
                },
                task_id="diagnosis-chain-observe",
                operation_id="diagnosis-chain-observe-1",
            )
            observation_ref = observation.get("observation_ref")
            semantic_match = (
                observation.get("status") == "complete"
                and isinstance(observation_ref, Mapping)
                and observation.get("coverage", {}).get("complete") is True
                and observation.get("freshness", {}).get("status") == "live"
                and observation.get("freshness", {}).get("observed_at")
                == qualification_observed_at
            )
            report["fixture"].update(
                {
                    "generated_receipt_id": observation.get("receipt_id"),
                    "semantic_match": semantic_match,
                }
            )
            execute_calls += 1
            waiting = first.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": fixture["target"],
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "purpose": "qualify the diagnosis-to-development chain",
                    "observation_ref": observation_ref,
                },
                task_id="diagnosis-chain-run",
                operation_id="diagnosis-chain-start",
            )
            blocked_receipt = waiting.get("diagnostic_receipt")
            blocked_gaps = (
                blocked_receipt.get("gaps", [])
                if isinstance(blocked_receipt, Mapping)
                else []
            )
            initial_binding = _gate_binding(waiting)
            initial_evidence = _diagnostic_evidence_ids(waiting)
            report["blocked_path"] = {
                "gate": _gate_name(waiting),
                "diagnostic_status": (
                    blocked_receipt.get("status")
                    if isinstance(blocked_receipt, Mapping)
                    else ""
                ),
                "gap_present": source_fixture["diagnosis_gap"] in blocked_gaps,
                "gate_binding_complete": _gate_binding_complete(initial_binding),
                "evidence_present": _evidence_ids_complete(initial_evidence),
                "same_gate_after_resume": False,
                "same_gate_after_restart": False,
                "same_evidence_after_resume": False,
                "same_evidence_after_restart": False,
            }
            if _gate_name(waiting) != "diagnosis.acceptance":
                report["metrics"].update(
                    {
                        "execute_calls": execute_calls,
                        "observe_calls": calls["observe"],
                        "target_calls_after_observe": calls["debug_run"],
                        "duplicate_diagnosis_gates": 0,
                        "duplicate_development_gates": 0,
                        "duplicate_development_work": 0,
                        "durable_outcome_records": 0,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
                )
                return _finalize(report)
            execute_calls += 1
            resumed = first.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": waiting["run_id"]},
                task_id="diagnosis-chain-run",
                operation_id="diagnosis-chain-resume",
            )
            report["blocked_path"]["same_gate_after_resume"] = (
                _gate_binding_complete(initial_binding)
                and _gate_binding_complete(_gate_binding(resumed))
                and _gate_binding(resumed) == initial_binding
            )
            report["blocked_path"]["same_evidence_after_resume"] = (
                _evidence_ids_complete(initial_evidence)
                and _evidence_ids_complete(_diagnostic_evidence_ids(resumed))
                and _diagnostic_evidence_ids(resumed) == initial_evidence
            )
        finally:
            first.close()

        accepted = _run_worker(
            "accept-diagnosis",
            runtime_root=runtime_root,
            fixture_path=fixture_path,
            database=database,
            blobs=blobs,
            root=root,
            state={
                "run_id": waiting["run_id"],
                "qualification_observed_at": qualification_observed_at,
            },
        )
        execute_calls += int(accepted["execute_calls"])
        report["blocked_path"]["same_gate_after_restart"] = (
            _gate_binding_complete(initial_binding)
            and _gate_binding_complete(accepted["restart_gate_binding"])
            and accepted["restart_gate_binding"] == initial_binding
        )
        report["blocked_path"]["same_evidence_after_restart"] = (
            _evidence_ids_complete(initial_evidence)
            and _evidence_ids_complete(accepted["restart_evidence_ids"])
            and tuple(accepted["restart_evidence_ids"]) == initial_evidence
        )
        report["recovery_path"] = {
            "gate": accepted["development_gate"],
            "gate_binding_complete": _gate_binding_complete(
                accepted["development_gate_binding"]
            ),
            "accepted_receipt_present": bool(
                str(accepted["accepted_receipt_id"]).strip()
            ),
            "accepted_diagnosis_survived_restart": False,
            "outcome": "",
            "acceptance": {},
        }
        completed = _run_worker(
            "complete-development",
            runtime_root=runtime_root,
            fixture_path=fixture_path,
            database=database,
            blobs=blobs,
            root=root,
            state={
                "run_id": accepted["development_run_id"],
                "observation_ref": observation_ref,
                "qualification_observed_at": qualification_observed_at,
            },
        )
        execute_calls += int(completed["execute_calls"])
        report["recovery_path"].update(
            {
                "accepted_diagnosis_survived_restart": (
                    completed["replayed_gate"] == "developer.change"
                    and _gate_binding_complete(
                        accepted["development_gate_binding"]
                    )
                    and _gate_binding_complete(
                        completed["replayed_gate_binding"]
                    )
                    and completed["replayed_gate_binding"]
                    == accepted["development_gate_binding"]
                    and bool(str(accepted["accepted_receipt_id"]).strip())
                    and bool(str(completed["replayed_receipt_id"]).strip())
                    and completed["replayed_receipt_id"]
                    == accepted["accepted_receipt_id"]
                ),
                "outcome": completed["outcome"],
                "acceptance": completed["acceptance"],
            }
        )
        report["terminal_paths"] = dict(completed["terminal_paths"])

        _, SQLiteRuntimeRepository, _ = runtime_types
        repository = SQLiteRuntimeRepository(database)
        for status, run_id in completed["terminal_run_ids"].items():
            terminal_events = repository.events(run_id)
            report["terminal_paths"][status] = bool(
                report["terminal_paths"][status]
            ) and not _events_include_development(
                terminal_events
            ) and _events_have_single_outcome(terminal_events, status)
        run_events = repository.events(waiting["run_id"])
        gate_names = [
            str(event["payload"]["gate"].get("name") or "")
            for event in run_events
            if event.get("kind") == "RunGateOpened"
            and isinstance(event.get("payload"), Mapping)
            and isinstance(event["payload"].get("gate"), Mapping)
        ]
        developer_phase_count = sum(
            event.get("kind") == "RunGateSubmitted"
            and isinstance(event.get("payload"), Mapping)
            and isinstance(event["payload"].get("phase"), Mapping)
            and event["payload"]["phase"].get("phase_type")
            == "developer.change"
            for event in run_events
        )
        outcome_events = [
            event
            for event in run_events
            if event.get("kind") == "RunOutcomeRecorded"
        ]
        durable_outcome_completed = (
            len(outcome_events) == 1
            and isinstance(outcome_events[0].get("payload"), Mapping)
            and isinstance(outcome_events[0]["payload"].get("outcome"), Mapping)
            and outcome_events[0]["payload"]["outcome"].get("status")
            == "completed"
        )
        report["metrics"].update(
            {
                "execute_calls": execute_calls,
                "observe_calls": calls["observe"],
                "target_calls_after_observe": (
                    calls["debug_run"]
                    + int(accepted["debug_run_calls"])
                    + int(completed["debug_run_calls"])
                ),
                "duplicate_diagnosis_gates": max(
                    0,
                    gate_names.count("diagnosis.acceptance") - 1,
                ),
                "duplicate_development_gates": max(
                    0,
                    gate_names.count("developer.change") - 1,
                ),
                "duplicate_development_work": max(0, developer_phase_count - 1),
                "durable_outcome_records": len(outcome_events),
                "durable_outcome_completed": durable_outcome_completed,
                "restart_worker_pids": [accepted["pid"], completed["pid"]],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    return _finalize(report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--worker",
        choices=("accept-diagnosis", "complete-development"),
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--blobs", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.worker:
        required = (args.database, args.blobs, args.state, args.output)
        if any(path is None for path in required):
            parser.error("worker mode requires --database, --blobs, --state, and --output")
        try:
            report = _worker_result(
                args.worker,
                runtime_root=args.runtime_root,
                fixture_path=args.fixture,
                database=args.database,
                blobs=args.blobs,
                state_path=args.state,
            )
            returncode = 0
        except Exception as error:  # pragma: no cover - worker fail-closed
            report = {"error": f"{type(error).__name__}: {error}"}
            returncode = 1
        encoded = json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return returncode
    try:
        report = qualify_diagnosis_chain(
            runtime_root=args.runtime_root,
            fixture_path=args.fixture,
        )
    except Exception as error:  # pragma: no cover - CLI fail-closed evidence
        report = _finalize(
            {
                "schema": SCHEMA,
                "fixture": {},
                "blocked_path": {},
                "recovery_path": {},
                "terminal_paths": {},
                "metrics": {},
                "execution_error": f"{type(error).__name__}: {error}",
            }
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
