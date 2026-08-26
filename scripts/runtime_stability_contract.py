"""Schema and fail-closed validation for Runtime stability evidence."""

from __future__ import annotations

from collections.abc import Mapping

from scripts.evidence_report import evidence_fingerprint


SCHEMA = "openubmc-agent-workflow.runtime-stability.v1"
STORM_WORKERS = 16
GATE_WORKERS = 8
CAPACITY_RUNS = 128
CAPACITY_BATCH_SIZE = 32
ARTIFACT_CAPACITY_RECORDS = 64
ARTIFACT_CAPACITY_BATCH_SIZE = 16
SOAK_RESTART_CYCLES = 4
SOAK_RUNS_PER_CYCLE = 16
MAX_CAPACITY_SECONDS = 30.0
MAX_CAPACITY_PEAK_RSS_BYTES = 512 * 1024 * 1024
MAX_CAPACITY_PEAK_PYTHON_BYTES = 128 * 1024 * 1024
MAX_CAPACITY_STORAGE_BYTES = 64 * 1024 * 1024
MAX_ARTIFACT_CAPACITY_SECONDS = 15.0
MAX_ARTIFACT_STORAGE_BYTES = 16 * 1024 * 1024
MAX_SOAK_SECONDS = 30.0
MAX_SOAK_PEAK_RSS_BYTES = 512 * 1024 * 1024
MAX_SOAK_PEAK_BYTES = 128 * 1024 * 1024
MAX_SOAK_STORAGE_BYTES = 32 * 1024 * 1024
MAX_EVENTS_PER_RUN = 16


def ci_parameters() -> dict[str, int | float]:
    return {
        "storm_workers": STORM_WORKERS,
        "gate_workers": GATE_WORKERS,
        "capacity_runs": CAPACITY_RUNS,
        "capacity_batch_size": CAPACITY_BATCH_SIZE,
        "artifact_capacity_records": ARTIFACT_CAPACITY_RECORDS,
        "artifact_capacity_batch_size": ARTIFACT_CAPACITY_BATCH_SIZE,
        "soak_restart_cycles": SOAK_RESTART_CYCLES,
        "soak_runs_per_cycle": SOAK_RUNS_PER_CYCLE,
        "max_capacity_seconds": MAX_CAPACITY_SECONDS,
        "max_capacity_peak_rss_bytes": MAX_CAPACITY_PEAK_RSS_BYTES,
        "max_capacity_peak_python_bytes": MAX_CAPACITY_PEAK_PYTHON_BYTES,
        "max_capacity_storage_bytes": MAX_CAPACITY_STORAGE_BYTES,
        "max_artifact_capacity_seconds": MAX_ARTIFACT_CAPACITY_SECONDS,
        "max_artifact_storage_bytes": MAX_ARTIFACT_STORAGE_BYTES,
        "max_soak_seconds": MAX_SOAK_SECONDS,
        "max_soak_peak_rss_bytes": MAX_SOAK_PEAK_RSS_BYTES,
        "max_soak_peak_bytes": MAX_SOAK_PEAK_BYTES,
        "max_soak_storage_bytes": MAX_SOAK_STORAGE_BYTES,
        "max_events_per_run": MAX_EVENTS_PER_RUN,
    }


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
        not isinstance(environment.get(name), str)
        or not environment[name].strip()
        for name in ("python", "python_implementation", "platform")
    ):
        raise ValueError("Runtime stability environment is incomplete")
    if report.get("environment_fingerprint") != evidence_fingerprint(environment):
        raise ValueError("Runtime stability environment fingerprint is invalid")
    parameters = report.get("parameters")
    expected_parameters = ci_parameters()
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
            == GATE_WORKERS + 1,
            _integer(gate.get("failed_calls"), "gate failed calls") == 0,
            _integer(gate.get("unique_runs"), "gate unique runs") == 1,
            _integer(gate.get("gate_submissions"), "gate submissions") == 1,
            _integer(gate.get("outcome_events"), "gate outcome events") == 1,
            _integer(gate.get("open_incidents"), "gate open incidents") == 0,
            _integer(gate.get("unique_turns"), "gate unique Turns") == 1,
            gate.get("turn_states") == {"completed": GATE_WORKERS},
            gate.get("canonical_turn_state") == "completed",
            gate.get("canonical_turn_matches") is True,
            _integer(
                gate.get("canonical_reattach_backend_calls"),
                "gate canonical reattach backend calls",
            )
            == 0,
        )
    ):
        raise ValueError("Runtime stability Gate concurrency did not converge")

    capacity = _scenario(report, "capacity")
    capacity_batches = CAPACITY_RUNS // CAPACITY_BATCH_SIZE
    events_per_batch = capacity.get("events_per_batch")
    cumulative_capacity_events = capacity.get("cumulative_events_by_batch")
    capacity_storage_by_batch = capacity.get("storage_bytes_by_batch")
    capacity_storage_growth = capacity.get("storage_growth_bytes_by_batch")
    if (
        not isinstance(events_per_batch, list)
        or not isinstance(cumulative_capacity_events, list)
        or not isinstance(capacity_storage_by_batch, list)
        or not isinstance(capacity_storage_growth, list)
        or len(events_per_batch) != capacity_batches
        or len(cumulative_capacity_events) != capacity_batches
        or len(capacity_storage_by_batch) != capacity_batches
        or len(capacity_storage_growth) != capacity_batches
    ):
        raise ValueError("Runtime stability capacity growth evidence is incomplete")
    capacity_total_events = _integer(
        capacity.get("total_events"),
        "capacity total events",
    )
    normalized_capacity_cumulative = [
        _integer(value, "capacity cumulative events", minimum=1)
        for value in cumulative_capacity_events
    ]
    normalized_batch_events = [
        _integer(value, "capacity batch events", minimum=1)
        for value in events_per_batch
    ]
    expected_capacity_cumulative: list[int] = []
    running_capacity_events = 0
    for value in normalized_batch_events:
        running_capacity_events += value
        expected_capacity_cumulative.append(running_capacity_events)
    normalized_capacity_storage = [
        _integer(value, "capacity batch storage", minimum=1)
        for value in capacity_storage_by_batch
    ]
    normalized_storage_growth = [
        _integer(
            value,
            "capacity batch storage growth",
            minimum=-MAX_CAPACITY_STORAGE_BYTES,
        )
        for value in capacity_storage_growth
    ]
    capacity_storage_bytes = _integer(
        capacity.get("storage_bytes"),
        "capacity storage bytes",
        minimum=1,
    )
    max_storage_growth_per_batch = MAX_CAPACITY_STORAGE_BYTES // capacity_batches
    if not all(
        (
            _integer(capacity.get("execute_calls"), "capacity execute calls")
            == CAPACITY_RUNS,
            _integer(capacity.get("failed_calls"), "capacity failed calls") == 0,
            _integer(capacity.get("completed_turns"), "capacity completed turns")
            == CAPACITY_RUNS,
            _integer(capacity.get("completed_runs"), "capacity completed runs")
            == CAPACITY_RUNS,
            _integer(capacity.get("invalid_runs"), "capacity invalid runs") == 0,
            _integer(capacity.get("outcome_events"), "capacity outcome events")
            == CAPACITY_RUNS,
            _integer(capacity.get("open_incidents"), "capacity open incidents")
            == 0,
            _integer(
                capacity.get("incomplete_operations"),
                "capacity incomplete operations",
            )
            == 0,
            _integer(
                capacity.get("max_events_per_run"),
                "capacity max events per run",
            )
            <= MAX_EVENTS_PER_RUN,
            capacity_total_events <= CAPACITY_RUNS * MAX_EVENTS_PER_RUN,
            sum(normalized_batch_events) == capacity_total_events,
            all(
                value <= CAPACITY_BATCH_SIZE * MAX_EVENTS_PER_RUN
                for value in normalized_batch_events
            ),
            normalized_capacity_cumulative == expected_capacity_cumulative,
            all(
                value <= MAX_CAPACITY_STORAGE_BYTES
                for value in normalized_capacity_storage
            ),
            normalized_capacity_storage[-1] == capacity_storage_bytes,
            normalized_storage_growth
            == [
                current - previous
                for previous, current in zip(
                    [0, *normalized_capacity_storage[:-1]],
                    normalized_capacity_storage,
                )
            ],
            max(normalized_storage_growth) <= max_storage_growth_per_batch,
            capacity_storage_bytes <= MAX_CAPACITY_STORAGE_BYTES,
            _integer(
                capacity.get("peak_rss_bytes"),
                "capacity peak RSS",
                minimum=1,
            )
            <= MAX_CAPACITY_PEAK_RSS_BYTES,
            _integer(
                capacity.get("peak_python_allocation_bytes"),
                "capacity peak Python allocation",
                minimum=1,
            )
            <= MAX_CAPACITY_PEAK_PYTHON_BYTES,
            _number(capacity.get("elapsed_seconds"), "capacity elapsed seconds")
            <= MAX_CAPACITY_SECONDS,
        )
    ):
        raise ValueError("Runtime stability capacity exceeded a hard threshold")

    artifact = _scenario(report, "artifact_lifecycle")
    artifact_batches = ARTIFACT_CAPACITY_RECORDS // ARTIFACT_CAPACITY_BATCH_SIZE
    artifact_records_by_batch = artifact.get("records_by_batch")
    artifact_storage_by_batch = artifact.get("storage_bytes_by_batch")
    if (
        not isinstance(artifact_records_by_batch, list)
        or not isinstance(artifact_storage_by_batch, list)
        or len(artifact_records_by_batch) != artifact_batches
        or len(artifact_storage_by_batch) != artifact_batches
    ):
        raise ValueError("Runtime stability Artifact lifecycle growth evidence is incomplete")
    normalized_artifact_records = [
        _integer(value, "Artifact lifecycle records", minimum=1)
        for value in artifact_records_by_batch
    ]
    normalized_artifact_storage = [
        _integer(value, "Artifact lifecycle storage", minimum=1)
        for value in artifact_storage_by_batch
    ]
    if not all(
        (
            _integer(artifact.get("created_raw_records"), "Artifact raw records")
            == ARTIFACT_CAPACITY_RECORDS,
            _integer(
                artifact.get("created_redacted_records"),
                "Artifact redacted records",
            )
            == 1,
            _integer(
                artifact.get("created_ephemeral_records"),
                "Artifact ephemeral records",
            )
            == 1,
            _integer(artifact.get("shared_raw_digests"), "Artifact shared digests")
            == 1,
            artifact.get("redacted_digest_distinct") is True,
            normalized_artifact_records
            == list(
                range(
                    ARTIFACT_CAPACITY_BATCH_SIZE,
                    ARTIFACT_CAPACITY_RECORDS + 1,
                    ARTIFACT_CAPACITY_BATCH_SIZE,
                )
            ),
            all(
                value <= MAX_ARTIFACT_STORAGE_BYTES
                for value in normalized_artifact_storage
            ),
            _integer(
                artifact.get("restart_record_count"),
                "Artifact restart records",
            )
            == ARTIFACT_CAPACITY_RECORDS + 2,
            _integer(
                artifact.get("restart_resolutions"),
                "Artifact restart resolutions",
            )
            == 2,
            _integer(
                artifact.get("first_gc_deleted_records"),
                "Artifact first GC records",
            )
            == ARTIFACT_CAPACITY_RECORDS // 2 + 1,
            _integer(
                artifact.get("first_gc_deleted_content"),
                "Artifact first GC content",
            )
            == 1,
            artifact.get("shared_content_preserved_after_partial_gc") is True,
            _integer(
                artifact.get("released_run_records"),
                "Artifact released Run records",
            )
            == ARTIFACT_CAPACITY_RECORDS // 2,
            _integer(
                artifact.get("second_gc_deleted_records"),
                "Artifact second GC records",
            )
            == ARTIFACT_CAPACITY_RECORDS // 2,
            _integer(
                artifact.get("second_gc_deleted_content"),
                "Artifact second GC content",
            )
            == 1,
            artifact.get("shared_content_deleted_after_final_reference") is True,
            artifact.get("expired_resolution_rejected") is True,
            artifact.get("released_resolution_rejected") is True,
            _integer(
                artifact.get("final_record_count"),
                "Artifact final records",
            )
            == 1,
            _integer(
                artifact.get("final_managed_record_count"),
                "Artifact final managed records",
            )
            == 1,
            _integer(
                artifact.get("final_redacted_record_count"),
                "Artifact final redacted records",
            )
            == 1,
            _integer(
                artifact.get("final_audit_record_count"),
                "Artifact final audit records",
            )
            == 1,
            _integer(
                artifact.get("final_content_files"),
                "Artifact final content files",
            )
            == 1,
            _integer(
                artifact.get("storage_bytes"),
                "Artifact storage bytes",
                minimum=1,
            )
            <= MAX_ARTIFACT_STORAGE_BYTES,
            _number(
                artifact.get("elapsed_seconds"),
                "Artifact lifecycle elapsed seconds",
            )
            <= MAX_ARTIFACT_CAPACITY_SECONDS,
        )
    ):
        raise ValueError("Runtime stability Artifact lifecycle contract failed")

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
    normalized_cycle_events = [
        _integer(value, "soak cycle events", minimum=1)
        for value in events_per_cycle
    ]
    expected_soak_cumulative: list[int] = []
    running_soak_events = 0
    for value in normalized_cycle_events:
        running_soak_events += value
        expected_soak_cumulative.append(running_soak_events)
    normalized_soak_storage = [
        _integer(value, "soak cycle storage", minimum=1)
        for value in storage_by_cycle
    ]
    soak_storage_bytes = _integer(
        soak.get("storage_bytes"),
        "soak storage bytes",
        minimum=1,
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
            _integer(
                soak.get("replay_backend_read_calls"),
                "soak replay backend calls",
            )
            == 0,
            _integer(soak.get("invalid_runs"), "soak invalid runs") == 0,
            _integer(soak.get("outcome_events"), "soak outcome events")
            == expected_runs,
            _integer(soak.get("open_incidents"), "soak open incidents") == 0,
            _integer(
                soak.get("incomplete_operations"),
                "soak incomplete operations",
            )
            == 0,
            _integer(soak.get("max_events_per_run"), "soak max events per run")
            <= MAX_EVENTS_PER_RUN,
            total_events <= expected_runs * MAX_EVENTS_PER_RUN,
            sum(normalized_cycle_events) == total_events,
            normalized_cumulative == expected_soak_cumulative,
            bounded_cycle_events,
            normalized_soak_storage[-1] == soak_storage_bytes,
            all(
                value <= MAX_SOAK_STORAGE_BYTES
                for value in normalized_soak_storage
            ),
            soak_storage_bytes <= MAX_SOAK_STORAGE_BYTES,
            _integer(
                soak.get("peak_rss_bytes"),
                "soak peak RSS",
                minimum=1,
            )
            <= MAX_SOAK_PEAK_RSS_BYTES,
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
        for name in (
            "duplicate_storm",
            "gate_concurrency",
            "capacity",
            "artifact_lifecycle",
            "restart_soak",
        )
    }
    calculated_promotable = scenario_statuses == {"passed"}
    if report.get("promotable") is not calculated_promotable:
        raise ValueError("Runtime stability promotion status is inconsistent")
    if require_promotable and not calculated_promotable:
        raise ValueError("Runtime stability evidence is not promotable")
