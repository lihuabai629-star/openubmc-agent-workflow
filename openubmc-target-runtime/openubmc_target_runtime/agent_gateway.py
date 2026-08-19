"""Small semantic Agent interface over the shared openUBMC Runtime Core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from .catalog import OperationDescriptor
from .contracts import RUNTIME_API_VERSION
from .workflow import DEFAULT_PHASE_REGISTRY


AGENT_GATEWAY_SCHEMA = f"{RUNTIME_API_VERSION}/agent-gateway-v1"
OBSERVATION_RECEIPT_SCHEMA = f"{AGENT_GATEWAY_SCHEMA}/observation-receipt"
TURN_SCHEMA = f"{AGENT_GATEWAY_SCHEMA}/turn"
OBSERVATION_MAX_BYTES = 4 * 1024
TURN_MAX_BYTES = 8 * 1024
TOOLS_LIST_MAX_BYTES = 8 * 1024
GATE_SCHEMA_MAX_BYTES = 2 * 1024

_CAPABILITY_ALIASES = {
    "ssh": "ssh_transport",
    "telnet": "remote_log_file",
    "mdbctl": "mdbctl",
    "busctl": "busctl",
    "dbus": "dbus_env",
    "alarms": "active_alarms",
}
_CAPABILITY_STATES = frozenset({"available", "unavailable", "not_checked"})
_ASSURANCE_LEVELS = frozenset({"auto", "fast", "assured"})


class AgentGatewayError(ValueError):
    """Base error for the semantic Agent interface."""


class ScopeViolation(AgentGatewayError):
    """Raised when an observation requests an undeclared evidence surface."""


class AgentGatewayRuntimePort(Protocol):
    """Internal seam implemented by the Runtime service and test adapters."""

    def observe_operation(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def run_operation(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def run_snapshot(self, run_id: str) -> Mapping[str, object]: ...

    def record_run_outcome(
        self,
        *,
        task_id: str,
        run_id: str,
        outcome: str,
        summary: str,
        details: Mapping[str, object],
    ) -> Mapping[str, object]: ...


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


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


@dataclass(frozen=True)
class SelectorContract:
    selector_id: str
    kind: str
    names: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: Mapping[str, object], index: int) -> "SelectorContract":
        unexpected = set(value) - {"id", "kind", "names", "queries"}
        if unexpected:
            raise ScopeViolation(
                "selector contains undeclared fields: " + ", ".join(sorted(unexpected))
            )
        kind = _text(value.get("kind")).lower()
        selector_id = _text(value.get("id")) or f"selector-{index}"
        if kind == "capability":
            raw_names = value.get("names", [])
            if not isinstance(raw_names, list) or not raw_names:
                raise ScopeViolation("capability selector requires a non-empty names array")
            names = tuple(dict.fromkeys(_text(item).lower() for item in raw_names))
            unsupported = sorted(set(names) - set(_CAPABILITY_ALIASES))
            if unsupported:
                raise ScopeViolation(
                    "unsupported capability selectors: " + ", ".join(unsupported)
                )
            return cls(selector_id=selector_id, kind=kind, names=names)
        if kind == "mdb":
            raw_queries = value.get("queries", [])
            if not isinstance(raw_queries, list) or not raw_queries:
                raise ScopeViolation("mdb selector requires a non-empty queries array")
            queries = tuple(_text(item) for item in raw_queries)
            if any(not query for query in queries):
                raise ScopeViolation("mdb queries must not be empty")
            return cls(selector_id=selector_id, kind=kind, queries=queries)
        raise ScopeViolation(f"unsupported selector kind: {kind or '<empty>'}")

    def to_public_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"id": self.selector_id, "kind": self.kind}
        if self.names:
            result["names"] = list(self.names)
        if self.queries:
            result["queries"] = list(self.queries)
        return result


@dataclass(frozen=True)
class ScopeContract:
    target: str
    selectors: tuple[SelectorContract, ...]
    freshness_mode: str
    max_age_seconds: int
    assurance: str
    deadline: float

    @classmethod
    def from_query(cls, query: Mapping[str, object]) -> "ScopeContract":
        unexpected = set(query) - {
            "target",
            "selectors",
            "freshness",
            "assurance",
            "deadline",
        }
        if unexpected:
            raise ScopeViolation(
                "query contains undeclared fields: " + ", ".join(sorted(unexpected))
            )
        target = _text(query.get("target"))
        if not target:
            raise ScopeViolation("target is required")
        raw_selectors = query.get("selectors")
        if not isinstance(raw_selectors, list) or not raw_selectors:
            raise ScopeViolation("selectors must be a non-empty array")
        selectors = tuple(
            SelectorContract.from_value(_mapping(value), index)
            for index, value in enumerate(raw_selectors, start=1)
        )
        freshness = _mapping(query.get("freshness"))
        if set(freshness) - {"mode", "max_age_seconds"}:
            raise ScopeViolation("freshness contains undeclared fields")
        freshness_mode = _text(freshness.get("mode") or "live").lower()
        max_age = freshness.get("max_age_seconds", 0)
        if freshness_mode != "live":
            raise ScopeViolation("only live evidence is supported by the Agent interface")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age != 0:
            raise ScopeViolation("live evidence requires max_age_seconds=0")
        assurance = _text(query.get("assurance") or "auto").lower()
        if assurance not in _ASSURANCE_LEVELS:
            raise ScopeViolation("assurance must be auto, fast, or assured")
        deadline = query.get("deadline", 180)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise ScopeViolation("deadline must be a positive number")
        if float(deadline) <= 0:
            raise ScopeViolation("deadline must be a positive number")
        return cls(
            target=target,
            selectors=selectors,
            freshness_mode=freshness_mode,
            max_age_seconds=max_age,
            assurance=assurance,
            deadline=float(deadline),
        )

    def runtime_arguments(self, *, assured: bool) -> dict[str, object]:
        queries = [
            query
            for selector in self.selectors
            if selector.kind == "mdb"
            for query in selector.queries
        ]
        result: dict[str, object] = {
            "ip": self.target,
            "deadline": self.deadline,
            "mdb_queries": queries,
            "mdb_only": True,
            "_agent_capability_names": [
                name
                for selector in self.selectors
                if selector.kind == "capability"
                for name in selector.names
            ],
            "_agent_assured": assured,
        }
        result["profile"] = "mdb"
        return result

    def to_public_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "selectors": [selector.to_public_dict() for selector in self.selectors],
            "freshness": {
                "mode": self.freshness_mode,
                "max_age_seconds": self.max_age_seconds,
            },
            "assurance": self.assurance,
        }


class CostGovernor:
    """Enforce model-visible result budgets without exposing raw Evidence."""

    @staticmethod
    def observation(document: Mapping[str, object]) -> dict[str, object]:
        result = dict(document)
        if len(_json_bytes(result)) <= OBSERVATION_MAX_BYTES:
            return result
        compacted = dict(result)
        compacted["content_compacted"] = True
        compacted["results"] = _compact_value(
            result.get("results", {}), max_depth=3, max_items=10, max_string=192
        )
        if len(_json_bytes(compacted)) <= OBSERVATION_MAX_BYTES:
            return compacted
        coverage = _mapping(result.get("coverage"))
        return {
            "schema": OBSERVATION_RECEIPT_SCHEMA,
            "receipt_id": result.get("receipt_id", ""),
            "status": "incomplete",
            "scope": result.get("scope", {}),
            "assurance": result.get("assurance", "fast"),
            "freshness": result.get("freshness", {}),
            "target": result.get("target", {}),
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
        result["facts"] = []
        result["outcome"] = {
            "status": result.get("state", "blocked"),
            "summary": "Turn details exceeded the 8KB Agent budget",
        }
        result.setdefault("gaps", []).append("turn_exceeds_8kb_budget")
        return result


class ResultProjector:
    """Project Runtime implementation details into Agent semantic receipts."""

    @staticmethod
    def _capability_state(capabilities: Mapping[str, object], name: str) -> str:
        runtime_name = _CAPABILITY_ALIASES[name]
        if runtime_name not in capabilities:
            return "not_checked"
        return "available" if capabilities.get(runtime_name) is True else "unavailable"

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
        target_detail = self._target_identity(result)
        receipt_seed = {
            "scope": scope.to_public_dict(),
            "observed_at": observed_at,
            "results": observations,
        }
        receipt_id = "observation-" + _fingerprint(receipt_seed)[:24]
        for claim in claims:
            claim["receipt_id"] = receipt_id
        for selector in scope.selectors:
            evidence.append(
                {
                    "uri": f"receipt://{receipt_id}#{selector.selector_id}",
                    "selector_id": selector.selector_id,
                }
            )
        requested = sum(counts.values())
        gaps = []
        if counts["not_checked"]:
            gaps.append(f"{counts['not_checked']} requested observations were not checked")
        document = {
            "schema": OBSERVATION_RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "status": "complete" if counts["not_checked"] == 0 else "incomplete",
            "scope": scope.to_public_dict(),
            "assurance": assurance,
            "freshness": {
                "mode": scope.freshness_mode,
                "max_age_seconds": scope.max_age_seconds,
                "observed_at": observed_at,
                "status": "live" if observed_at else "unknown",
            },
            "target": {"selector": scope.target, "identity": target_detail},
            "results": observations,
            "coverage": {
                "requested": requested,
                "available": counts["available"],
                "unavailable": counts["unavailable"],
                "not_checked": counts["not_checked"],
                "complete": counts["not_checked"] == 0,
            },
            "claims": claims,
            "evidence": evidence,
            "gaps": gaps,
        }
        return CostGovernor.observation(document)

    @staticmethod
    def _gate_schema(phase_type: str) -> dict[str, object]:
        descriptor = DEFAULT_PHASE_REGISTRY.require(phase_type)
        required = [
            field
            for field in descriptor.required_fields
            if field
            not in {
                "case_id",
                "expected_revision",
                "idempotency_key",
                "phase_type",
                "producer_identity",
                "status",
                "summary",
            }
        ]
        properties = {
            "status": {
                "type": "string",
                "enum": ["completed", "failed", "cancelled"],
            },
            "summary": {"type": "string", "minLength": 1},
            "payload": {
                "type": "object",
                "description": "Phase receipt fields named by required_fields.",
                "additionalProperties": True,
            },
        }
        schema = {
            "type": "object",
            "required": ["status", "summary", "payload"],
            "properties": properties,
            "additionalProperties": False,
            "required_fields": required,
            "receipt_schema": descriptor.receipt_schema,
        }
        if len(_json_bytes(schema)) > GATE_SCHEMA_MAX_BYTES:
            raise AgentGatewayError("gate schema exceeds the 2KB budget")
        return schema

    def turn(self, raw: Mapping[str, object]) -> dict[str, object]:
        envelope = _mapping(getattr(raw, "envelope", {}))
        runtime_state = _text(
            raw.get("status") or envelope.get("status") or "completed"
        )
        state = {
            "waiting_phase_record": "waiting_response",
            "budget_exhausted": "running",
        }.get(runtime_state, runtime_state)
        run_id = _text(envelope.get("case_id") or raw.get("case_id"))
        facts = envelope.get("facts", [])
        gaps = list(envelope.get("gaps", [])) if isinstance(envelope.get("gaps"), list) else []
        gate = None
        if runtime_state == "waiting_phase_record":
            phase_type = _text(raw.get("required_phase_type"))
            gate = {
                "kind": "phase",
                "name": phase_type,
                "owner": _text(raw.get("required_skill")),
                "input_schema": self._gate_schema(phase_type),
            }
        elif runtime_state in {
            "waiting_external",
            "blocked",
            "mutation_outcome_unknown",
        }:
            gate = {
                "kind": "blocker",
                "name": _text(
                    raw.get("required_operation") or raw.get("blocked_operation_id")
                ),
                "message": _text(raw.get("next_action")),
            }
        outcome = None
        if bool(raw.get("completed")) or runtime_state in {
            "completed",
            "failed",
            "cancelled",
        }:
            closeout = _mapping(raw.get("closeout"))
            outcome = {
                "status": state,
                "summary": _text(
                    closeout.get("summary")
                    or raw.get("summary")
                    or envelope.get("summary")
                ),
                "acceptance": _compact_value(
                    closeout.get("checks", closeout.get("acceptance", [])),
                    max_depth=3,
                    max_items=16,
                    max_string=256,
                ),
            }
        document = {
            "schema": TURN_SCHEMA,
            "run_id": run_id,
            "state": state,
            "gate": gate,
            "facts": facts if isinstance(facts, list) else [],
            "gaps": gaps,
            "outcome": outcome,
            "next": (
                "respond to the current phase gate"
                if runtime_state == "waiting_phase_record"
                else (
                    "resume the run"
                    if runtime_state == "budget_exhausted"
                    else _text(raw.get("next_action"))
                )
            ),
        }
        return CostGovernor.turn(document)


class AgentGateway:
    """Deep module exposing only observe(Query) and execute(Action)."""

    def __init__(
        self,
        runtime: AgentGatewayRuntimePort,
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
        scope = ScopeContract.from_query(query)
        assured = scope.assurance == "assured"
        operation = "debug_collect"
        raw = self.runtime.observe_operation(
            operation,
            scope.runtime_arguments(assured=assured),
            task_id=task_id,
            operation_id=operation_id,
        )
        return self.projector.observation(
            raw,
            scope,
            assurance="assured" if assured else "fast",
        )

    def _record_phase_response(
        self,
        action: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        cancelled: bool = False,
    ) -> Mapping[str, object]:
        run_id = _text(action.get("run_id"))
        snapshot = self.runtime.run_snapshot(run_id)
        continuation = _mapping(snapshot.get("continuation"))
        handoff = _mapping(continuation.get("handoff_arguments"))
        contract = dict(_mapping(handoff.get("phase_record_contract")))
        if not contract:
            raise AgentGatewayError("run is not waiting at a phase response gate")
        response = _mapping(action.get("response"))
        payload = _mapping(response.get("payload"))
        contract.update(payload)
        contract["status"] = "cancelled" if cancelled else _text(response.get("status"))
        contract["summary"] = (
            _text(response.get("summary"))
            or ("run cancelled at the current gate" if cancelled else "")
        )
        self.runtime.run_operation(
            "phase_record",
            contract,
            task_id=task_id,
            operation_id=f"{operation_id}-response",
        )
        return self.runtime.run_operation(
            "workflow.next",
            {"case_id": run_id, "include_closeout_bundle": False},
            task_id=task_id,
            operation_id=f"{operation_id}-next",
        )

    def execute(
        self,
        action: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> dict[str, object]:
        kind = _text(action.get("kind")).lower()
        if kind == "start":
            arguments: dict[str, object] = {
                "ip": _text(action.get("target")),
                "intent": _text(action.get("intent") or "diagnosis-only"),
                "final_purpose": _text(action.get("purpose") or "complete the requested workflow"),
                "max_steps": int(action.get("max_steps", 8)),
                "include_closeout_bundle": False,
            }
            delivery = _text(action.get("delivery_strategy"))
            if delivery:
                arguments["delivery_strategy"] = delivery
            workflow = action.get("workflow")
            if isinstance(workflow, Mapping):
                arguments["workflow"] = dict(workflow)
            raw = self.runtime.run_operation(
                "workflow.advance",
                arguments,
                task_id=task_id,
                operation_id=operation_id,
            )
        elif kind == "respond":
            raw = self._record_phase_response(
                action, task_id=task_id, operation_id=operation_id
            )
        elif kind == "resume":
            raw = self.runtime.run_operation(
                "workflow.next",
                {
                    "case_id": _text(action.get("run_id")),
                    "max_steps": int(action.get("max_steps", 8)),
                    "include_closeout_bundle": False,
                },
                task_id=task_id,
                operation_id=operation_id,
            )
        elif kind == "control":
            command = _text(action.get("command")).lower()
            if command == "cancel":
                raw = self._record_phase_response(
                    action,
                    task_id=task_id,
                    operation_id=operation_id,
                    cancelled=True,
                )
            elif command in {"continue", "reconcile"}:
                raw = self.runtime.run_operation(
                    "workflow.next",
                    {
                        "case_id": _text(action.get("run_id")),
                        "include_closeout_bundle": False,
                    },
                    task_id=task_id,
                    operation_id=operation_id,
                )
            else:
                raise AgentGatewayError("control command must be continue, reconcile, or cancel")
        else:
            raise AgentGatewayError("execute kind must be start, respond, resume, or control")
        turn = self.projector.turn(raw)
        if turn.get("outcome") is not None and turn.get("run_id"):
            outcome = _mapping(turn["outcome"])
            recorded = self.runtime.record_run_outcome(
                task_id=task_id,
                run_id=_text(turn["run_id"]),
                outcome=(
                    "completed"
                    if turn.get("state") == "completed"
                    else "failed"
                ),
                summary=_text(outcome.get("summary")) or "workflow completed",
                details={"state": turn.get("state"), "gaps": turn.get("gaps", [])},
            )
            turn["outcome_recorded"] = bool(recorded)
        return CostGovernor.turn(turn)

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
            "target": {"type": "string", "minLength": 1},
            "selectors": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1},
                        "kind": {"type": "string", "enum": ["capability", "mdb"]},
                        "names": {"type": "array", "items": {"type": "string"}},
                        "queries": {"type": "array", "items": {"type": "string"}},
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
            "assurance": {
                "type": "string",
                "enum": ["auto", "fast", "assured"],
                "default": "auto",
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
            "purpose": {"type": "string"},
            "delivery_strategy": {
                "type": "string",
                "enum": ["source-only", "live-patch", "build-upgrade"],
            },
            "workflow": {"type": "object", "additionalProperties": {"type": "object"}},
            "response": {"type": "object", "additionalProperties": True},
            "command": {
                "type": "string",
                "enum": ["continue", "reconcile", "cancel"],
            },
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 64, "default": 8},
        },
        "additionalProperties": False,
    }
    return (
        OperationDescriptor(
            name="observe",
            description="Return one bounded live ObservationReceipt for exact read-only selectors.",
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
