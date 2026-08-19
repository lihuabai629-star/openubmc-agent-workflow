"""Automatic observation and durable Run progression behind one semantic seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Protocol

from .artifact_store import LocalArtifactStore
from .semantic_runtime import (
    ArtifactRef,
    AssuranceUnavailable,
    CancelRun,
    CommandConflict,
    Gate,
    GateConflict,
    Incident,
    ObservationQuery,
    ObservationRef,
    ObservationResult,
    Outcome,
    ReconcileRun,
    ResumeRun,
    RunCommand,
    RunTurn,
    SemanticRuntimePort,
    StartRun,
    SubmitGate,
    fingerprint,
)
from .workflow import DEFAULT_PHASE_REGISTRY


WORKFLOW_INTERNAL_MAX_STEPS = 64
GATE_SCHEMA_MAX_BYTES = 4 * 1024
_CAPABILITY_KEYS = {
    "ssh": "ssh_transport",
    "telnet": "remote_log_file",
    "mdbctl": "mdbctl",
    "busctl": "busctl",
    "dbus": "dbus_env",
    "alarms": "active_alarm_endpoint_verified",
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _projection(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(snapshot.get("projection"))


def _continuation(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    return _mapping(snapshot.get("continuation"))


def _string_schema(*, enum: list[str] | None = None) -> dict[str, object]:
    schema: dict[str, object] = {"type": "string", "minLength": 1}
    if enum is not None:
        schema["enum"] = enum
    return schema


def _string_array_schema() -> dict[str, object]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
    }


def _artifact_ref_schema(kind: str, *, require_version: bool) -> dict[str, object]:
    required = [
        "handle",
        "digest",
        "kind",
        "size",
        "provenance",
        "retention_hint",
        "target",
        "run_id",
    ]
    if require_version:
        required.append("version")
    return {
        "type": "object",
        "required": required,
        "properties": {
            "schema": {"type": "string"},
            "handle": _string_schema(),
            "digest": {"type": "string", "minLength": 64, "maxLength": 71},
            "kind": {"type": "string", "enum": [kind]},
            "size": {"type": "integer", "minimum": 0},
            "provenance": _string_schema(),
            "retention_hint": _string_schema(),
            "version": _string_schema(),
            "target": _string_schema(),
            "run_id": _string_schema(),
        },
        "additionalProperties": False,
    }


def gate_input_schema(phase_type: str, *, delivery_strategy: str) -> dict[str, object]:
    descriptor = DEFAULT_PHASE_REGISTRY.require(phase_type)
    if phase_type == "developer.change":
        payload_properties: dict[str, object] = {
            "source_revision": _string_schema(),
            "authored_files": _string_array_schema(),
            "verification_plan": _string_array_schema(),
            "design": {"type": "object", "additionalProperties": True},
            "validation_results": {"type": "array"},
            "source_delivery": {
                "type": "string",
                "enum": ["local_only", "committed", "pushed", "pull_request"],
            },
            "known_gaps": _string_array_schema(),
            "artifact_ref": _artifact_ref_schema(
                "openubmc-live-patch", require_version=False
            ),
            "remote_path": _string_schema(),
            "restart_scope": _string_schema(),
        }
        completed_required = [
            "source_revision",
            "authored_files",
            "verification_plan",
        ]
        if delivery_strategy == "live-patch":
            completed_required.extend(
                ["artifact_ref", "remote_path", "restart_scope"]
            )
    elif phase_type == "build.artifact":
        payload_properties = {
            "source_revision": _string_schema(),
            "artifact_ref": _artifact_ref_schema(
                "openubmc-hpm", require_version=True
            ),
            "component_versions": {"type": "array"},
            "build_commands": _string_array_schema(),
            "build_logs": _string_array_schema(),
            "known_gaps": _string_array_schema(),
        }
        completed_required = ["source_revision", "artifact_ref"]
    else:
        raise GateConflict(f"unsupported Agent Gate phase: {phase_type}")
    schema = {
        "type": "object",
        "required": ["status", "summary", "payload"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["completed", "failed", "cancelled"],
            },
            "summary": _string_schema(),
            "payload": {
                "type": "object",
                "properties": payload_properties,
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"status": {"const": "completed"}}},
                "then": {
                    "properties": {
                        "payload": {"required": completed_required}
                    }
                },
            }
        ],
        "receipt_schema": descriptor.receipt_schema,
    }
    if len(str(schema).encode("utf-8")) > GATE_SCHEMA_MAX_BYTES:
        raise GateConflict("Gate schema exceeds the 4 KiB Runtime budget")
    return schema


class ObservationDriver(Protocol):
    def observe_once(
        self,
        query: ObservationQuery,
        *,
        assured: bool,
        task_id: str,
        operation_id: str,
        prior: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]: ...

    def persist_observation(
        self,
        raw: Mapping[str, object],
        *,
        query: ObservationQuery,
        assurance: str,
    ) -> Mapping[str, object]: ...


class RunDriver(Protocol):
    def start_run(
        self,
        command: StartRun,
        *,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def run_snapshot(self, run_id: str) -> Mapping[str, object]: ...

    def persist_gate(
        self,
        run_id: str,
        gate: Gate,
        *,
        workflow_cycle_id: str,
        workflow_step_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def record_gate_response(
        self,
        command: SubmitGate | CancelRun,
        *,
        gate: Gate,
        response: Mapping[str, object],
        submission_digest: str,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def execute_step(
        self,
        run_id: str,
        *,
        operation: str,
        workflow_step_id: str,
        task_id: str,
    ) -> Mapping[str, object]: ...

    def reconcile_run(
        self,
        run_id: str,
        *,
        task_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def record_incident(
        self,
        run_id: str,
        incident: Incident,
        *,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def resolve_incident(
        self,
        run_id: str,
        incident_id: str,
        *,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def record_outcome(
        self,
        run_id: str,
        *,
        status: str,
        summary: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def defer_verification(
        self,
        run_id: str,
        *,
        workflow_step_id: str,
        operation_id: str,
    ) -> Mapping[str, object]: ...

    def project_outcome(
        self,
        turn: RunTurn,
        *,
        task_id: str,
    ) -> Mapping[str, object]: ...


class ObservationEngine:
    """Collect, automatically assure when useful, and persist one observation."""

    def __init__(self, driver: ObservationDriver) -> None:
        self.driver = driver

    @staticmethod
    def _needs_assurance(
        raw: Mapping[str, object], query: ObservationQuery
    ) -> bool:
        result = _mapping(raw.get("result"))
        capabilities = _mapping(result.get("capabilities"))
        lanes = _mapping(result.get("lanes"))
        ssh = _mapping(lanes.get("ssh"))
        mdb_index = 0
        for selector in query.selectors:
            if selector.kind == "capability":
                if any(
                    _CAPABILITY_KEYS[name] not in capabilities
                    or (
                        name == "alarms"
                        and capabilities.get(_CAPABILITY_KEYS[name]) is not True
                    )
                    for name in selector.names
                ):
                    return True
                continue
            for _query in selector.queries:
                name = "mdbctl" if mdb_index == 0 else f"mdbctl_{mdb_index + 1}"
                mdb_index += 1
                if name not in ssh:
                    return True
        return not bool(
            _text(raw.get("observed_at"))
            or _text(result.get("completed_at"))
            or _text(result.get("started_at"))
        )

    def observe(
        self,
        query: ObservationQuery,
        *,
        task_id: str,
        operation_id: str,
    ) -> ObservationResult:
        raw = self.driver.observe_once(
            query,
            assured=False,
            task_id=task_id,
            operation_id=operation_id,
        )
        assurance = "fast"
        if self._needs_assurance(raw, query):
            try:
                raw = self.driver.observe_once(
                    query,
                    assured=True,
                    task_id=task_id,
                    operation_id=f"{operation_id}-assured",
                    prior=raw,
                )
            except AssuranceUnavailable:
                pass
            else:
                assurance = "assured"
        source = self.driver.persist_observation(
            raw,
            query=query,
            assurance=assurance,
        )
        return ObservationResult(
            query=query,
            raw=dict(raw),
            assurance=assurance,
            observation_ref=ObservationRef.from_public_dict(source),
        )


class RunEngine:
    """The semantic transition authority for Agent Runs."""

    def __init__(
        self,
        driver: RunDriver,
        *,
        artifact_store: LocalArtifactStore | None = None,
    ) -> None:
        self.driver = driver
        self.artifact_store = artifact_store or LocalArtifactStore()

    @staticmethod
    def _run_id(snapshot: Mapping[str, object]) -> str:
        return _text(_projection(snapshot).get("case_id"))

    @staticmethod
    def _submission_record(
        snapshot: Mapping[str, object], submission_id: str
    ) -> Mapping[str, object] | None:
        projection = _projection(snapshot)
        records = projection.get("gate_submissions", [])
        if not isinstance(records, list):
            records = projection.get("phase_records", [])
        if not isinstance(records, list):
            return None
        return next(
            (
                record
                for record in reversed(records)
                if isinstance(record, Mapping)
                and _text(record.get("submission_id")) == submission_id
            ),
            None,
        )

    def _current_gate(
        self,
        snapshot: Mapping[str, object],
        *,
        operation_id: str,
    ) -> Gate | None:
        projection = _projection(snapshot)
        continuation = _continuation(snapshot)
        if not _text(continuation.get("required_phase_type")):
            return None
        persisted = projection.get("current_gate")
        if isinstance(persisted, Mapping) and persisted:
            if (
                _text(persisted.get("workflow_cycle_id"))
                != _text(continuation.get("workflow_cycle_id") or "cycle-1")
                or _text(persisted.get("workflow_step_id"))
                != _text(continuation.get("required_workflow_step_id"))
            ):
                raise GateConflict("persisted Gate does not match the Run continuation")
            return Gate.from_public_dict(persisted)
        phase_type = _text(continuation.get("required_phase_type"))
        owner = _text(continuation.get("required_skill"))
        step_id = _text(continuation.get("required_workflow_step_id"))
        cycle_id = _text(continuation.get("workflow_cycle_id") or "cycle-1")
        delivery = _text(projection.get("delivery_strategy"))
        schema = gate_input_schema(phase_type, delivery_strategy=delivery)
        schema_digest = fingerprint(schema)
        prior_versions = [
            int(item.get("gate_version", 0))
            for item in projection.get("run_gates", [])
            if isinstance(item, Mapping)
            and _text(item.get("workflow_cycle_id")) == cycle_id
            and _text(item.get("workflow_step_id")) == step_id
            and isinstance(item.get("gate_version", 0), int)
            and not isinstance(item.get("gate_version", 0), bool)
        ]
        version = max(prior_versions, default=0) + 1
        gate_id = "gate-" + fingerprint(
            {
                "run_id": projection.get("case_id", ""),
                "workflow_definition": projection.get("workflow_definition", {}),
                "workflow_cycle_id": cycle_id,
                "workflow_step_id": step_id,
                "phase_type": phase_type,
                "gate_version": version,
                "schema_digest": schema_digest,
            }
        )[:32]
        gate = Gate(
            gate_id=gate_id,
            version=version,
            name=phase_type,
            owner=owner,
            input_schema=schema,
            schema_digest=schema_digest,
        )
        persisted_snapshot = self.driver.persist_gate(
            self._run_id(snapshot),
            gate,
            workflow_cycle_id=cycle_id,
            workflow_step_id=step_id,
            operation_id=operation_id,
        )
        current = _projection(persisted_snapshot).get("current_gate")
        if not isinstance(current, Mapping):
            raise GateConflict("Gate persistence did not return an open Gate")
        return Gate.from_public_dict(current)

    @staticmethod
    def _validate_gate(command: SubmitGate | CancelRun, gate: Gate) -> None:
        if command.gate_id != gate.gate_id:
            raise GateConflict("Gate submission targets a different gate_id")
        if command.gate_version != gate.version:
            raise GateConflict("Gate submission targets a stale gate_version")
        if command.schema_digest != gate.schema_digest:
            raise GateConflict("Gate submission targets a different schema digest")

    @staticmethod
    def _validate_duplicate_gate(
        command: SubmitGate | CancelRun,
        prior: Mapping[str, object],
    ) -> None:
        if command.gate_id != _text(prior.get("gate_id")):
            raise GateConflict("duplicate submission targets a different gate_id")
        if command.gate_version != int(prior.get("gate_version", 0)):
            raise GateConflict("duplicate submission targets a stale gate_version")
        prior_digest = _text(
            prior.get("schema_digest") or prior.get("gate_schema_digest")
        ).removeprefix("sha256:")
        if command.schema_digest != prior_digest:
            raise GateConflict("duplicate submission targets a different schema digest")

    @staticmethod
    def _validate_schema_value(
        value: object,
        schema: Mapping[str, object],
        *,
        path: str,
    ) -> None:
        expected_type = schema.get("type")
        valid_type = {
            "object": isinstance(value, Mapping),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
        }.get(str(expected_type), True)
        if not valid_type:
            raise GateConflict(f"{path} has the wrong type")
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise GateConflict(f"{path} is not an allowed value")
        if isinstance(value, str):
            minimum = schema.get("minLength")
            maximum = schema.get("maxLength")
            if isinstance(minimum, int) and len(value) < minimum:
                raise GateConflict(f"{path} must not be empty")
            if isinstance(maximum, int) and len(value) > maximum:
                raise GateConflict(f"{path} exceeds its length limit")
        if isinstance(value, int) and not isinstance(value, bool):
            minimum = schema.get("minimum")
            if isinstance(minimum, int) and value < minimum:
                raise GateConflict(f"{path} is below its minimum")
        if isinstance(value, Mapping):
            properties = _mapping(schema.get("properties"))
            required = schema.get("required", [])
            if isinstance(required, list):
                missing = [name for name in required if name not in value]
                if missing:
                    raise GateConflict(
                        f"{path} omits required fields: {', '.join(missing)}"
                    )
            if schema.get("additionalProperties") is False:
                unexpected = sorted(set(value) - set(properties))
                if unexpected:
                    raise GateConflict(
                        f"{path} contains undeclared fields: {', '.join(unexpected)}"
                    )
            for name, item in value.items():
                child_schema = properties.get(name)
                if isinstance(child_schema, Mapping):
                    RunEngine._validate_schema_value(
                        item, child_schema, path=f"{path}.{name}"
                    )
        if isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, Mapping):
                for index, item in enumerate(value):
                    RunEngine._validate_schema_value(
                        item, item_schema, path=f"{path}[{index}]"
                    )

    @staticmethod
    def _completed_payload_required(gate: Gate) -> list[str]:
        all_of = gate.input_schema.get("allOf", [])
        if not isinstance(all_of, list) or not all_of:
            return []
        first = _mapping(all_of[0])
        then = _mapping(first.get("then"))
        properties = _mapping(then.get("properties"))
        payload = _mapping(properties.get("payload"))
        required = payload.get("required", [])
        return [str(name) for name in required] if isinstance(required, list) else []

    def _normalized_response(
        self,
        command: SubmitGate | CancelRun,
        *,
        gate: Gate,
        projection: Mapping[str, object],
    ) -> dict[str, object]:
        if isinstance(command, CancelRun):
            return {
                "status": "cancelled",
                "summary": "run cancelled at the current gate",
                "payload": {},
            }
        response = _mapping(command.response)
        self._validate_schema_value(response, gate.input_schema, path="response")
        status = _text(response.get("status")).lower()
        summary = _text(response.get("summary"))
        raw_payload = response.get("payload", {})
        if not isinstance(raw_payload, Mapping):
            raise GateConflict("Gate response payload must be an object")
        if status == "completed":
            missing = [
                name
                for name in self._completed_payload_required(gate)
                if name not in raw_payload
            ]
            if missing:
                raise GateConflict(
                    "Gate response payload omits required fields: "
                    + ", ".join(missing)
                )
        payload = dict(raw_payload)
        raw_artifact_ref = payload.get("artifact_ref")
        if isinstance(raw_artifact_ref, Mapping):
            artifact_ref = ArtifactRef.from_public_dict(raw_artifact_ref)
            payload_schema = _mapping(
                _mapping(gate.input_schema.get("properties")).get("payload")
            )
            artifact_schema = _mapping(
                _mapping(payload_schema.get("properties")).get("artifact_ref")
            )
            kind_schema = _mapping(
                _mapping(artifact_schema.get("properties")).get("kind")
            )
            expected_kinds = kind_schema.get("enum", [])
            targets = projection.get("targets", [])
            expected_target = ""
            if (
                isinstance(targets, list)
                and targets
                and isinstance(targets[0], Mapping)
            ):
                expected_target = _text(targets[0].get("address"))
            artifact_path = self.artifact_store.resolve(
                artifact_ref,
                expected_kinds=(
                    str(kind) for kind in expected_kinds
                ) if isinstance(expected_kinds, list) else (),
                expected_target=expected_target,
                expected_run_id=_text(projection.get("case_id")),
            )
            payload["artifact_ref"] = artifact_ref.to_public_dict()
            payload["artifact_path"] = str(artifact_path)
            payload["artifact_sha256"] = artifact_ref.digest
            if artifact_ref.version:
                payload["product_version"] = artifact_ref.version
        return {"status": status, "summary": summary, "payload": payload}

    @staticmethod
    def _current_incident(projection: Mapping[str, object]) -> Incident | None:
        raw = projection.get("current_incident")
        if not isinstance(raw, Mapping) or not raw:
            return None
        return Incident(
            incident_id=_text(raw.get("incident_id")),
            code=_text(raw.get("code")),
            message=_text(raw.get("message")),
            effect_id=_text(raw.get("effect_id")),
            recoverable=bool(raw.get("recoverable", True)),
        )

    @staticmethod
    def _outcome(projection: Mapping[str, object]) -> Outcome | None:
        raw = projection.get("run_outcome")
        if not isinstance(raw, Mapping) or not raw:
            return None
        return Outcome(
            status=_text(raw.get("status")),
            summary=_text(raw.get("summary")),
            acceptance=raw.get("acceptance", []),
        )

    @staticmethod
    def _unknown_mutation(
        projection: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        return next(
            (
                item
                for item in reversed(list(projection.get("operations", [])))
                if isinstance(item, Mapping)
                and _text(item.get("status")) == "mutation_outcome_unknown"
            ),
            None,
        )

    @staticmethod
    def _terminal_step(
        projection: Mapping[str, object]
    ) -> tuple[str, Mapping[str, object]] | None:
        states = projection.get("workflow_step_states", {})
        if not isinstance(states, Mapping):
            return None
        return next(
            (
                (str(step_id), state)
                for step_id, state in states.items()
                if isinstance(state, Mapping)
                and _text(state.get("workflow_cycle_id"))
                == _text(projection.get("workflow_cycle_id") or "cycle-1")
                and _text(state.get("status")) in {"failed", "cancelled"}
            ),
            None,
        )

    @staticmethod
    def _mutation_completed_before_verification(
        projection: Mapping[str, object]
    ) -> bool:
        states = projection.get("workflow_step_states", {})
        return isinstance(states, Mapping) and any(
            isinstance(state, Mapping)
            and _text(state.get("name")) in {"live_patch_run", "upgrade_run"}
            and _text(state.get("status")) in {"completed", "verified", "succeeded"}
            for state in states.values()
        )

    @staticmethod
    def _step_summary(
        projection: Mapping[str, object], step: Mapping[str, object]
    ) -> str:
        operation_id = _text(step.get("operation_id"))
        for record in reversed(list(projection.get("phase_records", []))):
            if (
                isinstance(record, Mapping)
                and _text(record.get("operation_id")) == operation_id
            ):
                return _text(record.get("summary"))
        for record in reversed(list(projection.get("operations", []))):
            if (
                isinstance(record, Mapping)
                and _text(record.get("operation_id")) == operation_id
            ):
                return _text(record.get("summary"))
        return "workflow step did not complete"

    def _turn(
        self,
        snapshot: Mapping[str, object],
        *,
        gate: Gate | Mapping[str, object] | None = None,
        observation_ref: ObservationRef | None = None,
        state: str = "",
        next_action: str = "",
    ) -> RunTurn:
        projection = _projection(snapshot)
        outcome = self._outcome(projection)
        incident = self._current_incident(projection)
        selected_state = state or (
            outcome.status
            if outcome is not None
            else "incident"
            if incident is not None
            else _text(projection.get("status") or "running")
        )
        gaps: tuple[object, ...] = ()
        if projection.get("closeout_recovery_gap"):
            gaps = (_text(projection.get("closeout_recovery_gap")),)
        return RunTurn(
            run_id=self._run_id(snapshot),
            state=selected_state,
            gate=gate,
            incident=incident,
            gaps=gaps,
            outcome=outcome,
            next_action=next_action,
            observation_ref=observation_ref,
        )

    def _project_terminal(self, turn: RunTurn, *, task_id: str) -> RunTurn:
        if turn.outcome is None:
            return turn
        try:
            recorded = bool(self.driver.project_outcome(turn, task_id=task_id))
        except Exception as exc:
            return replace(
                turn,
                gaps=(
                    *turn.gaps,
                    f"session_outcome_projection_failed: {type(exc).__name__}",
                ),
            )
        return replace(turn, outcome_recorded=recorded)

    def _record_incident(
        self,
        snapshot: Mapping[str, object],
        *,
        code: str,
        message: str,
        effect_id: str,
        operation_id: str,
    ) -> Mapping[str, object]:
        run_id = self._run_id(snapshot)
        incident = Incident(
            incident_id="incident-" + fingerprint(
                {
                    "run_id": run_id,
                    "code": code,
                    "effect_id": effect_id,
                    "message": message,
                }
            )[:32],
            code=code,
            message=message,
            effect_id=effect_id,
        )
        return self.driver.record_incident(
            run_id, incident, operation_id=operation_id
        )

    def _advance(
        self,
        snapshot: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        observation_ref: ObservationRef | None = None,
        allow_auto_reconcile: bool = True,
    ) -> RunTurn:
        for _step_index in range(WORKFLOW_INTERNAL_MAX_STEPS):
            projection = _projection(snapshot)
            outcome = self._outcome(projection)
            if outcome is not None:
                return self._project_terminal(
                    self._turn(snapshot, observation_ref=observation_ref),
                    task_id=task_id,
                )
            current_incident = self._current_incident(projection)
            unknown = self._unknown_mutation(projection)
            if unknown is not None and allow_auto_reconcile:
                try:
                    snapshot = self.driver.reconcile_run(
                        self._run_id(snapshot),
                        task_id=task_id,
                        operation_id=f"{operation_id}-auto-reconcile",
                    )
                except Exception as exc:
                    snapshot = self.driver.run_snapshot(self._run_id(snapshot))
                    unknown = self._unknown_mutation(_projection(snapshot))
                    if unknown is not None:
                        snapshot = self._record_incident(
                            snapshot,
                            code="mutation_outcome_unknown",
                            message=f"{type(exc).__name__}: {exc}",
                            effect_id=_text(unknown.get("operation_id")),
                            operation_id=f"{operation_id}-incident",
                        )
                        return self._turn(snapshot, state="incident")
                else:
                    if current_incident is not None:
                        snapshot = self.driver.resolve_incident(
                            self._run_id(snapshot),
                            current_incident.incident_id,
                            operation_id=f"{operation_id}-incident-resolved",
                        )
                    continue
            if unknown is not None:
                snapshot = self._record_incident(
                    snapshot,
                    code="mutation_outcome_unknown",
                    message="mutation outcome remains unknown after automatic reconcile",
                    effect_id=_text(unknown.get("operation_id")),
                    operation_id=f"{operation_id}-incident",
                )
                return self._turn(snapshot, state="incident")
            if current_incident is not None:
                return self._turn(snapshot, state="incident")
            terminal = self._terminal_step(projection)
            if terminal is not None:
                terminal_step_id, terminal_step = terminal
                if (
                    _text(terminal_step.get("name")) == "debug_collect"
                    and _text(terminal_step.get("status")) == "failed"
                    and self._mutation_completed_before_verification(projection)
                ):
                    snapshot = self.driver.defer_verification(
                        self._run_id(snapshot),
                        workflow_step_id=terminal_step_id,
                        operation_id=f"{operation_id}-verification-deferred",
                    )
                    return self._turn(
                        snapshot,
                        state="running",
                        next_action="resume the Run to retry fresh target verification",
                    )
                status = _text(terminal_step.get("status"))
                snapshot = self.driver.record_outcome(
                    self._run_id(snapshot),
                    status=status,
                    summary=self._step_summary(projection, terminal_step),
                    operation_id=f"{operation_id}-outcome",
                )
                continue
            continuation = _continuation(snapshot)
            if bool(continuation.get("workflow_complete")):
                snapshot = self.driver.record_outcome(
                    self._run_id(snapshot),
                    status="completed",
                    summary="workflow completed",
                    operation_id=f"{operation_id}-outcome",
                )
                continue
            required_phase = _text(continuation.get("required_phase_type"))
            if required_phase:
                gate = self._current_gate(
                    snapshot,
                    operation_id=f"{operation_id}-gate",
                )
                if gate is None:
                    raise GateConflict("Run continuation did not produce a Gate")
                return self._turn(
                    self.driver.run_snapshot(self._run_id(snapshot)),
                    gate=gate,
                    observation_ref=observation_ref,
                    state="waiting_response",
                    next_action="respond to the current Gate",
                )
            required_operation = _text(continuation.get("required_operation"))
            workflow_step_id = _text(
                continuation.get("required_workflow_step_id")
            )
            if required_operation:
                try:
                    snapshot = self.driver.execute_step(
                        self._run_id(snapshot),
                        operation=required_operation,
                        workflow_step_id=workflow_step_id,
                        task_id=task_id,
                    )
                except Exception as exc:
                    snapshot = self.driver.run_snapshot(self._run_id(snapshot))
                    if self._unknown_mutation(_projection(snapshot)) is not None:
                        continue
                    terminal = self._terminal_step(_projection(snapshot))
                    if terminal is not None:
                        continue
                    snapshot = self._record_incident(
                        snapshot,
                        code="domain_execution_failed",
                        message=f"{type(exc).__name__}: {exc}",
                        effect_id=required_operation,
                        operation_id=f"{operation_id}-incident",
                    )
                    return self._turn(snapshot, state="incident")
                state = _mapping(
                    _mapping(_projection(snapshot).get("workflow_step_states")).get(
                        workflow_step_id
                    )
                )
                if _text(state.get("status")) in {"accepted", "running"}:
                    return self._turn(
                        snapshot,
                        observation_ref=observation_ref,
                        state="running",
                        next_action="resume the Run to reattach to the current Effect",
                    )
                continue
            snapshot = self._record_incident(
                snapshot,
                code="invalid_run_continuation",
                message="Run has no Gate, operation, or terminal Outcome",
                effect_id="",
                operation_id=f"{operation_id}-incident",
            )
            return self._turn(snapshot, state="incident")
        snapshot = self._record_incident(
            snapshot,
            code="internal_step_limit",
            message="workflow exceeded the internal 64-step progression limit",
            effect_id="",
            operation_id=f"{operation_id}-incident-limit",
        )
        return self._turn(snapshot, state="incident")

    def _submit_gate(
        self,
        command: SubmitGate | CancelRun,
        *,
        task_id: str,
        operation_id: str,
    ) -> RunTurn:
        snapshot = self.driver.run_snapshot(command.run_id)
        prior = self._submission_record(snapshot, command.submission_id)
        if prior is not None:
            self._validate_duplicate_gate(command, prior)
            persisted_gate = next(
                (
                    item
                    for item in _projection(snapshot).get("run_gates", [])
                    if isinstance(item, Mapping)
                    and _text(item.get("gate_id")) == command.gate_id
                    and int(item.get("gate_version", 0)) == command.gate_version
                ),
                None,
            )
            if not isinstance(persisted_gate, Mapping):
                raise GateConflict("duplicate submission Gate is unavailable")
            gate = Gate.from_public_dict(persisted_gate)
            response = self._normalized_response(
                command, gate=gate, projection=_projection(snapshot)
            )
            submission_digest = fingerprint(response)
            if _text(prior.get("submission_digest")) != submission_digest:
                raise CommandConflict(
                    "submission_id was already used with different Gate input"
                )
            return self._advance(
                snapshot,
                task_id=task_id,
                operation_id=f"{operation_id}-duplicate",
            )
        gate = self._current_gate(snapshot, operation_id=f"{operation_id}-gate")
        if gate is None:
            raise GateConflict("Run is not waiting at a Gate")
        self._validate_gate(command, gate)
        response = self._normalized_response(
            command, gate=gate, projection=_projection(snapshot)
        )
        submission_digest = fingerprint(response)
        snapshot = self.driver.record_gate_response(
            command,
            gate=gate,
            response=response,
            submission_digest=submission_digest,
            task_id=task_id,
            operation_id=f"{operation_id}-response",
        )
        return self._advance(
            snapshot,
            task_id=task_id,
            operation_id=operation_id,
        )

    def execute(
        self,
        command: RunCommand,
        *,
        task_id: str,
        operation_id: str,
    ) -> RunTurn:
        if isinstance(command, StartRun):
            snapshot = self.driver.start_run(
                command,
                task_id=task_id,
                operation_id=operation_id,
            )
            return self._advance(
                snapshot,
                task_id=task_id,
                operation_id=operation_id,
                observation_ref=command.observation_ref,
            )
        if isinstance(command, (SubmitGate, CancelRun)):
            return self._submit_gate(
                command,
                task_id=task_id,
                operation_id=operation_id,
            )
        if isinstance(command, ReconcileRun):
            snapshot = self.driver.reconcile_run(
                command.run_id,
                task_id=task_id,
                operation_id=operation_id,
            )
            current = self._current_incident(_projection(snapshot))
            if (
                current is not None
                and self._unknown_mutation(_projection(snapshot)) is None
            ):
                snapshot = self.driver.resolve_incident(
                    command.run_id,
                    current.incident_id,
                    operation_id=f"{operation_id}-incident-resolved",
                )
            return self._advance(
                snapshot,
                task_id=task_id,
                operation_id=operation_id,
                allow_auto_reconcile=False,
            )
        if isinstance(command, ResumeRun):
            return self._advance(
                self.driver.run_snapshot(command.run_id),
                task_id=task_id,
                operation_id=operation_id,
            )
        raise TypeError(f"unsupported RunCommand: {type(command).__name__}")


class SemanticRuntime(SemanticRuntimePort):
    """Compose ObservationEngine and RunEngine behind the two-method seam."""

    def __init__(
        self,
        observation_engine: ObservationEngine,
        run_engine: RunEngine,
    ) -> None:
        self.observation_engine = observation_engine
        self.run_engine = run_engine

    def observe(
        self,
        query: ObservationQuery,
        *,
        task_id: str,
        operation_id: str,
    ) -> ObservationResult:
        return self.observation_engine.observe(
            query,
            task_id=task_id,
            operation_id=operation_id,
        )

    def execute(
        self,
        command: RunCommand,
        *,
        task_id: str,
        operation_id: str,
    ) -> RunTurn:
        return self.run_engine.execute(
            command,
            task_id=task_id,
            operation_id=operation_id,
        )
