"""Schema and fail-closed validation for Runtime stability evidence."""

from __future__ import annotations

from collections.abc import Mapping

from scripts.evidence_report import evidence_fingerprint


SCHEMA = "openubmc-agent-workflow.runtime-stability.v1"
STORM_WORKERS = 16
GATE_WORKERS = 8
SOAK_RESTART_CYCLES = 4
SOAK_RUNS_PER_CYCLE = 16
MAX_SOAK_SECONDS = 30.0
MAX_SOAK_PEAK_BYTES = 128 * 1024 * 1024
MAX_SOAK_STORAGE_BYTES = 32 * 1024 * 1024
MAX_EVENTS_PER_RUN = 16


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Runtime stability {name} is invalid")
    return value


def _number(value: object, name: str, *, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < minimum
    ):
        raise ValueError(f"Runtime stability {name} is invalid")
    return float(value)


def _scenario(report: Mapping[str, object], name: str) -> Mapping[str, object]:
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, Mapping):
        raise ValueError("Runtime stability scenarios are unavailable")
    value = scenarios.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"Runtime stability {name} evidence is unavailable")
    return value


def verify_runtime_stability_report(
    report: Mapping[str, object],
    *,
    expected_source_commit: str,
    require_promotable: bool = False,
) -> None:
    if report.get("schema") != SCHEMA:
        raise ValueError("Runtime stability schema is unsupported")
    unsigned = dict(report)
    expected_digest = unsigned.pop("evidence_digest", None)
    if expected_digest != evidence_fingerprint(unsigned):
        raise ValueError("Runtime stability evidence digest is invalid")
    if str(report.get("source_commit", "")).lower() != expected_source_commit.lower():
        raise ValueError("Runtime stability source commit does not match")
    environment = report.get("environment")
    if not isinstance(environment, Mapping) or any(
        not str(environment.get(name, "")).strip()
        for name in ("python", "python_implementation", "platform")
    ):
        raise ValueError("Runtime stability environment is incomplete")
    parameters = report.get("parameters")
    expected_parameters = {
        "storm_workers": STORM_WORKERS,
        "gate_workers": GATE_WORKERS,
        "soak_restart_cycles": SOAK_RESTART_CYCLES,
        "soak_runs_per_cycle": SOAK_RUNS_PER_CYCLE,
        "max_soak_seconds": MAX_SOAK_SECONDS,
        "max_soak_peak_bytes": MAX_SOAK_PEAK_BYTES,
        "max_soak_storage_bytes": MAX_SOAK_STORAGE_BYTES,
        "max_events_per_run": MAX_EVENTS_PER_RUN,
    }
    if parameters != expected_parameters:
        raise ValueError("Runtime stability parameters do not match the CI profile")

    storm = _scenario(report, "duplicate_storm")
    if not all(
        (
            _integer(storm.get("execute_calls"), "storm execute calls")
            == STORM_WORKERS + 2,
            _integer(storm.get("failed_calls"), "storm failed calls") == 0,
            _integer(storm.get("unique_runs"), "storm unique runs") == 1,
            _integer(storm.get("operation_count"), "storm operation count") == 1,
            _integer(storm.get("command_decisions"), "storm command decisions")
            == 1,
            _integer(storm.get("outcome_events"), "storm outcome events") == 1,
            _integer(storm.get("open_incidents"), "storm open incidents") == 0,
            storm.get("same_key_different_hash_rejected") is True,
        )
    ):
        raise ValueError("Runtime stability duplicate storm did not converge")

    gate = _scenario(report, "gate_concurrency")
    if not all(
        (
            _integer(gate.get("execute_calls"), "gate execute calls")
            == GATE_WORKERS,
            _integer(gate.get("failed_calls"), "gate failed calls") == 0,
            _integer(gate.get("unique_runs"), "gate unique runs") == 1,
            _integer(gate.get("gate_submissions"), "gate submissions") == 1,
            _integer(gate.get("outcome_events"), "gate outcome events") == 1,
            _integer(gate.get("open_incidents"), "gate open incidents") == 0,
        )
    ):
        raise ValueError("Runtime stability Gate concurrency did not converge")

    soak = _scenario(report, "restart_soak")
    expected_runs = SOAK_RESTART_CYCLES * SOAK_RUNS_PER_CYCLE
    expected_calls = expected_runs * 2
    total_events = _integer(soak.get("total_events"), "soak total events", minimum=1)
    events_per_cycle = soak.get("events_per_cycle")
    cumulative_events = soak.get("cumulative_events_by_cycle")
    storage_by_cycle = soak.get("storage_bytes_by_cycle")
    if (
        not isinstance(events_per_cycle, list)
        or not isinstance(cumulative_events, list)
        or not isinstance(storage_by_cycle, list)
        or len(events_per_cycle) != SOAK_RESTART_CYCLES
        or len(cumulative_events) != SOAK_RESTART_CYCLES
        or len(storage_by_cycle) != SOAK_RESTART_CYCLES
    ):
        raise ValueError("Runtime stability cycle growth evidence is incomplete")
    bounded_cycle_events = all(
        _integer(value, "soak cycle events", minimum=1)
        <= SOAK_RUNS_PER_CYCLE * MAX_EVENTS_PER_RUN
        for value in events_per_cycle
    )
    normalized_cumulative = [
        _integer(value, "soak cumulative events", minimum=1)
        for value in cumulative_events
    ]
    monotonic_events = normalized_cumulative == sorted(normalized_cumulative)
    bounded_storage = all(
        _integer(value, "soak cycle storage", minimum=1)
        <= MAX_SOAK_STORAGE_BYTES
        for value in storage_by_cycle
    )
    if not all(
        (
            _integer(soak.get("execute_calls"), "soak execute calls")
            == expected_calls,
            _integer(soak.get("failed_calls"), "soak failed calls") == 0,
            _integer(soak.get("completed_turns"), "soak completed turns")
            == expected_calls,
            _integer(soak.get("completed_runs"), "soak completed runs")
            == expected_runs,
            _integer(soak.get("replay_mismatches"), "soak replay mismatches")
            == 0,
            _integer(soak.get("invalid_runs"), "soak invalid runs") == 0,
            _integer(soak.get("outcome_events"), "soak outcome events")
            == expected_runs,
            _integer(soak.get("open_incidents"), "soak open incidents") == 0,
            _integer(soak.get("max_events_per_run"), "soak max events per run")
            <= MAX_EVENTS_PER_RUN,
            total_events <= expected_runs * MAX_EVENTS_PER_RUN,
            sum(events_per_cycle) == total_events,
            normalized_cumulative[-1] == total_events,
            bounded_cycle_events,
            monotonic_events,
            bounded_storage,
            _integer(soak.get("storage_bytes"), "soak storage bytes", minimum=1)
            <= MAX_SOAK_STORAGE_BYTES,
            _integer(
                soak.get("peak_traced_memory_bytes"),
                "soak peak memory",
                minimum=1,
            )
            <= MAX_SOAK_PEAK_BYTES,
            _number(soak.get("elapsed_seconds"), "soak elapsed seconds")
            <= MAX_SOAK_SECONDS,
        )
    ):
        raise ValueError("Runtime stability restart soak exceeded a hard threshold")

    scenario_statuses = {
        str(_scenario(report, name).get("status", ""))
        for name in ("duplicate_storm", "gate_concurrency", "restart_soak")
    }
    calculated_promotable = scenario_statuses == {"passed"}
    if report.get("promotable") is not calculated_promotable:
        raise ValueError("Runtime stability promotion status is inconsistent")
    if require_promotable and not calculated_promotable:
        raise ValueError("Runtime stability evidence is not promotable")
