"""Small semantic Agent interface over the shared openUBMC Runtime Core."""

from __future__ import annotations

from collections.abc import Mapping
import json

from .catalog import OperationDescriptor
from .capabilities import CAPABILITY_ALIASES, CAPABILITY_STATES
from .contracts import RUNTIME_API_VERSION
from .diagnostic_receipt import DiagnosticReceipt
from .semantic_runtime import (
    AgentGatewayError,
    GATE_SCHEMA_MAX_BYTES,
    ObservationQuery,
    ObservationRef,
    RunTurn,
    ScopeContract,
    ScopeViolation,
    SelectorContract,
    SemanticRuntimePort,
    decode_run_command,
    fingerprint,
)
from .observation import observation_consistency


AGENT_GATEWAY_SCHEMA = f"{RUNTIME_API_VERSION}/agent-gateway-v1"
OBSERVATION_RECEIPT_SCHEMA = f"{AGENT_GATEWAY_SCHEMA}/observation-receipt"
TURN_SCHEMA = f"{AGENT_GATEWAY_SCHEMA}/turn"
OBSERVATION_MAX_BYTES = 4 * 1024
TURN_PROJECTION_TARGET_BYTES = 8 * 1024
# Compatibility name for callers that report the historical projection target.
# Runtime-owned Turn semantics may exceed it; it is not a control-flow maximum.
TURN_MAX_BYTES = TURN_PROJECTION_TARGET_BYTES
TOOLS_LIST_MAX_BYTES = 8 * 1024
EXECUTE_TEXT_PROJECTION_TARGET_BYTES = 4 * 1024

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


def _selector_identity_scope(value: object) -> dict[str, object]:
    scope = _mapping(value)
    raw_selectors = scope.get("selectors", [])
    selectors = [
        {
            "id": _bounded_text(_mapping(item).get("id"), 64),
            "kind": _bounded_text(_mapping(item).get("kind"), 16),
        }
        for item in (raw_selectors if isinstance(raw_selectors, list) else [])[:16]
    ]
    return {
        "target": _bounded_text(scope.get("target"), 512),
        "selectors": selectors,
        "freshness": _compact_value(
            scope.get("freshness", {}),
            max_depth=2,
            max_items=4,
            max_string=64,
        ),
    }


def _selector_identity_consistency(
    value: object,
    *,
    include_selectors: bool = True,
) -> dict[str, object]:
    consistency = _mapping(value)
    raw_selectors = consistency.get("selectors", [])
    result: dict[str, object] = {
        "classification": _bounded_text(
            consistency.get("classification"), 32
        ),
        "reusable": False,
    }
    if include_selectors:
        result["selectors"] = [
            {
                "selector_id": _bounded_text(
                    _mapping(item).get("selector_id"), 64
                ),
                "kind": _bounded_text(_mapping(item).get("kind"), 16),
                "status": _bounded_text(_mapping(item).get("status"), 16),
            }
            for item in (
                raw_selectors if isinstance(raw_selectors, list) else []
            )[:16]
        ]
    return result


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


class CostGovernor:
    """Enforce model-visible result budgets without exposing raw Evidence."""

    @staticmethod
    def _turn_gate(value: object) -> dict[str, object] | None:
        gate = _mapping(value)
        if not gate:
            return None
        kind = _text(gate.get("kind"))
        if kind == "phase":
            input_schema = dict(_mapping(gate.get("input_schema")))
            if len(_json_bytes(input_schema)) > GATE_SCHEMA_MAX_BYTES:
                raise AgentGatewayError(
                    "Gate schema exceeds the hard 4 KiB contract"
                )
        return dict(gate)

    @staticmethod
    def _turn_diagnostic_receipt(value: object) -> dict[str, object] | None:
        receipt = _mapping(value)
        if not receipt:
            return None
        return (
            DiagnosticReceipt.from_public_dict(receipt)
            .compacted_for_agent()
            .to_public_dict()
        )

    @staticmethod
    def observation(document: Mapping[str, object]) -> dict[str, object]:
        result = dict(document)
        if len(_json_bytes(result)) <= OBSERVATION_MAX_BYTES:
            return result
        compacted = dict(result)
        compacted["content_compacted"] = True
        compacted.pop("observation_ref", None)
        compacted["results"] = _compact_value(
            result.get("results", {}), max_depth=3, max_items=10, max_string=192
        )
        compacted["status"] = "incomplete"
        coverage = dict(_mapping(compacted.get("coverage")))
        coverage["complete"] = False
        compacted["coverage"] = coverage
        gaps = list(compacted.get("gaps", []))
        gaps.append("result_compacted_to_fit_4kb_budget; narrow the selectors")
        compacted["gaps"] = gaps
        if len(_json_bytes(compacted)) <= OBSERVATION_MAX_BYTES:
            return compacted
        coverage = _mapping(result.get("coverage"))
        fallback = {
            "schema": OBSERVATION_RECEIPT_SCHEMA,
            "receipt_id": _bounded_text(result.get("receipt_id"), 128),
            "status": "incomplete",
            "scope": _selector_identity_scope(result.get("scope", {})),
            "freshness": _compact_value(
                result.get("freshness", {}),
                max_depth=2,
                max_items=8,
                max_string=128,
            ),
            "target": _compact_value(
                result.get("target", {}),
                max_depth=3,
                max_items=8,
                max_string=128,
            ),
            "results": {},
            "consistency": _selector_identity_consistency(
                result.get("consistency", {}),
                include_selectors=False,
            ),
            "coverage": {
                "requested": coverage.get("requested", 0),
                "available": 0,
                "unavailable": 0,
                "not_checked": coverage.get("requested", 0),
                "complete": False,
            },
            "claims": [],
            "evidence": [],
            "gaps": ["result_exceeds_4kb_budget; narrow the selectors"],
            "content_compacted": True,
        }
        if len(_json_bytes(fallback)) <= OBSERVATION_MAX_BYTES:
            return fallback
        minimal = {
            "schema": OBSERVATION_RECEIPT_SCHEMA,
            "receipt_id": _bounded_text(result.get("receipt_id"), 128),
            "status": "incomplete",
            "scope": _selector_identity_scope(result.get("scope", {})),
            "freshness": {"status": "unknown"},
            "consistency": _selector_identity_consistency(
                result.get("consistency", {})
            ),
            "target": {},
            "results": {},
            "coverage": {
                "requested": coverage.get("requested", 0),
                "available": 0,
                "unavailable": 0,
                "not_checked": coverage.get("requested", 0),
                "complete": False,
            },
            "claims": [],
            "evidence": [],
            "gaps": ["result_exceeds_4kb_budget; narrow the selectors"],
            "content_compacted": True,
        }
        if len(_json_bytes(minimal)) <= OBSERVATION_MAX_BYTES:
            return minimal
        scope = _selector_identity_scope(result.get("scope", {}))
        return {
            "schema": OBSERVATION_RECEIPT_SCHEMA,
            "receipt_id": _bounded_text(result.get("receipt_id"), 128),
            "status": "incomplete",
            "scope": {
                "selectors": [
                    {"id": selector["id"]}
                    for selector in scope["selectors"]
                ]
            },
            "consistency": _selector_identity_consistency(
                result.get("consistency", {}),
                include_selectors=False,
            ),
            "coverage": {
                "requested": coverage.get("requested", 0),
                "available": 0,
                "unavailable": 0,
                "not_checked": coverage.get("requested", 0),
                "complete": False,
            },
            "gaps": ["result_exceeds_4kb_budget; narrow the selectors"],
            "content_compacted": True,
        }

    @staticmethod
    def turn(document: Mapping[str, object]) -> dict[str, object]:
        result = dict(document)
        runtime_gate = CostGovernor._turn_gate(document.get("gate"))
        if runtime_gate != document.get("gate"):
            result["gate"] = runtime_gate
            result["content_compacted"] = True
            result["projection_compacted"] = True
            result["projection_target_exceeded"] = False
            result["manual_narrowing_required"] = False
            result["budget_blocker"] = False
        if len(_json_bytes(result)) <= TURN_MAX_BYTES:
            return result
        result["content_compacted"] = True
        result["projection_compacted"] = True
        result["projection_target_exceeded"] = False
        result["manual_narrowing_required"] = False
        result["budget_blocker"] = False
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
            "content_compacted": True,
            "projection_compacted": True,
            "projection_target_exceeded": False,
            "manual_narrowing_required": False,
            "budget_blocker": False,
        }
        if "observation_ref" in result:
            fallback["observation_ref"] = document.get("observation_ref")
        if "outcome_recorded" in result:
            fallback["outcome_recorded"] = bool(result.get("outcome_recorded"))
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
                facts.append(fact)
        return tuple(facts[-8:])

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
        diagnostic_receipt = document.get("diagnostic_receipt")
        if isinstance(diagnostic_receipt, Mapping):
            receipt_gaps = diagnostic_receipt.get("gaps", [])
            if isinstance(receipt_gaps, list):
                current_gaps = document.get("gaps", [])
                gaps = list(current_gaps) if isinstance(current_gaps, list) else []
                document["gaps"] = list(
                    dict.fromkeys((*gaps, *receipt_gaps))
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
        return {
            "schema": schema,
            key: "",
            "status" if operation == "observe" else "state": "failed",
            "error": {
                "code": type(exc).__name__,
                "message": _text(exc)[:1024],
            },
            "gaps": ["operation_failed"],
        }


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
                            "items": {"type": "string", "minLength": 1, "maxLength": 64},
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
    execute_schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {
            "kind": {"type": "string", "enum": ["start", "respond", "resume", "control"]},
            "run_id": {"type": "string", "minLength": 1},
            "target": {"type": "string", "minLength": 1},
            "targets": {
                "type": "array",
                "minItems": 2,
                "maxItems": 16,
                "description": (
                    "Runtime-owned multi-target diagnosis scope. Target identities and "
                    "roles are normalized before the Run opens."
                ),
                "items": {
                    "type": "object",
                    "required": ["ip"],
                    "properties": {
                        "ip": {"type": "string", "minLength": 1, "maxLength": 512},
                        "role": {
                            "type": "string",
                            "enum": ["reference", "candidate", "symmetric"],
                        },
                        "target_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 128,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "intent": {"type": "string"},
            "entry_operation": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": (
                    "Optional registered Domain Pack entry. READ_ONLY entries run as "
                    "one-step diagnosis-only Runs; mutation entries require a typed "
                    "Runtime-owned route for the selected intent."
                ),
            },
            "entry_arguments": {
                "type": "object",
                "description": (
                    "Typed arguments for the selected entry_operation; Runtime-owned "
                    "identity, target, intent, and authorization fields are forbidden."
                ),
                "additionalProperties": True,
            },
            "purpose": {"type": "string"},
            "delivery_strategy": {
                "type": "string",
                "enum": ["source-only", "live-patch", "build-upgrade"],
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
                    "kind": {"type": "string", "enum": ["observation"]},
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
            "gate_id": {"type": "string", "minLength": 1},
            "gate_version": {"type": "integer", "minimum": 1},
            "schema_digest": {"type": "string", "minLength": 64},
            "incident_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "submission_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "response": {
                "type": "object",
                "required": ["status", "summary", "payload"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["completed", "failed", "cancelled"],
                    },
                    "summary": {"type": "string", "minLength": 1},
                    "payload": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": False,
            },
            "command": {
                "type": "string",
                "enum": ["reconcile", "cancel"],
            },
            "deadline": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 120,
                "default": 120,
                "description": (
                    "Maximum time to wait for the next actionable Turn; "
                    "it does not change Run command identity."
                ),
            },
        },
        "additionalProperties": False,
    }
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
            description="Start or continue one Runtime workflow and return only the next semantic Turn.",
            input_schema=execute_schema,
            exposure="agent",
            audience="agent",
            cost_hint="medium",
            scope_contract="workflow-action-v1",
            result_projector="turn-v1",
        ),
    )
