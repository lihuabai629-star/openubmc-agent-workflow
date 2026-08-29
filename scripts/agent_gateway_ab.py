#!/usr/bin/env python3
"""Run and evaluate paired AB/BA qualification for the semantic Agent Gateway."""

from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Iterable, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[1]
TARGET_RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(TARGET_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(TARGET_RUNTIME_ROOT))

from openubmc_target_runtime.diagnostic_receipt import (  # noqa: E402
    diagnostic_result_value_evaluable,
)


SCHEMA = "openubmc-agent-workflow.agent-gateway-ab.v3"
RUN_EVIDENCE_SCHEMA = f"{SCHEMA}/run-evidence-v2"
RUN_ATTESTATION_SCHEMA = f"{RUN_EVIDENCE_SCHEMA}/ssh-signature-v2"
RUN_ATTESTATION_IDENTITY = "openubmc-agent-workflow-qualification"
RUN_ATTESTATION_NAMESPACE = "openubmc-agent-gateway-ab"
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
SKILL_DISCLOSURE_VALIDITY_THRESHOLDS = {
    "min_arm_valid_rate": 0.95,
    "max_invalid_pair_fraction": 0.10,
    "max_candidate_valid_rate_regression": 0.05,
}
BENCHMARK_TARGET = "10.121.136.200"
QUALIFICATION_MODEL = "gpt-5.6-sol"
QUALIFICATION_CODEX_VERSION = "codex-cli 0.150.0"
QUALIFICATION_CODEX_CONFIG = (
    "features.shell_tool=false",
    "features.plugins=false",
    'model_provider="cliproxy"',
    'model_providers.cliproxy.name="CLIProxyAPI"',
    'model_providers.cliproxy.base_url="http://82.156.104.157/v1"',
    'model_providers.cliproxy.env_key="CLI_PROXY_API_KEY"',
    'model_providers.cliproxy.wire_api="responses"',
    "model_providers.cliproxy.supports_websockets=false",
)
LEGACY_V201_EXECUTE_PROMPT_DIGEST = (
    "sha256:b506e4f7e609c15edfef1720cb17dc6aaafcc1071d00f47efc668ca93ea9e41f"
)
LEGACY_V201_EXECUTE_SOURCE_COMMITS = frozenset(
    {
        "8a83cf221068bb563880736b6de232a8db17bac8",
        "c34c894fe8486eae8b2154721091f1bffd5d38cf",
        "ca06513d4a6ce7ef3c18a0ad592dcdcc99da44d9",
        "d1c12c37beef29dc59192266145b78c7154052bc",
    }
)
MCP_STARTUP_TIMEOUT_SECONDS = 120
MCP_TOOL_TIMEOUT_SECONDS = 900
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
SKILL_DISCLOSURE_CAPABILITIES = ("mdbctl",)
SKILL_DISCLOSURE_MDB_FIELDS = (
    (
        "name",
        "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Name",
    ),
    (
        "resourceid",
        "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive ResourceId",
    ),
    (
        "presence",
        "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Presence",
    ),
)
SKILL_DISCLOSURE_MDB_QUERIES = tuple(
    query for _, query in SKILL_DISCLOSURE_MDB_FIELDS
)
BASELINE_PHASE_CONTRACT_STABLE_FIELDS = (
    "receipt_schema",
    "case_id",
    "idempotency_key",
    "phase_type",
    "producer_identity",
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


def observe_scope_acceptance(
    tools: list[Mapping[str, object]], *, scenario: str = "observation"
) -> dict[str, object]:
    errors: list[str] = []
    expected_capabilities = (
        SKILL_DISCLOSURE_CAPABILITIES
        if scenario == "skill-disclosure"
        else BENCHMARK_CAPABILITIES
    )
    expected_queries = (
        SKILL_DISCLOSURE_MDB_QUERIES
        if scenario == "skill-disclosure"
        else BENCHMARK_MDB_QUERIES
    )
    if any(
        item.get("type") == "mcp_tool_call"
        and item.get("server") != "openubmc-target-runtime"
        for item in tools
    ):
        errors.append("unrelated MCP tools")
    calls = [
        item
        for item in tools
        if item.get("type") == "mcp_tool_call"
        and item.get("server") == "openubmc-target-runtime"
    ]
    if len(calls) != 1 or calls[0].get("tool") != "observe":
        return {
            "passed": False,
            "errors": ["arm must call observe exactly once"],
            "reason_codes": ["observe_call_count"],
        }
    if any(item.get("type") == "command_execution" for item in tools):
        errors.append("arm must not execute shell commands")
    call = calls[0]
    arguments = _json_object(call.get("arguments"))
    if arguments.get("target") != BENCHMARK_TARGET:
        errors.append("target does not match the benchmark target")
    freshness = _json_object(arguments.get("freshness"))
    if freshness != {"mode": "live", "max_age_seconds": 0}:
        errors.append("freshness must request one live observation")
    if "assurance" in arguments:
        errors.append("legacy assurance input")
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
    if normalized_names != expected_capabilities:
        errors.append("capability selector does not match the required capabilities")
    queries = mdb.get("queries", []) if isinstance(mdb, Mapping) else []
    if not isinstance(queries, list) or tuple(queries) != expected_queries:
        errors.append("MDB selector does not match the exact benchmark queries")

    result = _json_object(call.get("result"))
    receipt = _json_object(
        result.get("structured_content") or result.get("structuredContent")
    )
    receipt_id = str(receipt.get("receipt_id", ""))
    if not receipt_id or receipt.get("status") != "complete":
        errors.append("arm must return a complete ObservationReceipt")
    coverage = _json_object(receipt.get("coverage"))
    expected_coverage = {
        "requested": len(expected_capabilities) + len(expected_queries),
        "available": len(expected_capabilities) + len(expected_queries),
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
    if observed_capabilities != {name: "available" for name in expected_capabilities}:
        errors.append("capability results are not fully available")
    mdb_result = _json_object(results.get(mdb_id))
    mdb_values = mdb_result.get("values", [])
    if (
        not isinstance(mdb_values, list)
        or len(mdb_values) != len(expected_queries)
        or any(
            not isinstance(item, Mapping)
            or item.get("query_index") != index
            or item.get("status") != "available"
            or "value" not in item
            for index, item in enumerate(mdb_values)
        )
    ):
        errors.append("MDB results do not contain all available raw values")
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


def _skill_disclosure_observed_values(
    tools: Iterable[Mapping[str, object]],
) -> dict[str, object]:
    for call in tools:
        if (
            call.get("type") != "mcp_tool_call"
            or call.get("server") != "openubmc-target-runtime"
            or call.get("tool") != "observe"
        ):
            continue
        arguments = _json_object(call.get("arguments"))
        selectors = arguments.get("selectors")
        selector_values = selectors if isinstance(selectors, list) else []
        capability_id = ""
        mdb_id = ""
        for selector in selector_values:
            if not isinstance(selector, Mapping):
                continue
            kind = str(selector.get("kind", "")).lower()
            if kind == "capability":
                capability_id = str(selector.get("id", ""))
            elif kind == "mdb":
                mdb_id = str(selector.get("id", ""))
        results = _json_object(_structured_tool_result(call).get("results"))
        observed: dict[str, object] = {}
        capabilities = _json_object(results.get(capability_id)).get("values", [])
        if isinstance(capabilities, list):
            for item in capabilities:
                if (
                    isinstance(item, Mapping)
                    and str(item.get("name", "")).lower() == "mdbctl"
                ):
                    observed["mdbctl"] = item.get("status")
        values = _json_object(results.get(mdb_id)).get("values", [])
        if isinstance(values, list):
            by_index = {
                item.get("query_index"): item.get("value")
                for item in values
                if isinstance(item, Mapping)
                and item.get("status") == "available"
                and "value" in item
            }
            for index, (field, _) in enumerate(SKILL_DISCLOSURE_MDB_FIELDS):
                if index in by_index:
                    observed[field] = by_index[index]
        return observed
    return {}


def _result_case_identity(result: Mapping[str, object]) -> tuple[str, bool]:
    top_level = str(result.get("case_id") or "")
    envelope = _json_object(result.get("agent_envelope"))
    nested = str(envelope.get("case_id") or "")
    return top_level or nested, not (top_level and nested and top_level != nested)


def _qualification_source_receipt() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "qualification source-only receipt completed",
        "payload": {
            "source_revision": "qualification-source",
            "authored_files": ["src/qualification.lua"],
            "verification_plan": ["run qualification tests"],
        },
    }


def _qualification_diagnosis_receipt(
    evidence_ids: Iterable[str],
) -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "qualification diagnosis accepted",
        "payload": {
            "root_cause": "qualification source defect",
            "evidence_ids": list(evidence_ids),
            "known_gaps": [],
        },
    }


def _qualification_diagnosis_respond_template() -> str:
    return json.dumps(
        {
            "kind": "respond",
            "run_id": "<structured_content.run_id>",
            "gate_id": "<structured_content.gate.gate_id>",
            "gate_version": "<structured_content.gate.gate_version>",
            "schema_digest": "<structured_content.gate.schema_digest>",
            "response": _qualification_diagnosis_receipt(
                ["<all current diagnostic_receipt.evidence[].evidence_id values>"]
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace(
        '"gate_version":"<structured_content.gate.gate_version>"',
        '"gate_version":<structured_content.gate.gate_version>',
    )


def _complete_diagnostic_receipt(
    value: Mapping[str, object],
    *,
    expected_operation: str,
    expected_evidence_ids: Iterable[str] = (),
    expected_root_cause: str = "",
) -> bool:
    expected = list(expected_evidence_ids)
    if not str(value.get("receipt_id") or "").strip():
        return False
    if value.get("operation") != expected_operation:
        return False
    if value.get("agent_acceptance") != "complete":
        return False
    if value.get("status") != "complete" or value.get("content_complete") is not True:
        return False
    coverage = _json_object(value.get("coverage"))
    requested = coverage.get("requested")
    visible_names = (
        "visible_evaluable",
        "visible_unavailable",
        "visible_not_checked",
    )
    visible_present = [name in coverage for name in visible_names]
    if any(visible_present) and not all(visible_present):
        return False
    if (
        isinstance(requested, bool)
        or not isinstance(requested, int)
        or requested < 1
        or coverage.get("complete") is not True
        or coverage.get("evaluable") != requested
        or coverage.get("unavailable") != 0
        or coverage.get("not_checked") != 0
    ):
        return False
    if all(visible_present) and (
        coverage.get("visible_evaluable") != requested
        or coverage.get("visible_unavailable") != 0
        or coverage.get("visible_not_checked") != 0
    ):
        return False
    raw_results = value.get("results")
    if not isinstance(raw_results, list):
        return False
    result_ids: list[str] = []
    visible_available = 0
    visible_unavailable = 0
    visible_not_checked = 0
    for item in raw_results:
        if not isinstance(item, Mapping):
            return False
        result_id = str(item.get("result_id") or "").strip()
        if not result_id or result_id in result_ids:
            return False
        result_ids.append(result_id)
        status = item.get("status")
        if status == "available":
            if not diagnostic_result_value_evaluable(item.get("value")):
                return False
            visible_available += 1
        elif status == "unavailable":
            visible_unavailable += 1
        elif status == "not_checked":
            visible_not_checked += 1
        else:
            return False
    raw_compacted_results = value.get("compacted_results")
    compacted_ids: list[str] = []
    if raw_compacted_results is not None:
        compacted_results = _json_object(raw_compacted_results)
        raw_ids = compacted_results.get("result_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return False
        compacted_ids = [str(item).strip() for item in raw_ids]
        if (
            any(not item for item in compacted_ids)
            or len(compacted_ids) != len(set(compacted_ids))
            or set(compacted_ids).intersection(result_ids)
        ):
            return False
        compacted_status = compacted_results.get("status")
        if compacted_status == "available":
            return False
        elif compacted_status == "unavailable":
            visible_unavailable += len(compacted_ids)
        elif compacted_status == "not_checked":
            visible_not_checked += len(compacted_ids)
        else:
            return False
    if len(result_ids) + len(compacted_ids) != requested:
        return False
    if (
        visible_available != requested
        or visible_unavailable != 0
        or visible_not_checked != 0
    ):
        return False
    freshness = _json_object(value.get("freshness"))
    if freshness.get("status") not in {"fresh", "complete"}:
        return False
    if freshness.get("complete") is False:
        return False
    if not str(freshness.get("observed_at") or "").strip():
        return False
    for field in ("unavailable_dimensions", "lost_dimensions", "stale_evidence"):
        if freshness.get(field) not in (None, []):
            return False
    raw_evidence = value.get("evidence", [])
    evidence_ids = [
        str(item.get("evidence_id"))
        for item in raw_evidence
        if isinstance(item, Mapping) and item.get("evidence_id")
    ] if isinstance(raw_evidence, list) else []
    raw_gaps = value.get("gaps", [])
    gaps = (
        [str(item) for item in raw_gaps]
        if isinstance(raw_gaps, list)
        else ["invalid_gap_shape"]
    )
    content_compacted = value.get("content_compacted") is True
    compacted_gap = "diagnostic_receipt_compacted" in gaps
    raw_compacted_count = coverage.get("compacted", 0)
    if isinstance(raw_compacted_count, bool) or not isinstance(
        raw_compacted_count, int
    ):
        return False
    if content_compacted:
        if (
            not compacted_gap
            or not all(visible_present)
            or raw_compacted_count < requested
        ):
            return False
    elif compacted_gap or raw_compacted_count != 0 or compacted_ids:
        return False
    if expected_root_cause:
        matching_results = [
            _json_object(item)
            for item in raw_results
            if _diagnostic_value_contains_root_cause(
                _json_object(item).get("value"),
                expected_root_cause,
            )
        ]
        if not matching_results or not any(
            isinstance(item.get("evidence_ids"), list)
            and item.get("evidence_ids") == expected
            and all(
                isinstance(evidence_id, str) and evidence_id
                for evidence_id in item.get("evidence_ids", [])
            )
            for item in matching_results
        ):
            return False
    return (
        bool(evidence_ids)
        and (not expected or evidence_ids == expected)
        and set(gaps).issubset({"diagnostic_receipt_compacted"})
    )


def _diagnostic_value_contains_root_cause(
    value: object,
    expected_root_cause: str,
) -> bool:
    if isinstance(value, Mapping):
        if str(value.get("root_cause") or "").strip() == expected_root_cause:
            return True
        summary = value.get("summary")
        if isinstance(summary, list) and any(
            isinstance(item, Mapping)
            and str(item.get("path") or "") == "$.root_cause"
            and str(item.get("value") or "").strip() == expected_root_cause
            for item in summary
        ):
            return True
    return False


def _qualification_respond_template() -> str:
    template = {
        "kind": "respond",
        "run_id": "<structured_content.run_id>",
        "gate_id": "<structured_content.gate.gate_id>",
        "gate_version": "<structured_content.gate.gate_version>",
        "schema_digest": "<structured_content.gate.schema_digest>",
        "response": _qualification_source_receipt(),
    }
    encoded = json.dumps(
        template,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return encoded.replace(
        '"gate_version":"<structured_content.gate.gate_version>"',
        '"gate_version":<structured_content.gate.gate_version>',
    )


def _observe_template(
    capabilities: Iterable[str],
    queries: Iterable[str],
) -> str:
    return json.dumps(
        {
            "target": BENCHMARK_TARGET,
            "freshness": {"mode": "live", "max_age_seconds": 0},
            "selectors": [
                {
                    "id": "capabilities",
                    "kind": "capability",
                    "names": [name.upper() for name in capabilities],
                },
                {
                    "id": "drive",
                    "kind": "mdb",
                    "queries": list(queries),
                },
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _qualification_observe_template() -> str:
    return _observe_template(BENCHMARK_CAPABILITIES, BENCHMARK_MDB_QUERIES)


def _skill_disclosure_observe_template() -> str:
    return _observe_template(
        SKILL_DISCLOSURE_CAPABILITIES,
        SKILL_DISCLOSURE_MDB_QUERIES,
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
    if kinds not in (["start", "respond"], ["start", "respond", "respond"]):
        errors.append(
            "candidate must use one start and one or two ordered Gate responses"
        )
    if any(item.get("type") == "command_execution" for item in tools):
        errors.append("candidate must not execute shell commands")
    if len(calls) in {2, 3}:
        start_arguments = _json_object(calls[0].get("arguments"))
        start_result = _structured_tool_result(calls[0])
        if start_arguments.get("target") != BENCHMARK_TARGET:
            errors.append("execute target does not match the benchmark target")
        if start_arguments.get("intent") != "diagnose-and-fix":
            errors.append("execute intent must be diagnose-and-fix")
        if start_arguments.get("delivery_strategy") != "source-only":
            errors.append("execute delivery strategy must be source-only")
        current_result = start_result
        current_gate = _json_object(current_result.get("gate"))
        if start_result.get("state") != "waiting_response" or not current_gate:
            errors.append("start must return one actionable Runtime Gate")
        response_index = 1
        if current_gate.get("owner") == "openubmc-debug":
            if current_gate.get("name") != "diagnosis.acceptance":
                errors.append("Debug-owned Gate must be diagnosis.acceptance")
            if len(calls) != 3:
                errors.append(
                    "diagnosis.acceptance requires a diagnosis response before development"
                )
            else:
                diagnosis_arguments = _json_object(calls[1].get("arguments"))
                if diagnosis_arguments.get("run_id") != start_result.get("run_id"):
                    errors.append("Diagnosis response must continue the same Run")
                for name in ("gate_id", "gate_version", "schema_digest"):
                    if current_gate.get(name) in (None, ""):
                        errors.append(f"diagnosis Gate must provide {name}")
                    elif diagnosis_arguments.get(name) != current_gate.get(name):
                        errors.append(f"Diagnosis response must preserve {name}")
                diagnostic_receipt = _json_object(
                    start_result.get("diagnostic_receipt")
                )
                raw_evidence = diagnostic_receipt.get("evidence", [])
                evidence_ids = [
                    str(item.get("evidence_id"))
                    for item in raw_evidence
                    if isinstance(item, Mapping) and item.get("evidence_id")
                ] if isinstance(raw_evidence, list) else []
                if not evidence_ids:
                    errors.append(
                        "diagnosis Gate must expose current diagnostic evidence identities"
                    )
                if _json_object(diagnosis_arguments.get("response")) != (
                    _qualification_diagnosis_receipt(evidence_ids)
                ):
                    errors.append(
                        "Diagnosis response must match the fixed grounded receipt"
                    )
                current_result = _structured_tool_result(calls[1])
                current_gate = _json_object(current_result.get("gate"))
                if (
                    current_result.get("state") != "waiting_response"
                    or current_gate.get("owner") != "openubmc-developer"
                    or current_gate.get("name") != "developer.change"
                ):
                    errors.append(
                        "diagnosis response must return the Developer Gate"
                    )
                if not _complete_diagnostic_receipt(
                    _json_object(current_result.get("diagnostic_receipt")),
                    expected_operation="diagnosis.acceptance",
                    expected_evidence_ids=evidence_ids,
                    expected_root_cause=str(
                        _json_object(
                            _json_object(diagnosis_arguments.get("response")).get(
                                "payload"
                            )
                        ).get("root_cause")
                        or ""
                    ).strip(),
                ):
                    errors.append(
                        "diagnosis response must return a complete evaluable receipt"
                    )
                response_index = 2
        elif current_gate.get("owner") != "openubmc-developer":
            errors.append("source-only Gate must be owned by Debug or Developer")
        elif len(calls) != 2:
            errors.append(
                "a start that returns developer.change requires exactly one response"
            )
        if current_gate.get("owner") == "openubmc-developer" and (
            current_gate.get("name") != "developer.change"
        ):
            errors.append("Developer-owned Gate must be developer.change")
        if current_gate.get("owner") == "openubmc-developer" and (
            not _complete_diagnostic_receipt(
                _json_object(current_result.get("diagnostic_receipt")),
                expected_operation=(
                    "diagnosis.acceptance" if response_index == 2 else "debug_run"
                ),
                expected_evidence_ids=(
                    evidence_ids if response_index == 2 else ()
                ),
            )
        ):
            errors.append(
                "Developer Gate requires a complete evaluable diagnostic receipt"
            )
        final_arguments = _json_object(calls[response_index].get("arguments"))
        final_result = _structured_tool_result(calls[response_index])
        if final_arguments.get("run_id") != start_result.get("run_id"):
            errors.append("Gate response must continue the same Run")
        for name in ("gate_id", "gate_version", "schema_digest"):
            if current_gate.get(name) in (None, ""):
                errors.append(f"Developer Gate must provide {name}")
            elif final_arguments.get(name) != current_gate.get(name):
                errors.append(f"Gate response must preserve {name}")
        response = _json_object(final_arguments.get("response"))
        if response != _qualification_source_receipt():
            errors.append("Gate response must match the fixed source receipt")
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
    phase_positions = [
        index for index, name in enumerate(names) if name == "phase_record"
    ]
    valid_shape = False
    start_calls: list[Mapping[str, object]] = []
    final_call: Mapping[str, object] = {}
    if len(phase_positions) == 1:
        phase_index = phase_positions[0]
        start_calls = calls[:phase_index]
        final_calls = calls[phase_index + 1 :]
        valid_shape = (
            bool(start_calls)
            and all(call.get("tool") == "workflow.advance" for call in start_calls)
            and len(final_calls) == 1
            and final_calls[0].get("tool") in {"workflow.advance", "workflow.next"}
        )
        if final_calls:
            final_call = final_calls[0]
    if not valid_shape:
        errors.append(
            "baseline must use one or more workflow.advance calls, one phase_record, and one continuation"
        )
    if any(item.get("type") == "command_execution" for item in tools):
        errors.append("baseline must not execute shell commands")
    if valid_shape:
        start_results = [_structured_tool_result(call) for call in start_calls]
        start_arguments = [_json_object(call.get("arguments")) for call in start_calls]
        phase_call = calls[phase_positions[0]]
        phase_arguments = _json_object(phase_call.get("arguments"))
        phase_result = _structured_tool_result(phase_call)
        final_arguments = _json_object(final_call.get("arguments"))
        final_result = _structured_tool_result(final_call)
        case_ids: list[str] = []
        contracts: list[Mapping[str, object]] = []
        revisions: list[object] = []
        for arguments, result in zip(start_arguments, start_results):
            if arguments.get("ip") != BENCHMARK_TARGET:
                errors.append("baseline target does not match the benchmark target")
            if arguments.get("intent") != "diagnose-and-fix":
                errors.append("baseline intent must be diagnose-and-fix")
            if arguments.get("delivery_strategy") != "source-only":
                errors.append("baseline delivery strategy must be source-only")
            handoff = _json_object(result.get("handoff_arguments"))
            contract = _json_object(handoff.get("phase_record_contract"))
            envelope = _json_object(result.get("agent_envelope"))
            result_case, case_consistent = _result_case_identity(result)
            case_ids.append(result_case)
            contracts.append(contract)
            revisions.append(result.get("revision", envelope.get("revision")))
            if not case_consistent:
                errors.append(
                    "baseline start result Case must match its agent envelope"
                )
            if (
                result.get("status") != "waiting_phase_record"
                or result.get("required_skill") != "openubmc-developer"
                or not contract
            ):
                errors.append("baseline start must return one Developer phase Gate")
        case_id = case_ids[-1]
        contract = contracts[-1]
        current_revision = revisions[-1]
        if not case_id or any(item != case_id for item in case_ids):
            errors.append("baseline repeated starts must preserve the same Case")
        if any(item.get("case_id") != case for item, case in zip(contracts, case_ids)):
            errors.append("baseline start phase contract must match the Case")
        for prior_contract in contracts[:-1]:
            for name in BASELINE_PHASE_CONTRACT_STABLE_FIELDS:
                if prior_contract.get(name) != contract.get(name):
                    errors.append(
                        f"baseline repeated starts must preserve phase contract {name}"
                    )
        for name in BASELINE_PHASE_CONTRACT_STABLE_FIELDS:
            if phase_arguments.get(name) != contract.get(name):
                errors.append(f"baseline phase_record must preserve {name}")
        if phase_arguments.get("expected_revision") != current_revision:
            errors.append("baseline phase_record must use the current envelope revision")
        if phase_arguments.get("status") != "completed":
            errors.append("baseline phase_record must complete the source phase")
        phase_case, phase_case_consistent = _result_case_identity(phase_result)
        if not phase_case_consistent:
            errors.append(
                "baseline phase_record result Case must match its agent envelope"
            )
        if phase_case != case_id:
            errors.append("baseline phase_record result must use the same Case")
        if final_arguments.get("case_id") != case_id:
            errors.append("baseline continuation must use the same Case")
        final_case, final_case_consistent = _result_case_identity(final_result)
        if not final_case_consistent:
            errors.append(
                "baseline continuation result Case must match its agent envelope"
            )
        if final_case != case_id:
            errors.append("baseline continuation result must use the same Case")
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
        if (
            scenario in {"observation", "skill-disclosure"}
            and item.get("tool") != "observe"
        ):
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


@dataclass(frozen=True)
class RunEvidenceRecord:
    arm: str
    pair: int
    order: int
    scenario: str
    events: tuple[Mapping[str, object], ...]
    final: str
    exit_code: int
    duration_seconds: float

    @classmethod
    def capture(
        cls,
        *,
        arm: str,
        pair: int,
        order: int,
        scenario: str,
        events: Iterable[Mapping[str, object]],
        final: str,
        exit_code: int,
        duration_seconds: float,
    ) -> RunEvidenceRecord:
        runner_event = {
            "type": "runner.completed",
            "exit_code": exit_code,
            "duration_seconds": round(duration_seconds, 3),
        }
        return cls(
            arm=arm,
            pair=pair,
            order=order,
            scenario=scenario,
            events=(*events, runner_event),
            final=final,
            exit_code=exit_code,
            duration_seconds=round(duration_seconds, 3),
        )

    @classmethod
    def parse(cls, value: object, *, index: int) -> RunEvidenceRecord:
        run = _json_object(value)
        arm = run.get("arm")
        pair = run.get("pair")
        order = run.get("order")
        scenario = run.get("scenario")
        events = run.get("events")
        final = run.get("final")
        if arm not in {"A", "B"}:
            raise ValueError(f"AB run evidence item {index} has an invalid arm")
        if not isinstance(pair, int) or isinstance(pair, bool) or pair < 1:
            raise ValueError(f"AB run evidence item {index} has an invalid pair")
        if not isinstance(order, int) or isinstance(order, bool) or order not in {1, 2}:
            raise ValueError(f"AB run evidence item {index} has an invalid order")
        if scenario not in {
            "observation",
            "skill-disclosure",
            "execute-source-only",
        }:
            raise ValueError(f"AB run evidence item {index} has an invalid scenario")
        if not isinstance(events, list) or not all(
            isinstance(event, Mapping) for event in events
        ):
            raise ValueError(f"AB run evidence item {index} has invalid events")
        if not isinstance(final, str):
            raise ValueError(f"AB run evidence item {index} has an invalid final output")
        completed = [
            event for event in events if event.get("type") == "runner.completed"
        ]
        if len(completed) != 1:
            raise ValueError(
                f"AB run evidence item {index} must contain one runner outcome"
            )
        exit_code = completed[0].get("exit_code")
        duration = completed[0].get("duration_seconds")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise ValueError(f"AB run evidence item {index} has an invalid exit code")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or float(duration) <= 0
        ):
            raise ValueError(f"AB run evidence item {index} has an invalid duration")
        return cls(
            arm=str(arm),
            pair=pair,
            order=order,
            scenario=str(scenario),
            events=tuple(events),
            final=final,
            exit_code=exit_code,
            duration_seconds=float(duration),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "arm": self.arm,
            "pair": self.pair,
            "order": self.order,
            "events": list(self.events),
            "final": self.final,
        }

    def metric(self) -> dict[str, object]:
        completed = [
            event for event in self.events if event.get("type") == "turn.completed"
        ]
        usage = _json_object(completed[-1].get("usage")) if completed else {}
        tools = []
        for event in self.events:
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
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        cached_tokens = int(usage.get("cached_input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        acceptance = semantic_acceptance(
            self.final,
            scenario=self.scenario,
            observed_values=(
                _skill_disclosure_observed_values(tools)
                if self.scenario == "skill-disclosure"
                else None
            ),
        )
        if self.scenario == "skill-disclosure":
            scope_validation = observe_scope_acceptance(
                tools, scenario=self.scenario
            )
        elif self.arm == "B":
            scope_validation = (
                observe_scope_acceptance(tools)
                if self.scenario == "observation"
                else candidate_execute_acceptance(tools)
            )
        else:
            scope_validation = (
                baseline_execute_acceptance(tools)
                if self.scenario == "execute-source-only"
                else {"passed": True, "errors": []}
            )
        scope_ok = bool(scope_validation["passed"])
        model_turns = max(
            1,
            sum(
                event.get("type") == "item.completed"
                and _json_object(event.get("item")).get("type") == "agent_message"
                for event in self.events
            ),
        )
        return {
            "scenario": self.scenario,
            "arm": self.arm,
            "pair": self.pair,
            "order": self.order,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
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
                list(self.events),
                arm=self.arm,
                scenario=self.scenario,
                fallback=self.duration_seconds,
            ),
            "gate_roundtrips": int(scope_validation.get("gate_roundtrips", 0) or 0),
            "resume_calls": int(scope_validation.get("resume_calls", 0) or 0),
            "mcp_tools": [
                {"tool": name, "count": count}
                for name, count in sorted(mcp_tools.items())
            ],
            "final_chars": len(self.final),
            "semantic_acceptance": acceptance,
            "scope_acceptance": scope_ok,
            "scope_validation": scope_validation,
            "valid": (
                self.exit_code == 0
                and input_tokens + output_tokens > 0
                and acceptance["passed"]
                and scope_ok
            ),
        }


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
    final = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    return RunEvidenceRecord.capture(
        arm=arm,
        pair=pair,
        order=order,
        scenario=scenario,
        events=events,
        final=final,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
    ).metric()


def metrics_from_run_evidence(value: object) -> list[dict[str, object]]:
    document = _json_object(value)
    if document.get("schema") != RUN_EVIDENCE_SCHEMA:
        raise ValueError("AB run evidence schema is invalid")
    raw_runs = document.get("runs")
    if not isinstance(raw_runs, list):
        raise ValueError("AB run evidence must contain a runs array")
    return [
        RunEvidenceRecord.parse(raw_run, index=index).metric()
        for index, raw_run in enumerate(raw_runs, 1)
    ]


def _run_source_binding_errors(
    value: object,
    *,
    expected_candidate_commit: str,
    expected_baseline_commit: str,
) -> list[str]:
    document = _json_object(value)
    errors: list[str] = []
    source = _json_object(document.get("source"))
    expected = {
        "A": expected_baseline_commit,
        "B": expected_candidate_commit,
    }
    if source.get("candidate_commit") != expected_candidate_commit:
        errors.append("AB run source commit does not match the release candidate")
    if source.get("baseline_commit") != expected_baseline_commit:
        errors.append("AB run source commit does not match the qualification baseline")
    runs = document.get("runs")
    if not isinstance(runs, list):
        return errors
    for index, value in enumerate(runs, 1):
        run = _json_object(value)
        arm = run.get("arm")
        if arm in expected and run.get("source_commit") != expected[arm]:
            errors.append(
                f"AB run source commit does not match arm {arm} at item {index}"
            )
    return errors


def _text_sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_prompt_skill_path(prompt: str, *, scenario: str) -> str:
    if scenario == "execute-source-only":
        return re.sub(
            r"(?m)^(使用已安装的 ).+( 所定义的(?:原生 Case Continuation 路径| Agent Gateway 路径)。)$",
            r"\1<skill-path>\2",
            prompt,
            count=1,
        )
    if scenario == "observation":
        return re.sub(
            r"(?m)^(完整读取并严格遵循 ).+(。)$",
            r"\1<skill-path>\2",
            prompt,
            count=1,
        )
    return prompt


def _run_prompt_binding_errors(
    value: object,
    *,
    expected_scenario: str,
    expected_prompt_digest: object,
) -> list[str]:
    document = _json_object(value)
    runs = document.get("runs")
    if not isinstance(runs, list):
        return []
    prompt_contract = (
        qualification_prompt_contracts(expected_scenario).get(
            expected_prompt_digest
        )
        if isinstance(expected_prompt_digest, str)
        else None
    )
    if prompt_contract is None:
        return ["AB run prompt contract is not registered"]
    errors: list[str] = []
    for index, value in enumerate(runs, 1):
        run = _json_object(value)
        prompt = run.get("prompt")
        prompt_sha256 = run.get("prompt_sha256")
        if not isinstance(prompt, str) or not prompt:
            errors.append(f"AB run prompt is unavailable at item {index}")
            continue
        if prompt_sha256 != _text_sha256(prompt):
            errors.append(f"AB run prompt digest is invalid at item {index}")
        arm = run.get("arm")
        if arm in {"A", "B"} and _normalize_prompt_skill_path(
            prompt,
            scenario=expected_scenario,
        ) != prompt_contract[str(arm)]:
            errors.append(
                f"AB run prompt does not match the qualification prompt contract at item {index}"
            )
    return errors


def _run_environment_binding_errors(
    value: object,
    *,
    expected_fingerprint: object,
) -> list[str]:
    document = _json_object(value)
    runs = document.get("runs")
    if not isinstance(runs, list):
        return []
    return [
        f"AB run environment does not match the release evidence at item {index}"
        for index, value in enumerate(runs, 1)
        if _json_object(value).get("environment_fingerprint")
        != expected_fingerprint
    ]


def _run_qualification_contract_errors(
    value: object,
    *,
    expected_requested_pairs: object,
    expected_schedule_digest: str,
) -> list[str]:
    document = _json_object(value)
    runs = document.get("runs")
    if not isinstance(runs, list):
        return []
    expected = {
        "requested_pairs": expected_requested_pairs,
        "schedule_digest": expected_schedule_digest,
    }
    mismatches = [
        index
        for index, value in enumerate(runs, 1)
        if _json_object(value).get("qualification_contract") != expected
    ]
    if not mismatches:
        return []
    suffix = ", ".join(str(index) for index in mismatches[:5])
    if len(mismatches) > 5:
        suffix += ", ..."
    return [
        "AB run qualification contract does not match the requested checkpoint "
        f"and schedule at items {suffix}"
    ]


def _execution_identity_binding(
    events: Iterable[Mapping[str, object]],
) -> tuple[str, bool]:
    event_list = list(events)
    thread_ids = [
        str(event.get("thread_id", ""))
        for event in event_list
        if event.get("type") == "thread.started"
    ]
    if len(thread_ids) > 1:
        raise ValueError("AB run must contain one Codex thread identity")
    runner_ids = [
        str(event.get("execution_id", ""))
        for event in event_list
        if event.get("type") == "runner.started"
    ]
    if len(thread_ids) == 1:
        selected = thread_ids[0]
        error = "AB run Codex thread identity is invalid"
        has_thread = True
    elif len(runner_ids) == 1:
        selected = runner_ids[0]
        error = "AB runner execution identity is invalid"
        has_thread = False
    else:
        raise ValueError("AB run must contain one execution identity")
    try:
        return str(uuid.UUID(selected)), has_thread
    except ValueError as exc:
        raise ValueError(error) from exc


def _run_attestation_errors(
    value: object, *, public_key: Path
) -> list[str]:
    document = _json_object(value)
    runs = document.get("runs")
    if not isinstance(runs, list):
        return []
    if not public_key.is_file():
        return ["AB run attestation public key is unavailable"]
    try:
        expected_fingerprint = _ssh_key_fingerprint(public_key)
        key_fields = public_key.read_text(encoding="utf-8").strip().split()
    except (OSError, UnicodeDecodeError, ValueError):
        return ["AB run attestation public key is invalid"]
    if len(key_fields) < 2:
        return ["AB run attestation public key is invalid"]
    errors: list[str] = []
    execution_ids: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="openubmc-ab-verify-") as raw:
        root = Path(raw)
        allowed_signers = root / "allowed-signers"
        allowed_signers.write_text(
            f"{RUN_ATTESTATION_IDENTITY} {key_fields[0]} {key_fields[1]}\n",
            encoding="utf-8",
        )
        for index, value in enumerate(runs, 1):
            run = dict(_json_object(value))
            execution_id = run.get("execution_id")
            try:
                normalized_execution_id = str(uuid.UUID(str(execution_id)))
            except (ValueError, AttributeError):
                errors.append(f"AB run attestation execution identity is invalid at item {index}")
            else:
                events = run.get("events")
                try:
                    event_execution_id, has_thread = _execution_identity_binding(
                        event
                        for event in events
                        if isinstance(event, Mapping)
                    ) if isinstance(events, list) else ("", False)
                except ValueError:
                    event_execution_id, has_thread = "", False
                runner_failures = [
                    event.get("exit_code")
                    for event in events
                    if isinstance(event, Mapping)
                    and event.get("type") == "runner.completed"
                    and isinstance(event.get("exit_code"), int)
                    and not isinstance(event.get("exit_code"), bool)
                    and int(event.get("exit_code")) != 0
                ] if isinstance(events, list) else []
                if event_execution_id != normalized_execution_id:
                    errors.append(
                        f"AB run attestation execution identity does not match the runner event at item {index}"
                    )
                elif not has_thread and len(runner_failures) != 1:
                    errors.append(
                        f"AB run attestation execution identity does not match the runner event at item {index}"
                    )
                if normalized_execution_id in execution_ids:
                    errors.append(
                        f"AB run attestation execution identity is duplicated at item {index}"
                    )
                execution_ids.add(normalized_execution_id)
            attestation = _json_object(run.pop("attestation", None))
            signature = attestation.get("signature")
            if (
                attestation.get("schema") != RUN_ATTESTATION_SCHEMA
                or attestation.get("identity") != RUN_ATTESTATION_IDENTITY
                or attestation.get("namespace") != RUN_ATTESTATION_NAMESPACE
                or attestation.get("key_fingerprint") != expected_fingerprint
                or not isinstance(signature, str)
                or not signature
            ):
                errors.append(f"AB run attestation is invalid at item {index}")
                continue
            try:
                signature_bytes = base64.b64decode(signature, validate=True)
            except (ValueError, binascii.Error):
                errors.append(f"AB run attestation signature is invalid at item {index}")
                continue
            signature_path = root / f"run-{index}.sig"
            signature_path.write_bytes(signature_bytes)
            completed = subprocess.run(
                [
                    "ssh-keygen",
                    "-Y",
                    "verify",
                    "-q",
                    "-f",
                    str(allowed_signers),
                    "-I",
                    RUN_ATTESTATION_IDENTITY,
                    "-n",
                    RUN_ATTESTATION_NAMESPACE,
                    "-s",
                    str(signature_path),
                ],
                input=_canonical_json_bytes(run),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode:
                errors.append(f"AB run attestation signature is invalid at item {index}")
    return errors


def _resource_id_conclusion_supported(text: str, folded: str) -> bool:
    cautious_patterns = (
        r"(?:不能|无法|不足以)[^，,。；;！？!?\n]{0,32}(?:证明|说明|表明|判断|判定|确定|认定|确认)",
        r"(?:不代表|不等于|并非|不是|不属于)[^，,。；;！？!?\n]{0,32}异常",
    )
    cautious = (
        "resourceid" in folded
        and "异常" in text
        and any(re.search(pattern, folded) for pattern in cautious_patterns)
    )
    if not cautious:
        return False
    negative_terms = ("不能", "无法", "不足以", "不代表", "不等于", "并非", "不是", "不属于")
    action_terms = ("处理", "维修", "修复", "更换", "升级", "操作", "处置", "整改", "恢复", "重启")
    finality_terms = (
        "最终",
        "结论",
        "结果",
        "明确判定",
        "已经判定",
        "已判定",
        "确认异常",
        "异常成立",
        "异常属实",
        "异常确定",
    )
    uncertainty_verb = r"(?:仍需|还需要|还需|需要|尚需|有待|取决于)"
    uncertainty_evidence = (
        r"(?:(?:其他|更多|额外|补充)?证据|接口(?:规范|定义|契约|语义)|"
        r"预期(?:值|行为|结果)|基线(?:值|行为|结果)|参考(?:值|标准|规范)|"
        r"对照(?:值|标准)|契约(?:证据)?|语义(?:定义|契约)|上下文(?:证据)?)"
    )
    uncertainty_patterns = (
        rf"^(?:判定|判断|确定|确认|认定)(?:是否)?异常"
        rf"[^，,。；;！？!?\n]{{0,24}}{uncertainty_verb}"
        rf"[^，,。；;！？!?\n]{{0,24}}{uncertainty_evidence}\s*$",
        rf"(?:是否异常|异常(?:是否|与否))"
        rf"[^，,。；;！？!?\n]{{0,24}}{uncertainty_verb}"
        rf"[^，,。；;！？!?\n]{{0,24}}{uncertainty_evidence}\s*$",
        rf"^{uncertainty_verb}[^，,。；;！？!?\n]{{0,24}}{uncertainty_evidence}"
        rf"[^，,。；;！？!?\n]{{0,24}}"
        rf"(?:判定|判断|确定|确认|认定)(?:是否)?异常\s*$",
    )
    clauses = [
        clause.strip()
        for clause in re.split(
            r"[，,、。；;！？!?\n]+|(?=但(?:是)?|却|然而|不过|可是)|"
            r"(?=(?:而|同时|并且)(?:最终|明确|正式|已|已经|结论|结果|"
            r"判定|认定|确认))",
            folded,
        )
        if clause.strip()
    ]
    positive_patterns = (
        r"resourceid[^，,。；;！？!?\n]{0,32}异常",
        r"(?:最终)?(?:结论|结果)[^，,。；;！？!?\n]{0,32}异常",
        r"(?:判定|认定)(?!是否)[^，,。；;！？!?\n]{0,32}异常",
        r"确认(?:为|是|属于|构成)[^，,。；;！？!?\n]{0,16}异常",
        r"(?:为|是|属于|构成|确属)异常",
        r"异常(?:成立|属实|确定)",
    )
    carry_finality = False
    for clause in clauses:
        explicit_uncertainty = any(term in clause for term in ("是否", "与否"))
        has_finality = carry_finality or any(
            term in clause for term in finality_terms
        )
        positive = (
            "异常" in clause
            and any(re.search(pattern, clause) for pattern in positive_patterns)
        )
        uncertainty = (
            not any(term in clause for term in action_terms)
            and (explicit_uncertainty or not has_finality)
            and any(re.search(pattern, clause) for pattern in uncertainty_patterns)
        )
        if (
            positive
            and not any(term in clause for term in negative_terms)
            and not uncertainty
        ):
            return False
        if positive or uncertainty:
            carry_finality = False
        elif (
            re.fullmatch(
                r"(?:最终|结论|结果|判断|最终结论|最终结果|最终判断)"
                r"(?:是|为|[:：]|如下(?:所示)?[:：]?)?",
                clause,
            )
            or re.match(
                r"^(?:最终结论|最终结果|最终判断|结论|结果|判断)"
                r"(?:是|为|[:：]|如下(?:所示)?[:：]?)",
                clause,
            )
        ):
            carry_finality = True
    return True


def _parenthetical_annotation_matches(annotation: str, expected: str) -> bool:
    marker = re.match(
        r"^(?:原始(?:返回|值)?|实际(?:返回|值)?|返回(?:值)?|raw(?:\s+value)?|value)"
        r"\s*(?:[:：=]|为|是)?\s*(?P<value>.+?)\s*$",
        annotation,
    )
    if marker is None:
        return False
    annotated_value = marker.group("value").strip(" \t\r\n\\\"'`")
    return annotated_value == expected


def _parenthetical_annotation_describes_quotes(annotation: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:原始(?:值|返回)?|实际值?)?(?:含|包含)(?:双)?引号",
            annotation.strip(),
        )
    )


def _reported_value_matches(reported: str, expected: str) -> bool:
    if not reported.startswith(expected):
        return False
    tail = reported[len(expected):]
    terminal = set(" \t\r\n\\，,；;.。！？!?、：:）)]}】》」』")
    if all(character in terminal for character in tail):
        return True
    remainder = tail.lstrip(" \t\\")
    if remainder.startswith(("（", "(")):
        closing = "）" if remainder[0] == "（" else ")"
        close_index = remainder.find(closing, 1)
        if close_index > 0:
            annotation = remainder[1:close_index]
            suffix = remainder[close_index + 1:]
            if (
                (
                    _parenthetical_annotation_matches(annotation, expected)
                    or _parenthetical_annotation_describes_quotes(annotation)
                )
                and all(character in terminal for character in suffix)
            ):
                return True
    if not remainder or remainder[0] not in "，,；;。！？!?":
        return False
    conclusion = remainder[1:].lstrip()
    return conclusion.startswith(
        ("不能", "无法", "不足以", "这些", "证据", "结论", "本次", "现有")
    )


def _reported_skill_value(
    lines: Iterable[str],
    *,
    field: str,
    value: object,
) -> bool:
    labels = ("mdbctl", "name", "resourceid", "presence")
    label_patterns = {
        label: re.compile(rf"(?<![0-9a-z_]){label}(?![0-9a-z_])")
        for label in labels
    }
    segments: list[str] = []
    for line in lines:
        for match in label_patterns[field].finditer(line):
            segment_end = len(line)
            for pattern in label_patterns.values():
                next_match = pattern.search(line, match.end())
                if next_match is not None:
                    segment_end = min(segment_end, next_match.start())
            segments.append(line[match.end():segment_end])
    value_prefixes = (
        "的实际值为",
        "实际值为",
        "原始值为",
        "原始值",
        "当前上报",
        "上报",
        "值为",
        "status",
        "状态",
        "value",
        "is",
        "为",
        "是",
    )
    reported_values: list[str] = []
    for segment in segments:
        reported = segment.lstrip(" \t:：=,，;；-–—\\")
        for prefix in value_prefixes:
            if reported.startswith(prefix):
                reported = reported[len(prefix):].lstrip(
                    " \t:：=,，;；-–—\\"
                )
                break
        reported_values.append(reported)
    expected = str(value).lower().replace('"', "").replace("'", "")
    if field == "mdbctl" and expected == "available":
        return any(
            _reported_value_matches(reported, "available")
            or _reported_value_matches(reported, "可用")
            for reported in reported_values
        )
    return any(
        _reported_value_matches(reported, expected)
        for reported in reported_values
    )


def semantic_acceptance(
    text: str,
    *,
    scenario: str = "observation",
    observed_values: Mapping[str, object] | None = None,
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
    if scenario == "skill-disclosure":
        folded = text.lower().replace("`", "")
        required = ("mdbctl", "name", "resourceid", "presence")
        missing = [token for token in required if token not in folded]
        if observed_values:
            normalized_lines = [
                line.lower()
                .replace("`", "")
                .replace('"', "")
                .replace("'", "")
                for line in text.splitlines()
            ]
            for field, value in observed_values.items():
                expected = str(value).lower().replace('"', "").replace("'", "")
                if not _reported_skill_value(
                    normalized_lines,
                    field=field,
                    value=value,
                ):
                    missing.append(f"{field}={expected}")
        conclusion = _resource_id_conclusion_supported(text, folded)
        return {
            "passed": not missing and conclusion,
            "missing": missing,
            "conclusion_supported": conclusion,
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
        ("不能" in text or "无法" in text)
        and "resourceid" in folded
        and "异常" in text
    )
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


def _raw_run_valid(item: Mapping[str, object]) -> bool:
    exit_code = item.get("exit_code")
    return (
        isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code == 0
        and _json_object(item.get("semantic_acceptance")).get("passed") is True
        and item.get("scope_acceptance") is True
        and _json_object(item.get("scope_validation")).get("passed") is True
    )


def _skill_disclosure_dispatch_noise(item: Mapping[str, object]) -> bool:
    scope = _json_object(item.get("scope_validation"))
    return (
        item.get("exit_code") == 0
        and item.get("command_events") == 0
        and item.get("mcp_events") == 0
        and item.get("tool_events") == 0
        and scope.get("reason_codes") == ["observe_call_count"]
    )


def _skill_disclosure_validity(
    metrics: list[Mapping[str, object]],
    *,
    pair_ids: list[int],
    invalid_pairs: list[dict[str, object]],
) -> dict[str, object]:
    attempted_pairs = len(pair_ids)
    attempts = {
        arm: [item for item in metrics if str(item.get("arm")) == arm]
        for arm in ("A", "B")
    }
    valid_counts = {
        arm: sum(_raw_run_valid(item) for item in items)
        for arm, items in attempts.items()
    }
    rates = {
        arm: (
            round(valid_counts[arm] / attempted_pairs, 6)
            if attempted_pairs
            else 0.0
        )
        for arm in ("A", "B")
    }
    invalid_pair_fraction = (
        round(len(invalid_pairs) / attempted_pairs, 6)
        if attempted_pairs
        else 1.0
    )
    candidate_regression = round(rates["A"] - rates["B"], 6)
    attempts_by_key = {
        (int(item.get("pair", 0)), str(item.get("arm", ""))): item
        for item in metrics
    }
    non_noise_invalid_runs = [
        {"pair": int(pair["pair"]), "arm": arm}
        for pair in invalid_pairs
        for arm, valid in _json_object(pair.get("valid")).items()
        if arm == "B"
        and valid is not True
        and not _skill_disclosure_dispatch_noise(
            attempts_by_key.get((int(pair["pair"]), str(arm)), {})
        )
    ]
    errors: list[str] = []
    if any(len(items) != attempted_pairs for items in attempts.values()):
        errors.append("each attempted pair must contain one run for both arms")
    if rates["A"] < SKILL_DISCLOSURE_VALIDITY_THRESHOLDS["min_arm_valid_rate"]:
        errors.append("baseline arm validity is below 95%")
    if rates["B"] < SKILL_DISCLOSURE_VALIDITY_THRESHOLDS["min_arm_valid_rate"]:
        errors.append("candidate arm validity is below 95%")
    if (
        invalid_pair_fraction
        > SKILL_DISCLOSURE_VALIDITY_THRESHOLDS["max_invalid_pair_fraction"]
    ):
        errors.append("invalid pair fraction exceeds 10%")
    if (
        candidate_regression
        > SKILL_DISCLOSURE_VALIDITY_THRESHOLDS[
            "max_candidate_valid_rate_regression"
        ]
    ):
        errors.append("candidate validity regresses by more than 5 percentage points")
    if non_noise_invalid_runs:
        errors.append("candidate non-noise invalid behavior is not allowed")
    return {
        "passed": not errors,
        "attempted_pairs": attempted_pairs,
        "arm_valid_counts": valid_counts,
        "arm_valid_rates": rates,
        "invalid_pair_fraction": invalid_pair_fraction,
        "candidate_valid_rate_regression": candidate_regression,
        "non_noise_invalid_runs": non_noise_invalid_runs,
        "errors": errors,
    }


def analyze(metrics: list[Mapping[str, object]]) -> dict[str, object]:
    paired: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    pair_ids = sorted({int(item.get("pair", 0)) for item in metrics})
    attempted_pairs = len(pair_ids)
    invalid: list[dict[str, object]] = []
    for pair_id in pair_ids:
        members = [item for item in metrics if int(item.get("pair", 0)) == pair_id]
        by_arm = {str(item.get("arm")): item for item in members}
        recomputed_validity = {
            arm: _raw_run_valid(item) for arm, item in by_arm.items()
        }
        if (
            set(by_arm) != {"A", "B"}
            or not all(recomputed_validity.values())
        ):
            invalid.append(
                {
                    "pair": pair_id,
                    "arms": sorted(by_arm),
                    "valid": recomputed_validity,
                    "claimed_valid": {
                        arm: bool(item.get("valid")) for arm, item in by_arm.items()
                    },
                }
            )
            continue
        paired.append((by_arm["A"], by_arm["B"]))
    summaries: dict[str, object] = {}
    valid_pairs = len(paired)
    for metric in METRICS:
        metric_pairs = [
            (baseline, candidate)
            for baseline, candidate in paired
            if all(
                isinstance(item.get(metric), (int, float))
                and not isinstance(item.get(metric), bool)
                and math.isfinite(float(item[metric]))
                and float(item[metric]) > 0
                for item in (baseline, candidate)
            )
        ]
        metric_complete = valid_pairs > 0 and len(metric_pairs) == valid_pairs
        ratios = [
            float(candidate[metric]) / float(baseline[metric])
            for baseline, candidate in metric_pairs
        ]
        if ratios:
            point = _geometric_mean(ratios)
            upper = bootstrap_upper(ratios)
            threshold_passed = (
                point <= THRESHOLDS["geometric_mean_ratio_max"]
                and upper <= THRESHOLDS["one_sided_95_upper_max"]
            )
            p95_ratio = (
                _percentile(
                    (
                        float(candidate[metric])
                        for _baseline, candidate in metric_pairs
                    ),
                    0.95,
                )
                / _percentile(
                    (
                        float(baseline[metric])
                        for baseline, _candidate in metric_pairs
                    ),
                    0.95,
                )
                if attempted_pairs >= CHECKPOINTS[-1]
                else None
            )
            if p95_ratio is not None:
                threshold_passed = (
                    threshold_passed
                    and p95_ratio <= THRESHOLDS["p95_ratio_max_at_30_pairs"]
                )
            summaries[metric] = {
                "paired_ratios": [round(value, 6) for value in ratios],
                "paired_samples": len(metric_pairs),
                "expected_paired_samples": valid_pairs,
                "geometric_mean_ratio": round(point, 6),
                "one_sided_95_upper": round(upper, 6),
                "p95_ratio": round(p95_ratio, 6) if p95_ratio is not None else None,
                "complete": metric_complete,
                "passed": metric_complete and threshold_passed,
            }
        else:
            summaries[metric] = {
                "paired_ratios": [],
                "paired_samples": 0,
                "expected_paired_samples": valid_pairs,
                "geometric_mean_ratio": None,
                "one_sided_95_upper": None,
                "p95_ratio": None,
                "complete": False,
                "passed": False,
            }
    scenarios = {
        str(item.get("scenario", ""))
        for item in metrics
        if str(item.get("scenario", ""))
    }
    scenario = next(iter(scenarios)) if len(scenarios) == 1 else ""
    validity = (
        _skill_disclosure_validity(
            metrics,
            pair_ids=pair_ids,
            invalid_pairs=invalid,
        )
        if scenario == "skill-disclosure"
        else None
    )
    validity_pass = bool(validity["passed"]) if validity is not None else not invalid
    if attempted_pairs not in CHECKPOINTS:
        next_pairs = next(
            (checkpoint for checkpoint in CHECKPOINTS if checkpoint > attempted_pairs),
            None,
        )
        decision = "collect_more" if next_pairs is not None else "failed"
    elif valid_pairs < CHECKPOINTS[0]:
        next_pairs = next(
            (checkpoint for checkpoint in CHECKPOINTS if checkpoint > attempted_pairs),
            None,
        )
        decision = "collect_more" if next_pairs is not None else "failed"
    elif validity_pass:
        decision = "passed"
        next_pairs = None
    elif attempted_pairs < CHECKPOINTS[1]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[1]
    elif attempted_pairs < CHECKPOINTS[2]:
        decision = "collect_more"
        next_pairs = CHECKPOINTS[2]
    else:
        decision = "failed"
        next_pairs = None
    efficiency_gaps = [
        metric
        for metric in METRICS
        if not bool(_json_object(summaries.get(metric)).get("complete"))
    ]
    efficiency_warnings = [
        metric
        for metric in METRICS
        if bool(_json_object(summaries.get(metric)).get("complete"))
        and not bool(_json_object(summaries.get(metric)).get("passed"))
    ]
    efficiency_decision = (
        "incomplete"
        if efficiency_gaps
        else "warning"
        if efficiency_warnings
        else "passed"
    )
    result: dict[str, object] = {
        "schema": SCHEMA,
        "valid_pairs": valid_pairs,
        "invalid_pairs": invalid,
        "metrics": summaries,
        "efficiency_decision": efficiency_decision,
        "efficiency_warnings": efficiency_warnings,
        "efficiency_gaps": efficiency_gaps,
        "decision": decision,
        "next_pair_target": next_pairs,
        "thresholds": dict(THRESHOLDS),
    }
    if validity is not None:
        result["validity"] = validity
        result["validity_thresholds"] = dict(
            SKILL_DISCLOSURE_VALIDITY_THRESHOLDS
        )
    return result


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
    if requested_pairs not in CHECKPOINTS:
        errors.append("AB schedule requested pair count is not a qualification checkpoint")
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


def _require_clean_source(repo: Path, description: str) -> None:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    if completed.stdout.strip():
        raise RuntimeError(
            f"AB qualification requires a clean {description} so the "
            "recorded source commit identifies the tested source"
        )


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
            _require_clean_source(destination, "benchmark worktree")
            return
        raise RuntimeError(f"benchmark worktree already exists at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(destination), ref],
        cwd=repo,
        check=True,
    )


def _execute_dispatch_instruction(unresolved_failure: str) -> str:
    return (
        "openubmc-target-runtime MCP 调用入口已在当前基准会话注册。"
        "只有实际发出的 MCP tool call 才计入调用次数；如果尚未发出 MCP 调用，"
        "就等待对应入口完成解析后发出它，这不算失败或重试。"
        f"不得在实际调用前把入口尚未解析报告为{unresolved_failure}。"
    )


def _prompt(
    skill_path: Path, *, scenario: str = "observation", arm: str = "B"
) -> str:
    if scenario == "skill-disclosure":
        return "\n".join(
            (
                "这是一次真实 BMC 环境下的 openubmc-debug Skill 渐进披露配对基准。",
                "使用已安装的 $openubmc-debug，并按需读取直接链接的 references。",
                "只使用 openubmc-debug 与 openubmc-target-runtime.observe；不得使用其他 Skill、知识库、网络搜索、源码、日志或 shell。",
                "不要列出 MCP resources/templates，不要做工具发现；直接调用 openubmc-target-runtime.observe。",
                "openubmc-target-runtime.observe 已在当前基准会话注册。只有实际发出的 MCP tool call 才计入唯一一次调用；如果尚未发出 MCP 调用，就等待入口完成解析后发出它，这不算一次失败或重试。",
                f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
                "执行一次只读现场核验。唯一一次 observe 必须严格使用下面的完整 JSON 参数，不得增删字段、拆分调用或重试：",
                _skill_disclosure_observe_template(),
                "该请求使用 Runtime 默认自动策略和 live freshness；不得枚举其他对象或属性。",
                "报告原始值，并判断这些证据能否单独证明 ResourceId=0 异常，不得臆测源码语义。",
                "中文回答，不超过 500 字。",
            )
        ) + "\n"
    if scenario == "execute-source-only":
        if arm == "A":
            return "\n".join(
                (
                    "这是一次真实 BMC 环境下的 Runtime compatibility execute 配对资格基准。",
                    f"使用已安装的 {skill_path} 所定义的原生 Case Continuation 路径。",
                    "只允许使用 openubmc-debug 与 Gate 指定的 openubmc-developer；不得使用其他 Skill、知识库、网络搜索或 shell。",
                    "不要列出 MCP resources/templates，不要做工具发现。",
                    _execute_dispatch_instruction("工具不可用或调用失败"),
                    f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
                    "第一次调用 openubmc-target-runtime.workflow.advance：ip 为目标，intent=diagnose-and-fix，delivery_strategy=source-only，final_purpose=qualify Runtime source-only execution。",
                    "标准 content 的“尚未完成”只表示 Case 正在等待 phase_record，不是调用失败。case_id、顶层 revision 与 handoff_arguments.phase_record_contract 都必须从同一次 structured_content 记录为非空原值；不得在读取 structured_content 前报告这些字段缺失，不得重新开始 Case。",
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
                _execute_dispatch_instruction("工具不可用或 start 失败"),
                f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
                "第一次且仅第一次调用：kind=start，intent=diagnose-and-fix，delivery_strategy=source-only。",
                "读取每次工具结果的 structured_content，保存 run_id、gate.name、gate.owner、gate.gate_id、gate.gate_version 与 gate.schema_digest；标准 content 首部的同值 GateBinding 行可用于核对。绑定字段都必须是非空原值；不得再次 start，不得提交空字符串或尖括号占位符。",
                "如果 start 返回 diagnosis.acceptance Gate，先使用下面模板响应；evidence_ids 必须替换为当前 diagnostic_receipt.evidence 中全部 evidence_id 原值，随后从响应结果读取新的 developer.change Gate。若 start 已直接返回 developer.change Gate，则跳过本步：",
                _qualification_diagnosis_respond_template(),
                "Runtime 返回 developer.change Gate 后，不读写任何源码，提交固定基准 source receipt，参数必须严格采用下面的完整 JSON 模板，并把尖括号占位符替换为当前 structured_content 中对应的原值：",
                _qualification_respond_template(),
                "kind、run_id、gate_id、gate_version、schema_digest、response 都是 respond 参数的顶层字段；response 内只含 status、summary、payload，不得把任何 Gate binding 放入 response 或 payload。",
                "每个 Gate 只能响应一次；全程只能有一次 start 和一到两次 respond，不得省略 Gate binding，不得 poll、不得调用 resume、不得修改目标。",
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


def _legacy_v201_execute_prompt(arm: str) -> str:
    if arm == "A":
        return "\n".join(
            (
                "这是一次真实 BMC 环境下的 Runtime compatibility execute 配对资格基准。",
                "使用已安装的 <skill-path> 所定义的原生 Case Continuation 路径。",
                "只允许使用 openubmc-debug 与 Gate 指定的 openubmc-developer；不得使用其他 Skill、知识库、网络搜索或 shell。",
                "不要列出 MCP resources/templates，不要做工具发现。",
                "openubmc-target-runtime MCP 调用入口已在当前基准会话注册。只有实际发出的 MCP tool call 才计入调用次数；如果尚未发出 MCP 调用，就等待对应入口完成解析后发出它，这不算失败或重试。不得在实际调用前把入口尚未解析报告为工具不可用或调用失败。",
                f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
                "第一次调用 openubmc-target-runtime.workflow.advance：ip 为目标，intent=diagnose-and-fix，delivery_strategy=source-only，final_purpose=qualify Runtime source-only execution。",
                "标准 content 的“尚未完成”只表示 Case 正在等待 phase_record，不是调用失败。case_id、顶层 revision 与 handoff_arguments.phase_record_contract 都必须从同一次 structured_content 记录为非空原值；不得在读取 structured_content 前报告这些字段缺失，不得重新开始 Case。",
                "第二次调用 phase_record：参数必须是扁平 JSON 对象。展开 phase_record_contract，但 expected_revision 必须替换为 workflow.advance 结果的当前顶层 revision；再把 status=completed、source_revision=qualification-source、summary=qualification source-only receipt completed、authored_files=[src/qualification.lua]、verification_plan=[run qualification tests] 全部放在同一顶层。严禁创建 receipt 或 payload 嵌套字段。",
                "第三次调用 workflow.next，且只携带同一 case_id，直接推进到终态。",
                "不得读写源码、不得调用 mutation 工具、不得修改目标。",
                "最终中文回答必须包含原文：source-only Runtime Outcome completed。回答不超过 200 字。",
            )
        ) + "\n"
    if arm != "B":
        raise ValueError("legacy v2.0.1 execute prompt arm must be A or B")
    return "\n".join(
        (
            "这是一次真实 BMC 环境下的 Runtime execute 配对资格基准。",
            "使用已安装的 <skill-path> 所定义的 Agent Gateway 路径。",
            "只允许使用 openubmc-debug 与 Gate 指定的 openubmc-developer；不得使用其他 Skill、知识库、网络搜索或 shell。",
            "不要列出 MCP resources/templates，不要做工具发现；直接调用 openubmc-target-runtime.execute。",
            "openubmc-target-runtime MCP 调用入口已在当前基准会话注册。只有实际发出的 MCP tool call 才计入调用次数；如果尚未发出 MCP 调用，就等待对应入口完成解析后发出它，这不算失败或重试。不得在实际调用前把入口尚未解析报告为工具不可用或 start 失败。",
            f"目标 BMC：{BENCHMARK_TARGET}。凭据位于标准私有配置中，不得显示凭据值。",
            "第一次且仅第一次调用：kind=start，intent=diagnose-and-fix，delivery_strategy=source-only。",
            "读取 start 工具结果的 structured_content，保存 run_id、gate.gate_id、gate.gate_version 与 gate.schema_digest；标准 content 首部的同值 GateBinding 行可用于核对。四项都必须是非空原值；如尚未取得就继续读取当前工具结果，不得再次 start，不得提交空字符串或尖括号占位符。",
            "Runtime 返回 developer.change Gate 后，不读写任何源码，提交固定基准 receipt。第二次且仅第二次调用 execute，参数必须严格采用下面的完整 JSON 模板，并把尖括号占位符替换为 structured_content 中对应的原值：",
            '{"kind":"respond","run_id":"<structured_content.run_id>","gate_id":"<structured_content.gate.gate_id>","gate_version":<structured_content.gate.gate_version>,"schema_digest":"<structured_content.gate.schema_digest>","response":{"status":"completed","summary":"qualification source-only receipt completed","payload":{"source_revision":"qualification-source","authored_files":["src/qualification.lua"],"verification_plan":["run qualification tests"]}}}',
            "kind、run_id、gate_id、gate_version、schema_digest、response 都是 respond 参数的顶层字段；response 内只含 status、summary、payload，不得把任何 Gate binding 放入 response 或 payload。",
            "同一 Gate 只能响应一次；不得省略 Gate binding，不得 poll、不得调用 resume、不得修改目标。",
            "必须推进到终态，并在最终中文回答中包含原文：source-only Runtime Outcome completed。回答不超过 200 字。",
        )
    ) + "\n"


SCENARIOS = ("observation", "skill-disclosure", "execute-source-only")


def prompt_digest(scenario: str) -> str:
    if scenario not in SCENARIOS:
        raise ValueError(f"unsupported AB scenario: {scenario}")
    payload = {
        arm: _prompt(Path("<skill-path>"), scenario=scenario, arm=arm)
        for arm in ("A", "B")
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def qualification_prompt_contracts(
    scenario: str,
) -> dict[str, dict[str, str]]:
    current = {
        arm: _prompt(Path("<skill-path>"), scenario=scenario, arm=arm)
        for arm in ("A", "B")
    }
    contracts = {prompt_digest(scenario): current}
    if scenario == "execute-source-only":
        legacy = {
            arm: _legacy_v201_execute_prompt(arm) for arm in ("A", "B")
        }
        legacy_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                legacy,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if legacy_digest != LEGACY_V201_EXECUTE_PROMPT_DIGEST:
            raise RuntimeError("legacy v2.0.1 execute prompt contract drifted")
        contracts[LEGACY_V201_EXECUTE_PROMPT_DIGEST] = legacy
    return contracts


def _prompt_contract_allowed_for_source(
    prompt_contract_digest: object,
    *,
    scenario: str,
    candidate_commit: str,
    baseline_commit: str,
) -> bool:
    if not isinstance(prompt_contract_digest, str):
        return False
    if prompt_contract_digest == prompt_digest(scenario):
        return True
    return bool(
        scenario == "execute-source-only"
        and prompt_contract_digest == LEGACY_V201_EXECUTE_PROMPT_DIGEST
        and candidate_commit in LEGACY_V201_EXECUTE_SOURCE_COMMITS
        and baseline_commit == DEFAULT_BASELINE_REF
    )


QUALIFICATION_PROMPT_DIGEST = prompt_digest("execute-source-only")


@dataclass(frozen=True)
class RunConfig:
    arm: str
    source_root: Path
    interface_profile: str


def run_configs(
    scenario: str,
    baseline_root: Path,
    candidate_root: Path,
) -> dict[str, RunConfig]:
    return {
        "A": RunConfig(
            "A",
            baseline_root,
            "agent" if scenario == "skill-disclosure" else "",
        ),
        "B": RunConfig("B", candidate_root, "agent"),
    }


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


def codex_exec_command(
    args: argparse.Namespace,
    config: RunConfig,
    final_path: Path,
) -> list[str]:
    """Build the fixed Codex command used by either qualification arm."""
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
                + json.dumps(
                    str(
                        config.source_root
                        / "openubmc-debug"
                        / "scripts"
                        / "target_runtime_mcp.py"
                    )
                )
                + "]"
            ),
            "-c",
            'mcp_servers.openubmc-target-runtime.env_vars=["OPENUBMC_CREDENTIALS_FILE","OPENUBMC_DEBUG_CREDENTIALS_FILE","OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE","OPENUBMC_EVALUATION_TASK_ID","OPENUBMC_MCP_TASK_ID","OPENUBMC_MCP_SESSION_ID","OPENUBMC_MCP_CLIENT"]',
            "-c",
            "mcp_servers.openubmc-target-runtime.required=true",
            "-c",
            (
                "mcp_servers.openubmc-target-runtime.startup_timeout_sec="
                f"{MCP_STARTUP_TIMEOUT_SECONDS}"
            ),
            "-c",
            (
                "mcp_servers.openubmc-target-runtime.tool_timeout_sec="
                f"{MCP_TOOL_TIMEOUT_SECONDS}"
            ),
        )
    )
    return command


def _git_commit(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _require_pinned_sources(
    *,
    repo: Path,
    candidate_root: Path,
    baseline_root: Path,
    candidate_commit: str,
    baseline_commit: str,
    baseline_ref: str,
) -> None:
    _require_clean_source(repo, "candidate repository")
    _require_clean_source(candidate_root, "candidate worktree")
    _require_clean_source(baseline_root, "baseline worktree")
    if _git_commit(candidate_root, "HEAD") != candidate_commit:
        raise RuntimeError("AB candidate worktree drifted during qualification")
    if _git_commit(baseline_root, "HEAD") != baseline_commit:
        raise RuntimeError("AB baseline worktree drifted during qualification")
    if _git_commit(repo, "HEAD") != candidate_commit:
        raise RuntimeError("AB candidate source moved during qualification")
    if _git_commit(repo, baseline_ref) != baseline_commit:
        raise RuntimeError("AB baseline source moved during qualification")


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


def qualification_codex_identity(executable: str) -> dict[str, str]:
    selected = Path(executable).expanduser()
    if not selected.is_absolute():
        raise RuntimeError("qualification Codex executable must be absolute")
    try:
        resolved = selected.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("qualification Codex executable is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RuntimeError("qualification Codex executable is not executable")
    return {
        "codex_executable": str(resolved),
        "codex_sha256": _sha256(resolved),
    }


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _ssh_key_fingerprint(public_key: Path) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-lf", str(public_key), "-E", "sha256"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode or len(completed.stdout.split()) < 2:
        raise ValueError("cannot fingerprint AB attestation public key")
    return completed.stdout.split()[1]


@contextmanager
def _staged_private_key(private_key: Path, *, prefix: str):
    with tempfile.TemporaryDirectory(prefix=prefix) as raw:
        root = Path(raw)
        key = root / "private-key"
        key.write_bytes(private_key.read_bytes())
        key.chmod(0o600)
        yield root, key


def _derive_public_key(key: Path, *, root: Path) -> Path:
    public_key = root / "public-key.pub"
    public = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(key)],
        check=False,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if public.returncode or not public.stdout.strip():
        raise ValueError("cannot derive AB attestation public key")
    public_key.write_text(public.stdout.strip() + "\n", encoding="utf-8")
    return public_key


def _ssh_private_key_fingerprint(private_key: Path) -> str:
    with _staged_private_key(private_key, prefix="openubmc-ab-key-") as (root, key):
        public_key = _derive_public_key(key, root=root)
        return _ssh_key_fingerprint(public_key)


def attest_run_record(
    value: Mapping[str, object], *, private_key: Path
) -> dict[str, object]:
    run = dict(value)
    run.pop("attestation", None)
    with _staged_private_key(private_key, prefix="openubmc-ab-attest-") as (root, key):
        payload = root / "run.json"
        payload.write_bytes(_canonical_json_bytes(run))
        completed = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-q",
                "-f",
                str(key),
                "-n",
                RUN_ATTESTATION_NAMESPACE,
                str(payload),
            ],
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode:
            raise ValueError("cannot sign AB run evidence")
        public_key = _derive_public_key(key, root=root)
        run["attestation"] = {
            "schema": RUN_ATTESTATION_SCHEMA,
            "identity": RUN_ATTESTATION_IDENTITY,
            "namespace": RUN_ATTESTATION_NAMESPACE,
            "key_fingerprint": _ssh_key_fingerprint(public_key),
            "signature": base64.b64encode(
                Path(f"{payload}.sig").read_bytes()
            ).decode("ascii"),
        }
    return run


def _execution_identity(events: Iterable[Mapping[str, object]]) -> str:
    return _execution_identity_binding(events)[0]


def release_evidence(
    *,
    scenario: str,
    requested_pairs: int,
    candidate_source_commit: str,
    baseline_source_commit: str,
    model: str,
    metrics_path: Path,
    schedule_path: Path,
    run_evidence_path: Path,
    analysis: Mapping[str, object],
    environment: Mapping[str, object],
    codex_config: Iterable[str] = (),
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
        "benchmark": {
            "target": BENCHMARK_TARGET,
            "prompt_digest": prompt_digest(scenario),
            "codex_version": QUALIFICATION_CODEX_VERSION,
            "codex_config": list(codex_config),
        },
        "environment": environment_record,
        "environment_fingerprint": _fingerprint(environment_record),
        "thresholds": dict(THRESHOLDS),
        "samples": {
            "requested_pairs": requested_pairs,
            "valid_pairs": int(analysis.get("valid_pairs", 0) or 0),
            "invalid_pairs": list(analysis.get("invalid_pairs", [])),
            "decision": analysis.get("decision"),
            "efficiency_decision": analysis.get("efficiency_decision"),
            "efficiency_warnings": list(
                analysis.get("efficiency_warnings", [])
            ),
            "efficiency_gaps": list(analysis.get("efficiency_gaps", [])),
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
            "run_evidence": {
                "path": run_evidence_path.name,
                "sha256": _sha256(run_evidence_path),
                "size_bytes": run_evidence_path.stat().st_size,
            },
        },
    }
    if scenario == "skill-disclosure":
        evidence["validity_thresholds"] = dict(
            SKILL_DISCLOSURE_VALIDITY_THRESHOLDS
        )
        samples = evidence["samples"]
        assert isinstance(samples, dict)
        samples["validity"] = dict(_json_object(analysis.get("validity")))
    evidence["evidence_digest"] = _fingerprint(evidence)
    return evidence


def verify_summary(
    summary_path: Path,
    *,
    expected_source_commit: str,
    expected_baseline_commit: str = DEFAULT_BASELINE_REF,
    expected_scenario: str = "execute-source-only",
    attestation_public_key: Path | None = None,
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
    if not isinstance(invalid_pairs, list):
        errors.append("AB summary invalid pairs must be an array")
        invalid_pairs = []
    if expected_scenario == "skill-disclosure":
        validity = _json_object(summary.get("validity"))
        if validity.get("passed") is not True:
            errors.append("Skill disclosure validity gate did not pass")
        if (
            summary.get("validity_thresholds")
            != SKILL_DISCLOSURE_VALIDITY_THRESHOLDS
        ):
            errors.append("Skill disclosure validity thresholds do not match the contract")
    elif invalid_pairs:
        errors.append("AB summary contains invalid pairs")
    if summary.get("thresholds") != THRESHOLDS:
        errors.append("AB summary thresholds do not match the release contract")
    metric_summary = _json_object(summary.get("metrics"))
    expected_efficiency_gaps = [
        metric
        for metric in METRICS
        if not bool(_json_object(metric_summary.get(metric)).get("complete"))
    ]
    expected_efficiency_warnings = [
        metric
        for metric in METRICS
        if bool(_json_object(metric_summary.get(metric)).get("complete"))
        and not bool(_json_object(metric_summary.get(metric)).get("passed"))
    ]
    expected_efficiency_decision = (
        "incomplete"
        if expected_efficiency_gaps
        else "warning"
        if expected_efficiency_warnings
        else "passed"
    )
    if summary.get("efficiency_decision") != expected_efficiency_decision:
        errors.append("AB efficiency decision does not match the metric results")
    if summary.get("efficiency_warnings") != expected_efficiency_warnings:
        errors.append("AB efficiency warnings do not match the metric results")
    if summary.get("efficiency_gaps") != expected_efficiency_gaps:
        errors.append("AB efficiency gaps do not match the metric results")
    if expected_efficiency_gaps:
        errors.append("AB efficiency evidence is incomplete")

    evidence = _json_object(summary.get("release_evidence"))
    if evidence.get("scenario") != expected_scenario:
        errors.append("AB release evidence scenario does not match verification")
    source = _json_object(evidence.get("source"))
    if source.get("candidate_commit") != expected_source_commit:
        errors.append("AB candidate source commit does not match the release candidate")
    if source.get("baseline_commit") != expected_baseline_commit:
        errors.append(
            "AB baseline source commit does not match the qualification contract"
        )
    if source.get("candidate_commit") == source.get("baseline_commit"):
        errors.append("AB candidate source commit must differ from the baseline commit")
    samples = _json_object(evidence.get("samples"))
    if samples.get("valid_pairs") != valid_pairs or samples.get("invalid_pairs") != invalid_pairs:
        errors.append("AB release evidence sample counts do not match the summary")
    if samples.get("decision") != summary.get("decision"):
        errors.append("AB release evidence decision does not match the summary")
    if samples.get("efficiency_decision") != summary.get("efficiency_decision"):
        errors.append(
            "AB release evidence efficiency decision does not match the summary"
        )
    if samples.get("efficiency_warnings") != summary.get("efficiency_warnings"):
        errors.append(
            "AB release evidence efficiency warnings do not match the summary"
        )
    if samples.get("efficiency_gaps") != summary.get("efficiency_gaps"):
        errors.append(
            "AB release evidence efficiency gaps do not match the summary"
        )
    requested_pairs = samples.get("requested_pairs")
    if (
        isinstance(requested_pairs, int)
        and not isinstance(requested_pairs, bool)
        and requested_pairs >= CHECKPOINTS[-1]
    ):
        for metric in METRICS:
            p95_ratio = _json_object(metric_summary.get(metric)).get("p95_ratio")
            if (
                not isinstance(p95_ratio, (int, float))
                or isinstance(p95_ratio, bool)
                or not math.isfinite(float(p95_ratio))
                or float(p95_ratio) <= 0
            ):
                errors.append(f"AB terminal p95 ratio is missing or invalid: {metric}")
    if expected_scenario == "skill-disclosure":
        if samples.get("validity") != summary.get("validity"):
            errors.append("AB release evidence validity does not match the summary")
        if (
            evidence.get("validity_thresholds")
            != SKILL_DISCLOSURE_VALIDITY_THRESHOLDS
        ):
            errors.append("AB release evidence validity thresholds do not match the contract")
    if evidence.get("thresholds") != THRESHOLDS:
        errors.append("AB release evidence thresholds do not match the release contract")
    if evidence.get("model") != QUALIFICATION_MODEL:
        errors.append(
            "AB release evidence model does not match the qualification contract"
        )
    benchmark = _json_object(evidence.get("benchmark"))
    if benchmark.get("target") != BENCHMARK_TARGET:
        errors.append("AB benchmark target does not match the qualification contract")
    benchmark_prompt_digest = benchmark.get("prompt_digest")
    if not _prompt_contract_allowed_for_source(
        benchmark_prompt_digest,
        scenario=expected_scenario,
        candidate_commit=expected_source_commit,
        baseline_commit=expected_baseline_commit,
    ):
        errors.append("AB benchmark prompt does not match the qualification contract")
    if benchmark.get("codex_version") != QUALIFICATION_CODEX_VERSION:
        errors.append("AB Codex version does not match the qualification contract")
    if benchmark.get("codex_config") != list(QUALIFICATION_CODEX_CONFIG):
        errors.append("AB Codex config does not match the qualification contract")
    environment = _json_object(evidence.get("environment"))
    if environment.get("codex") != QUALIFICATION_CODEX_VERSION:
        errors.append(
            "AB executed Codex version does not match the qualification contract"
        )
    codex_executable = environment.get("codex_executable")
    codex_sha256 = environment.get("codex_sha256")
    if (
        not isinstance(codex_executable, str)
        or not Path(codex_executable).is_absolute()
        or not isinstance(codex_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", codex_sha256) is None
    ):
        errors.append("AB Codex executable identity is invalid")
    if evidence.get("environment_fingerprint") != _fingerprint(dict(environment)):
        errors.append("AB release evidence environment fingerprint is invalid")

    artifacts = _json_object(evidence.get("artifacts"))
    artifact_paths: dict[str, Path] = {}
    for name in ("all_metrics", "schedule", "run_evidence"):
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
    recomputed_metrics: list[dict[str, object]] | None = None
    run_evidence_value: object | None = None
    run_evidence_path = artifact_paths.get("run_evidence")
    if run_evidence_path is not None:
        try:
            run_evidence_value = json.loads(
                run_evidence_path.read_text(encoding="utf-8")
            )
            errors.extend(
                _run_source_binding_errors(
                    run_evidence_value,
                    expected_candidate_commit=expected_source_commit,
                    expected_baseline_commit=expected_baseline_commit,
                )
            )
            errors.extend(
                _run_prompt_binding_errors(
                    run_evidence_value,
                    expected_scenario=expected_scenario,
                    expected_prompt_digest=benchmark_prompt_digest,
                )
            )
            errors.extend(
                _run_environment_binding_errors(
                    run_evidence_value,
                    expected_fingerprint=evidence.get(
                        "environment_fingerprint"
                    ),
                )
            )
            if attestation_public_key is None:
                errors.append("AB run attestation public key is required")
            else:
                errors.extend(
                    _run_attestation_errors(
                        run_evidence_value,
                        public_key=attestation_public_key,
                    )
                )
            recomputed_metrics = metrics_from_run_evidence(run_evidence_value)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            errors.append(f"cannot recompute AB run metrics: {type(exc).__name__}")
    if raw_metrics is not None and recomputed_metrics is not None:
        if raw_metrics != recomputed_metrics:
            errors.append("AB raw metrics are not derived from the run evidence")
        try:
            recomputed = analyze(recomputed_metrics)
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            errors.append(f"cannot analyze AB run evidence: {type(exc).__name__}")
        else:
            analysis_fields = (
                "schema",
                "valid_pairs",
                "invalid_pairs",
                "metrics",
                "efficiency_decision",
                "efficiency_warnings",
                "efficiency_gaps",
                "decision",
                "next_pair_target",
                "thresholds",
                "validity",
                "validity_thresholds",
            )
            if any(
                summary.get(name) != recomputed.get(name)
                for name in analysis_fields
            ):
                errors.append("AB summary is not derived from the run evidence")
    schedule_path = artifact_paths.get("schedule")
    if schedule_path is not None and recomputed_metrics is not None:
        try:
            raw_schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read AB schedule: {type(exc).__name__}")
        else:
            if run_evidence_value is not None:
                errors.extend(
                    _run_qualification_contract_errors(
                        run_evidence_value,
                        expected_requested_pairs=samples.get("requested_pairs"),
                        expected_schedule_digest=_fingerprint(raw_schedule),
                    )
                )
            errors.extend(
                validate_schedule(
                    raw_schedule,
                    recomputed_metrics,
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
        "efficiency_decision": summary.get("efficiency_decision"),
        "efficiency_warnings": summary.get("efficiency_warnings", []),
        "efficiency_gaps": summary.get("efficiency_gaps", []),
    }


def run_benchmark(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    _require_clean_source(repo, "candidate repository")
    if args.model != QUALIFICATION_MODEL:
        raise RuntimeError(
            f"qualification model must be {QUALIFICATION_MODEL}"
        )
    if tuple(args.codex_config) != QUALIFICATION_CODEX_CONFIG:
        raise RuntimeError("qualification Codex config does not match the contract")
    codex_identity = qualification_codex_identity(args.codex)
    args.codex = codex_identity["codex_executable"]
    codex_version = _version([args.codex, "--version"])
    if codex_version != QUALIFICATION_CODEX_VERSION:
        raise RuntimeError(
            f"qualification Codex must be {QUALIFICATION_CODEX_VERSION}; "
            f"got {codex_version}"
        )
    attestation_private_key = args.attestation_private_key.expanduser().resolve()
    if not attestation_private_key.is_file():
        raise RuntimeError("AB attestation private key is unavailable")
    attestation_public_key = args.attestation_public_key.expanduser().resolve()
    if not attestation_public_key.is_file():
        raise RuntimeError("AB attestation public key is unavailable")
    try:
        private_fingerprint = _ssh_private_key_fingerprint(attestation_private_key)
        public_fingerprint = _ssh_key_fingerprint(attestation_public_key)
    except (OSError, ValueError) as exc:
        raise RuntimeError("AB attestation key is invalid") from exc
    if private_fingerprint != public_fingerprint:
        raise RuntimeError("AB attestation private key does not match the trusted public key")
    work_root = args.work_root.resolve()
    candidate_source_commit = _git_commit(repo, "HEAD")
    baseline_source_commit = _git_commit(repo, args.baseline_ref)
    baseline_root = work_root / "variants" / f"baseline-{baseline_source_commit[:12]}"
    candidate_root = work_root / "variants" / f"candidate-{candidate_source_commit[:12]}"
    _prepare_worktree(repo, baseline_root, baseline_source_commit)
    _prepare_worktree(repo, candidate_root, candidate_source_commit)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output.resolve() if args.output else work_root / f"results-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    schedule = balanced_schedule(args.pairs, seed=args.seed)
    (output / "schedule.json").write_text(
        json.dumps(schedule, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    qualification_contract = {
        "requested_pairs": args.pairs,
        "schedule_digest": _fingerprint(schedule),
    }
    configs = run_configs(args.scenario, baseline_root, candidate_root)
    environment = os.environ.copy()
    environment["OPENUBMC_CREDENTIALS_FILE"] = str(args.credentials)
    environment["OPENUBMC_DEBUG_CREDENTIALS_FILE"] = str(args.credentials)
    environment_record = {
        "python": platform.python_version(),
        "node": _version(["node", "--version"]),
        "codex": codex_version,
        **codex_identity,
        "platform": platform.platform(),
    }
    environment_fingerprint = _fingerprint(environment_record)
    metrics: list[dict[str, object]] = []
    run_evidence: dict[str, object] = {
        "schema": RUN_EVIDENCE_SCHEMA,
        "source": {
            "candidate_commit": candidate_source_commit,
            "baseline_commit": baseline_source_commit,
        },
        "runs": [],
    }
    run_evidence_path = output / "run_evidence.json"
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
            command = codex_exec_command(args, config, final_path)
            runner_execution_id = str(uuid.uuid4())
            run_env = dict(environment)
            run_env["HOME"] = str(home)
            run_env["CODEX_HOME"] = str(home / ".codex")
            run_env["OPENUBMC_EVALUATION_TASK_ID"] = runner_execution_id
            run_env["OPENUBMC_MCP_TASK_ID"] = runner_execution_id
            run_env["OPENUBMC_MCP_SESSION_ID"] = runner_execution_id
            run_env["OPENUBMC_MCP_CLIENT"] = "codex-qualification"
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
            events = [
                {
                    "type": "runner.started",
                    "execution_id": runner_execution_id,
                },
                *_read_events(events_path),
            ]
            final = final_path.read_text(encoding="utf-8") if final_path.exists() else ""
            record = RunEvidenceRecord.capture(
                arm=arm,
                pair=pair,
                order=order,
                scenario=args.scenario,
                events=events,
                final=final,
                exit_code=exit_code,
                duration_seconds=duration,
            )
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        record.events[-1],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            raw_runs = run_evidence["runs"]
            assert isinstance(raw_runs, list)
            run_mapping = record.to_mapping()
            run_mapping["source_commit"] = (
                candidate_source_commit if arm == "B" else baseline_source_commit
            )
            run_mapping["execution_id"] = _execution_identity(events)
            run_mapping["prompt"] = prompt
            run_mapping["prompt_sha256"] = _text_sha256(prompt)
            run_mapping["environment_fingerprint"] = environment_fingerprint
            run_mapping["qualification_contract"] = qualification_contract
            raw_runs.append(
                attest_run_record(
                    run_mapping,
                    private_key=attestation_private_key,
                )
            )
            run_evidence_path.write_text(
                json.dumps(run_evidence, ensure_ascii=False, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            metric = record.metric()
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
    _require_pinned_sources(
        repo=repo,
        candidate_root=candidate_root,
        baseline_root=baseline_root,
        candidate_commit=candidate_source_commit,
        baseline_commit=baseline_source_commit,
        baseline_ref=args.baseline_ref,
    )
    metrics_path = output / "all_metrics.json"
    schedule_path = output / "schedule.json"
    summary["release_evidence"] = release_evidence(
        scenario=args.scenario,
        requested_pairs=args.pairs,
        candidate_source_commit=candidate_source_commit,
        baseline_source_commit=baseline_source_commit,
        model=args.model,
        codex_config=args.codex_config,
        metrics_path=metrics_path,
        schedule_path=schedule_path,
        run_evidence_path=run_evidence_path,
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
    verify_parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    verify_parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="execute-source-only",
    )
    verify_parser.add_argument("--repo", type=Path, default=Path.cwd())
    verify_parser.add_argument(
        "--attestation-public-key",
        type=Path,
        required=True,
    )
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--repo", type=Path, default=Path.cwd())
    run_parser.add_argument("--work-root", type=Path, required=True)
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--baseline-ref", default=DEFAULT_BASELINE_REF)
    run_parser.add_argument("--pairs", type=int, choices=CHECKPOINTS, default=10)
    run_parser.add_argument("--seed", type=int, default=20260819)
    run_parser.add_argument("--credentials", type=Path, required=True)
    run_parser.add_argument(
        "--attestation-private-key",
        type=Path,
        required=True,
    )
    run_parser.add_argument(
        "--attestation-public-key",
        type=Path,
        required=True,
    )
    run_parser.add_argument("--codex", default="codex")
    run_parser.add_argument("--codex-cwd", type=Path, default=Path("/home/workspace"))
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--codex-config", action="append", default=[])
    run_parser.add_argument("--pause-seconds", type=float, default=5)
    run_parser.add_argument("--only-arm", choices=("A", "B"))
    run_parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
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
        repo = args.repo.resolve()
        expected = _git_commit(repo, args.source_ref)
        expected_baseline = _git_commit(repo, args.baseline_ref)
        verification = verify_summary(
            args.summary.expanduser().absolute(),
            expected_source_commit=expected,
            expected_baseline_commit=expected_baseline,
            expected_scenario=args.scenario,
            attestation_public_key=args.attestation_public_key.expanduser().absolute(),
        )
        print(json.dumps(verification, ensure_ascii=False, indent=2))
        return 0 if verification["promotable"] else 1
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
