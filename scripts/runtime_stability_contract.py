"""Schema and fail-closed validation for Runtime stability evidence."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.diagnostic_receipt import (
    DiagnosticItemStatus,
    DiagnosticStatus,
)  # noqa: E402
from openubmc_target_runtime.semantic_runtime import RunTurn  # noqa: E402

from scripts.evidence_report import evidence_fingerprint  # noqa: E402


SCHEMA_V1 = "openubmc-agent-workflow.runtime-stability.v1"
SCHEMA = "openubmc-agent-workflow.runtime-stability.v2"
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
DUAL_PROJECTION_TEXT_TARGET_BYTES = 4 * 1024
DUAL_PROJECTION_MIN_PREVIEW_BYTES = 2 * 1024
DUAL_PROJECTION_STRUCTURED_TURN_DIGESTS = {
    "gate": "sha256:5fd2cf9de8fc41e75d1b36bc35a365ae99d1b4dc90ea86d7ee1bd8d0f2457871",
    "terminal": "sha256:340234c5816c2e9a738211a0929a302883a9c1ccaef342d8178e052681e13c39",
}
DUAL_PROJECTION_SEMANTIC_TEXT_MARKERS = (
    "openUBMC 工作流",
    "GateBinding ",
    "Outcome ",
    "DiagnosticReceipt ",
    "DiagnosticReceiptRef ",
    "capabilities_shown=",
    "next_action: ",
    "evidence_ids_shown=",
    "evidence_ids: ",
    "results_shown=",
    "result_ids: ",
)
DUAL_PROJECTION_REQUIRED_TEXT_LINE_DIGESTS = {
    "gate": (
        "sha256:d960111f36c771d581999ed83227f973284229d9f43972082a16d01f94e0cd21",
        "sha256:cc51ad782922d90c50a53e40f5f65ff100ca3e5a1d1aef8c39ed03a347bfce76",
        "sha256:53e77c7e177b22753ee4574245edb0bfd59c537f955e96b27b3cb4cac0ecc71c",
        "sha256:5af18c5d13c6bea731ef8aed7df686d2a9eadd1e78f1814e779cce432cac1ba1",
        "sha256:95a9ddbf8f7d562c0e848d2a193278e9d4f07764d7da347a7b9e463f3889262e",
        "sha256:bd30b82d8d069cd0877ee94b283573806407cc65d9117c8cc01d49b34f63c28d",
        "sha256:46457a95085ffe22678335a62f9bc6b8ab1e70929ab9b778a70d6972d44ec42c",
        "sha256:d939816350e72da5ab898848848690ceaee627b384bba0777edfc5d75204cecf",
    ),
    "terminal": (
        "sha256:88e8d03f71fb04aa8dae6469701d467e219043f76756a48f404a8df2b9deb725",
        "sha256:8847a347f031f52955f041ec307b467177e624897d46382edbf613ec0d19411c",
        "sha256:72eb13d62fc10eeaf64346cbf53cc7b91dfdb62c9945ece88548da892d78983d",
        "sha256:95a9ddbf8f7d562c0e848d2a193278e9d4f07764d7da347a7b9e463f3889262e",
        "sha256:bd30b82d8d069cd0877ee94b283573806407cc65d9117c8cc01d49b34f63c28d",
        "sha256:46457a95085ffe22678335a62f9bc6b8ab1e70929ab9b778a70d6972d44ec42c",
        "sha256:ed5cab1036cdccc6d83a388f2b0aa59de6b36a7463001a6bae92b72a200f54bd",
    ),
}


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


def json_size_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def standard_text(result: Mapping[str, object]) -> str:
    content = result.get("content", [])
    return "".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ) if isinstance(content, list) else ""


def projection_measurement(result: Mapping[str, object]) -> dict[str, int]:
    structured = result.get("structuredContent", {})
    return {
        "standard_text_bytes": len(standard_text(result).encode("utf-8")),
        "structured_content_bytes": json_size_bytes(structured),
        "combined_mcp_result_bytes": json_size_bytes(result),
    }


def _sha256_digest(encoded: bytes) -> str:
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_digest(encoded)


def _schema_digest(schema: Mapping[str, object]) -> str:
    return _canonical_json_digest(schema)


def _structured_turn_digest(turn: Mapping[str, object]) -> str:
    return _canonical_json_digest(turn)


def _verify_runtime_stability_report(
    report: Mapping[str, object],
    *,
    expected_source_commit: str,
    require_promotable: bool = False,
    allow_legacy_v1: bool,
) -> None:
    schema = report.get("schema")
    if schema not in {SCHEMA_V1, SCHEMA}:
        raise ValueError("Runtime stability schema is unsupported")
    if schema == SCHEMA_V1 and not allow_legacy_v1:
        raise ValueError(
            "Runtime stability legacy v1 evidence requires explicit compatibility mode"
        )
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
            == STORM_WORKERS + (2 if schema == SCHEMA_V1 else 3),
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
            == GATE_WORKERS + (1 if schema == SCHEMA_V1 else 3),
            _integer(gate.get("failed_calls"), "gate failed calls") == 0,
            _integer(gate.get("unique_runs"), "gate unique runs") == 1,
            _integer(gate.get("gate_submissions"), "gate submissions")
            == (1 if schema == SCHEMA_V1 else 2),
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
            == CAPACITY_RUNS * (1 if schema == SCHEMA_V1 else 2),
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

    if schema == SCHEMA:
        projection = _scenario(report, "dual_projection")
        correctness = projection.get("correctness")
        efficiency = projection.get("efficiency")
        measurements = projection.get("measurements")
        representative = projection.get("representative_receipt")
        canonical_results = projection.get("canonical_results")
        if (
            not isinstance(correctness, Mapping)
            or not isinstance(efficiency, Mapping)
            or not isinstance(measurements, Mapping)
            or not isinstance(representative, Mapping)
            or not isinstance(canonical_results, Mapping)
        ):
            raise ValueError(
                "Runtime stability dual-projection evidence is incomplete"
            )
        target_bytes = _integer(
            efficiency.get("standard_text_target_bytes"),
            "dual-projection text target",
            minimum=1,
        )
        if target_bytes != DUAL_PROJECTION_TEXT_TARGET_BYTES:
            raise ValueError(
                "Runtime stability dual-projection text target is invalid"
            )
        turns: dict[str, Mapping[str, object]] = {}
        turn_text: dict[str, str] = {}
        expected_warnings: list[str] = []
        for turn_name in ("gate", "terminal"):
            raw_result = canonical_results.get(turn_name)
            reported_measurement = measurements.get(turn_name)
            if (
                not isinstance(raw_result, Mapping)
                or not isinstance(reported_measurement, Mapping)
            ):
                raise ValueError(
                    "Runtime stability dual-projection measurements are incomplete"
                )
            if (
                "isError" in raw_result
                and raw_result.get("isError") is not False
            ):
                raise ValueError(
                    "Runtime stability dual-projection MCP result is an error"
                )
            recomputed = projection_measurement(raw_result)
            if reported_measurement != recomputed:
                raise ValueError(
                    "Runtime stability dual-projection measurement does not "
                    "match canonical MCP output"
                )
            if any(value <= 0 for value in recomputed.values()):
                raise ValueError(
                    "Runtime stability dual-projection measurement is invalid"
                )
            if (
                recomputed["combined_mcp_result_bytes"]
                <= recomputed["standard_text_bytes"]
                + recomputed["structured_content_bytes"]
            ):
                raise ValueError(
                    "Runtime stability dual-projection combined measurement is invalid"
                )
            if recomputed["standard_text_bytes"] > target_bytes:
                expected_warnings.append(
                    f"{turn_name}_standard_text_target_exceeded"
                )
            structured = raw_result.get("structuredContent")
            if not isinstance(structured, Mapping):
                raise ValueError(
                    "Runtime stability dual-projection structured Turn is unavailable"
                )
            turns[turn_name] = structured
            turn_text[turn_name] = standard_text(raw_result)
            semantic_line_digests = tuple(
                _sha256_digest(line.encode("utf-8"))
                for line in turn_text[turn_name].splitlines()
                if any(
                    marker in line
                    for marker in DUAL_PROJECTION_SEMANTIC_TEXT_MARKERS
                )
            )
            if (
                semantic_line_digests
                != DUAL_PROJECTION_REQUIRED_TEXT_LINE_DIGESTS[turn_name]
            ):
                raise ValueError(
                    "Runtime stability dual-projection standard text semantic "
                    "lines are missing, conflicting, or out of order"
                )

        gate_turn = turns["gate"]
        terminal_turn = turns["terminal"]
        try:
            typed_gate_turn = RunTurn.from_public_dict(gate_turn)
            typed_terminal_turn = RunTurn.from_public_dict(terminal_turn)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Runtime stability dual-projection structured semantics are invalid"
            ) from exc
        gate_receipt = gate_turn.get("diagnostic_receipt")
        terminal_receipt_ref = terminal_turn.get("diagnostic_receipt_ref")
        if (
            gate_turn.get("schema")
            != "openubmc.target-runtime.v1/agent-gateway-v1/turn"
            or terminal_turn.get("schema")
            != "openubmc.target-runtime.v1/agent-gateway-v1/turn"
            or not isinstance(gate_receipt, Mapping)
            or not isinstance(terminal_receipt_ref, Mapping)
            or gate_receipt.get("schema")
            != "openubmc.target-runtime.v1/diagnostic-receipt-v1"
            or terminal_receipt_ref.get("schema")
            != (
                "openubmc.target-runtime.v1/agent-gateway-v1/"
                "diagnostic-receipt-ref-v1"
            )
        ):
            raise ValueError(
                "Runtime stability dual-projection structured semantics differ"
            )
        raw_results = gate_receipt.get("results", [])
        receipt_results = raw_results if isinstance(raw_results, list) else []
        result_kinds = sorted(
            str(item.get("kind", ""))
            for item in receipt_results
            if isinstance(item, Mapping) and item.get("kind")
        )
        sentinels = [
            str(value.get("qualification_sentinel", ""))
            for item in receipt_results
            if isinstance(item, Mapping)
            and isinstance((value := item.get("value")), Mapping)
            and value.get("qualification_sentinel")
        ]
        preview_payloads = [
            preview[len(marker) :]
            for item in receipt_results
            if isinstance(item, Mapping)
            and isinstance((value := item.get("value")), Mapping)
            and isinstance((preview := value.get("preview")), str)
            and (sentinel := str(value.get("qualification_sentinel", "")))
            and preview.startswith((marker := sentinel + "::"))
        ]
        result_values_complete = all(
            isinstance((value := item.get("value")), Mapping)
            and value.get("content_complete") is True
            and isinstance((preview := value.get("preview")), str)
            and preview.startswith(
                str(value.get("qualification_sentinel", "")) + "::"
            )
            for item in receipt_results
            if isinstance(item, Mapping)
        )
        preview_bytes = {
            str(item.get("result_id", "")): len(preview.encode("utf-8"))
            for item in receipt_results
            if isinstance(item, Mapping)
            and item.get("result_id")
            and isinstance((value := item.get("value")), Mapping)
            and isinstance((preview := value.get("preview")), str)
        }
        if (
            len(preview_bytes) != 6
            or any(
                value < DUAL_PROJECTION_MIN_PREVIEW_BYTES
                for value in preview_bytes.values()
            )
        ):
            raise ValueError(
                "Runtime stability dual-projection long diagnostic results are incomplete"
            )
        combined_text = "\n".join(turn_text.values())
        preview_duplicated = any(
            value and value in combined_text
            for value in (*sentinels, *preview_payloads)
        )
        evidence = gate_receipt.get("evidence", [])
        gate = gate_turn.get("gate")
        gate_schema = gate.get("input_schema") if isinstance(gate, Mapping) else None
        gate_semantics_complete = all(
            (
                typed_gate_turn.state == "waiting_response",
                typed_gate_turn.gate is not None,
                isinstance(gate_schema, Mapping),
                (
                    gate.get("schema_digest") == _schema_digest(gate_schema)
                    if isinstance(gate, Mapping)
                    and isinstance(gate_schema, Mapping)
                    else False
                ),
                gate_turn.get("response_required") is True,
                gate_turn.get("next_action") is None,
                bool(gate.get("submission_id")) if isinstance(gate, Mapping) else False,
                "GateBinding" in turn_text["gate"],
                "submission_id=" in turn_text["gate"],
            )
        )
        terminal_semantics_complete = all(
            (
                typed_terminal_turn.state == "completed",
                typed_terminal_turn.outcome is not None,
                (
                    typed_terminal_turn.outcome.status == "completed"
                    if typed_terminal_turn.outcome is not None
                    else False
                ),
                typed_terminal_turn.outcome_recorded,
                "Outcome status=completed" in turn_text["terminal"],
            )
        )
        typed_gate_receipt = typed_gate_turn.diagnostic_receipt
        typed_terminal_receipt = typed_terminal_turn.diagnostic_receipt
        durable_gate_receipt = dict(gate_receipt)
        durable_gate_receipt.pop("agent_acceptance", None)
        gate_receipt_digest = _canonical_json_digest(durable_gate_receipt)
        expected_evidence_ids = [
            str(item.get("evidence_id", ""))
            for item in gate_receipt.get("evidence", [])
            if isinstance(item, Mapping) and item.get("evidence_id")
        ]
        expected_result_ids = [
            str(item.get("result_id", ""))
            for item in receipt_results
            if isinstance(item, Mapping) and item.get("result_id")
        ]
        expected_reference = {
            "schema": (
                "openubmc.target-runtime.v1/agent-gateway-v1/"
                "diagnostic-receipt-ref-v1"
            ),
            "receipt_id": gate_receipt.get("receipt_id"),
            "operation": gate_receipt.get("operation"),
            "status": gate_receipt.get("status"),
            "agent_acceptance": "complete",
            "digest": gate_receipt_digest,
            "coverage": gate_receipt.get("coverage"),
            "freshness": gate_receipt.get("freshness"),
            "content_complete": True,
            "truncated": False,
            "gaps": [],
            "evidence_ids": expected_evidence_ids,
            "result_ids": expected_result_ids,
            "reconstruction": {
                "authority": "runtime-core",
                "run_id": "run-dual-projection-qualification",
                "field": "diagnostic_receipt",
                "digest": gate_receipt_digest,
            },
        }
        expected_repeated_projection = {
            "repeated_reference": True,
            "repeated_fields": ["diagnostic_receipt"],
            "full_bytes": json_size_bytes(gate_receipt),
            "reference_bytes": json_size_bytes(expected_reference),
            "saved_bytes": (
                json_size_bytes(gate_receipt)
                - json_size_bytes(expected_reference)
            ),
            "target_exceeded_causes": [
                {
                    "field": "diagnostic_receipt",
                    "bytes": json_size_bytes(gate_receipt),
                }
            ],
        }
        receipt_semantics_complete = all(
            (
                typed_gate_receipt is not None,
                typed_terminal_receipt is None,
                terminal_receipt_ref == expected_reference,
                (
                    typed_gate_receipt.status is DiagnosticStatus.COMPLETE
                    if typed_gate_receipt is not None
                    else False
                ),
                (
                    typed_gate_receipt.coverage.complete
                    and typed_gate_receipt.coverage.evaluable
                    == typed_gate_receipt.coverage.requested
                    and typed_gate_receipt.coverage.unavailable == 0
                    and typed_gate_receipt.coverage.not_checked == 0
                    and len(typed_gate_receipt.results)
                    == typed_gate_receipt.coverage.requested
                    and all(
                        item.status is DiagnosticItemStatus.AVAILABLE
                        for item in typed_gate_receipt.results
                    )
                    if typed_gate_receipt is not None
                    else False
                ),
            )
        )
        source_completeness_preserved = all(
            (
                gate_receipt.get("status") == "complete",
                gate_receipt.get("content_complete") is True,
                isinstance(gate_receipt.get("coverage"), Mapping),
                gate_receipt.get("coverage", {}).get("complete") is True,
                terminal_receipt_ref.get("status") == "complete",
                terminal_receipt_ref.get("content_complete") is True,
                isinstance(terminal_receipt_ref.get("coverage"), Mapping),
                terminal_receipt_ref.get("coverage", {}).get("complete") is True,
            )
        )
        agent_acceptance_preserved = all(
            (
                gate_receipt.get("agent_acceptance") == "complete",
                terminal_receipt_ref.get("agent_acceptance") == "complete",
                "agent_acceptance=complete" in turn_text["gate"],
                "agent_acceptance=complete" in turn_text["terminal"],
            )
        )
        expected_correctness = {
            "mcp_results_successful": True,
            "gate_semantics_complete": gate_semantics_complete,
            "terminal_semantics_complete": terminal_semantics_complete,
            "source_completeness_preserved": source_completeness_preserved,
            "agent_acceptance_preserved": agent_acceptance_preserved,
            "passed": all(
                (
                    gate_semantics_complete,
                    terminal_semantics_complete,
                    source_completeness_preserved,
                    agent_acceptance_preserved,
                    receipt_semantics_complete,
                    result_kinds
                    == [
                        "active-alarms",
                        "bounded-logs",
                        "mdb",
                        "service-tree",
                        "target-clock",
                        "version-file",
                    ],
                    len(sentinels) == 6,
                    len(set(sentinels)) == 6,
                    result_values_complete,
                    all(
                        item.get("projection_truncated") is not True
                        for item in receipt_results
                        if isinstance(item, Mapping)
                    ),
                    gate_receipt.get("content_compacted") is not True,
                    not preview_duplicated,
                )
            ),
        }
        expected_representative = {
            "result_count": len(receipt_results),
            "result_kinds": result_kinds,
            "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
            "structured_semantics_complete": all(
                (
                    terminal_receipt_ref == expected_reference,
                    gate_receipt.get("content_compacted") is not True,
                    all(
                        item.get("projection_truncated") is not True
                        for item in receipt_results
                        if isinstance(item, Mapping)
                    ),
                )
            ),
            "gate_structured_semantics_complete": (
                gate_receipt.get("content_compacted") is not True
            ),
            "terminal_structured_semantics_complete": (
                terminal_receipt_ref == expected_reference
            ),
            "preview_sentinel_count": len(sentinels),
            "preview_values_duplicated": preview_duplicated,
            "minimum_preview_bytes": DUAL_PROJECTION_MIN_PREVIEW_BYTES,
            "preview_bytes": preview_bytes,
            "repeated_projection": expected_repeated_projection,
        }
        for turn_name, structured in turns.items():
            if (
                _structured_turn_digest(structured)
                != DUAL_PROJECTION_STRUCTURED_TURN_DIGESTS[turn_name]
            ):
                raise ValueError(
                    "Runtime stability dual-projection structured semantics "
                    "do not match the canonical fixture"
                )
        expected_decision = "warning" if expected_warnings else "passed"
        if (
            projection.get("status")
            != ("passed" if expected_correctness["passed"] else "failed")
            or correctness != expected_correctness
            or representative != expected_representative
            or efficiency.get("blocks_promotability") is not False
            or efficiency.get("decision") != expected_decision
            or efficiency.get("warnings") != expected_warnings
        ):
            raise ValueError("Runtime stability dual-projection contract failed")

    soak = _scenario(report, "restart_soak")
    expected_runs = SOAK_RESTART_CYCLES * SOAK_RUNS_PER_CYCLE
    expected_calls = expected_runs * (2 if schema == SCHEMA_V1 else 3)
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
            == expected_runs * 2,
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

    scenario_names = [
        "duplicate_storm",
        "gate_concurrency",
        "capacity",
        "artifact_lifecycle",
        "restart_soak",
    ]
    if schema == SCHEMA:
        scenario_names.append("dual_projection")
    scenario_statuses = {
        str(_scenario(report, name).get("status", ""))
        for name in scenario_names
    }
    calculated_promotable = scenario_statuses == {"passed"}
    if report.get("promotable") is not calculated_promotable:
        raise ValueError("Runtime stability promotion status is inconsistent")
    if require_promotable and not calculated_promotable:
        raise ValueError("Runtime stability evidence is not promotable")


def verify_runtime_stability_report(
    report: Mapping[str, object],
    *,
    expected_source_commit: str,
    require_promotable: bool = False,
) -> None:
    """Verify current evidence; promotion paths require the v2 contract."""

    _verify_runtime_stability_report(
        report,
        expected_source_commit=expected_source_commit,
        require_promotable=require_promotable,
        allow_legacy_v1=False,
    )


def verify_legacy_runtime_stability_report(
    report: Mapping[str, object],
    *,
    expected_source_commit: str,
    require_promotable: bool = False,
) -> None:
    """Read immutable v1 evidence without permitting it in current promotion."""

    if report.get("schema") != SCHEMA_V1:
        raise ValueError("Runtime stability evidence is not legacy v1")
    _verify_runtime_stability_report(
        report,
        expected_source_commit=expected_source_commit,
        require_promotable=require_promotable,
        allow_legacy_v1=True,
    )
