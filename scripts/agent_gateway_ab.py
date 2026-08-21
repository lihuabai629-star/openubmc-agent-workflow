#!/usr/bin/env python3
"""Run and evaluate paired AB/BA qualification for the semantic Agent Gateway."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import time
from typing import Iterable, Mapping


SCHEMA = "openubmc-agent-workflow.agent-gateway-ab.v2"
DEFAULT_BASELINE_REF = "35b36efb6503d05a811b51bf09fb5f8dead0e208"
CHECKPOINTS = (10, 20, 30)
METRICS = (
    "total_tokens",
    "noncached_input_plus_output",
    "tool_output_bytes",
    "model_turns",
    "duration_seconds",
    "time_to_next_actionable_turn_seconds",
)
THRESHOLDS = {
    "geometric_mean_ratio_max": 1.10,
    "one_sided_95_upper_max": 1.15,
    "p95_ratio_max_at_30_pairs": 1.20,
    "first_decision_pairs": 10,
    "expansion_pairs": [20, 30],
    "duplicate_dangerous_effects": 0,
    "false_successes": 0,
    "unknown_new_identity_retries": 0,
}
BENCHMARK_TARGET = "10.121.136.200"
BENCHMARK_CAPABILITIES = ("ssh", "telnet", "mdbctl", "busctl")
BENCHMARK_MDB_QUERIES = (
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Name",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Protocol",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive ResourceId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive SlotNumber",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Presence",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive TemperatureCelsius",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo Type",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo SocketId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.DriveStatus Health",
)


def _json_object(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _read_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            events.append(value)
    return events


def _tool_output_bytes(item: Mapping[str, object]) -> int:
    if item.get("type") == "command_execution":
        return len(str(item.get("aggregated_output", "")).encode("utf-8"))
    return len(
        json.dumps(
            {"result": item.get("result"), "error": item.get("error")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def candidate_scope_acceptance(tools: list[Mapping[str, object]]) -> dict[str, object]:
    errors: list[str] = []
    calls = [
        item
        for item in tools
        if item.get("type") == "mcp_tool_call"
        and item.get("server") == "openubmc-target-runtime"
    ]
    if len(calls) != 1 or calls[0].get("tool") != "observe":
        return {"passed": False, "errors": ["candidate must call observe exactly once"]}
    if any(item.get("type") == "command_execution" for item in tools):
        errors.append("candidate must not execute shell commands")
    call = calls[0]
    arguments = _json_object(call.get("arguments"))
    if arguments.get("target") != BENCHMARK_TARGET:
        errors.append("target does not match the benchmark target")
    freshness = _json_object(arguments.get("freshness"))
    if freshness != {"mode": "live", "max_age_seconds": 0}:
        errors.append("freshness must request one live observation")
    if str(arguments.get("assurance", "auto")).lower() != "auto":
        errors.append("assurance must be auto")
    selectors = arguments.get("selectors")
    selector_values = selectors if isinstance(selectors, list) else []
    capability_selectors = [
        item
        for item in selector_values
        if isinstance(item, Mapping) and str(item.get("kind", "")).lower() == "capability"
    ]
    mdb_selectors = [
        item
        for item in selector_values
        if isinstance(item, Mapping) and str(item.get("kind", "")).lower() == "mdb"
    ]
    if len(capability_selectors) != 1 or len(mdb_selectors) != 1 or len(selector_values) != 2:
        errors.append("selectors must contain exactly one capability and one MDB selector")
        capability = {}
        mdb = {}
    else:
        capability = capability_selectors[0]
        mdb = mdb_selectors[0]
    names = capability.get("names", []) if isinstance(capability, Mapping) else []
    normalized_names = (
        tuple(str(item).strip().lower() for item in names)
        if isinstance(names, list)
        else ()
    )
    if normalized_names != BENCHMARK_CAPABILITIES:
        errors.append("capability selector does not match the four required capabilities")
    queries = mdb.get("queries", []) if isinstance(mdb, Mapping) else []
    if not isinstance(queries, list) or tuple(queries) != BENCHMARK_MDB_QUERIES:
        errors.append("MDB selector does not match the nine exact queries")

    result = _json_object(call.get("result"))
    receipt = _json_object(
        result.get("structured_content") or result.get("structuredContent")
    )
    receipt_id = str(receipt.get("receipt_id", ""))
    if not receipt_id or receipt.get("status") != "complete":
        errors.append("candidate must return a complete ObservationReceipt")
    coverage = _json_object(receipt.get("coverage"))
    expected_coverage = {
        "requested": 13,
        "available": 13,
        "unavailable": 0,
        "not_checked": 0,
        "complete": True,
    }
    if any(coverage.get(name) != value for name, value in expected_coverage.items()):
        errors.append("receipt coverage is incomplete")
    results = _json_object(receipt.get("results"))
    capability_id = str(capability.get("id", "")) if isinstance(capability, Mapping) else ""
    mdb_id = str(mdb.get("id", "")) if isinstance(mdb, Mapping) else ""
    capability_result = _json_object(results.get(capability_id))
    capability_values = capability_result.get("values", [])
    observed_capabilities = {
        str(item.get("name", "")).lower(): str(item.get("status", ""))
        for item in capability_values
        if isinstance(item, Mapping)
    } if isinstance(capability_values, list) else {}
    if observed_capabilities != {name: "available" for name in BENCHMARK_CAPABILITIES}:
        errors.append("capability results are not fully available")
    mdb_result = _json_object(results.get(mdb_id))
    mdb_values = mdb_result.get("values", [])
    if (
        not isinstance(mdb_values, list)
        or len(mdb_values) != len(BENCHMARK_MDB_QUERIES)
        or any(
            not isinstance(item, Mapping)
            or item.get("query_index") != index
            or item.get("status") != "available"
            or "value" not in item
            for index, item in enumerate(mdb_values)
        )
    ):
        errors.append("MDB results do not contain nine available raw values")
    claims = receipt.get("claims", [])
    grounded_ids = {
        str(item.get("selector_id", ""))
        for item in claims
        if isinstance(item, Mapping)
        and item.get("status") == "grounded"
        and item.get("receipt_id") == receipt_id
    } if isinstance(claims, list) else set()
    if grounded_ids != {capability_id, mdb_id}:
        errors.append("receipt claims are not grounded to both selectors")
    return {"passed": not errors, "errors": errors}


def _structured_tool_result(call: Mapping[str, object]) -> Mapping[str, object]:
    result = _json_object(call.get("result"))
    return _json_object(
        result.get("structured_content") or result.get("structuredContent")
    )


def candidate_execute_acceptance(
    tools: list[Mapping[str, object]],
) -> dict[str, object]:
    errors: list[str] = []
    calls = [
        item
        for item in tools
        if item.get("type") == "mcp_tool_call"
        and item.get("server") == "openubmc-target-runtime"
    ]
    if any(call.get("tool") != "execute" for call in calls):
        errors.append("candidate execute qualification may call only execute")
    kinds = [str(_json_object(call.get("arguments")).get("kind", "")) for call in calls]
    if kinds != ["start", "respond"]:
        errors.append("candidate must use one start and one Gate response")
    if any(item.get("type") == "command_execution" for item in tools):
        errors.append("candidate must not execute shell commands")
    if len(calls) == 2:
        start_arguments = _json_object(calls[0].get("arguments"))
        start_result = _structured_tool_result(calls[0])
        final_arguments = _json_object(calls[1].get("arguments"))
        final_result = _structured_tool_result(calls[1])
        if start_arguments.get("target") != BENCHMARK_TARGET:
            errors.append("execute target does not match the benchmark target")
        if start_arguments.get("intent") != "diagnose-and-fix":
            errors.append("execute intent must be diagnose-and-fix")
        if start_arguments.get("delivery_strategy") != "source-only":
            errors.append("execute delivery strategy must be source-only")
        gate = _json_object(start_result.get("gate"))
        if start_result.get("state") != "waiting_response" or not gate:
            errors.append("start must return one actionable Developer Gate")
        if gate.get("owner") != "openubmc-developer":
            errors.append("source-only Gate must be owned by openubmc-developer")
        if final_arguments.get("run_id") != start_result.get("run_id"):
            errors.append("Gate response must continue the same Run")
        response = _json_object(final_arguments.get("response"))
        if response.get("status") != "completed":
            errors.append("Gate response must complete the source phase")
        outcome = _json_object(final_result.get("outcome"))
        if final_result.get("state") != "completed" or outcome.get("status") != "completed":
            errors.append("Gate response must return a completed Runtime Outcome")
    return {
        "passed": not errors,
        "errors": errors,
        "gate_roundtrips": kinds.count("respond"),
        "resume_calls": kinds.count("resume"),
    }


def baseline_execute_acceptance(
    tools: list[Mapping[str, object]],
) -> dict[str, object]:
    errors: list[str] = []
    calls = [
        item
        for item in tools
        if item.get("type") == "mcp_tool_call"
        and item.get("server") == "openubmc-target-runtime"
    ]
    names = [str(call.get("tool", "")) for call in calls]
    if names not in (
        ["workflow.advance", "phase_record", "workflow.next"],
        ["workflow.advance", "phase_record", "workflow.advance"],
    ):
        errors.append(
            "baseline must use workflow.advance, phase_record, and one continuation"
        )
    if any(item.get("type") == "command_execution" for item in tools):
        errors.append("baseline must not execute shell commands")
    if len(calls) == 3:
        start_arguments = _json_object(calls[0].get("arguments"))
        start_result = _structured_tool_result(calls[0])
        phase_arguments = _json_object(calls[1].get("arguments"))
        final_arguments = _json_object(calls[2].get("arguments"))
        final_result = _structured_tool_result(calls[2])
        if start_arguments.get("ip") != BENCHMARK_TARGET:
            errors.append("baseline target does not match the benchmark target")
        if start_arguments.get("intent") != "diagnose-and-fix":
            errors.append("baseline intent must be diagnose-and-fix")
        if start_arguments.get("delivery_strategy") != "source-only":
            errors.append("baseline delivery strategy must be source-only")
        handoff = _json_object(start_result.get("handoff_arguments"))
        contract = _json_object(handoff.get("phase_record_contract"))
        envelope = _json_object(start_result.get("agent_envelope"))
        case_id = str(start_result.get("case_id") or envelope.get("case_id", ""))
        current_revision = start_result.get("revision", envelope.get("revision"))
        if (
            start_result.get("status") != "waiting_phase_record"
            or start_result.get("required_skill") != "openubmc-developer"
            or not contract
        ):
            errors.append("baseline start must return one Developer phase Gate")
        for name in ("case_id", "idempotency_key", "phase_type", "producer_identity"):
            if phase_arguments.get(name) != contract.get(name):
                errors.append(f"baseline phase_record must preserve {name}")
        if phase_arguments.get("expected_revision") != current_revision:
            errors.append("baseline phase_record must use the current envelope revision")
        if phase_arguments.get("status") != "completed":
            errors.append("baseline phase_record must complete the source phase")
        if final_arguments.get("case_id") != case_id:
            errors.append("baseline continuation must use the same Case")
        if final_result.get("status") != "completed" or final_result.get("completed") is not True:
            errors.append("baseline continuation must return a terminal workflow")
    return {
        "passed": not errors,
        "errors": errors,
        "gate_roundtrips": names.count("phase_record"),
        "resume_calls": names.count("workflow.next"),
    }


def _actionable_elapsed(
    events: list[Mapping[str, object]], *, arm: str, scenario: str, fallback: float
) -> float:
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = _json_object(event.get("item"))
        if item.get("type") != "mcp_tool_call":
            continue
        if scenario == "observation" and item.get("tool") != "observe":
            continue
        if scenario == "execute-source-only":
            structured = _structured_tool_result(item)
            if arm == "B":
                if item.get("tool") != "execute":
                    continue
                state = str(structured.get("state", ""))
                if state not in {"waiting_response", "incident", "running", "completed", "failed"}:
                    continue
            else:
                if item.get("tool") not in {"workflow.advance", "workflow.next"}:
                    continue
                if structured.get("status") not in {
                    "waiting_phase_record",
                    "completed",
                    "failed",
                    "blocked",
                    "mutation_outcome_unknown",
                }:
                    continue
        elapsed = event.get("observed_elapsed_seconds")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            return round(float(elapsed), 3)
        return round(fallback, 3)
    return round(fallback, 3)


def metric_from_run(
    *,
    arm: str,
    pair: int,
    order: int,
    events_path: Path,
    final_path: Path,
    exit_code: int,
    duration_seconds: float,
    scenario: str = "observation",
) -> dict[str, object]:
    events = _read_events(events_path)
    completed = [event for event in events if event.get("type") == "turn.completed"]
    usage = _json_object(completed[-1].get("usage")) if completed else {}
    tools = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = _json_object(event.get("item"))
        if item.get("type") in {"command_execution", "mcp_tool_call"}:
            tools.append(item)
    mcp_tools: dict[str, int] = {}
    for item in tools:
        if item.get("type") == "mcp_tool_call":
            name = str(item.get("tool", ""))
            mcp_tools[name] = mcp_tools.get(name, 0) + 1
    final = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    cached_tokens = int(usage.get("cached_input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    acceptance = semantic_acceptance(final, scenario=scenario)
    scope_validation = (
        (
            candidate_scope_acceptance(tools)
            if scenario == "observation"
            else candidate_execute_acceptance(tools)
        )
        if arm == "B"
        else (
            baseline_execute_acceptance(tools)
            if scenario == "execute-source-only"
            else {"passed": True, "errors": []}
        )
    )
    scope_ok = bool(scope_validation["passed"])
    model_turns = max(
        1,
        sum(
            event.get("type") == "item.completed"
            and _json_object(event.get("item")).get("type") == "agent_message"
            for event in events
        ),
    )
    return {
        "scenario": scenario,
        "arm": arm,
        "pair": pair,
        "order": order,
        "exit_code": exit_code,
        "duration_seconds": round(duration_seconds, 3),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": int(
            usage.get("reasoning_output_tokens", 0) or 0
        ),
        "total_tokens": input_tokens + output_tokens,
        "noncached_input_plus_output": input_tokens - cached_tokens + output_tokens,
        "tool_events": len(tools),
        "command_events": sum(
            item.get("type") == "command_execution" for item in tools
        ),
        "mcp_events": sum(item.get("type") == "mcp_tool_call" for item in tools),
        "tool_output_bytes": sum(_tool_output_bytes(item) for item in tools),
        "model_turns": model_turns,
        "time_to_next_actionable_turn_seconds": _actionable_elapsed(
            events,
            arm=arm,
            scenario=scenario,
            fallback=duration_seconds,
        ),
        "gate_roundtrips": int(scope_validation.get("gate_roundtrips", 0) or 0),
        "resume_calls": int(scope_validation.get("resume_calls", 0) or 0),
        "mcp_tools": [
            {"tool": name, "count": count}
            for name, count in sorted(mcp_tools.items())
        ],
        "final_chars": len(final),
        "semantic_acceptance": acceptance,
        "scope_acceptance": scope_ok,
        "scope_validation": scope_validation,
        "valid": (
            exit_code == 0
            and input_tokens + output_tokens > 0
            and acceptance["passed"]
            and scope_ok
        ),
    }


def semantic_acceptance(
    text: str, *, scenario: str = "observation"
) -> dict[str, object]:
    if scenario == "execute-source-only":
        folded = text.lower().replace("`", "")
        required = ("source-only", "runtime", "outcome", "completed")
        missing = [token for token in required if token not in folded]
        return {
            "passed": not missing,
            "missing": missing,
            "conclusion_supported": not missing,
        }
    folded = text.lower().replace("`", "")
    required_groups = {
        "capabilities": ("ssh", "telnet", "mdbctl", "busctl"),
        "drive_fields": (
            "name",
            "protocol",
            "resourceid",
            "slotnumber",
            "presence",
            "temperaturecelsius",
            "type",
            "socketid",
            "health",
        ),
    }
    missing = [
        token
        for tokens in required_groups.values()
        for token in tokens
        if token not in folded
    ]
    conclusion = (
        "不能" in text or "无法" in text
    ) and "resourceid" in folded and "异常" in text
    return {
        "passed": not missing and conclusion,
        "missing": missing,
        "conclusion_supported": conclusion,
    }


def balanced_schedule(pairs: int, *, seed: int) -> list[tuple[int, str, str]]:
    if pairs < 1:
        raise ValueError("pairs must be positive")
    orders = [("A", "B")] * ((pairs + 1) // 2) + [("B", "A")] * (pairs // 2)
    random.Random(seed).shuffle(orders)
    return [(index, first, second) for index, (first, second) in enumerate(orders, 1)]


def _geometric_mean(values: Iterable[float]) -> float:
    samples = list(values)
    if not samples or any(value <= 0 for value in samples):
        raise ValueError("geometric mean requires positive samples")
    return math.exp(statistics.fmean(math.log(value) for value in samples))


def _percentile(values: Iterable[float], percentile: float) -> float:
    samples = sorted(values)
    if not samples:
        raise ValueError("percentile requires samples")
    index = max(0, min(len(samples) - 1, math.ceil(percentile * len(samples)) - 1))
    return samples[index]


def bootstrap_upper(
    ratios: list[float], *, seed: int = 20260819, iterations: int = 20_000
) -> float:
    if not ratios:
        raise ValueError("bootstrap requires paired ratios")
    generator = random.Random(seed)
    size = len(ratios)
    estimates = [
        _geometric_mean(ratios[generator.randrange(size)] for _ in range(size))
        for _ in range(iterations)
    ]
    return _percentile(estimates, 0.95)


def analyze(metrics: list[Mapping[str, object]]) -> dict[str, object]:
    paired: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    pair_ids = sorted({int(item.get("pair", 0)) for item in metrics})
    invalid: list[dict[str, object]] = []
    for pair_id in pair_ids:
        members = [item for item in metrics if int(item.get("pair", 0)) == pair_id]
        by_arm = {str(item.get("arm")): item for item in members}
        metric_gaps = {
            arm: [
                metric
                for metric in METRICS
                if not isinstance(item.get(metric), (int, float))
                or isinstance(item.get(metric), bool)
                or float(item[metric]) <= 0
            ]
            for arm, item in by_arm.items()
        }
        if (
            set(by_arm) != {"A", "B"}
            or not all(bool(item.get("valid")) for item in by_arm.values())
            or any(metric_gaps.values())
        ):
            invalid.append(
                {
                    "pair": pair_id,
                    "arms": sorted(by_arm),
                    "valid": {
                        arm: bool(item.get("valid")) for arm, item in by_arm.items()
                    },
                    "missing_or_nonpositive_metrics": metric_gaps,
                }
            )
            continue
        paired.append((by_arm["A"], by_arm["B"]))
    summaries: dict[str, object] = {}
    all_pass = True
    for metric in METRICS:
        ratios = [float(candidate[metric]) / float(baseline[metric]) for baseline, candidate in paired]
        if ratios:
            point = _geometric_mean(ratios)
            upper = bootstrap_upper(ratios)
            metric_pass = (
                point <= THRESHOLDS["geometric_mean_ratio_max"]
                and upper <= THRESHOLDS["one_sided_95_upper_max"]
            )
            p95_ratio = (
                _percentile(
                    (float(candidate[metric]) for _baseline, candidate in paired),
                    0.95,
                )
                / _percentile(
                    (float(baseline[metric]) for baseline, _candidate in paired),
                    0.95,
                )
                if len(paired) >= 30
                else None
            )
            if p95_ratio is not None:
                metric_pass = (
                    metric_pass
                    and p95_ratio <= THRESHOLDS["p95_ratio_max_at_30_pairs"]
                )
            summaries[metric] = {
                "paired_ratios": [round(value, 6) for value in ratios],
                "geometric_mean_ratio": round(point, 6),
                "one_sided_95_upper": round(upper, 6),
                "p95_ratio": round(p95_ratio, 6) if p95_ratio is not None else None,
                "passed": metric_pass,
            }
            all_pass = all_pass and metric_pass
        else:
            summaries[metric] = {"passed": False}
            all_pass = False
    valid_pairs = len(paired)
    if valid_pairs < CHECKPOINTS[0]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[0]
    elif all_pass:
        decision = "passed"
        next_pairs = None
    elif valid_pairs < CHECKPOINTS[1]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[1]
    elif valid_pairs < CHECKPOINTS[2]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[2]
    else:
        decision = "failed"
        next_pairs = None
    return {
        "schema": SCHEMA,
        "valid_pairs": valid_pairs,
        "invalid_pairs": invalid,
        "metrics": summaries,
        "decision": decision,
        "next_pair_target": next_pairs,
        "thresholds": dict(THRESHOLDS),
    }


def validate_schedule(
    schedule: object,
    metrics: list[Mapping[str, object]],
    *,
    requested_pairs: object,
    scenario: str,
) -> list[str]:
    errors: list[str] = []
    if (
        not isinstance(requested_pairs, int)
        or isinstance(requested_pairs, bool)
        or requested_pairs < 1
    ):
        return ["AB schedule requested pair count is invalid"]
    if not isinstance(schedule, list):
        return ["AB schedule must contain an array"]
    if len(schedule) != requested_pairs:
        errors.append("AB schedule length does not match the requested pair count")

    expected_runs: dict[tuple[int, int], str] = {}
    order_counts = {("A", "B"): 0, ("B", "A"): 0}
    for expected_pair, item in enumerate(schedule, 1):
        if not isinstance(item, list) or len(item) != 3:
            errors.append("AB schedule entries must be [pair, first_arm, second_arm]")
            continue
        pair, first, second = item
        if (
            not isinstance(pair, int)
            or isinstance(pair, bool)
            or pair != expected_pair
        ):
            errors.append("AB schedule pair identifiers must be contiguous")
            continue
        if (
            not isinstance(first, str)
            or not isinstance(second, str)
            or {first, second} != {"A", "B"}
        ):
            errors.append("AB schedule must contain one A arm and one B arm per pair")
            continue
        order = (first, second)
        order_counts[order] += 1
        expected_runs[(pair, 1)] = str(first)
        expected_runs[(pair, 2)] = str(second)

    if order_counts[("A", "B")] != (requested_pairs + 1) // 2 or order_counts[
        ("B", "A")
    ] != requested_pairs // 2:
        errors.append("AB schedule is not balanced between AB and BA order")

    observed_runs: dict[tuple[int, int], str] = {}
    for item in metrics:
        pair = item.get("pair")
        order = item.get("order")
        arm = item.get("arm")
        if (
            not isinstance(pair, int)
            or isinstance(pair, bool)
            or not isinstance(order, int)
            or isinstance(order, bool)
            or not isinstance(arm, str)
            or arm not in {"A", "B"}
        ):
            errors.append("AB raw metrics contain an invalid schedule binding")
            continue
        key = (pair, order)
        if key in observed_runs:
            errors.append("AB raw metrics contain duplicate schedule entries")
            continue
        observed_runs[key] = str(arm)
        if item.get("scenario") != scenario:
            errors.append("AB raw metrics scenario does not match release evidence")
    if observed_runs != expected_runs:
        errors.append("AB raw metrics do not match the recorded schedule")
    return errors


def _run(command: list[str], *, cwd: Path, env: Mapping[str, str], stdin: str, stdout: Path, stderr: Path) -> int:
    with stdout.open("w", encoding="utf-8") as output, stderr.open("w", encoding="utf-8") as errors:
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            bufsize=1,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(stdin)
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                output.write(line)
                continue
            if isinstance(event, dict):
                event["observed_elapsed_seconds"] = round(
                    time.monotonic() - started, 3
                )
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
            else:
                output.write(line)
        return process.wait()


def _prepare_worktree(repo: Path, destination: Path, ref: str) -> None:
    if destination.exists():
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=destination,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        expected = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()
        if current.returncode == 0 and current.stdout.strip() == expected:
            return
        raise RuntimeError(f"benchmark worktree already exists at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(destination), ref],
        cwd=repo,
        check=True,
    )


def _prompt(
    skill_path: Path, *, scenario: str = "observation", arm: str = "B"
) -> str:
    if scenario == "execute-source-only":
        if arm == "A":
            return "\n".join(
                (
                    "这是一次真实 BMC 环境下的 Runtime compatibility execute 配对资格基准。",
                    f"使用已安装的 {skill_path} 所定义的原生 Case Continuation 路径。",
                    "只允许使用 openubmc-debug 与 Gate 指定的 openubmc-developer；不得使用其他 Skill、知识库、网络搜索或 shell。",
                    "不要列出 MCP resources/templates，不要做工具发现。",
                    f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
                    "第一次调用 openubmc-target-runtime.workflow.advance：ip 为目标，intent=diagnose-and-fix，delivery_strategy=source-only，final_purpose=qualify Runtime source-only execution。",
                    "保存返回的 case_id、顶层 revision 与 handoff_arguments.phase_record_contract；不得重新开始 Case。",
                    "第二次调用 phase_record：参数必须是扁平 JSON 对象。展开 phase_record_contract，但 expected_revision 必须替换为 workflow.advance 结果的当前顶层 revision；再把 status=completed、source_revision=qualification-source、summary=qualification source-only receipt completed、authored_files=[src/qualification.lua]、verification_plan=[run qualification tests] 全部放在同一顶层。严禁创建 receipt 或 payload 嵌套字段。",
                    "第三次调用 workflow.next，且只携带同一 case_id，直接推进到终态。",
                    "不得读写源码、不得调用 mutation 工具、不得修改目标。",
                    "最终中文回答必须包含原文：source-only Runtime Outcome completed。回答不超过 200 字。",
                )
            ) + "\n"
        return "\n".join(
            (
                "这是一次真实 BMC 环境下的 Runtime execute 配对资格基准。",
                f"使用已安装的 {skill_path} 所定义的 Agent Gateway 路径。",
                "只允许使用 openubmc-debug 与 Gate 指定的 openubmc-developer；不得使用其他 Skill、知识库、网络搜索或 shell。",
                "不要列出 MCP resources/templates，不要做工具发现；直接调用 openubmc-target-runtime.execute。",
                f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
                "第一次且仅第一次调用：kind=start，intent=diagnose-and-fix，delivery_strategy=source-only。",
                "保存 start Result 的 run_id、gate.gate_id、gate.gate_version 与 gate.schema_digest；不得再次 start。",
                "Runtime 返回 developer.change Gate 后，不读写任何源码，提交固定基准 receipt。response 必须是三字段对象：status=completed；summary=qualification source-only receipt completed；payload。",
                "第二次且仅第二次调用：kind=respond，并原样携带保存的 run_id、gate_id、gate_version、schema_digest；payload 内只含 source_revision=qualification-source、authored_files=[src/qualification.lua]、verification_plan=[run qualification tests]。",
                "同一 Gate 只能响应一次；不得省略 Gate binding，不得 poll、不得调用 resume、不得修改目标。",
                "必须推进到终态，并在最终中文回答中包含原文：source-only Runtime Outcome completed。回答不超过 200 字。",
            )
        ) + "\n"
    return "\n".join(
        (
            "这是一次真实 BMC 环境下的 openubmc-debug 配对性能基准。",
            f"完整读取并严格遵循 {skill_path}。",
            "请使用该版本 Skill 的原生默认路径，不得人为禁用或改用另一版本机制。不得使用其他 Skill、知识库、网络搜索、源码或日志。",
            f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
            "执行只读现场核验：",
            "1. 获取 live 能力快照，确认 SSH、Telnet、MDBCTL、BUSCTL。",
            "2. 对固定对象 Drive_1_010102 执行以下九条精确 MDB getprop，不得枚举其他对象或属性：",
            *(f"   - {query}" for query in BENCHMARK_MDB_QUERIES),
            "3. 报告原始值，并判断这些证据能否单独证明 ResourceId=0 异常，不得臆测源码语义。",
            "禁止写操作、状态变更、宽查询、日志和源码读取。中文回答，不超过 500 字。",
        )
    ) + "\n"


@dataclass(frozen=True)
class RunConfig:
    arm: str
    source_root: Path
    interface_profile: str


def prepare_arm_home(home: Path, source_root: Path) -> None:
    """Install the selected arm's Skill into the isolated benchmark home."""
    for skill_name in ("openubmc-debug", "openubmc-developer"):
        skill_source = source_root / skill_name
        if not skill_source.is_dir():
            raise FileNotFoundError(f"{skill_name} Skill not found: {skill_source}")
        for client_root in (".agents", ".codex"):
            link = home / client_root / "skills" / skill_name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(skill_source, target_is_directory=True)


def _git_commit(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _require_clean_candidate(repo: Path) -> None:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    if completed.stdout.strip():
        raise RuntimeError(
            "AB qualification requires a clean candidate repository so the "
            "recorded source commit identifies the tested source"
        )


def _version(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else "unavailable"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def release_evidence(
    *,
    scenario: str,
    requested_pairs: int,
    candidate_source_commit: str,
    baseline_source_commit: str,
    model: str,
    metrics_path: Path,
    schedule_path: Path,
    analysis: Mapping[str, object],
    environment: Mapping[str, object],
) -> dict[str, object]:
    environment_record = dict(environment)
    evidence: dict[str, object] = {
        "schema": f"{SCHEMA}/release-evidence-v1",
        "scenario": scenario,
        "source": {
            "candidate_commit": candidate_source_commit,
            "baseline_commit": baseline_source_commit,
        },
        "model": model,
        "environment": environment_record,
        "environment_fingerprint": _fingerprint(environment_record),
        "thresholds": dict(THRESHOLDS),
        "samples": {
            "requested_pairs": requested_pairs,
            "valid_pairs": int(analysis.get("valid_pairs", 0) or 0),
            "invalid_pairs": list(analysis.get("invalid_pairs", [])),
        },
        "artifacts": {
            "all_metrics": {
                "path": metrics_path.name,
                "sha256": _sha256(metrics_path),
                "size_bytes": metrics_path.stat().st_size,
            },
            "schedule": {
                "path": schedule_path.name,
                "sha256": _sha256(schedule_path),
                "size_bytes": schedule_path.stat().st_size,
            },
        },
    }
    evidence["evidence_digest"] = _fingerprint(evidence)
    return evidence


def verify_summary(
    summary_path: Path, *, expected_source_commit: str
) -> dict[str, object]:
    errors: list[str] = []
    try:
        value = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "schema": f"{SCHEMA}/verification-v1",
            "promotable": False,
            "errors": [f"cannot read AB summary: {type(exc).__name__}"],
        }
    if not isinstance(value, Mapping):
        return {
            "schema": f"{SCHEMA}/verification-v1",
            "promotable": False,
            "errors": ["AB summary must be a JSON object"],
        }
    summary = dict(value)
    if summary.get("schema") != SCHEMA:
        errors.append("AB summary schema is not the current qualification schema")
    if summary.get("decision") != "passed":
        errors.append("AB summary decision is not passed")
    valid_pairs = int(summary.get("valid_pairs", 0) or 0)
    if valid_pairs < CHECKPOINTS[0]:
        errors.append("AB summary has fewer than ten valid pairs")
    invalid_pairs = summary.get("invalid_pairs", [])
    if not isinstance(invalid_pairs, list) or invalid_pairs:
        errors.append("AB summary contains invalid pairs")
    if summary.get("thresholds") != THRESHOLDS:
        errors.append("AB summary thresholds do not match the release contract")
    metric_summary = _json_object(summary.get("metrics"))
    for metric in METRICS:
        if not bool(_json_object(metric_summary.get(metric)).get("passed")):
            errors.append(f"AB metric did not pass: {metric}")

    evidence = _json_object(summary.get("release_evidence"))
    if evidence.get("scenario") != "execute-source-only":
        errors.append("AB release evidence is not execute-source-only")
    source = _json_object(evidence.get("source"))
    if source.get("candidate_commit") != expected_source_commit:
        errors.append("AB candidate source commit does not match the release candidate")
    samples = _json_object(evidence.get("samples"))
    if samples.get("valid_pairs") != valid_pairs or samples.get("invalid_pairs") != invalid_pairs:
        errors.append("AB release evidence sample counts do not match the summary")
    if evidence.get("thresholds") != THRESHOLDS:
        errors.append("AB release evidence thresholds do not match the release contract")
    if not str(evidence.get("model", "")).strip():
        errors.append("AB release evidence is missing the model")
    environment = _json_object(evidence.get("environment"))
    if evidence.get("environment_fingerprint") != _fingerprint(dict(environment)):
        errors.append("AB release evidence environment fingerprint is invalid")

    artifacts = _json_object(evidence.get("artifacts"))
    artifact_paths: dict[str, Path] = {}
    for name in ("all_metrics", "schedule"):
        artifact = _json_object(artifacts.get(name))
        path = Path(str(artifact.get("path", "")))
        if not path.is_absolute():
            path = summary_path.parent / path
        if not path.is_file():
            errors.append(f"AB {name} artifact is unavailable")
            continue
        if artifact.get("sha256") != _sha256(path):
            errors.append(f"AB {name} digest does not match the artifact")
        if artifact.get("size_bytes") != path.stat().st_size:
            errors.append(f"AB {name} size does not match the artifact")
        artifact_paths[name] = path

    raw_metrics: list[Mapping[str, object]] | None = None
    metrics_path = artifact_paths.get("all_metrics")
    if metrics_path is not None:
        try:
            loaded_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read AB raw metrics: {type(exc).__name__}")
        else:
            if not isinstance(loaded_metrics, list) or not all(
                isinstance(item, Mapping) for item in loaded_metrics
            ):
                errors.append("AB raw metrics must contain an array of objects")
            else:
                raw_metrics = loaded_metrics
                try:
                    recomputed = analyze(raw_metrics)
                except (KeyError, OverflowError, TypeError, ValueError) as exc:
                    errors.append(f"cannot analyze AB raw metrics: {type(exc).__name__}")
                else:
                    analysis_fields = (
                        "schema",
                        "valid_pairs",
                        "invalid_pairs",
                        "metrics",
                        "decision",
                        "next_pair_target",
                        "thresholds",
                    )
                    if any(
                        summary.get(name) != recomputed.get(name)
                        for name in analysis_fields
                    ):
                        errors.append("AB summary is not derived from the raw metrics")
    schedule_path = artifact_paths.get("schedule")
    if schedule_path is not None and raw_metrics is not None:
        try:
            raw_schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read AB schedule: {type(exc).__name__}")
        else:
            errors.extend(
                validate_schedule(
                    raw_schedule,
                    raw_metrics,
                    requested_pairs=samples.get("requested_pairs"),
                    scenario=str(evidence.get("scenario", "")),
                )
            )
    expected_evidence_digest = evidence.get("evidence_digest")
    evidence_without_digest = dict(evidence)
    evidence_without_digest.pop("evidence_digest", None)
    if expected_evidence_digest != _fingerprint(evidence_without_digest):
        errors.append("AB release evidence digest is invalid")
    return {
        "schema": f"{SCHEMA}/verification-v1",
        "promotable": not errors,
        "errors": errors,
        "source_commit": expected_source_commit,
        "valid_pairs": valid_pairs,
        "invalid_pairs": invalid_pairs if isinstance(invalid_pairs, list) else [],
        "summary_path": str(summary_path),
        "summary_sha256": _sha256(summary_path),
        "evidence_digest": expected_evidence_digest,
    }


def run_benchmark(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    _require_clean_candidate(repo)
    work_root = args.work_root.resolve()
    baseline_root = work_root / "variants" / "baseline"
    _prepare_worktree(repo, baseline_root, args.baseline_ref)
    candidate_root = repo
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output.resolve() if args.output else work_root / f"results-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    schedule = balanced_schedule(args.pairs, seed=args.seed)
    (output / "schedule.json").write_text(
        json.dumps(schedule, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    configs = {
        "A": RunConfig("A", baseline_root, ""),
        "B": RunConfig("B", candidate_root, "agent"),
    }
    environment = os.environ.copy()
    environment["OPENUBMC_CREDENTIALS_FILE"] = str(args.credentials)
    environment["OPENUBMC_DEBUG_CREDENTIALS_FILE"] = str(args.credentials)
    metrics: list[dict[str, object]] = []
    for pair, first, second in schedule:
        ordered_arms = tuple(
            arm for arm in (first, second) if args.only_arm is None or arm == args.only_arm
        )
        for order, arm in enumerate(ordered_arms, 1):
            config = configs[arm]
            run_dir = output / f"pair-{pair:02d}-{order}-{arm}"
            home = run_dir / "home"
            run_dir.mkdir(parents=True)
            home.mkdir()
            prepare_arm_home(home, config.source_root)
            prompt = _prompt(
                config.source_root / "openubmc-debug" / "SKILL.md",
                scenario=args.scenario,
                arm=arm,
            )
            (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            final_path = run_dir / "final.md"
            events_path = run_dir / "events.jsonl"
            stderr_path = run_dir / "stderr.log"
            command = [
                args.codex,
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--json",
                "--sandbox",
                "danger-full-access",
                "--skip-git-repo-check",
                "-C",
                str(args.codex_cwd),
                "-m",
                args.model,
                "-o",
                str(final_path),
            ]
            for value in args.codex_config:
                command.extend(("-c", value))
            command.extend(
                (
                    "-c",
                    'mcp_servers.openubmc-target-runtime.command="/usr/bin/python3"',
                    "-c",
                    (
                        "mcp_servers.openubmc-target-runtime.args=["
                        + json.dumps(str(config.source_root / "openubmc-debug" / "scripts" / "target_runtime_mcp.py"))
                        + "]"
                    ),
                    "-c",
                    'mcp_servers.openubmc-target-runtime.env_vars=["OPENUBMC_CREDENTIALS_FILE","OPENUBMC_DEBUG_CREDENTIALS_FILE","OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE"]',
                    "-c",
                    "mcp_servers.openubmc-target-runtime.tool_timeout_sec=900",
                )
            )
            run_env = dict(environment)
            run_env["HOME"] = str(home)
            run_env["CODEX_HOME"] = os.environ.get("CODEX_HOME", "/root/.codex")
            if config.interface_profile:
                run_env["OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE"] = config.interface_profile
            else:
                run_env.pop("OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE", None)
            started = time.monotonic()
            exit_code = _run(
                command,
                cwd=args.codex_cwd,
                env=run_env,
                stdin=prompt,
                stdout=events_path,
                stderr=stderr_path,
            )
            duration = time.monotonic() - started
            metric = metric_from_run(
                arm=arm,
                pair=pair,
                order=order,
                events_path=events_path,
                final_path=final_path,
                exit_code=exit_code,
                duration_seconds=duration,
                scenario=args.scenario,
            )
            metrics.append(metric)
            (run_dir / "metrics.json").write_text(
                json.dumps(metric, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (output / "all_metrics.json").write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(metric, ensure_ascii=False, sort_keys=True), flush=True)
            if args.pause_seconds:
                time.sleep(args.pause_seconds)
    summary = analyze(metrics)
    metrics_path = output / "all_metrics.json"
    schedule_path = output / "schedule.json"
    environment_record = {
        "python": platform.python_version(),
        "node": _version(["node", "--version"]),
        "codex": _version([args.codex, "--version"]),
        "platform": platform.platform(),
    }
    summary["release_evidence"] = release_evidence(
        scenario=args.scenario,
        requested_pairs=args.pairs,
        candidate_source_commit=_git_commit(repo, "HEAD"),
        baseline_source_commit=_git_commit(repo, args.baseline_ref),
        model=args.model,
        metrics_path=metrics_path,
        schedule_path=schedule_path,
        analysis=summary,
        environment=environment_record,
    )
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False, indent=2))
    if args.only_arm is not None:
        return 0 if metrics and all(bool(item["valid"]) for item in metrics) else 1
    return 0 if summary["decision"] == "passed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("metrics", type=Path)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("summary", type=Path)
    verify_parser.add_argument("--source-ref", required=True)
    verify_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--work-root", type=Path, required=True)
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    run_parser.add_argument("--pairs", type=int, default=10)
    run_parser.add_argument("--seed", type=int, default=20260819)
    run_parser.add_argument("--credentials", type=Path, required=True)
    run_parser.add_argument("--codex", default="codex")
    run_parser.add_argument("--codex-cwd", type=Path, default=Path("/home/workspace"))
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--codex-config", action="append", default=[])
    run_parser.add_argument("--pause-seconds", type=float, default=5)
    run_parser.add_argument("--only-arm", choices=("A", "B"))
    run_parser.add_argument(
        "--scenario",
        choices=("observation", "execute-source-only"),
        default="observation",
    )
    args = parser.parse_args(argv)
    if args.command == "analyze":
        value = json.loads(args.metrics.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            parser.error("metrics must contain an array")
        print(json.dumps(analyze(value), ensure_ascii=False, indent=2))
        return 0
    if args.command == "verify":
        expected = _git_commit(args.repo.resolve(), args.source_ref)
        verification = verify_summary(
            args.summary.expanduser().absolute(),
            expected_source_commit=expected,
        )
        print(json.dumps(verification, ensure_ascii=False, indent=2))
        return 0 if verification["promotable"] else 1
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
