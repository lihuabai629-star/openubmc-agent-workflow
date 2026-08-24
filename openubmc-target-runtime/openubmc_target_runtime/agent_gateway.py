"""Small semantic Agent interface over the shared openUBMC Runtime Core."""

from __future__ import annotations

from collections.abc import Mapping
import json

from .catalog import OperationDescriptor
from .contracts import RUNTIME_API_VERSION
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
TURN_MAX_BYTES = 8 * 1024
TOOLS_LIST_MAX_BYTES = 8 * 1024

_CAPABILITY_ALIASES = {
    "ssh": "ssh_transport",
    "telnet": "remote_log_file",
    "mdbctl": "mdbctl",
    "busctl": "busctl",
    "dbus": "dbus_env",
    "alarms": "active_alarm_endpoint_verified",
}
_CAPABILITY_STATES = frozenset({"available", "unavailable", "not_checked"})


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
        kind = _bounded_text(gate.get("kind"), 32)
        if kind == "phase":
            input_schema = dict(_mapping(gate.get("input_schema")))
            if len(_json_bytes(input_schema)) > GATE_SCHEMA_MAX_BYTES:
                return {
                    "kind": "blocker",
                    "name": "gate_schema_exceeds_budget",
                    "message": "phase gate schema exceeded the Agent budget",
                }
            return {
                "kind": kind,
                "gate_id": _bounded_text(gate.get("gate_id"), 128),
                "gate_version": gate.get("gate_version", 0),
                "schema_digest": _bounded_text(gate.get("schema_digest"), 80),
                "name": _bounded_text(gate.get("name"), 128),
                "owner": _bounded_text(gate.get("owner"), 128),
                "input_schema": input_schema,
            }
        return {
            "kind": kind or "blocker",
            "name": _bounded_text(gate.get("name"), 128),
            "message": _bounded_text(gate.get("message"), 512),
        }

    @staticmethod
    def _turn_gaps(value: object) -> list[str]:
        gaps = value if isinstance(value, list) else []
        bounded = [_bounded_text(item, 256) for item in gaps[:8]]
        if "turn_exceeds_8kb_budget" not in bounded:
            bounded.append("turn_exceeds_8kb_budget")
        return bounded

    @staticmethod
    def _turn_outcome(value: object) -> dict[str, object] | None:
        outcome = _mapping(value)
        if not outcome:
            return None
        return {
            "status": _bounded_text(outcome.get("status"), 64),
            "summary": _bounded_text(outcome.get("summary"), 512),
            "acceptance": _compact_value(
                outcome.get("acceptance", []),
                max_depth=2,
                max_items=8,
                max_string=128,
            ),
        }

    @staticmethod
    def _turn_incident(value: object) -> dict[str, object] | None:
        incident = _mapping(value)
        if not incident:
            return None
        return {
            "incident_id": _bounded_text(incident.get("incident_id"), 128),
            "code": _bounded_text(incident.get("code"), 128),
            "message": _bounded_text(incident.get("message"), 512),
            "effect_id": _bounded_text(incident.get("effect_id"), 128),
            "recoverable": bool(incident.get("recoverable", True)),
            "recovery_path": _bounded_text(
                incident.get("recovery_path"), 64
            ),
            "allowed_commands": [
                _bounded_text(item, 32)
                for item in (
                    incident.get("allowed_commands", [])
                    if isinstance(incident.get("allowed_commands"), list)
                    else []
                )[:4]
            ],
            "operator_action": _bounded_text(
                incident.get("operator_action"), 512
            ),
        }

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
        if len(_json_bytes(result)) <= TURN_MAX_BYTES:
            return result
        result["content_compacted"] = True
        result["facts"] = _compact_value(
            result.get("facts", []), max_depth=3, max_items=16, max_string=256
        )
        result["outcome"] = _compact_value(
            result.get("outcome"), max_depth=3, max_items=12, max_string=512
        )
        if len(_json_bytes(result)) <= TURN_MAX_BYTES:
            return result
        fallback: dict[str, object] = {
            "schema": TURN_SCHEMA,
            "run_id": _bounded_text(result.get("run_id"), 512),
            "state": _bounded_text(result.get("state") or "blocked", 64),
            "gate": CostGovernor._turn_gate(result.get("gate")),
            "incident": CostGovernor._turn_incident(result.get("incident")),
            "facts": [],
            "gaps": CostGovernor._turn_gaps(result.get("gaps")),
            "outcome": CostGovernor._turn_outcome(result.get("outcome")),
            "next": _bounded_text(result.get("next"), 512),
            "content_compacted": True,
        }
        if "observation_ref" in result:
            fallback["observation_ref"] = _compact_value(
                result.get("observation_ref"),
                max_depth=2,
                max_items=8,
                max_string=128,
            )
        if "outcome_recorded" in result:
            fallback["outcome_recorded"] = bool(result.get("outcome_recorded"))
        if len(_json_bytes(fallback)) <= TURN_MAX_BYTES:
            return fallback
        return {
            "schema": TURN_SCHEMA,
            "run_id": _bounded_text(result.get("run_id"), 128),
            "state": _bounded_text(result.get("state") or "blocked", 64),
            "gate": {
                "kind": "blocker",
                "name": "output_budget",
                "message": "Turn details exceeded the 8KB Agent budget",
            },
            "incident": CostGovernor._turn_incident(result.get("incident")),
            "facts": [],
            "gaps": ["turn_exceeds_8kb_budget"],
            "outcome": CostGovernor._turn_outcome(result.get("outcome")),
            "next": "inspect persisted Case evidence through the operator profile",
            "content_compacted": True,
        }


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
        runtime_name = _CAPABILITY_ALIASES[name]
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
        counts = {state: 0 for state in _CAPABILITY_STATES}
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
        return CostGovernor.turn(
            {"schema": TURN_SCHEMA, **turn.to_public_dict()}
        )


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
