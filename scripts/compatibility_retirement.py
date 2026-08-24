#!/usr/bin/env python3
"""Produce compatibility-writer retirement evidence for Operator and CI use."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping
from pathlib import Path
import subprocess
import sys
import time


BASELINE_SCHEMA = (
    "openubmc-agent-workflow.compatibility-retirement-baseline.v1"
)
INCREMENT_SCHEMA = (
    "openubmc-agent-workflow.compatibility-retirement-increment.v3"
)
DECISION_SCHEMA = (
    "openubmc-agent-workflow.compatibility-retirement-decision.v3"
)
WRITER_METRICS = {
    "observe.assurance": (("feature", "observe.assurance"),),
    "execute.control_continue": (
        ("feature", "execute.control_continue"),
    ),
    "execute.observation_receipt": (
        ("feature", "execute.observation_receipt"),
    ),
    "phase_record": (
        ("feature", "phase_record"),
        ("operation", "phase_record"),
    ),
    "workflow.next": (
        ("feature", "workflow.next"),
        ("operation", "workflow.next"),
    ),
}
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_gate_contract import (  # noqa: E402
    REQUIRED_RELEASE_GATES,
    RETIREMENT_RELEASE_ARTIFACTS,
    verify_release_gate_report,
)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _full_commit(value: str, *, name: str = "source_commit") -> str:
    commit = value.strip().lower()
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise ValueError(f"{name} must be a full Git commit SHA")
    return commit


def _finite_timestamp(value: object, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(timestamp):
        raise ValueError(f"{name} must be finite")
    if timestamp <= 0:
        raise ValueError(f"{name} must be positive")
    return timestamp


def _counts(value: object, *, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"compatibility telemetry {name} must be an object")
    normalized: dict[str, int] = {}
    for raw_metric, raw_count in value.items():
        metric = str(raw_metric).strip()
        if not metric:
            raise ValueError(f"compatibility telemetry {name} has an empty metric")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise ValueError(f"compatibility telemetry {name}.{metric} must be an integer")
        if raw_count < 0:
            raise ValueError(f"compatibility telemetry {name}.{metric} cannot be negative")
        normalized[metric] = raw_count
    return dict(sorted(normalized.items()))


def _nonnegative_deltas(value: object, *, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError(f"compatibility increment {name} must be an object")
    normalized: dict[str, int] = {}
    for raw_metric, raw_delta in value.items():
        metric = str(raw_metric).strip()
        if not metric:
            raise ValueError(f"compatibility increment {name} has an empty metric")
        if isinstance(raw_delta, bool) or not isinstance(raw_delta, int):
            raise ValueError(
                f"compatibility increment {name}.{metric} must be an integer"
            )
        if raw_delta < 0:
            raise ValueError(
                f"compatibility increment {name}.{metric} cannot be negative"
            )
        normalized[metric] = raw_delta
    return dict(sorted(normalized.items()))


def _timestamps(
    value: object,
    *,
    name: str,
    counts: Mapping[str, int],
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"compatibility telemetry {name} must be an object")
    normalized: dict[str, float] = {}
    for raw_metric, raw_timestamp in value.items():
        metric = str(raw_metric).strip()
        if isinstance(raw_timestamp, bool) or not isinstance(
            raw_timestamp, (int, float)
        ):
            raise ValueError(f"compatibility telemetry {name}.{metric} must be numeric")
        timestamp = _finite_timestamp(
            raw_timestamp,
            name=f"compatibility telemetry {name}.{metric}",
        )
        normalized[metric] = timestamp
    if set(normalized) != set(counts):
        raise ValueError(f"compatibility telemetry {name} must match its counts")
    return dict(sorted(normalized.items()))


def _normalize_telemetry(value: Mapping[str, object]) -> dict[str, object]:
    try:
        raw_tracking_started_at = value["tracking_started_at"]
        total_calls = int(value["total_calls"])
        total_features = int(value["total_features"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("compatibility telemetry summary is incomplete") from exc
    tracking_started_at = _finite_timestamp(
        raw_tracking_started_at,
        name="compatibility telemetry tracking_started_at",
    )
    operations = _counts(value.get("operation_counts"), name="operation_counts")
    features = _counts(value.get("feature_counts"), name="feature_counts")
    if total_calls != sum(operations.values()):
        raise ValueError("compatibility telemetry total_calls does not match counts")
    if total_features != sum(features.values()):
        raise ValueError("compatibility telemetry total_features does not match counts")
    raw_last_seen = value.get("last_seen_at")
    if not isinstance(raw_last_seen, Mapping):
        raise ValueError("compatibility telemetry last_seen_at must be an object")
    operation_seen = _timestamps(
        raw_last_seen.get("operations"),
        name="last_seen_at.operations",
        counts=operations,
    )
    feature_seen = _timestamps(
        raw_last_seen.get("features"),
        name="last_seen_at.features",
        counts=features,
    )
    if any(timestamp < tracking_started_at for timestamp in operation_seen.values()):
        raise ValueError("operation last_seen_at predates telemetry tracking")
    if any(timestamp < tracking_started_at for timestamp in feature_seen.values()):
        raise ValueError("feature last_seen_at predates telemetry tracking")
    return {
        "tracking_started_at": tracking_started_at,
        "total_calls": total_calls,
        "operation_counts": operations,
        "total_features": total_features,
        "feature_counts": features,
        "last_seen_at": {
            "operations": operation_seen,
            "features": feature_seen,
        },
    }


def _telemetry_from_status(value: Mapping[str, object]) -> dict[str, object]:
    selected = value.get("result", value)
    if not isinstance(selected, Mapping):
        raise ValueError("Runtime status result must be an object")
    telemetry = selected.get("compatibility_telemetry", selected)
    if not isinstance(telemetry, Mapping):
        raise ValueError("Runtime status compatibility_telemetry must be an object")
    return _normalize_telemetry(telemetry)


def _verify_evidence(
    value: Mapping[str, object],
    *,
    schema: str,
    label: str,
) -> None:
    if value.get("schema") != schema:
        raise ValueError(f"{label} schema is unsupported")
    expected = value.get("evidence_digest")
    unsigned = dict(value)
    unsigned.pop("evidence_digest", None)
    if expected != _fingerprint(unsigned):
        raise ValueError(f"{label} evidence digest is invalid")


def create_baseline(
    runtime_status: Mapping[str, object],
    *,
    source_commit: str,
    captured_at: float,
) -> dict[str, object]:
    commit = _full_commit(source_commit)
    captured = _finite_timestamp(captured_at, name="baseline captured_at")
    telemetry = _telemetry_from_status(runtime_status)
    latest_seen = max(
        [telemetry["tracking_started_at"]]
        + list(telemetry["last_seen_at"]["operations"].values())
        + list(telemetry["last_seen_at"]["features"].values())
    )
    if captured < latest_seen:
        raise ValueError("baseline captured_at predates compatibility telemetry")
    report: dict[str, object] = {
        "schema": BASELINE_SCHEMA,
        "source_commit": commit,
        "captured_at": captured,
        "telemetry": telemetry,
    }
    report["evidence_digest"] = _fingerprint(report)
    return report


def _metric_deltas(
    baseline: Mapping[str, int], current: Mapping[str, int]
) -> dict[str, int]:
    deltas = {
        metric: current.get(metric, 0) - baseline.get(metric, 0)
        for metric in sorted(set(baseline) | set(current))
    }
    regressions = [metric for metric, delta in deltas.items() if delta < 0]
    if regressions:
        raise ValueError(
            "compatibility telemetry count regressed for " + ", ".join(regressions)
        )
    return deltas


def _changed_timestamps_without_count(
    baseline: Mapping[str, float],
    current: Mapping[str, float],
    deltas: Mapping[str, int],
) -> list[str]:
    changed = []
    for metric in sorted(set(baseline) | set(current)):
        current_seen = current.get(metric, 0.0)
        baseline_seen = baseline.get(metric, 0.0)
        if deltas.get(metric, 0) == 0 and current_seen != baseline_seen:
            changed.append(metric)
    return changed


def _counts_without_newer_timestamp(
    baseline: Mapping[str, float],
    current: Mapping[str, float],
    deltas: Mapping[str, int],
) -> list[str]:
    return [
        metric
        for metric, delta in sorted(deltas.items())
        if delta > 0
        and current.get(metric, 0.0) <= baseline.get(metric, 0.0)
    ]


def _increment_deltas(
    baseline_telemetry: Mapping[str, object],
    current_telemetry: Mapping[str, object],
    *,
    baseline_captured: float,
    current_captured: float,
) -> tuple[dict[str, int], dict[str, int]]:
    latest_baseline_seen = max(
        [baseline_telemetry["tracking_started_at"]]
        + list(baseline_telemetry["last_seen_at"]["operations"].values())
        + list(baseline_telemetry["last_seen_at"]["features"].values())
    )
    if baseline_captured < latest_baseline_seen:
        raise ValueError("baseline captured_at predates compatibility telemetry")
    if current_captured <= baseline_captured:
        raise ValueError("increment captured_at must be later than the baseline")
    latest_current_seen = max(
        [current_telemetry["tracking_started_at"]]
        + list(current_telemetry["last_seen_at"]["operations"].values())
        + list(current_telemetry["last_seen_at"]["features"].values())
    )
    if current_captured < latest_current_seen:
        raise ValueError("increment captured_at predates current telemetry")
    if (
        current_telemetry["tracking_started_at"]
        != baseline_telemetry["tracking_started_at"]
    ):
        raise ValueError("compatibility telemetry tracking identity changed")
    operation_deltas = _metric_deltas(
        baseline_telemetry["operation_counts"],
        current_telemetry["operation_counts"],
    )
    feature_deltas = _metric_deltas(
        baseline_telemetry["feature_counts"],
        current_telemetry["feature_counts"],
    )
    changed_without_count = {
        "operations": _changed_timestamps_without_count(
            baseline_telemetry["last_seen_at"]["operations"],
            current_telemetry["last_seen_at"]["operations"],
            operation_deltas,
        ),
        "features": _changed_timestamps_without_count(
            baseline_telemetry["last_seen_at"]["features"],
            current_telemetry["last_seen_at"]["features"],
            feature_deltas,
        ),
    }
    changed_metrics = [
        f"{kind}.{metric}"
        for kind, metrics in changed_without_count.items()
        for metric in metrics
    ]
    if changed_metrics:
        raise ValueError(
            "compatibility telemetry last_seen_at changed without a count: "
            + ", ".join(changed_metrics)
        )
    stale_count_timestamps = {
        "operations": _counts_without_newer_timestamp(
            baseline_telemetry["last_seen_at"]["operations"],
            current_telemetry["last_seen_at"]["operations"],
            operation_deltas,
        ),
        "features": _counts_without_newer_timestamp(
            baseline_telemetry["last_seen_at"]["features"],
            current_telemetry["last_seen_at"]["features"],
            feature_deltas,
        ),
    }
    stale_metrics = [
        f"{kind}.{metric}"
        for kind, metrics in stale_count_timestamps.items()
        for metric in metrics
    ]
    if stale_metrics:
        raise ValueError(
            "compatibility telemetry count increased without newer last_seen_at: "
            + ", ".join(stale_metrics)
        )
    return operation_deltas, feature_deltas


def create_increment(
    baseline: Mapping[str, object],
    runtime_status: Mapping[str, object],
    *,
    source_commit: str,
    captured_at: float,
) -> dict[str, object]:
    _verify_evidence(
        baseline,
        schema=BASELINE_SCHEMA,
        label="compatibility baseline",
    )
    current_commit = _full_commit(source_commit)
    baseline_commit = _full_commit(
        str(baseline.get("source_commit", "")),
        name="baseline source_commit",
    )
    baseline_captured = _finite_timestamp(
        baseline.get("captured_at"),
        name="baseline captured_at",
    )
    current_captured = _finite_timestamp(
        captured_at,
        name="increment captured_at",
    )
    baseline_telemetry = _normalize_telemetry(baseline["telemetry"])
    current_telemetry = _telemetry_from_status(runtime_status)
    operation_deltas, feature_deltas = _increment_deltas(
        baseline_telemetry,
        current_telemetry,
        baseline_captured=baseline_captured,
        current_captured=current_captured,
    )
    report: dict[str, object] = {
        "schema": INCREMENT_SCHEMA,
        "baseline_digest": baseline["evidence_digest"],
        "baseline_source_commit": baseline_commit,
        "baseline_captured_at": baseline_captured,
        "baseline_telemetry": baseline_telemetry,
        "source_commit": current_commit,
        "captured_at": current_captured,
        "operation_deltas": operation_deltas,
        "feature_deltas": feature_deltas,
        "telemetry": current_telemetry,
    }
    report["evidence_digest"] = _fingerprint(report)
    return report


def _verify_release_gate(
    report: Mapping[str, object], *, source_commit: str
) -> None:
    verify_release_gate_report(
        report,
        expected_source_commit=source_commit,
        require_promotable=True,
        required_artifacts=RETIREMENT_RELEASE_ARTIFACTS,
    )


def _writer_decision(
    metrics: tuple[tuple[str, str], ...],
    *,
    increment: Mapping[str, object],
) -> dict[str, object]:
    blockers: list[str] = []
    metric_values: list[dict[str, object]] = []
    for kind, metric in metrics:
        deltas = increment[f"{kind}_deltas"]
        delta = int(deltas.get(metric, 0))
        metric_values.append({"kind": kind, "name": metric, "delta": delta})
        if delta > 0:
            blockers.append(f"{kind} count increased by {delta}")
    return {
        "ready": not blockers,
        "stage": "remove_compatibility_writer",
        "metrics": metric_values,
        "blockers": blockers,
    }


def evaluate_retirement(
    increment: Mapping[str, object],
    release_gate: Mapping[str, object],
) -> dict[str, object]:
    _verify_evidence(
        increment,
        schema=INCREMENT_SCHEMA,
        label="compatibility increment",
    )
    baseline_captured = _finite_timestamp(
        increment.get("baseline_captured_at"),
        name="increment baseline_captured_at",
    )
    baseline_source_commit = _full_commit(
        str(increment.get("baseline_source_commit", "")),
        name="increment baseline_source_commit",
    )
    baseline_telemetry_value = increment.get("baseline_telemetry")
    if not isinstance(baseline_telemetry_value, Mapping):
        raise ValueError("compatibility increment baseline_telemetry must be an object")
    baseline_telemetry = _normalize_telemetry(baseline_telemetry_value)
    reconstructed_baseline = {
        "schema": BASELINE_SCHEMA,
        "source_commit": baseline_source_commit,
        "captured_at": baseline_captured,
        "telemetry": baseline_telemetry,
    }
    if increment.get("baseline_digest") != _fingerprint(reconstructed_baseline):
        raise ValueError("compatibility increment baseline evidence is invalid")
    current_captured = _finite_timestamp(
        increment.get("captured_at"),
        name="increment captured_at",
    )
    telemetry = increment.get("telemetry")
    if not isinstance(telemetry, Mapping):
        raise ValueError("compatibility increment telemetry must be an object")
    normalized_telemetry = _normalize_telemetry(telemetry)
    operation_deltas = _nonnegative_deltas(
        increment.get("operation_deltas"),
        name="operation_deltas",
    )
    feature_deltas = _nonnegative_deltas(
        increment.get("feature_deltas"),
        name="feature_deltas",
    )
    if set(operation_deltas) != set(normalized_telemetry["operation_counts"]):
        raise ValueError(
            "compatibility increment operation_deltas must match telemetry counts"
        )
    if set(feature_deltas) != set(normalized_telemetry["feature_counts"]):
        raise ValueError(
            "compatibility increment feature_deltas must match telemetry counts"
        )
    expected_operation_deltas, expected_feature_deltas = _increment_deltas(
        baseline_telemetry,
        normalized_telemetry,
        baseline_captured=baseline_captured,
        current_captured=current_captured,
    )
    if operation_deltas != expected_operation_deltas:
        raise ValueError(
            "compatibility increment operation_deltas do not match baseline telemetry"
        )
    if feature_deltas != expected_feature_deltas:
        raise ValueError(
            "compatibility increment feature_deltas do not match baseline telemetry"
        )
    validated_increment = dict(increment)
    validated_increment["operation_deltas"] = operation_deltas
    validated_increment["feature_deltas"] = feature_deltas
    source_commit = _full_commit(str(increment.get("source_commit", "")))
    _verify_release_gate(release_gate, source_commit=source_commit)
    writers = {
        name: _writer_decision(metrics, increment=validated_increment)
        for name, metrics in WRITER_METRICS.items()
    }
    profile_blockers: list[str] = []
    operation_increase = sum(
        int(value) for value in operation_deltas.values()
    )
    feature_increase = sum(
        int(value) for value in feature_deltas.values()
    )
    if operation_increase:
        profile_blockers.append(
            f"compatibility operation count increased by {operation_increase}"
        )
    if feature_increase:
        profile_blockers.append(
            f"compatibility feature count increased by {feature_increase}"
        )
    report: dict[str, object] = {
        "schema": DECISION_SCHEMA,
        "source_commit": source_commit,
        "increment_digest": increment["evidence_digest"],
        "release_gate_digest": release_gate["evidence_digest"],
        "writers": writers,
        "compatibility_profile": {
            "ready": not profile_blockers,
            "stage": "remove_compatibility_profile",
            "blockers": profile_blockers,
        },
        "preserved_readers": ["old_event_upcasters"],
    }
    report["evidence_digest"] = _fingerprint(report)
    return report


def _read_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _resolve_source_commit(
    repository: Path,
    *,
    source_commit: str | None,
    source_ref: str,
) -> str:
    if source_commit:
        return _full_commit(source_commit)
    completed = subprocess.run(
        ["git", "rev-parse", f"{source_ref}^{{commit}}"],
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or "cannot resolve source ref")
    return _full_commit(completed.stdout.strip())


def _write_report(report: Mapping[str, object], output: Path | None) -> None:
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=ROOT)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--source-commit")
    source.add_argument("--source-ref", default="HEAD")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    baseline_parser = commands.add_parser(
        "baseline", description="Capture a digest-bound compatibility telemetry baseline."
    )
    baseline_parser.add_argument("--runtime-status", type=Path, required=True)
    baseline_parser.add_argument("--captured-at", type=float)
    baseline_parser.add_argument("--output", type=Path)
    _add_source_options(baseline_parser)

    increment_parser = commands.add_parser(
        "increment", description="Compare Runtime status with a baseline."
    )
    increment_parser.add_argument("--baseline", type=Path, required=True)
    increment_parser.add_argument("--runtime-status", type=Path, required=True)
    increment_parser.add_argument("--captured-at", type=float)
    increment_parser.add_argument("--output", type=Path)
    _add_source_options(increment_parser)

    evaluate_parser = commands.add_parser(
        "evaluate", description="Bind zero-use evidence to a complete Release Gate."
    )
    evaluate_parser.add_argument("--increment", type=Path, required=True)
    evaluate_parser.add_argument("--release-gate", type=Path, required=True)
    evaluate_parser.add_argument("--output", type=Path)
    evaluate_parser.add_argument(
        "--writer",
        choices=sorted(WRITER_METRICS),
        help="Return success only when this writer is ready; defaults to the profile.",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "baseline":
            repository = args.repo.expanduser().absolute()
            commit = _resolve_source_commit(
                repository,
                source_commit=args.source_commit,
                source_ref=args.source_ref,
            )
            report = create_baseline(
                _read_json(args.runtime_status, label="Runtime status"),
                source_commit=commit,
                captured_at=args.captured_at or time.time(),
            )
        elif args.command == "increment":
            repository = args.repo.expanduser().absolute()
            baseline = _read_json(args.baseline, label="compatibility baseline")
            commit = _resolve_source_commit(
                repository,
                source_commit=args.source_commit,
                source_ref=args.source_ref,
            )
            report = create_increment(
                baseline,
                _read_json(args.runtime_status, label="Runtime status"),
                source_commit=commit,
                captured_at=args.captured_at or time.time(),
            )
        else:
            report = evaluate_retirement(
                _read_json(args.increment, label="compatibility increment"),
                _read_json(args.release_gate, label="Release Gate"),
            )
        _write_report(report, args.output)
        if args.command == "evaluate":
            selected = (
                report["writers"][args.writer]
                if args.writer
                else report["compatibility_profile"]
            )
            return 0 if selected["ready"] else 1
        return 0
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
