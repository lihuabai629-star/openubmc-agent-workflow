"""Deterministic evaluation of sanitized openUBMC session replays."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
from execution_router import ExecutionRouter, SUPPORTED_OPERATIONS, probe_protocol

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "openubmc-target-runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))
from openubmc_target_runtime.terminal_delivery import (  # noqa: E402
    TerminalAnswerError, TerminalAnswerStore, qualify_terminal_answer,
)


SCHEMA = "openubmc.sanitized-replay/v1"
DIMENSIONS = (
    "skill_routing",
    "execution_host",
    "evidence_lineage",
    "completion_calibration",
    "release_gates",
    "credential_containment",
    "convergence_cost",
    "recovery_rollback",
    "version_consistency",
    "final_answer",
)
_SENSITIVE_VALUE = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[^<\s][^,}\n]*")
_PRIVATE_ENDPOINT = re.compile(r"(?:https?://|ssh://)?(?:10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[0-1])\.)")
MAX_BOUNDARY_BYTES = 4096
SHELL_FALLBACK_FIELDS = (
    "reason_code",
    "host",
    "requested_scope",
    "evidence_boundary",
    "call_budget",
)


class ReplayError(ValueError):
    pass


def _live_observation(case: Mapping[str, object]) -> dict[str, object]:
    """Derive selected observations from packaged public entry points."""
    observed = dict(_mapping(case.get("observed"), "observed"))
    probe = case.get("probe")
    if probe is None:
        return observed
    probe = _mapping(probe, "probe")
    if probe.get("kind") == "build-route":
        request = str(probe.get("request", ""))
        if not request or len(request) > 256:
            raise ReplayError("build-route probe requires a bounded request")
        script = Path(__file__).resolve().parents[1] / "openubmc-build" / "scripts" / "build_route.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--request", request],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode not in (0, 2):
            raise ReplayError("build-route public probe failed")
        route = json.loads(completed.stdout)
        observed["skill_routing"] = {"owner": route["owner"]}
    elif probe.get("kind") == "execution-route":
        environment = str(probe.get("environment", ""))
        operation = str(probe.get("operation", ""))
        raw_tools = probe.get("tools")
        if not isinstance(raw_tools, list) or not all(isinstance(item, str) for item in raw_tools):
            raise ReplayError("execution-route probe tools must be a list")
        router = ExecutionRouter(environment=environment)
        health = probe_protocol(
            operation, host=str(probe.get("host", router.expected_host)),
            initialize=lambda: ({"protocolVersion": "2025-03-26"}
                                if probe.get("initialized", True) else {}),
            list_tools=lambda: {"tools": [{"name": name} for name in raw_tools]},
        )
        fallback_host = (str(probe.get("shell_host", router.expected_host))
                         if (not health.ready or health.host != router.expected_host
                             or operation not in SUPPORTED_OPERATIONS) else None)
        route = router.choose(
            operation,
            probe=health,
            requested_scope="sanitized fixture", evidence_boundary="router receipt",
            shell_host=fallback_host,
        )
        route_observation = {
            "host": route["execution_host"],
            "route": "structured-runtime" if route["path"] == "structured-runtime-mcp" else "shell-fallback",
        }
        if route["path"] == "shell-fallback":
            fallback = route["fallback"]
            route_observation.update({
                "reason_code": fallback["reason_code"],
                "requested_scope": route["requested_scope"],
                "evidence_boundary": route["evidence_boundary"],
                "call_budget": fallback["budget"],
            })
        observed["execution_host"] = route_observation
    elif probe.get("kind") == "terminal-answer":
        task_id = str(case.get("case_id", ""))
        outcome = {"status": "completed", "summary": "fixture"}
        with tempfile.TemporaryDirectory() as raw:
            store = TerminalAnswerStore(Path(raw) / "answers.json")
            store.prepare(task_id=task_id, run_id="fixture-run", outcome=outcome,
                          delivery_stage="diagnosed", text="fixture final")
            rollout = Path(raw) / "rollout.jsonl"
            events = [{"type": "session_meta", "payload": {"id": task_id}}]
            if probe.get("delivered") is True:
                prepared = store.get(task_id)
                event_time = (datetime.fromisoformat(prepared.prepared_at) + timedelta(seconds=1)).isoformat()
                events.append({"type": "response_item", "timestamp": event_time, "payload": {
                    "type": "message", "id": "fixture-host-final", "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "fixture final"}],
                }})
            rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n",
                               encoding="utf-8")
            try:
                store.acknowledge_rollout(
                    rollout, task_id=task_id, run_id="fixture-run", outcome=outcome,
                    delivery_stage="diagnosed",
                )
            except TerminalAnswerError as exc:
                if "final event is missing" not in str(exc):
                    raise
            result = qualify_terminal_answer(
                task_id=task_id, run_id="fixture-run", outcome=outcome,
                delivery_stage="diagnosed", record=store.get(task_id),
            )
        observed["final_answer"] = {"present": result["status"] == "passed", "task_id": task_id}
    else:
        raise ReplayError("unsupported live probe")
    return observed


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReplayError(f"{name} must be an object")
    return value


def sanitization_issues(value: object, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?i)^(?:password|passwd|secret|token|clientSecret|apiKey)$", key_text):
                if item not in (None, "", "<redacted>", "credential_ref"):
                    issues.append(f"{path}.{key_text}: secret value")
            issues.extend(sanitization_issues(item, f"{path}.{key_text}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            issues.extend(sanitization_issues(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        if _SENSITIVE_VALUE.search(value):
            issues.append(f"{path}: inline secret")
        if _PRIVATE_ENDPOINT.search(value):
            issues.append(f"{path}: private endpoint")
        if "/home/" in value or "\\Users\\" in value:
            issues.append(f"{path}: private local path")
    return issues


def _bounded_evidence(case: Mapping[str, object], name: str) -> Mapping[str, object]:
    boundaries = _mapping(case.get("evidence_boundary"), "evidence_boundary")
    boundary = _mapping(boundaries.get(name), f"evidence_boundary.{name}")
    raw = json.dumps(
        boundary,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not boundary or len(raw) > MAX_BOUNDARY_BYTES:
        raise ReplayError(
            f"evidence_boundary.{name} must be non-empty and at most "
            f"{MAX_BOUNDARY_BYTES} bytes"
        )
    issues = sanitization_issues(boundary, f"evidence_boundary.{name}")
    if issues:
        raise ReplayError("; ".join(issues))
    return boundary


def _dimension(case: Mapping[str, object], name: str) -> dict[str, object]:
    expected = _mapping(_mapping(case.get("expected"), "expected").get(name), f"expected.{name}")
    observed = _mapping(_mapping(case.get("observed"), "observed").get(name), f"observed.{name}")
    boundary = _bounded_evidence(case, name)
    failures: list[str] = []
    for key, wanted in expected.items():
        if observed.get(key) != wanted:
            failures.append(f"{key}: expected {wanted!r}, observed {observed.get(key)!r}")
    if name == "evidence_lineage":
        identity = expected.get("identity", {})
        for index, item in enumerate(observed.get("evidence", [])):
            if not isinstance(item, Mapping):
                failures.append(f"evidence[{index}] is not an object")
                continue
            for key in ("target", "address", "version", "operation"):
                if key in identity and item.get(key) != identity[key]:
                    failures.append(f"evidence[{index}].{key} is not bound to fixture identity")
    if name == "release_gates":
        gates = observed.get("gates", [])
        if expected.get("required") is True and (
            not isinstance(gates, list) or not gates
        ):
            failures.append("required release gates are missing")
        if any(not isinstance(item, Mapping) or item.get("status") != "pass" for item in gates):
            failures.append("a release gate did not pass")
    if name == "execution_host" and observed.get("route") == "shell-fallback":
        missing = [field for field in SHELL_FALLBACK_FIELDS if observed.get(field) in (None, "", [], {})]
        budget = observed.get("call_budget")
        if missing:
            failures.append("shell fallback is missing: " + ", ".join(missing))
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 32:
            failures.append("shell fallback call_budget must be between 1 and 32")
    if name == "credential_containment":
        failures.extend(sanitization_issues(observed))
    if name == "convergence_cost":
        actions = observed.get("actions", [])
        budget = int(expected.get("budget", 0))
        if len(actions) > budget:
            failures.append(f"action budget exceeded: {len(actions)}>{budget}")
        seen: dict[str, str] = {}
        for item in actions:
            if not isinstance(item, Mapping):
                continue
            command = str(item.get("command", ""))
            evidence = str(item.get("evidence_digest", ""))
            if command in seen and evidence == seen[command] and item.get("justified_retry") is not True:
                failures.append(f"equivalent action repeated without changed evidence: {command}")
            seen[command] = evidence
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "expected_behavior": dict(expected),
        "observed_behavior": dict(observed),
        "bounded_evidence": dict(boundary),
    }


def evaluate_case(case: Mapping[str, object]) -> dict[str, object]:
    case_id = str(case.get("case_id", ""))
    if not case_id:
        raise ReplayError("case_id is required")
    failures = sanitization_issues(case)
    try:
        observed = _live_observation(case)
    except (ReplayError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ReplayError(f"{case_id}: {exc}") from exc
    probe = case.get("probe")
    probe_result: dict[str, object] = {"status": "not_applicable", "failures": []}
    if isinstance(probe, Mapping):
        probe_dimension = {
            "build-route": "skill_routing",
            "execution-route": "execution_host",
            "terminal-answer": "final_answer",
        }.get(str(probe.get("kind")))
        wanted = _mapping(probe.get("expected_observation"), "probe.expected_observation")
        actual = _mapping(observed.get(probe_dimension), f"observed.{probe_dimension}")
        probe_failures = [
            f"{key}: expected {value!r}, observed {actual.get(key)!r}"
            for key, value in wanted.items() if actual.get(key) != value
        ]
        probe_result = {
            "status": "passed" if not probe_failures else "failed",
            "dimension": probe_dimension,
            "expected_observation": dict(wanted),
            "observed_observation": dict(actual),
            "failures": probe_failures,
        }
    case = {**case, "observed": observed}
    dimensions: dict[str, object] = {}
    for name in DIMENSIONS:
        try:
            dimensions[name] = _dimension(case, name)
        except ReplayError as exc:
            dimensions[name] = {
                "status": "failed",
                "failures": [str(exc)],
                "expected_behavior": {},
                "observed_behavior": {},
                "bounded_evidence": {},
            }
    if failures:
        dimensions["sanitization"] = {"status": "failed", "failures": failures}
    passed = not any(value.get("status") != "passed" for value in dimensions.values() if isinstance(value, Mapping))
    observed_status = "passed" if passed else "failed"
    expected_status = str(case.get("expected_result", "passed"))
    return {
        "schema": SCHEMA,
        "case_id": case_id,
        "status": "passed" if observed_status == expected_status and probe_result["status"] != "failed" else "failed",
        "observed_status": observed_status,
        "expected_status": expected_status,
        "dimensions": dimensions,
        "probe": probe_result,
    }


def evaluate_directory(directory: Path) -> dict[str, object]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        result = evaluate_case(_mapping(value, str(path)))
        result["fixture"] = path.name
        cases.append(result)
    if not cases:
        raise ReplayError("sanitized replay directory is empty")
    report = {
        "schema": f"{SCHEMA}/report",
        "status": "passed" if all(item["status"] == "passed" for item in cases) else "failed",
        "cases": cases,
        "failed_cases": [item["case_id"] for item in cases if item["status"] != "passed"],
    }
    report_issues = sanitization_issues(report)
    report["sanitization"] = {
        "status": "passed" if not report_issues else "failed",
        "failures": report_issues,
    }
    if report_issues:
        report["status"] = "failed"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = evaluate_directory(args.directory)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
