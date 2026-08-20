"""Typed semantic contracts shared by the Agent Gateway and Runtime Core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Protocol, TypeAlias

from .contracts import RUNTIME_API_VERSION


SEMANTIC_RUNTIME_SCHEMA = f"{RUNTIME_API_VERSION}/semantic-runtime-v1"
OBSERVATION_REF_SCHEMA = f"{SEMANTIC_RUNTIME_SCHEMA}/observation-ref"
ARTIFACT_REF_SCHEMA = f"{SEMANTIC_RUNTIME_SCHEMA}/artifact-ref"
AGENT_REQUEST_MAX_BYTES = 256 * 1024
AGENT_REQUEST_MAX_DEPTH = 32
AGENT_REQUEST_MAX_CONTAINER_ITEMS = 1024
AGENT_REQUEST_MAX_NODES = 8192
AGENT_REQUEST_MAX_STRING_BYTES = 128 * 1024
AGENT_REQUEST_MAX_KEY_BYTES = 256
GATE_SCHEMA_MAX_BYTES = 4 * 1024
OBSERVATION_SCOPE_MAX_BYTES = 2 * 1024
TARGET_MAX_BYTES = 512
SELECTOR_ID_MAX_BYTES = 64
SELECTOR_MAX_ITEMS = 16
CAPABILITY_NAME_MAX_BYTES = 64
CAPABILITY_MAX_ITEMS = 16
MDB_QUERY_MAX_BYTES = 1024
MDB_QUERY_MAX_ITEMS = 32

_ASSURANCE_LEVELS = frozenset({"auto", "fast", "assured"})
_CAPABILITY_ALIASES = frozenset(
    {"ssh", "telnet", "mdbctl", "busctl", "dbus", "alarms"}
)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SemanticRuntimeError(ValueError):
    """Base error for the typed semantic Runtime interface."""


class AgentGatewayError(SemanticRuntimeError):
    """Raised when an Agent request cannot be decoded safely."""


class ScopeViolation(AgentGatewayError):
    """Raised when an observation requests an undeclared evidence surface."""


class GateConflict(SemanticRuntimeError):
    """Raised when a Gate submission targets stale or unrelated Gate state."""


class CommandConflict(SemanticRuntimeError):
    """Raised when one durable submission identity is reused with new input."""


class ReferenceViolation(SemanticRuntimeError):
    """Raised when an ObservationRef or ArtifactRef is malformed."""


class AssuranceUnavailable(SemanticRuntimeError):
    """Raised when no scope-preserving assurance Adapter is available."""


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(value: object) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def bounded_request(value: Mapping[str, object]) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > AGENT_REQUEST_MAX_NODES:
            raise AgentGatewayError("Agent request exceeds the 8192-node input budget")
        if isinstance(current, str):
            if len(current.encode("utf-8")) > AGENT_REQUEST_MAX_STRING_BYTES:
                raise AgentGatewayError(
                    "Agent request string exceeds the 128 KiB input budget"
                )
            continue
        if isinstance(current, Mapping):
            if depth > AGENT_REQUEST_MAX_DEPTH:
                raise AgentGatewayError(
                    "Agent request exceeds the 32-level nesting budget"
                )
            if len(current) > AGENT_REQUEST_MAX_CONTAINER_ITEMS:
                raise AgentGatewayError(
                    "Agent request object exceeds the 1024-field input budget"
                )
            for key, item in current.items():
                if not isinstance(key, str):
                    raise AgentGatewayError("Agent request object keys must be strings")
                if len(key.encode("utf-8")) > AGENT_REQUEST_MAX_KEY_BYTES:
                    raise AgentGatewayError(
                        "Agent request object key exceeds the 256-byte input budget"
                    )
                pending.append((item, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            if depth > AGENT_REQUEST_MAX_DEPTH:
                raise AgentGatewayError(
                    "Agent request exceeds the 32-level nesting budget"
                )
            if len(current) > AGENT_REQUEST_MAX_CONTAINER_ITEMS:
                raise AgentGatewayError(
                    "Agent request array exceeds the 1024-item input budget"
                )
            pending.extend((item, depth + 1) for item in current)
    if len(json_bytes(value)) > AGENT_REQUEST_MAX_BYTES:
        raise AgentGatewayError("Agent request exceeds the 256 KiB input budget")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True)
class ObservationSelector:
    selector_id: str
    kind: str
    names: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()

    @classmethod
    def from_value(
        cls, value: Mapping[str, object], index: int
    ) -> "ObservationSelector":
        unexpected = set(value) - {"id", "kind", "names", "queries"}
        if unexpected:
            raise ScopeViolation(
                "selector contains undeclared fields: "
                + ", ".join(sorted(unexpected))
            )
        kind = _text(value.get("kind")).lower()
        selector_id = _text(value.get("id")) or f"selector-{index}"
        if len(selector_id.encode("utf-8")) > SELECTOR_ID_MAX_BYTES:
            raise ScopeViolation("selector id exceeds the 64-byte limit")
        if kind == "capability":
            raw_names = value.get("names", [])
            if not isinstance(raw_names, list) or not raw_names:
                raise ScopeViolation(
                    "capability selector requires a non-empty names array"
                )
            if len(raw_names) > CAPABILITY_MAX_ITEMS:
                raise ScopeViolation("capability selector exceeds the 16-name limit")
            if not all(isinstance(item, str) and item.strip() for item in raw_names):
                raise ScopeViolation("capability names must be non-empty strings")
            if any(
                len(item.strip().encode("utf-8")) > CAPABILITY_NAME_MAX_BYTES
                for item in raw_names
            ):
                raise ScopeViolation("capability name exceeds the 64-byte limit")
            names = tuple(dict.fromkeys(_text(item).lower() for item in raw_names))
            unsupported = sorted(set(names) - _CAPABILITY_ALIASES)
            if unsupported:
                raise ScopeViolation(
                    "unsupported capability selectors: " + ", ".join(unsupported)
                )
            return cls(selector_id=selector_id, kind=kind, names=names)
        if kind == "mdb":
            raw_queries = value.get("queries", [])
            if not isinstance(raw_queries, list) or not raw_queries:
                raise ScopeViolation("mdb selector requires a non-empty queries array")
            if len(raw_queries) > MDB_QUERY_MAX_ITEMS:
                raise ScopeViolation("mdb selector exceeds the 32-query limit")
            if not all(isinstance(item, str) for item in raw_queries):
                raise ScopeViolation("mdb queries must be strings")
            queries = tuple(_text(item) for item in raw_queries)
            if any(not query for query in queries):
                raise ScopeViolation("mdb queries must not be empty")
            if any(
                len(query.encode("utf-8")) > MDB_QUERY_MAX_BYTES
                for query in queries
            ):
                raise ScopeViolation("mdb query exceeds the 1024-byte limit")
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
class ObservationQuery:
    """One immutable, exact, read-only observation scope."""

    target: str
    selectors: tuple[ObservationSelector, ...]
    freshness_mode: str = "live"
    max_age_seconds: int = 0
    deadline: float = 180.0
    assurance: str = "auto"

    @classmethod
    def from_query(cls, query: Mapping[str, object]) -> "ObservationQuery":
        bounded_request(query)
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
        if len(target.encode("utf-8")) > TARGET_MAX_BYTES:
            raise ScopeViolation("target exceeds the 512-byte limit")
        raw_selectors = query.get("selectors")
        if not isinstance(raw_selectors, list) or not raw_selectors:
            raise ScopeViolation("selectors must be a non-empty array")
        if len(raw_selectors) > SELECTOR_MAX_ITEMS:
            raise ScopeViolation("selectors exceed the 16-item limit")
        selectors = tuple(
            ObservationSelector.from_value(_mapping(value), index)
            for index, value in enumerate(raw_selectors, start=1)
        )
        selector_ids = [selector.selector_id for selector in selectors]
        if len(set(selector_ids)) != len(selector_ids):
            raise ScopeViolation("selector ids must be unique")
        freshness = _mapping(query.get("freshness"))
        if set(freshness) - {"mode", "max_age_seconds"}:
            raise ScopeViolation("freshness contains undeclared fields")
        freshness_mode = _text(freshness.get("mode") or "live").lower()
        max_age = freshness.get("max_age_seconds", 0)
        if freshness_mode != "live":
            raise ScopeViolation("only live evidence is supported by the Agent interface")
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age != 0:
            raise ScopeViolation("live evidence requires max_age_seconds=0")
        legacy_assurance = _text(query.get("assurance") or "auto").lower()
        if legacy_assurance not in _ASSURANCE_LEVELS:
            raise ScopeViolation("assurance must be auto, fast, or assured")
        deadline = query.get("deadline", 180)
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            raise ScopeViolation("deadline must be a positive number")
        if float(deadline) <= 0:
            raise ScopeViolation("deadline must be a positive number")
        contract = cls(
            target=target,
            selectors=selectors,
            freshness_mode=freshness_mode,
            max_age_seconds=max_age,
            deadline=float(deadline),
            assurance="auto",
        )
        if len(json_bytes(contract.to_public_dict())) > OBSERVATION_SCOPE_MAX_BYTES:
            raise ScopeViolation("observation scope exceeds the 2KB budget")
        return contract

    def runtime_arguments(self, *, assured: bool) -> dict[str, object]:
        queries = [
            query
            for selector in self.selectors
            if selector.kind == "mdb"
            for query in selector.queries
        ]
        return {
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
            "profile": "mdb",
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "selectors": [selector.to_public_dict() for selector in self.selectors],
            "freshness": {
                "mode": self.freshness_mode,
                "max_age_seconds": self.max_age_seconds,
            },
        }


# Compatibility names remain importable while callers migrate to the domain language.
SelectorContract = ObservationSelector
ScopeContract = ObservationQuery


@dataclass(frozen=True)
class ObservationRef:
    handle: str
    digest: str
    size: int = 0
    provenance: str = "runtime-observation"
    retention_hint: str = "run-lifetime"
    kind: str = "observation"
    target: str = ""
    scope_digest: str = ""
    observed_at: str = ""
    target_fingerprint: str = ""
    target_epoch: int = 0

    def __post_init__(self) -> None:
        digest = self.digest.removeprefix("sha256:").lower()
        if not self.handle.strip():
            raise ReferenceViolation("ObservationRef handle is required")
        if _SHA256.fullmatch(digest) is None:
            raise ReferenceViolation("ObservationRef digest must be SHA-256")
        if isinstance(self.size, bool) or self.size < 0:
            raise ReferenceViolation("ObservationRef size must be non-negative")
        if self.kind != "observation":
            raise ReferenceViolation("ObservationRef kind must be observation")
        if self.provenance != "runtime-observation":
            raise ReferenceViolation(
                "ObservationRef provenance must be runtime-observation"
            )
        if not self.retention_hint.strip():
            raise ReferenceViolation("ObservationRef retention_hint is required")
        if not self.target.strip():
            raise ReferenceViolation("ObservationRef target is required")
        scope_digest = self.scope_digest.removeprefix("sha256:").lower()
        if _SHA256.fullmatch(scope_digest) is None:
            raise ReferenceViolation("ObservationRef scope_digest must be SHA-256")
        if not self.observed_at.strip():
            raise ReferenceViolation("ObservationRef observed_at is required")
        if isinstance(self.target_epoch, bool) or self.target_epoch < 0:
            raise ReferenceViolation("ObservationRef target_epoch must be non-negative")
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "scope_digest", scope_digest)

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "ObservationRef":
        digest = _text(value.get("digest") or value.get("sha256"))
        handle = _text(value.get("handle") or value.get("uri"))
        raw_size = value.get("size", value.get("byte_count", 0))
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else 0
        return cls(
            handle=handle,
            digest=digest,
            size=size,
            provenance=_text(value.get("provenance")),
            retention_hint=_text(value.get("retention_hint")),
            kind=_text(value.get("kind")),
            target=_text(value.get("target")),
            scope_digest=_text(value.get("scope_digest")),
            observed_at=_text(value.get("observed_at")),
            target_fingerprint=_text(value.get("target_fingerprint")),
            target_epoch=(
                value.get("target_epoch", 0)
                if isinstance(value.get("target_epoch", 0), int)
                and not isinstance(value.get("target_epoch", 0), bool)
                else 0
            ),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OBSERVATION_REF_SCHEMA,
            "handle": self.handle,
            "digest": f"sha256:{self.digest}",
            "kind": self.kind,
            "size": self.size,
            "provenance": self.provenance,
            "retention_hint": self.retention_hint,
            "target": self.target,
            "scope_digest": (
                f"sha256:{self.scope_digest}" if self.scope_digest else ""
            ),
            "observed_at": self.observed_at,
            "target_fingerprint": self.target_fingerprint,
            "target_epoch": self.target_epoch,
        }

    def to_source_dict(self) -> dict[str, object]:
        return {
            "schema": f"{RUNTIME_API_VERSION}/observation-source-v1",
            "blob_id": self.digest,
            "sha256": self.digest,
            "uri": self.handle,
            "byte_count": self.size,
            "kind": self.kind,
            "provenance": self.provenance,
            "retention_hint": self.retention_hint,
            "target": self.target,
            "scope_digest": self.scope_digest,
            "observed_at": self.observed_at,
            "target_fingerprint": self.target_fingerprint,
            "target_epoch": self.target_epoch,
        }


@dataclass(frozen=True)
class ArtifactRef:
    handle: str
    digest: str
    kind: str
    size: int = 0
    provenance: str = "external-build"
    retention_hint: str = "run-lifetime"
    version: str = ""
    target: str = ""
    run_id: str = ""

    def __post_init__(self) -> None:
        digest = self.digest.removeprefix("sha256:").lower()
        if not self.handle.strip():
            raise ReferenceViolation("ArtifactRef handle is required")
        if _SHA256.fullmatch(digest) is None:
            raise ReferenceViolation("ArtifactRef digest must be SHA-256")
        if not self.kind.strip():
            raise ReferenceViolation("ArtifactRef kind is required")
        if isinstance(self.size, bool) or self.size < 0:
            raise ReferenceViolation("ArtifactRef size must be non-negative")
        if not self.provenance.strip():
            raise ReferenceViolation("ArtifactRef provenance is required")
        if not self.retention_hint.strip():
            raise ReferenceViolation("ArtifactRef retention_hint is required")
        if not self.target.strip():
            raise ReferenceViolation("ArtifactRef target is required")
        if not self.run_id.strip():
            raise ReferenceViolation("ArtifactRef run_id is required")
        object.__setattr__(self, "digest", digest)

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "ArtifactRef":
        raw_size = value.get("size", 0)
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else 0
        return cls(
            handle=_text(value.get("handle") or value.get("path")),
            digest=_text(value.get("digest") or value.get("sha256")),
            kind=_text(value.get("kind")),
            size=size,
            provenance=_text(value.get("provenance")),
            retention_hint=_text(value.get("retention_hint")),
            version=_text(value.get("version") or value.get("product_version")),
            target=_text(value.get("target")),
            run_id=_text(value.get("run_id")),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": ARTIFACT_REF_SCHEMA,
            "handle": self.handle,
            "digest": f"sha256:{self.digest}",
            "kind": self.kind,
            "size": self.size,
            "provenance": self.provenance,
            "retention_hint": self.retention_hint,
            "version": self.version,
            "target": self.target,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class StartRun:
    target: str
    intent: str
    purpose: str
    delivery_strategy: str
    command_id: str
    input_digest: str
    observation_ref: ObservationRef | None = None
    legacy_observation_receipt: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SubmitGate:
    run_id: str
    response: Mapping[str, object]
    gate_id: str
    gate_version: int
    schema_digest: str
    submission_id: str = ""


@dataclass(frozen=True)
class ResumeRun:
    run_id: str


@dataclass(frozen=True)
class CancelRun:
    run_id: str
    gate_id: str
    gate_version: int
    schema_digest: str
    submission_id: str = ""


@dataclass(frozen=True)
class ReconcileRun:
    run_id: str


RunCommand: TypeAlias = StartRun | SubmitGate | ResumeRun | CancelRun | ReconcileRun


def _gate_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AgentGatewayError("gate_version must be a positive integer")
    return value


def _gate_id(value: object) -> str:
    selected = _text(value)
    if _SAFE_ID.fullmatch(selected) is None:
        raise AgentGatewayError("gate_id must be a safe 1-128 character identifier")
    return selected


def _schema_digest(value: object) -> str:
    selected = _text(value).removeprefix("sha256:").lower()
    if _SHA256.fullmatch(selected) is None:
        raise AgentGatewayError("schema_digest must be SHA-256")
    return selected


def _submission_id(value: object, *, binding: Mapping[str, object]) -> str:
    selected = _text(value) or "gate-submit-" + fingerprint(binding)[:32]
    if _SAFE_ID.fullmatch(selected) is None:
        raise AgentGatewayError("submission_id must be a safe 1-128 character identifier")
    return selected


def decode_run_command(
    action: Mapping[str, object], *, operation_id: str
) -> RunCommand:
    bounded_request(action)
    kind = _text(action.get("kind")).lower()
    if kind == "start":
        command_id = _text(operation_id)
        if _SAFE_ID.fullmatch(command_id) is None:
            raise AgentGatewayError(
                "StartRun operation_id must be a safe 1-128 character identifier"
            )
        if isinstance(action.get("workflow"), Mapping):
            raise AgentGatewayError(
                "dynamic workflow objects are not supported; choose intent and delivery_strategy"
            )
        target = _text(action.get("target"))
        if not target:
            raise AgentGatewayError("start requires target")
        intent = _text(action.get("intent") or "diagnosis-only").lower()
        raw_delivery = _text(action.get("delivery_strategy")).lower()
        delivery = raw_delivery or (
            "source-only" if intent == "diagnose-and-fix" else ""
        )
        if delivery and delivery not in {
            "source-only",
            "live-patch",
            "build-upgrade",
        }:
            raise AgentGatewayError("unsupported delivery_strategy")
        observation_ref = None
        raw_ref = action.get("observation_ref")
        legacy_receipt = action.get("observation_receipt")
        if isinstance(raw_ref, Mapping):
            observation_ref = ObservationRef.from_public_dict(raw_ref)
        if isinstance(legacy_receipt, Mapping):
            if observation_ref is not None:
                raise AgentGatewayError(
                    "start accepts observation_ref or observation_receipt, not both"
                )
            source = _mapping(
                legacy_receipt.get("observation_ref")
                or legacy_receipt.get("source")
            )
            observation_ref = ObservationRef.from_public_dict(source)
        purpose = _text(action.get("purpose") or "complete the requested workflow")
        semantic_input = {
            "schema": f"{SEMANTIC_RUNTIME_SCHEMA}/start-input-v1",
            "target": target,
            "intent": intent,
            "purpose": purpose,
            "delivery_strategy": delivery,
            "observation_ref": (
                observation_ref.to_public_dict()
                if observation_ref is not None
                else None
            ),
        }
        return StartRun(
            target=target,
            intent=intent,
            purpose=purpose,
            delivery_strategy=delivery,
            command_id=command_id,
            input_digest=fingerprint(semantic_input),
            observation_ref=observation_ref,
            legacy_observation_receipt=(
                dict(legacy_receipt) if isinstance(legacy_receipt, Mapping) else None
            ),
        )
    run_id = _text(action.get("run_id"))
    if not run_id:
        raise AgentGatewayError(f"{kind or 'execute'} requires run_id")
    if kind == "respond":
        response = action.get("response")
        if not isinstance(response, Mapping):
            raise AgentGatewayError("respond requires a response object")
        gate_id = _gate_id(action.get("gate_id"))
        gate_version = _gate_version(action.get("gate_version"))
        schema_digest = _schema_digest(action.get("schema_digest"))
        return SubmitGate(
            run_id=run_id,
            response=dict(response),
            gate_id=gate_id,
            gate_version=gate_version,
            schema_digest=schema_digest,
            submission_id=_submission_id(
                action.get("submission_id"),
                binding={
                    "run_id": run_id,
                    "gate_id": gate_id,
                    "gate_version": gate_version,
                    "schema_digest": schema_digest,
                },
            ),
        )
    if kind == "resume":
        return ResumeRun(run_id)
    if kind == "control":
        command = _text(action.get("command")).lower()
        if command == "cancel":
            gate_id = _gate_id(action.get("gate_id"))
            gate_version = _gate_version(action.get("gate_version"))
            schema_digest = _schema_digest(action.get("schema_digest"))
            return CancelRun(
                run_id=run_id,
                gate_id=gate_id,
                gate_version=gate_version,
                schema_digest=schema_digest,
                submission_id=_submission_id(
                    action.get("submission_id"),
                    binding={
                        "run_id": run_id,
                        "gate_id": gate_id,
                        "gate_version": gate_version,
                        "schema_digest": schema_digest,
                    },
                ),
            )
        if command == "reconcile":
            return ReconcileRun(run_id)
        if command == "continue":
            return ResumeRun(run_id)
        raise AgentGatewayError("control command must be continue, reconcile, or cancel")
    raise AgentGatewayError("execute kind must be start, respond, resume, or control")


@dataclass(frozen=True)
class Gate:
    gate_id: str
    version: int
    name: str
    owner: str
    input_schema: Mapping[str, object]
    schema_digest: str
    kind: str = "phase"

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "Gate":
        raw_version = value.get("gate_version", value.get("version", 0))
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise GateConflict("persisted Gate version is invalid")
        schema = value.get("input_schema")
        if not isinstance(schema, Mapping):
            raise GateConflict("persisted Gate schema is invalid")
        schema_digest = _schema_digest(value.get("schema_digest"))
        if fingerprint(schema) != schema_digest:
            raise GateConflict("persisted Gate schema digest is invalid")
        return cls(
            gate_id=_gate_id(value.get("gate_id")),
            version=_gate_version(raw_version),
            name=_text(value.get("name")),
            owner=_text(value.get("owner")),
            input_schema=dict(schema),
            schema_digest=schema_digest,
            kind=_text(value.get("kind") or "phase"),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "gate_id": self.gate_id,
            "gate_version": self.version,
            "schema_digest": f"sha256:{self.schema_digest}",
            "name": self.name,
            "owner": self.owner,
            "input_schema": dict(self.input_schema),
        }


@dataclass(frozen=True)
class Incident:
    incident_id: str
    code: str
    message: str
    effect_id: str = ""
    recoverable: bool = True

    def to_public_dict(self) -> dict[str, object]:
        return {
            "incident_id": self.incident_id,
            "code": self.code,
            "message": self.message,
            "effect_id": self.effect_id,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True)
class Outcome:
    status: str
    summary: str
    acceptance: object = field(default_factory=list)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "summary": self.summary,
            "acceptance": self.acceptance,
        }


def run_turn_facts(
    projection: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
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
            if isinstance(target_epoch, int) and not isinstance(target_epoch, bool):
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


@dataclass(frozen=True)
class RunTurn:
    run_id: str
    state: str
    gate: Gate | Mapping[str, object] | None = None
    incident: Incident | None = None
    facts: tuple[Mapping[str, object], ...] = ()
    gaps: tuple[object, ...] = ()
    outcome: Outcome | None = None
    next_action: str = ""
    observation_ref: ObservationRef | None = None
    outcome_recorded: bool = False

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "RunTurn":
        raw_gate = value.get("gate")
        gate = (
            Gate.from_public_dict(raw_gate)
            if isinstance(raw_gate, Mapping) and raw_gate
            else None
        )
        raw_incident = value.get("incident")
        incident = (
            Incident(
                incident_id=_text(raw_incident.get("incident_id")),
                code=_text(raw_incident.get("code")),
                message=_text(raw_incident.get("message")),
                effect_id=_text(raw_incident.get("effect_id")),
                recoverable=bool(raw_incident.get("recoverable", True)),
            )
            if isinstance(raw_incident, Mapping) and raw_incident
            else None
        )
        raw_outcome = value.get("outcome")
        outcome = (
            Outcome(
                status=_text(raw_outcome.get("status")),
                summary=_text(raw_outcome.get("summary")),
                acceptance=raw_outcome.get("acceptance", []),
            )
            if isinstance(raw_outcome, Mapping) and raw_outcome
            else None
        )
        raw_facts = value.get("facts", [])
        facts = tuple(
            dict(item)
            for item in raw_facts
            if isinstance(item, Mapping)
        ) if isinstance(raw_facts, list) else ()
        raw_gaps = value.get("gaps", [])
        gaps = tuple(raw_gaps) if isinstance(raw_gaps, list) else ()
        raw_observation_ref = value.get("observation_ref")
        observation_ref = (
            ObservationRef.from_public_dict(raw_observation_ref)
            if isinstance(raw_observation_ref, Mapping) and raw_observation_ref
            else None
        )
        return cls(
            run_id=_text(value.get("run_id")),
            state=_text(value.get("state")),
            gate=gate,
            incident=incident,
            facts=facts,
            gaps=gaps,
            outcome=outcome,
            next_action=_text(value.get("next")),
            observation_ref=observation_ref,
            outcome_recorded=bool(value.get("outcome_recorded", False)),
        )

    def to_public_dict(self) -> dict[str, object]:
        gate = (
            self.gate.to_public_dict()
            if isinstance(self.gate, Gate)
            else dict(self.gate)
            if isinstance(self.gate, Mapping)
            else None
        )
        result: dict[str, object] = {
            "run_id": self.run_id,
            "state": self.state,
            "gate": gate,
            "incident": (
                self.incident.to_public_dict() if self.incident is not None else None
            ),
            "facts": [dict(item) for item in self.facts],
            "gaps": list(self.gaps),
            "outcome": (
                self.outcome.to_public_dict() if self.outcome is not None else None
            ),
            "next": self.next_action,
        }
        if self.observation_ref is not None:
            result["observation_ref"] = self.observation_ref.to_public_dict()
        if self.outcome_recorded:
            result["outcome_recorded"] = True
        return result


@dataclass(frozen=True)
class ObservationResult:
    query: ObservationQuery
    raw: Mapping[str, object]
    assurance: str
    observation_ref: ObservationRef


class SemanticRuntimePort(Protocol):
    """The complete Runtime seam used by the Agent Gateway."""

    def observe(
        self,
        query: ObservationQuery,
        *,
        task_id: str,
        operation_id: str,
    ) -> ObservationResult: ...

    def execute(
        self,
        command: RunCommand,
        *,
        task_id: str,
        operation_id: str,
    ) -> RunTurn: ...
