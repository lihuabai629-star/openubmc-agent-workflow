"""Deterministic evaluation of sanitized openUBMC session replays."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import re


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
        "status": "passed" if observed_status == expected_status else "failed",
        "observed_status": observed_status,
        "expected_status": expected_status,
        "dimensions": dimensions,
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
