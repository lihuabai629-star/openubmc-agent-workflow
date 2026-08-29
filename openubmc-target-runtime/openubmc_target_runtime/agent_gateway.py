"""Small semantic Agent interface over the shared openUBMC Runtime Core."""

from __future__ import annotations

from collections.abc import Mapping
import json

from .catalog import OperationDescriptor
from .capabilities import CAPABILITY_ALIASES, CAPABILITY_STATES
from .contracts import RUNTIME_API_VERSION
from .diagnostic_receipt import (
    DiagnosticCoverage,
    DiagnosticReceipt,
    DiagnosticStatus,
)
from .semantic_runtime import (
    AgentPreflightError,
    AgentGatewayError,
    EXECUTE_ACTION_FIELD_TYPES,
    EXECUTE_ACTION_FIELDS,
    EXECUTE_ACTION_REQUIRED_FIELDS,
    GATE_SCHEMA_PROJECTION_TARGET_BYTES,
    GatePreflightError,
    ObservationQuery,
    ObservationRef,
    PreflightDetail,
    PreflightReason,
    RunTurn,
    ScopeContract,
    ScopeViolation,
    SelectorContract,
    SemanticRuntimePort,
    decode_run_command,
    fingerprint,
    is_safe_runtime_id,
    is_sha256_digest,
)
from .observation import observation_consistency


AGENT_GATEWAY_SCHEMA = f"{RUNTIME_API_VERSION}/agent-gateway-v1"
OBSERVATION_RECEIPT_SCHEMA = f"{AGENT_GATEWAY_SCHEMA}/observation-receipt"
TURN_SCHEMA = f"{AGENT_GATEWAY_SCHEMA}/turn"
OBSERVATION_PROJECTION_TARGET_BYTES = 4 * 1024
# Compatibility name for callers that report the historical projection target.
# ObservationReceipts may exceed it; it is not a completeness or control boundary.
OBSERVATION_MAX_BYTES = OBSERVATION_PROJECTION_TARGET_BYTES
TURN_PROJECTION_TARGET_BYTES = 8 * 1024
# Compatibility name for callers that report the historical projection target.
# Runtime-owned Turn semantics may exceed it; it is not a control-flow maximum.
TURN_MAX_BYTES = TURN_PROJECTION_TARGET_BYTES
TOOLS_LIST_MAX_BYTES = 8 * 1024
EXECUTE_TEXT_PROJECTION_TARGET_BYTES = 4 * 1024


def agent_projection_policy() -> dict[str, object]:
    """Describe display targets without granting them control-flow authority."""

    return {
        "budget_mode": "soft-display-target",
        "observation_receipt_target_bytes": OBSERVATION_PROJECTION_TARGET_BYTES,
        "gate_schema_target_bytes": GATE_SCHEMA_PROJECTION_TARGET_BYTES,
        "turn_target_bytes": TURN_PROJECTION_TARGET_BYTES,
        "target_exceeded_behavior": "preserve-runtime-semantics",
        "manual_narrowing_required_on_target_exceeded": False,
        "projection_budget_blocker": False,
    }


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return fingerprint(value)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _observe_preflight_example(detail: PreflightDetail) -> dict[str, object]:
    if ".names" in detail.field or detail.reason == PreflightReason.UNSUPPORTED_CAPABILITY:
        return {
            "target": "<BMC IP>",
            "selectors": [
                {
                    "id": "capabilities",
                    "kind": "capability",
                    "names": ["ssh", "mdbctl"],
                }
            ],
        }
    return {
        "target": "<BMC IP>",
        "selectors": [
            {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]}
        ],
        "freshness": {"mode": "live", "max_age_seconds": 0},
        "deadline": 180,
    }


def _execute_action_example(detail: PreflightDetail) -> dict[str, object]:
    context = detail.context
    if detail.reason in {
        PreflightReason.ARTIFACT_REQUIRED,
        PreflightReason.ARTIFACT_BINDING,
    }:
        canonical_artifact_ref: dict[str, object] = {
            "handle": "<absolute artifact path>",
            "digest": "sha256:" + "0" * 64,
            "kind": context.artifact_kind or "<Gate artifact kind>",
            "size": 0,
            "provenance": "openubmc-build",
            "retention_hint": "run-lifetime",
            "target": context.target or "<current Run target>",
            "run_id": context.run_id or "<current Run ID>",
        }
        if context.version_required:
            canonical_artifact_ref["version"] = "<artifact version>"
        response = dict(context.response)
        payload = dict(_mapping(response.get("payload")))
        original_ref = payload.get("artifact_ref")
        artifact_ref = (
            dict(original_ref)
            if isinstance(original_ref, Mapping)
            else dict(canonical_artifact_ref)
        )
        for binding_field in ("target", "run_id"):
            binding_value = artifact_ref.get(binding_field)
            if (
                detail.field.endswith(f".{binding_field}")
                or not isinstance(binding_value, str)
                or not binding_value.strip()
            ):
                artifact_ref[binding_field] = canonical_artifact_ref[binding_field]
        payload["artifact_ref"] = artifact_ref
        payload_examples: dict[str, object] = {
            "source_revision": "<built source revision>",
            "authored_files": [],
            "verification_plan": [],
            "remote_path": "<remote path>",
            "restart_scope": "<restart scope>",
        }
        for field in context.required_payload_fields:
            if field != "artifact_ref" and field not in payload:
                payload[field] = payload_examples.get(
                    field, f"<Gate-required {field}>"
                )
        response["status"] = response.get("status") or "completed"
        response["summary"] = response.get("summary") or "artifact produced"
        response["payload"] = payload
        example = {
            "kind": "respond",
            "run_id": context.run_id or "<current Run ID>",
            "gate_id": context.gate_id or "<current Gate ID>",
            "gate_version": context.gate_version or 1,
            "schema_digest": context.schema_digest or "<current Gate schema digest>",
            "response": response,
        }
        if context.submission_id:
            example["submission_id"] = context.submission_id
        return example

    kind = context.action_kind or "resume"
    run_id = context.run_id or "<current Run ID>"
    if detail.reason == PreflightReason.RECONCILE_PRECONDITION:
        return {"kind": "resume", "run_id": run_id}
    if kind == "start":
        example: dict[str, object] = {"kind": "start"}
        if context.target or not context.targets:
            example["target"] = context.target or "<BMC IP>"
        if context.targets:
            example["targets"] = [dict(item) for item in context.targets]
        example["intent"] = context.intent or "diagnosis-only"
        if context.purpose:
            example["purpose"] = context.purpose
        if context.delivery_strategy:
            example["delivery_strategy"] = context.delivery_strategy
        if context.observation_ref:
            example["observation_ref"] = dict(context.observation_ref)
        if context.entry_operation:
            example["entry_operation"] = context.entry_operation
            example["entry_arguments"] = dict(context.entry_arguments)
        if detail.reason == PreflightReason.DEADLINE:
            example["deadline"] = 120
        return example
    if kind == "respond":
        example = {
            "kind": "respond",
            "run_id": run_id,
            "gate_id": context.gate_id or "<current Gate ID>",
            "gate_version": context.gate_version or 1,
            "schema_digest": context.schema_digest
            or "<current Gate schema digest>",
            "response": dict(context.response)
            if context.response
            else {
                "status": "completed",
                "summary": "<bounded summary>",
                "payload": {},
            },
        }
        if context.submission_id or detail.field == "submission_id":
            example["submission_id"] = (
                context.submission_id or "<new submission identity>"
            )
    elif kind == "control":
        example = {
            "kind": "control",
            "run_id": run_id,
            "command": context.command or "reconcile",
        }
        if context.command == "cancel":
            if context.incident_id:
                example["incident_id"] = context.incident_id
            elif context.gate_id:
                example["gate_id"] = context.gate_id
                example["gate_version"] = context.gate_version or 1
                example["schema_digest"] = context.schema_digest
                if context.submission_id:
                    example["submission_id"] = context.submission_id
    else:
        example = {"kind": "resume", "run_id": run_id}
    if detail.reason == PreflightReason.DEADLINE:
        example["deadline"] = 120
    return example


def _preflight_guidance(
    operation: str,
    detail: PreflightDetail,
) -> tuple[dict[str, object], str]:
    if operation == "observe":
        actions = {
            PreflightReason.UNSUPPORTED_CAPABILITY: (
                "retry observe with the corrected canonical capability name"
            ),
            PreflightReason.UNSUPPORTED_SELECTOR_KIND: (
                "retry observe with capability or mdb selector kind"
            ),
            PreflightReason.MDB_GRAMMAR: (
                "retry observe with the corrected read-only mdbctl grammar"
            ),
            PreflightReason.FRESHNESS_MODE: (
                "retry observe with freshness.mode set to live"
            ),
            PreflightReason.LIVE_MAX_AGE: (
                "retry observe with freshness.max_age_seconds set to 0"
            ),
            PreflightReason.DEADLINE: (
                "retry observe with a positive numeric deadline"
            ),
        }
        return (
            _observe_preflight_example(detail),
            actions.get(
                detail.reason,
                f"correct {detail.field} to satisfy the reported contract and retry observe",
            ),
        )
    actions = {
        PreflightReason.RUN_ID_REQUIRED: (
            "copy run_id from the current Turn and retry execute"
        ),
        PreflightReason.DEADLINE: (
            "retry execute with deadline at or below 120 seconds"
        ),
        PreflightReason.RECONCILE_PRECONDITION: (
            "use the current Turn next_action; reconcile only after mutation_outcome_unknown"
        ),
        PreflightReason.GATE_BINDING: (
            "retry execute with the projected GateBinding, response, and submission identity"
        ),
        PreflightReason.ARTIFACT_REQUIRED: (
            "supply the Gate-required ArtifactRef bound to this Run and target, then retry respond"
        ),
        PreflightReason.ARTIFACT_BINDING: (
            "copy the complete ArtifactRef from the current build result, bind it to this Run and target, then retry respond"
        ),
    }
    if detail.reason == PreflightReason.RUNTIME_OWNED_FIELD:
        next_action = "remove Runtime-owned fields and retry execute"
    else:
        next_action = actions.get(
            detail.reason,
            f"correct {detail.field} to satisfy the reported contract and retry execute",
        )
    return _execute_action_example(detail), next_action


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _bounded_text(value: object, max_bytes: int) -> str:
    text = _text(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def _bounded_summary_value(value: object, *, limit: int = 480) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = rendered.encode("utf-8")
    if len(encoded) <= limit:
        return rendered
    return encoded[: limit - 3].decode("utf-8", errors="ignore") + "..."


def _diagnostic_result_text(value: object) -> str:
    result = _mapping(value)
    selected = result.get("value")
    selected_mapping = _mapping(selected)
    summaries = selected_mapping.get("summary", [])
    if isinstance(summaries, list) and summaries:
        samples: list[str] = []
        for raw_sample in summaries[:4]:
            sample = _mapping(raw_sample)
            path = str(sample.get("path", "value"))
            samples.append(
                f"{path}={_bounded_summary_value(sample.get('value'), limit=160)}"
            )
        rendered = "; ".join(samples)
    else:
        rendered = _bounded_summary_value(selected)
    return (
        f"result[{result.get('result_id', 'diagnosis')}] "
        f"status={result.get('status', 'not_checked')} "
        f"kind={result.get('kind', 'diagnosis')} "
        f"request={result.get('request', '')} value={rendered}"
    )


def _diagnostic_agent_acceptance(receipt: Mapping[str, object]) -> str:
    coverage = DiagnosticCoverage.from_public_dict(
        _mapping(receipt.get("coverage"))
    )
    return DiagnosticReceipt.agent_acceptance_for(
        DiagnosticStatus.parse(receipt.get("status")),
        coverage,
    ).value


def render_execute_turn_text(
    value: Mapping[str, object],
    *,
    heading: str | None = None,
) -> str:
    """Render the bounded standard-content projection of an execute Turn."""

    state = str(value.get("state", "unknown"))
    fixed_lines = [
        _bounded_text(
            heading if heading is not None else f"openUBMC 工作流状态：{state}。",
            512,
        )
    ]
    gate = _mapping(value.get("gate"))
    if gate:
        fixed_lines.append(
            "GateBinding "
            f"run_id={_bounded_text(value.get('run_id'), 128)} "
            f"gate_id={_bounded_text(gate.get('gate_id'), 128)} "
            f"gate_version={_bounded_text(gate.get('gate_version'), 32)} "
            f"schema_digest={_bounded_text(gate.get('schema_digest'), 128)}."
        )
    receipt = _mapping(value.get("diagnostic_receipt"))
    if not receipt:
        return "\n".join(fixed_lines)
    coverage = _mapping(receipt.get("coverage"))
    freshness = _mapping(receipt.get("freshness"))
    requested = coverage.get("requested", 0)
    evaluable = coverage.get("evaluable", 0)
    visible_evaluable = coverage.get("visible_evaluable", evaluable)
    visible_unavailable = coverage.get(
        "visible_unavailable",
        coverage.get("unavailable", 0),
    )
    visible_not_checked = coverage.get(
        "visible_not_checked",
        coverage.get("not_checked", 0),
    )
    fixed_lines.append(
        "DiagnosticReceipt "
        f"status={receipt.get('status', 'blocked')} "
        f"agent_acceptance={_diagnostic_agent_acceptance(receipt)} "
        f"source_coverage={evaluable}/{requested} "
        f"source_unavailable={coverage.get('unavailable', 0)} "
        f"source_not_checked={coverage.get('not_checked', 0)} "
        f"visible={visible_evaluable}/{requested} "
        f"visible_unavailable={visible_unavailable} "
        f"visible_not_checked={visible_not_checked} "
        f"complete={str(bool(coverage.get('complete'))).lower()} "
        f"freshness={freshness.get('status', 'unknown')} "
        f"truncated={str(bool(receipt.get('truncated'))).lower()} "
        "content_complete="
        f"{str(bool(receipt.get('content_complete'))).lower()}."
    )
    capabilities = receipt.get("capabilities", {})
    if isinstance(capabilities, Mapping) and capabilities:
        fixed_lines.append(
            "capabilities: "
            + ", ".join(
                f"{_bounded_text(name, 48)}={_bounded_text(status, 32)}"
                for name, status in list(capabilities.items())[:8]
            )
        )
    gaps = receipt.get("gaps", [])
    if isinstance(gaps, list) and gaps:
        fixed_lines.append(
            "diagnostic_gaps: "
            + ", ".join(_bounded_text(gap, 128) for gap in gaps[:8])
        )
    evidence_ids: list[str] = []
    evidence = receipt.get("evidence", [])
    if isinstance(evidence, list):
        evidence_ids.extend(
            str(evidence_id)
            for item in evidence
            if isinstance(item, Mapping)
            and (evidence_id := item.get("evidence_id"))
        )
    raw_results = receipt.get("results", [])
    results = raw_results if isinstance(raw_results, list) else []
    for result in results:
        result_evidence = _mapping(result).get("evidence_ids", [])
        if isinstance(result_evidence, list):
            evidence_ids.extend(str(item) for item in result_evidence if item)
    unique_evidence_ids = list(dict.fromkeys(evidence_ids))[:8]
    if unique_evidence_ids:
        fixed_lines.append(
            "evidence_ids: "
            + ", ".join(_bounded_text(item, 128) for item in unique_evidence_ids)
        )
    previews = [
        _diagnostic_result_text(result)
        for result in results[:8]
        if isinstance(result, Mapping)
    ]
    for shown in range(len(previews), -1, -1):
        compacted = shown < len(results)
        lines = [*fixed_lines]
        if compacted:
            lines.append("text_projection_compacted=true")
        lines.append(f"results_shown={shown}/{len(results)}")
        lines.extend(previews[:shown])
        rendered = "\n".join(lines)
        if len(rendered.encode("utf-8")) <= EXECUTE_TEXT_PROJECTION_TARGET_BYTES:
            return rendered
    return "\n".join(
        [*fixed_lines, "text_projection_compacted=true", f"results_shown=0/{len(results)}"]
    )


def _compact_value(
    value: object,
    *,
    depth: int = 0,
    max_depth: int = 4,
    max_items: int = 16,
    max_string: int = 512,
) -> object:
    if depth >= max_depth:
        if isinstance(value, (Mapping, list, tuple)):
            return "<compacted>"
    if isinstance(value, Mapping):
        return {
            str(key): _compact_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for key, item in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple)):
        return [
            _compact_value(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_string=max_string,
            )
            for item in list(value)[:max_items]
        ]
    if isinstance(value, str) and len(value.encode("utf-8")) > max_string:
        return value.encode("utf-8")[: max_string - 3].decode(
            "utf-8", errors="ignore"
        ) + "..."
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _project_preflight_binding(field: str, value: object) -> object:
    if field == "gate_version":
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else 1
        )
    if not isinstance(value, str):
        return value
    placeholders = {
        "run_id": "<current Run ID>",
        "gate_id": "<current Gate ID>",
        "schema_digest": "<current Gate schema digest>",
        "submission_id": "<new submission identity>",
        "incident_id": "<current Incident ID>",
    }
    if field in placeholders:
        if field == "schema_digest":
            valid = is_sha256_digest(value)
        else:
            valid = is_safe_runtime_id(value)
        return value if valid else placeholders[field]
    if len(value.encode("utf-8")) <= 128:
        return value
    expandable_placeholders = {
        "command": "<control command>",
        "kind": "<Action kind>",
    }
    return expandable_placeholders.get(field, _bounded_text(value, 128))


def _project_preflight_example(
    example: Mapping[str, object],
) -> dict[str, object]:
    binding_fields = frozenset(
        {
            "kind",
            "run_id",
            "gate_id",
            "gate_version",
            "schema_digest",
            "submission_id",
            "incident_id",
            "command",
            "deadline",
        }
    )
    projected = dict(example)
    for field, value in tuple(projected.items()):
        if field in binding_fields:
            projected[field] = _project_preflight_binding(field, value)
        elif field == "target":
            projected[field] = (
                value
                if isinstance(value, str)
                and len(value.encode("utf-8")) <= 512
                else "<BMC IP>"
            )
        elif field in {
            "targets",
            "intent",
            "purpose",
            "delivery_strategy",
            "observation_ref",
            "entry_operation",
            "entry_arguments",
        }:
            projected[field] = value
        elif field == "response":
            projected[field] = dict(_mapping(value))
        else:
            projected[field] = _compact_value(
                value,
                max_depth=3,
                max_items=8,
                max_string=128,
            )
    return projected


def _compact_observation_results(value: object) -> dict[str, object]:
    projected: dict[str, object] = {}
    for selector_id, raw_result in _mapping(value).items():
        result = dict(_mapping(raw_result))
        raw_values = result.get("values", [])
        if isinstance(raw_values, list):
            result["values"] = [
                {
                    **{
                        str(key): item
                        for key, item in _mapping(raw_item).items()
                        if key != "value"
                    },
                    **(
                        {
                            "value": _compact_value(
                                _mapping(raw_item).get("value"),
                                max_depth=8,
                                max_items=32,
                                max_string=192,
                            )
                        }
                        if "value" in _mapping(raw_item)
                        else {}
                    ),
                }
                for raw_item in raw_values
            ]
        projected[str(selector_id)] = result
    return projected


def _projection_telemetry(
    *,
    compacted: bool,
    target_exceeded: bool,
) -> dict[str, bool]:
    return {
        "content_compacted": compacted,
        "projection_compacted": compacted,
        "projection_target_exceeded": target_exceeded,
        "manual_narrowing_required": False,
        "budget_blocker": False,
    }


class CostGovernor:
    """Enforce model-visible result budgets without exposing raw Evidence."""

    @staticmethod
    def _turn_gate(value: object) -> dict[str, object] | None:
        gate = _mapping(value)
        if not gate:
            return None
        return dict(gate)

    @staticmethod
    def _gate_projection_target_exceeded(value: object) -> bool:
        gate = _mapping(value)
        return bool(
            _text(gate.get("kind")) == "phase"
            and len(_json_bytes(_mapping(gate.get("input_schema"))))
            > GATE_SCHEMA_PROJECTION_TARGET_BYTES
        )

    @staticmethod
    def _turn_diagnostic_receipt(value: object) -> dict[str, object] | None:
        receipt = _mapping(value)
        if not receipt:
            return None
        projected = (
            DiagnosticReceipt.from_public_dict(receipt)
            .compacted_for_agent()
            .to_public_dict()
        )
        projected["agent_acceptance"] = _diagnostic_agent_acceptance(projected)
        return projected

    @staticmethod
    def observation(document: Mapping[str, object]) -> dict[str, object]:
        result = dict(document)
        if len(_json_bytes(result)) <= OBSERVATION_PROJECTION_TARGET_BYTES:
            return result
        compacted = dict(result)
        projected_results = _compact_observation_results(
            result.get("results", {})
        )
        results_compacted = projected_results != result.get("results", {})
        compacted.update(
            _projection_telemetry(
                compacted=results_compacted,
                target_exceeded=False,
            )
        )
        compacted["projection_truncated"] = results_compacted
        compacted["results"] = projected_results
        compacted["projection_target_exceeded"] = (
            len(_json_bytes(compacted)) > OBSERVATION_PROJECTION_TARGET_BYTES
        )
        return compacted

    @staticmethod
    def turn(document: Mapping[str, object]) -> dict[str, object]:
        result = dict(document)
        if "diagnostic_receipt" in result:
            receipt = dict(_mapping(result.get("diagnostic_receipt")))
            receipt["agent_acceptance"] = _diagnostic_agent_acceptance(receipt)
            result["diagnostic_receipt"] = receipt
        runtime_gate = CostGovernor._turn_gate(document.get("gate"))
        gate_projection_target_exceeded = (
            CostGovernor._gate_projection_target_exceeded(runtime_gate)
        )
        if gate_projection_target_exceeded:
            result["gate_projection_target_exceeded"] = True
            result["manual_narrowing_required"] = False
            result["budget_blocker"] = False
        if len(_json_bytes(result)) <= TURN_MAX_BYTES:
            return result
        result.update(
            _projection_telemetry(compacted=True, target_exceeded=False)
        )
        result["facts"] = _compact_value(
            result.get("facts", []), max_depth=3, max_items=16, max_string=256
        )
        if "diagnostic_receipt" in result:
            result["diagnostic_receipt"] = (
                CostGovernor._turn_diagnostic_receipt(
                    result.get("diagnostic_receipt")
                )
            )
        if len(_json_bytes(result)) <= TURN_MAX_BYTES:
            return result
        runtime_incident = document.get("incident")
        runtime_outcome = document.get("outcome")
        fallback: dict[str, object] = {
            "schema": TURN_SCHEMA,
            "run_id": _bounded_text(result.get("run_id"), 512),
            "state": _bounded_text(result.get("state") or "blocked", 64),
            "gate": runtime_gate,
            "incident": runtime_incident,
            "facts": [],
            "gaps": [
                _bounded_text(item, 256)
                for item in (
                    result.get("gaps", [])
                    if isinstance(result.get("gaps"), list)
                    else []
                )[:8]
            ],
            "outcome": runtime_outcome,
            "next": result.get("next"),
            "next_action": document.get("next_action"),
            **_projection_telemetry(compacted=True, target_exceeded=False),
        }
        if gate_projection_target_exceeded:
            fallback["gate_projection_target_exceeded"] = True
        if "observation_ref" in result:
            fallback["observation_ref"] = document.get("observation_ref")
        if "outcome_recorded" in result:
            fallback["outcome_recorded"] = bool(result.get("outcome_recorded"))
        if "response_required" in result:
            fallback["response_required"] = bool(result.get("response_required"))
        if "progress" in result:
            fallback["progress"] = dict(_mapping(result.get("progress")))
        if "diagnostic_receipt" in result:
            fallback["diagnostic_receipt"] = (
                CostGovernor._turn_diagnostic_receipt(
                    result.get("diagnostic_receipt")
                )
            )
        if len(_json_bytes(fallback)) <= TURN_MAX_BYTES:
            return fallback
        fallback["projection_target_exceeded"] = True
        return fallback


class ResultProjector:
    """Project Runtime implementation details into Agent semantic receipts."""

    @staticmethod
    def run_facts(
        projection: Mapping[str, object],
    ) -> tuple[Mapping[str, object], ...]:
        """Select and bound the Agent-visible facts for one semantic Turn."""

        cycle_id = _text(projection.get("workflow_cycle_id") or "cycle-1")
        facts: list[dict[str, object]] = []
        operations = projection.get("operations", [])
        if isinstance(operations, list):
            for operation in operations:
                if not isinstance(operation, Mapping):
                    continue
                status = _text(operation.get("status"))
                if status not in {"completed", "succeeded", "verified"}:
                    continue
                operation_name = _text(operation.get("operation"))
                if not operation_name or operation_name in {
                    "phase_record",
                    "workflow.advance",
                    "workflow.next",
                }:
                    continue
                operation_cycle = _text(operation.get("workflow_cycle_id"))
                if operation_cycle and operation_cycle != cycle_id:
                    continue
                fact: dict[str, object] = {
                    "kind": "operation",
                    "name": operation_name,
                    "status": status,
                    "summary": _text(operation.get("summary")),
                }
                evidence_ids = operation.get("evidence_ids", [])
                if isinstance(evidence_ids, list) and evidence_ids:
                    fact["evidence_ids"] = [
                        _text(item) for item in evidence_ids[:8] if _text(item)
                    ]
                target_epoch = operation.get("target_epoch")
                if isinstance(target_epoch, int) and not isinstance(
                    target_epoch, bool
                ):
                    fact["target_epoch"] = target_epoch
                facts.append(fact)
        phases = projection.get("phase_records", [])
        if isinstance(phases, list):
            for phase in phases:
                if (
                    not isinstance(phase, Mapping)
                    or _text(phase.get("status")) != "completed"
                    or (
                        _text(phase.get("workflow_cycle_id"))
                        and _text(phase.get("workflow_cycle_id")) != cycle_id
                    )
                ):
                    continue
                fact = {
                    "kind": "phase",
                    "name": _text(phase.get("phase_type")),
                    "status": "completed",
                    "summary": _text(phase.get("summary")),
                }
                for name in (
                    "source_revision",
                    "artifact_sha256",
                    "product_version",
                ):
                    value = _text(phase.get(name))
                    if value:
                        fact[name] = value
                for name in (
                    "validation_summary",
                    "hardware_coverage",
                ):
                    value = phase.get(name)
                    if isinstance(value, Mapping):
                        fact[name] = dict(value)
                validation_gaps = phase.get("validation_gaps")
                if isinstance(validation_gaps, list) and validation_gaps:
                    fact["validation_gaps"] = [
                        _bounded_text(item, 256)
                        for item in validation_gaps[:8]
                    ]
                facts.append(fact)
        return tuple(facts[-8:])

    @staticmethod
    def _suggested_action(document: Mapping[str, object]) -> dict[str, object] | None:
        run_id = _text(document.get("run_id"))
        state = _text(document.get("state"))
        if not run_id or state in {"completed", "failed", "cancelled"}:
            return None
        gate = _mapping(document.get("gate"))
        if state == "waiting_response" and _text(gate.get("kind")) == "phase":
            action = {
                "kind": "respond",
                "run_id": run_id,
                "gate_id": gate.get("gate_id"),
                "gate_version": gate.get("gate_version"),
                "schema_digest": gate.get("schema_digest"),
            }
            if all(action.get(name) not in (None, "") for name in action):
                return action
            return None
        if state == "running":
            return {"kind": "resume", "run_id": run_id}
        incident = _mapping(document.get("incident"))
        if state != "incident" or not incident:
            return None
        raw_allowed = incident.get("allowed_commands", [])
        allowed_commands = {
            _text(item)
            for item in raw_allowed
            if isinstance(raw_allowed, list) and _text(item)
        }
        if (
            _text(incident.get("recovery_path")) == "reconcile"
            and "reconcile" in allowed_commands
        ):
            return {"kind": "control", "run_id": run_id, "command": "reconcile"}
        if "resume" in allowed_commands:
            return {"kind": "resume", "run_id": run_id}
        incident_id = _text(incident.get("incident_id"))
        if "cancel" in allowed_commands and incident_id:
            return {
                "kind": "control",
                "run_id": run_id,
                "command": "cancel",
                "incident_id": incident_id,
            }
        return None

    @staticmethod
    def _capability_state(capabilities: Mapping[str, object], name: str) -> str:
        runtime_name = CAPABILITY_ALIASES[name]
        if runtime_name not in capabilities:
            return "not_checked"
        value = capabilities.get(runtime_name)
        if name == "alarms" and value is not True:
            return "not_checked"
        if value is None:
            return "not_checked"
        return "available" if value is True else "unavailable"

    @staticmethod
    def _mdb_value(child: Mapping[str, object], query: str) -> object:
        payload = _mapping(child.get("payload"))
        result = _mapping(payload.get("result")) or _mapping(child.get("result"))
        stdout_lines = result.get("stdout_lines")
        if query.split(maxsplit=1)[0].lower() == "getprop" and isinstance(
            stdout_lines, list
        ):
            return stdout_lines[0] if stdout_lines else None
        preferred = {
            key: result[key]
            for key in (
                "properties",
                "objects",
                "records",
                "values",
                "value",
                "stdout_lines",
            )
            if key in result
        }
        return _compact_value(preferred or result, max_depth=5, max_items=24)

    @staticmethod
    def _target_identity(result: Mapping[str, object]) -> dict[str, object]:
        runtime = _mapping(result.get("runtime"))
        runtime_status = _mapping(runtime.get("status"))
        targets = runtime_status.get("targets", [])
        if not isinstance(targets, list) or not targets:
            return {}
        detail = _mapping(targets[0])
        target = _mapping(detail.get("target"))
        epochs = _mapping(detail.get("epochs"))
        identity = detail.get("identity")
        projected = {
            "host": target.get("host", ""),
            "fingerprint": target.get("fingerprint", ""),
            "target_epoch": epochs.get("target_epoch", 0),
        }
        if isinstance(identity, Mapping) and identity:
            projected["identity"] = _compact_value(
                identity, max_depth=2, max_items=8, max_string=128
            )
        return projected

    def observation(
        self,
        raw: Mapping[str, object],
        scope: ScopeContract,
        *,
        assurance: str,
        source: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        result = _mapping(raw.get("result"))
        capabilities = _mapping(result.get("capabilities"))
        lanes = _mapping(result.get("lanes"))
        ssh_lane = _mapping(lanes.get("ssh"))
        observations: dict[str, object] = {}
        claims: list[dict[str, object]] = []
        evidence: list[dict[str, object]] = []
        counts = {state: 0 for state in CAPABILITY_STATES}
        mdb_index = 0
        for selector in scope.selectors:
            if selector.kind == "capability":
                values = []
                for name in selector.names:
                    state = self._capability_state(capabilities, name)
                    counts[state] += 1
                    values.append({"name": name, "status": state})
                observations[selector.selector_id] = {
                    "kind": "capability",
                    "values": values,
                }
                claims.append(
                    {
                        "path": f"results.{selector.selector_id}",
                        "status": (
                            "partial"
                            if any(value["status"] == "not_checked" for value in values)
                            else "grounded"
                        ),
                        "selector_id": selector.selector_id,
                    }
                )
                continue
            values = []
            for query_index, query in enumerate(selector.queries):
                name = "mdbctl" if mdb_index == 0 else f"mdbctl_{mdb_index + 1}"
                mdb_index += 1
                child = _mapping(ssh_lane.get(name))
                if not child:
                    state = "not_checked"
                    value: object = None
                elif child.get("ok") is True:
                    state = "available"
                    value = self._mdb_value(child, query)
                else:
                    state = "unavailable"
                    value = {
                        "code": child.get("code", "unavailable"),
                        "reason": child.get("error", "") or child.get("reason", ""),
                    }
                counts[state] += 1
                values.append(
                    {"query_index": query_index, "status": state, "value": value}
                )
            observations[selector.selector_id] = {"kind": "mdb", "values": values}
            claims.append(
                {
                    "path": f"results.{selector.selector_id}",
                    "status": (
                        "partial"
                        if any(value["status"] == "not_checked" for value in values)
                        else "grounded"
                    ),
                    "selector_id": selector.selector_id,
                }
            )
        observed_at = (
            _text(raw.get("observed_at"))
            or _text(result.get("completed_at"))
            or _text(result.get("started_at"))
        )
        consistency = observation_consistency(raw)
        target_detail = self._target_identity(result)
        source_reusable = source is not None and source.get("reusable", True) is True
        observation_ref = (
            ObservationRef.from_public_dict(source).to_public_dict()
            if source and source_reusable
            else None
        )
        receipt_seed = {
            "scope": scope.to_public_dict(),
            "observed_at": observed_at,
            "results": observations,
            "observation_ref": observation_ref,
        }
        receipt_id = "observation-" + _fingerprint(receipt_seed)[:24]
        for claim in claims:
            claim["receipt_id"] = receipt_id
        for selector in scope.selectors:
            source_uri = _text(_mapping(source).get("uri"))
            evidence.append(
                {
                    "uri": (
                        f"{source_uri}#{selector.selector_id}"
                        if source_uri
                        else f"receipt://{receipt_id}#{selector.selector_id}"
                    ),
                    "selector_id": selector.selector_id,
                }
            )
        requested = sum(counts.values())
        raw_gaps = raw.get("gaps", [])
        gaps = (
            [_bounded_text(item, 256) for item in raw_gaps[:8]]
            if isinstance(raw_gaps, list)
            else []
        )
        if counts["not_checked"]:
            gaps.append(f"{counts['not_checked']} requested observations were not checked")
        consistency_gaps = consistency.get("gaps", [])
        if isinstance(consistency_gaps, list):
            gaps.extend(_bounded_text(item, 256) for item in consistency_gaps[:8])
        temporally_coherent = consistency.get("classification") == "coherent"
        if not temporally_coherent:
            for claim in claims:
                claim["status"] = "partial"
        document = {
            "schema": OBSERVATION_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "status": (
                "complete"
                if counts["not_checked"] == 0 and temporally_coherent
                else "incomplete"
            ),
            "scope": scope.to_public_dict(),
            "freshness": {
                "mode": scope.freshness_mode,
                "max_age_seconds": scope.max_age_seconds,
                "observed_at": observed_at,
                "status": (
                    "live" if observed_at and temporally_coherent else "unknown"
                ),
            },
            "target": {"selector": scope.target, "identity": target_detail},
            "results": observations,
            "consistency": consistency,
            "coverage": {
                "requested": requested,
                "available": counts["available"],
                "unavailable": counts["unavailable"],
                "not_checked": counts["not_checked"],
                "complete": counts["not_checked"] == 0 and temporally_coherent,
            },
            "claims": claims,
            "evidence": evidence,
            "gaps": list(dict.fromkeys(gaps))[:16],
        }
        if observation_ref is not None:
            document["observation_ref"] = observation_ref
        return CostGovernor.observation(document)

    def turn(self, turn: RunTurn) -> dict[str, object]:
        document = {"schema": TURN_SCHEMA, **turn.to_public_dict()}
        document["next_action"] = self._suggested_action(document)
        diagnostic_receipt = document.get("diagnostic_receipt")
        if isinstance(diagnostic_receipt, Mapping):
            receipt_gaps = diagnostic_receipt.get("gaps", [])
            if isinstance(receipt_gaps, list):
                current_gaps = document.get("gaps", [])
                gaps = list(current_gaps) if isinstance(current_gaps, list) else []
                document["gaps"] = list(
                    dict.fromkeys((*gaps, *receipt_gaps))
                )[:16]
        validation_gaps = [
            _bounded_text(gap, 256)
            for fact in document.get("facts", [])
            if isinstance(fact, Mapping)
            for gap in (
                fact.get("validation_gaps", [])
                if isinstance(fact.get("validation_gaps"), list)
                else []
            )
        ]
        if validation_gaps:
            current_gaps = document.get("gaps", [])
            gaps = list(current_gaps) if isinstance(current_gaps, list) else []
            document["gaps"] = list(
                dict.fromkeys((*gaps, *validation_gaps))
            )[:16]
        return CostGovernor.turn(document)


class AgentGateway:
    """Deep module exposing only observe(Query) and execute(Action)."""

    def __init__(
        self,
        runtime: SemanticRuntimePort,
        *,
        projector: ResultProjector | None = None,
    ) -> None:
        self.runtime = runtime
        self.projector = projector or ResultProjector()

    def observe(
        self,
        query: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        observation_query = ObservationQuery.from_query(query)
        result = self.runtime.observe(
            observation_query,
            task_id=task_id,
            operation_id=operation_id,
        )
        source = dict(result.source)
        if not source and result.observation_ref is not None:
            source = result.observation_ref.to_source_dict()
        return self.projector.observation(
            result.raw,
            result.query,
            assurance=result.assurance,
            source=source or None,
        )

    def execute(
        self,
        action: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        command = decode_run_command(action, operation_id=operation_id)
        turn = self.runtime.execute(
            command,
            task_id=task_id,
            operation_id=operation_id,
        )
        return self.projector.turn(turn)

    @staticmethod
    def error(operation: str, exc: Exception) -> dict[str, object]:
        schema = OBSERVATION_RECEIPT_SCHEMA if operation == "observe" else TURN_SCHEMA
        key = "receipt_id" if operation == "observe" else "run_id"
        result = {
            "schema": schema,
            key: "",
            "status" if operation == "observe" else "state": "failed",
            "error": {
                "code": (
                    "ScopeViolation"
                    if operation == "observe" and isinstance(exc, AgentPreflightError)
                    else type(exc).__name__
                ),
                "message": _text(exc)[:1024],
            },
            "gaps": ["operation_failed"],
        }
        if isinstance(exc, (AgentPreflightError, GatePreflightError)):
            detail = exc.detail
            error = result["error"]
            assert isinstance(error, dict)
            example, next_action = _preflight_guidance(operation, detail)
            projected_example = _project_preflight_example(example)
            error["field"] = detail.field
            error["example"] = projected_example
            if detail.supported:
                error["supported"] = list(detail.supported)
            if detail.limit is not None:
                error["limit"] = detail.limit
            result["next_action"] = next_action
            if len(_json_bytes(result)) > TURN_PROJECTION_TARGET_BYTES:
                result.update(
                    _projection_telemetry(
                        compacted=False,
                        target_exceeded=False,
                    )
                )
                result["projection_target_exceeded"] = True
                result["projection_target_overage_bytes"] = 0
                for _attempt in range(3):
                    overage = max(
                        1,
                        len(_json_bytes(result))
                        - TURN_PROJECTION_TARGET_BYTES,
                    )
                    if result["projection_target_overage_bytes"] == overage:
                        break
                    result["projection_target_overage_bytes"] = overage
        return result


def agent_operation_descriptors() -> tuple[OperationDescriptor, ...]:
    observe_schema = {
        "type": "object",
        "required": ["target", "selectors"],
        "properties": {
            "target": {"type": "string", "minLength": 1, "maxLength": 512},
            "selectors": {
                "type": "array",
                "minItems": 1,
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 64},
                        "kind": {"type": "string", "enum": ["capability", "mdb"]},
                        "names": {
                            "type": "array",
                            "maxItems": 16,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                                "description": (
                                    "Capability names are case-insensitive; canonical names: "
                                    + ", ".join(sorted(CAPABILITY_ALIASES))
                                ),
                            },
                        },
                        "queries": {
                            "type": "array",
                            "maxItems": 32,
                            "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "freshness": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["live"], "default": "live"},
                    "max_age_seconds": {"type": "integer", "enum": [0], "default": 0},
                },
                "additionalProperties": False,
            },
            "deadline": {"type": "number", "exclusiveMinimum": 0, "default": 180},
        },
        "additionalProperties": False,
    }
    deadline_ref = {"$ref": "#/$defs/deadline"}
    run_id_ref = {"$ref": "#/$defs/run_id"}
    gate_binding_properties = {
        "gate_id": {"$ref": "#/$defs/gate_id"},
        "gate_version": {"$ref": "#/$defs/gate_version"},
        "schema_digest": {"$ref": "#/$defs/schema_digest"},
        "submission_id": {"$ref": "#/$defs/submission_id"},
    }
    execute_schema = {
        "type": "object",
        "$defs": {
            "deadline": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 120,
                "default": 120,
                "description": "Accepted range: greater than 0 and at most 120 seconds.",
            },
            "run_id": {"type": "string", "minLength": 1},
            "gate_id": {"type": "string", "minLength": 1},
            "gate_version": {"type": "integer", "minimum": 1},
            "schema_digest": {"type": "string", "minLength": 64},
            "submission_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "target": {"type": "string", "minLength": 1, "maxLength": 512},
            "targets": {
                "type": "array",
                "minItems": 2,
                "maxItems": 16,
                "items": {
                    "type": "object",
                    "required": ["ip"],
                    "properties": {
                        "ip": {"type": "string", "minLength": 1, "maxLength": 512},
                        "role": {
                            "type": "string",
                            "enum": ["reference", "candidate", "symmetric"],
                        },
                        "target_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "additionalProperties": False,
                },
            },
            "observation_ref": {
                "type": "object",
                "required": [
                    "handle",
                    "digest",
                    "kind",
                    "size",
                    "provenance",
                    "retention_hint",
                    "target",
                    "scope_digest",
                    "observed_at",
                ],
                "properties": {
                    "schema": {"type": "string"},
                    "handle": {"type": "string", "minLength": 1},
                    "digest": {"type": "string", "minLength": 64},
                    "kind": {"type": "string", "const": "observation"},
                    "size": {"type": "integer", "minimum": 0},
                    "provenance": {"type": "string"},
                    "retention_hint": {"type": "string"},
                    "target": {"type": "string", "minLength": 1},
                    "scope_digest": {"type": "string", "minLength": 64},
                    "observed_at": {"type": "string", "minLength": 1},
                    "target_fingerprint": {"type": "string"},
                    "target_epoch": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
            "response": {
                "type": "object",
                "required": ["status", "summary", "payload"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["completed", "failed", "cancelled"],
                    },
                    "summary": {"type": "string", "minLength": 1},
                    "payload": {
                        "type": "object",
                        "description": "Must satisfy the active Turn gate input_schema payload contract.",
                        "additionalProperties": True,
                    },
                },
                "additionalProperties": False,
            },
        },
        "oneOf": [
            {
                "title": "start",
                "type": "object",
                "required": sorted(EXECUTE_ACTION_REQUIRED_FIELDS["start"]),
                "anyOf": [{"required": ["target"]}, {"required": ["targets"]}],
                "properties": {
                    "kind": {"const": "start"},
                    "target": {"$ref": "#/$defs/target"},
                    "targets": {"$ref": "#/$defs/targets"},
                    "intent": {"type": "string", "minLength": 1},
                    "entry_operation": {"type": "string", "minLength": 1, "maxLength": 128},
                    "entry_arguments": {
                        "type": "object",
                        "description": "Domain arguments only; Runtime-owned identity, authorization, workflow, epoch, and recovery fields are forbidden.",
                        "additionalProperties": True,
                    },
                    "purpose": {"type": "string"},
                    "delivery_strategy": {
                        "type": "string",
                        "enum": ["source-only", "live-patch", "build-upgrade"],
                    },
                    "observation_ref": {"$ref": "#/$defs/observation_ref"},
                    "deadline": deadline_ref,
                },
                "additionalProperties": False,
            },
            {
                "title": "respond",
                "type": "object",
                "required": sorted(EXECUTE_ACTION_REQUIRED_FIELDS["respond"]),
                "properties": {
                    "kind": {"const": "respond"},
                    "run_id": run_id_ref,
                    **gate_binding_properties,
                    "response": {"$ref": "#/$defs/response"},
                    "deadline": deadline_ref,
                },
                "additionalProperties": False,
            },
            {
                "title": "resume",
                "type": "object",
                "required": sorted(EXECUTE_ACTION_REQUIRED_FIELDS["resume"]),
                "properties": {
                    "kind": {"const": "resume"},
                    "run_id": run_id_ref,
                    "deadline": deadline_ref,
                },
                "additionalProperties": False,
            },
            {
                "title": "control",
                "type": "object",
                "required": sorted(EXECUTE_ACTION_REQUIRED_FIELDS["control"]),
                "properties": {
                    "kind": {"const": "control"},
                    "run_id": run_id_ref,
                    "command": {"type": "string", "enum": ["reconcile", "cancel"]},
                    "incident_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    **gate_binding_properties,
                    "deadline": deadline_ref,
                },
                "oneOf": [
                    {
                        "properties": {"command": {"const": "reconcile"}},
                        "not": {
                            "anyOf": [
                                {"required": [name]}
                                for name in (
                                    "incident_id",
                                    "gate_id",
                                    "gate_version",
                                    "schema_digest",
                                    "submission_id",
                                )
                            ]
                        },
                    },
                    {
                        "properties": {"command": {"const": "cancel"}},
                        "required": ["incident_id"],
                        "not": {
                            "anyOf": [
                                {"required": [name]}
                                for name in (
                                    "gate_id",
                                    "gate_version",
                                    "schema_digest",
                                    "submission_id",
                                )
                            ]
                        },
                    },
                    {
                        "properties": {"command": {"const": "cancel"}},
                        "required": ["gate_id", "gate_version", "schema_digest"],
                        "not": {"required": ["incident_id"]},
                    },
                ],
                "additionalProperties": False,
            },
        ],
    }
    for action_schema in execute_schema["oneOf"]:
        kind = action_schema["properties"]["kind"]["const"]
        if set(action_schema["properties"]) != EXECUTE_ACTION_FIELDS[kind]:
            raise RuntimeError(f"{kind} execute schema fields drifted from decoder")
        for name, expected_type in EXECUTE_ACTION_FIELD_TYPES[kind].items():
            field_schema = action_schema["properties"][name]
            reference = field_schema.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                field_schema = execute_schema["$defs"][reference.rsplit("/", 1)[-1]]
            actual_type = field_schema.get("type")
            if actual_type is None and "const" in field_schema:
                actual_type = (
                    "string" if isinstance(field_schema["const"], str) else None
                )
            if actual_type != expected_type:
                raise RuntimeError(
                    f"{kind}.{name} execute schema type drifted from decoder"
                )
    return (
        OperationDescriptor(
            name="observe",
            description=(
                "Return one bounded live ObservationReceipt for all exact read-only selectors "
                "needed by the current answer; combine capability and MDB selectors because "
                "preflight is internal."
            ),
            input_schema=observe_schema,
            lifecycle="read",
            exposure="agent",
            audience="agent",
            cost_hint="small",
            scope_contract="immutable-observation-scope-v1",
            result_projector="observation-receipt-v1",
        ),
        OperationDescriptor(
            name="execute",
            description=(
                "Start or continue one Runtime workflow and return only the next semantic "
                "Turn. Copy next_action bindings exactly; response_required with "
                "progress.status=no_progress means respond to the unchanged Gate instead "
                "of retrying resume."
            ),
            input_schema=execute_schema,
            exposure="agent",
            audience="agent",
            cost_hint="medium",
            scope_contract="workflow-action-v1",
            result_projector="turn-v1",
        ),
    )
