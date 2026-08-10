"""Persistent bounded context for openUBMC MCP operations.

The module deliberately keeps transport and domain execution outside.  It owns
case history, idempotency receipts, evidence blobs, compact agent envelopes,
and lifecycle policy while existing Target Runtime objects keep owning remote
connections, target identity, epochs, and mutation journals.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Protocol
import uuid

from .catalog import OperationCatalog, OperationDescriptor
from .contracts import RUNTIME_API_VERSION


CONTEXT_RUNTIME_SCHEMA = f"{RUNTIME_API_VERSION}/context-runtime"
CONTEXT_RUNTIME_STORAGE_VERSION = 1
AGENT_ENVELOPE_SCHEMA = f"{RUNTIME_API_VERSION}/agent-envelope"
AGENT_ENVELOPE_MAX_BYTES = 24_576
DEFAULT_CASE_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_STORAGE_SOFT_LIMIT_BYTES = 1024 * 1024 * 1024
DEFAULT_EVIDENCE_READ_BYTES = 64 * 1024
MAX_EVIDENCE_READ_BYTES = 1024 * 1024
DEFAULT_PROJECTION_CACHE_BYTES = 8 * 1024 * 1024
MAX_PROJECTED_OPERATIONS = 128
MAX_PROJECTED_PHASE_RECORDS = 32
MAX_PROJECTED_EVIDENCE_REFS = 256
_CONTEXT_CONTROL_ARGUMENTS = frozenset(
    {
        "case_id",
        "expected_revision",
        "idempotency_key",
        "deadline",
        "max_steps",
        "_workflow_cycle_id",
        "_workflow_step_id",
        "_workflow_step_kind",
        "_workflow_target_version",
        "_workflow_request_fingerprint",
        "_context_entry_operation",
    }
)
_TARGET_REPLACEMENT_RESET_ARGUMENTS = frozenset(
    {
        "ip",
        "targets",
        "target_id",
        "target_role",
        "role",
        "ssh_port",
        "telnet_port",
        "redfish_port",
    }
)
_TARGET_VERSION_ARGUMENTS = frozenset(
    {
        "ssh_port",
        "telnet_port",
        "redfish_port",
    }
)


def _process_start_marker(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    return fields[19] if len(fields) > 19 else ""


def _process_owner_is_active(pid: int, started: str) -> bool:
    if pid <= 0:
        return False
    observed = _process_start_marker(pid)
    if observed:
        return not started or observed == started
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class ContextRuntimeError(RuntimeError):
    """Base error with a stable external code."""

    code = "context_runtime_error"


class RevisionConflict(ContextRuntimeError):
    code = "revision_conflict"


class IdempotencyConflict(ContextRuntimeError):
    code = "idempotency_conflict"


class OperationAlreadyInProgress(ContextRuntimeError):
    code = "operation_in_progress"


class EvidenceUnavailable(ContextRuntimeError):
    code = "evidence_unavailable"


class CaseNotFound(ContextRuntimeError):
    code = "case_not_found"


class CaseClosed(ContextRuntimeError):
    code = "case_closed"


class CaseNotForgettable(ContextRuntimeError):
    code = "case_not_forgettable"


class MutationOutcomeUnknown(ContextRuntimeError):
    code = "mutation_outcome_unknown"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _safe_identifier(value: object, *, fallback: str) -> str:
    cooked = str(value or "").strip()
    if cooked and len(cooked) <= 128 and all(
        character.isalnum() or character in "._:-" for character in cooked
    ):
        return cooked
    return fallback


def _sanitize(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class PendingCaseEvent:
    kind: str
    payload: Mapping[str, object]
    operation_id: str = ""


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    blob_id: str
    media_type: str
    byte_count: int
    target_id: str
    generation: str
    provenance: str
    observed_at: float

    def to_public_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "blob_id": self.blob_id,
            "media_type": self.media_type,
            "byte_count": self.byte_count,
            "target_id": self.target_id,
            "generation": self.generation,
            "provenance": self.provenance,
            "observed_at": self.observed_at,
        }


class BlobRepository(Protocol):
    def put(self, body: bytes) -> str: ...

    def read(self, blob_id: str, *, offset: int, limit: int) -> bytes: ...

    def delete(self, blob_id: str) -> bool: ...

    def size_bytes(self) -> int: ...


class InMemoryBlobRepository:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.RLock()

    def put(self, body: bytes) -> str:
        if not isinstance(body, bytes):
            raise TypeError("blob body must be bytes")
        blob_id = hashlib.sha256(body).hexdigest()
        with self._lock:
            self._blobs.setdefault(blob_id, body)
        return blob_id

    def read(self, blob_id: str, *, offset: int = 0, limit: int = -1) -> bytes:
        with self._lock:
            try:
                body = self._blobs[blob_id]
            except KeyError as exc:
                raise EvidenceUnavailable(f"blob {blob_id} is unavailable") from exc
        if hashlib.sha256(body).hexdigest() != blob_id:
            raise EvidenceUnavailable(f"blob {blob_id} failed hash verification")
        end = None if limit < 0 else offset + limit
        return body[offset:end]

    def delete(self, blob_id: str) -> bool:
        with self._lock:
            return self._blobs.pop(blob_id, None) is not None

    def size_bytes(self) -> int:
        with self._lock:
            return sum(len(body) for body in self._blobs.values())


class FilesystemBlobRepository:
    """Content-addressed gzip blobs with atomic publication."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, blob_id: str) -> Path:
        if len(blob_id) != 64 or any(c not in "0123456789abcdef" for c in blob_id):
            raise EvidenceUnavailable("invalid blob identifier")
        return self.root / blob_id[:2] / f"{blob_id}.json.gz"

    def put(self, body: bytes) -> str:
        if not isinstance(body, bytes):
            raise TypeError("blob body must be bytes")
        blob_id = hashlib.sha256(body).hexdigest()
        destination = self._path(blob_id)
        with self._lock:
            if destination.is_file():
                try:
                    self.read(blob_id, offset=0, limit=0)
                    return blob_id
                except EvidenceUnavailable:
                    pass
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{blob_id}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as raw_file:
                    with gzip.GzipFile(fileobj=raw_file, mode="wb", mtime=0) as archive:
                        archive.write(body)
                    raw_file.flush()
                    os.fsync(raw_file.fileno())
                os.replace(raw_path, destination)
            finally:
                try:
                    os.unlink(raw_path)
                except FileNotFoundError:
                    pass
        return blob_id

    def read(self, blob_id: str, *, offset: int = 0, limit: int = -1) -> bytes:
        if offset < 0:
            raise EvidenceUnavailable("blob offset must be non-negative")
        path = self._path(blob_id)
        digest = hashlib.sha256()
        selected = bytearray()
        position = 0
        end = None if limit < 0 else offset + limit
        try:
            with gzip.open(path, "rb") as archive:
                while True:
                    chunk = archive.read(64 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    chunk_end = position + len(chunk)
                    if chunk_end > offset and (end is None or position < end):
                        start_in_chunk = max(0, offset - position)
                        end_in_chunk = len(chunk) if end is None else min(
                            len(chunk), end - position
                        )
                        if end_in_chunk > start_in_chunk:
                            selected.extend(chunk[start_in_chunk:end_in_chunk])
                    position = chunk_end
        except (OSError, EOFError) as exc:
            raise EvidenceUnavailable(f"blob {blob_id} is unavailable") from exc
        if digest.hexdigest() != blob_id:
            raise EvidenceUnavailable(f"blob {blob_id} failed hash verification")
        return bytes(selected)

    def delete(self, blob_id: str) -> bool:
        path = self._path(blob_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    def size_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.root.glob("*/*.json.gz")
            if path.is_file()
        )


def _empty_projection(case_id: str) -> dict[str, object]:
    return {
        "schema": CONTEXT_RUNTIME_SCHEMA,
        "case_id": case_id,
        "revision": 0,
        "status": "open",
        "closed": False,
        "intent": "",
        "entry_operation": "",
        "final_purpose": "",
        "change_boundary": "",
        "delivery_strategy": "",
        "targets": [],
        "target_version": 1,
        "workflow_inputs": {},
        "workflow_cycle_id": "cycle-1",
        "workflow_cycle_number": 1,
        "workflow_step_states": {},
        "workflow_step_attempts": {},
        "workflow_phase_values": {},
        "mutation_outcome_unknown_operations": {},
        "operations": [],
        "completed_operation_counts": {},
        "operation_count": 0,
        "phase_records": [],
        "phase_record_count": 0,
        "evidence_refs": [],
        "evidence_ref_count": 0,
        "projection_truncated": False,
        "next_actions": [],
        "last_access": 0.0,
    }


def _apply_case_events(
    projection: Mapping[str, object],
    events: Iterable[Mapping[str, object]],
    *,
    last_access: float = 0.0,
) -> dict[str, object]:
    public = json.loads(json.dumps(projection))
    operations: OrderedDict[str, dict[str, object]] = OrderedDict(
        (str(item.get("operation_id", "")), dict(item))
        for item in public.get("operations", [])
        if isinstance(item, Mapping) and item.get("operation_id")
    )
    evidence_by_id: OrderedDict[str, dict[str, object]] = OrderedDict(
        (str(item.get("evidence_id", "")), dict(item))
        for item in public.get("evidence_refs", [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    )
    phases = [
        dict(item)
        for item in public.get("phase_records", [])
        if isinstance(item, Mapping)
    ]
    completed_counts = {
        str(name): int(count)
        for name, count in dict(public.get("completed_operation_counts", {})).items()
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0
    }
    operation_count = int(public.get("operation_count", len(operations)))
    phase_record_count = int(public.get("phase_record_count", len(phases)))
    evidence_ref_count = int(public.get("evidence_ref_count", len(evidence_by_id)))
    workflow_step_states = {
        str(key): dict(value)
        for key, value in dict(public.get("workflow_step_states", {})).items()
        if isinstance(value, Mapping)
    }
    workflow_step_attempts = {
        str(key): int(value)
        for key, value in dict(public.get("workflow_step_attempts", {})).items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    workflow_phase_values = {
        str(key): dict(value)
        for key, value in dict(public.get("workflow_phase_values", {})).items()
        if isinstance(value, Mapping)
    }
    unknown_mutations = {
        str(key): dict(value)
        for key, value in dict(
            public.get("mutation_outcome_unknown_operations", {})
        ).items()
        if isinstance(value, Mapping)
    }
    for event in events:
        revision = int(event["revision"])
        public["revision"] = revision
        kind = str(event["kind"])
        operation_id = str(event.get("operation_id", ""))
        payload = event.get("payload", {})
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        if kind == "CaseOpened":
            public.update(
                {
                    "intent": str(payload.get("intent", "")),
                    "entry_operation": str(payload.get("entry_operation", "")),
                    "final_purpose": str(payload.get("final_purpose", "")),
                    "change_boundary": str(payload.get("change_boundary", "")),
                    "delivery_strategy": str(payload.get("delivery_strategy", "")),
                    "targets": list(payload.get("targets", [])),
                    "target_version": int(payload.get("target_version", 1)),
                    "workflow_inputs": dict(payload.get("workflow_inputs", {})),
                    "workflow_cycle_id": str(
                        payload.get("workflow_cycle_id", "cycle-1")
                    ),
                    "workflow_cycle_number": int(
                        payload.get("workflow_cycle_number", 1)
                    ),
                    "status": "open",
                }
            )
        elif kind == "CaseUpdated":
            for name in (
                "intent",
                "final_purpose",
                "change_boundary",
                "delivery_strategy",
            ):
                if name in payload:
                    public[name] = str(payload[name])
            if "targets" in payload:
                public["targets"] = list(payload.get("targets", []))
            if "workflow_inputs" in payload:
                public["workflow_inputs"] = dict(
                    payload.get("workflow_inputs", {})
                )
            if payload.get("target_changed") is True:
                public["target_version"] = int(
                    payload.get(
                        "target_version",
                        int(public.get("target_version", 1)) + 1,
                    )
                )
                workflow_step_states = {
                    key: value
                    for key, value in workflow_step_states.items()
                    if value.get("kind") == "phase"
                }
                workflow_step_attempts = {
                    key: value
                    for key, value in workflow_step_attempts.items()
                    if ":phase:" in key
                }
            public["status"] = "open"
        elif kind == "WorkflowCycleStarted":
            public["workflow_cycle_number"] = int(
                payload.get(
                    "workflow_cycle_number",
                    int(public.get("workflow_cycle_number", 1)) + 1,
                )
            )
            public["workflow_cycle_id"] = str(
                payload.get(
                    "workflow_cycle_id",
                    f"cycle-{public['workflow_cycle_number']}",
                )
            )
            workflow_step_states = {}
            workflow_step_attempts = {}
            workflow_phase_values = {}
            public["status"] = "open"
            public["next_actions"] = []
        elif kind == "WorkflowStepsInvalidated":
            retained_step_ids = payload.get("retained_step_ids")
            after_step_id = str(payload.get("after_step_id", ""))
            if isinstance(retained_step_ids, list):
                retained = {
                    str(step_id)
                    for step_id in retained_step_ids
                    if isinstance(step_id, str) and step_id
                }
                workflow_step_states = {
                    key: value
                    for key, value in workflow_step_states.items()
                    if key in retained
                }
            elif after_step_id:
                workflow_step_states = {
                    key: value
                    for key, value in workflow_step_states.items()
                    if key <= after_step_id
                }
            public["status"] = "open"
            public["next_actions"] = []
        elif kind == "OperationAccepted":
            operation_count += 1
            operations[operation_id] = {
                "operation_id": operation_id,
                "operation": str(payload.get("operation", "")),
                "status": "accepted",
                "idempotency_key": str(payload.get("idempotency_key", "")),
                "request_fingerprint": str(
                    payload.get("request_fingerprint", "")
                ),
                "accepted_revision": revision,
            }
            operation = operations[operation_id]
            for name in (
                "workflow_cycle_id",
                "workflow_step_id",
                "workflow_step_kind",
                "target_version",
                "target_id",
            ):
                if name in payload:
                    operation[name] = payload[name]
            step_id = str(payload.get("workflow_step_id", ""))
            cycle_id = str(payload.get("workflow_cycle_id", ""))
            step_kind = str(payload.get("workflow_step_kind", ""))
            if step_id and cycle_id and step_kind:
                target_version = int(payload.get("target_version", 0))
                attempt_key = (
                    f"{cycle_id}:{step_kind}:{step_id}:target-{target_version}"
                    if step_kind == "operation"
                    else f"{cycle_id}:{step_kind}:{step_id}"
                )
                workflow_step_attempts[attempt_key] = (
                    workflow_step_attempts.get(attempt_key, 0) + 1
                )
                operation["workflow_attempt"] = workflow_step_attempts[attempt_key]
        elif kind == "OperationStarted":
            operation = operations.setdefault(
                operation_id,
                {"operation_id": operation_id, "operation": ""},
            )
            operation["status"] = "running"
            operation["started_revision"] = revision
        elif kind == "EvidenceAttached":
            reference = payload.get("evidence")
            if isinstance(reference, Mapping):
                evidence_public = dict(reference)
                evidence_id = str(evidence_public.get("evidence_id", ""))
                if evidence_id:
                    if evidence_id not in evidence_by_id:
                        evidence_ref_count += 1
                    evidence_by_id[evidence_id] = evidence_public
                    operation = operations.get(operation_id)
                    if operation is not None:
                        operation.setdefault("evidence_ids", []).append(evidence_id)
        elif kind == "OperationProgressed":
            operation = operations.setdefault(
                operation_id,
                {"operation_id": operation_id, "operation": ""},
            )
            if "status" in payload:
                operation["status"] = str(payload["status"])
            if "next_actions" in payload:
                public["next_actions"] = list(payload["next_actions"])
            phase = payload.get("phase_record")
            if isinstance(phase, Mapping):
                phase_public = dict(phase)
                phases.append(phase_public)
                phase_record_count += 1
                step_id = str(phase_public.get("workflow_step_id", ""))
                cycle_id = str(phase_public.get("workflow_cycle_id", ""))
                if step_id and cycle_id == str(public.get("workflow_cycle_id", "")):
                    workflow_step_states[step_id] = {
                        "kind": "phase",
                        "name": str(phase_public.get("phase_type", "")),
                        "status": str(phase_public.get("status", "")),
                        "workflow_cycle_id": cycle_id,
                    }
                    workflow_phase_values[str(phase_public.get("phase_type", ""))] = (
                        phase_public
                    )
        elif kind == "OperationTerminal":
            operation = operations.setdefault(
                operation_id,
                {"operation_id": operation_id, "operation": ""},
            )
            operation["status"] = str(payload.get("status", "completed"))
            if operation["status"] in {"completed", "verified", "succeeded"}:
                name = str(operation.get("operation", ""))
                if name and name != "workflow.advance":
                    completed_counts[name] = completed_counts.get(name, 0) + 1
            operation["terminal_revision"] = revision
            operation["summary"] = str(payload.get("summary", ""))
            if "target_epoch" in payload:
                operation["target_epoch"] = payload["target_epoch"]
            if operation["status"] == "mutation_outcome_unknown":
                unknown_mutations[operation_id] = {
                    "operation_id": operation_id,
                    "operation": str(operation.get("operation", "")),
                    "status": operation["status"],
                }
            step_id = str(operation.get("workflow_step_id", ""))
            cycle_id = str(operation.get("workflow_cycle_id", ""))
            if step_id and cycle_id == str(public.get("workflow_cycle_id", "")):
                workflow_step_states[step_id] = {
                    "kind": str(operation.get("workflow_step_kind", "operation")),
                    "name": str(operation.get("operation", "")),
                    "status": str(operation.get("status", "")),
                    "workflow_cycle_id": cycle_id,
                    "target_version": int(operation.get("target_version", 0)),
                    "target_id": str(operation.get("target_id", "")),
                    "operation_id": operation_id,
                }
                if "target_epoch" in operation:
                    workflow_step_states[step_id]["target_epoch"] = operation[
                        "target_epoch"
                    ]
            if payload.get("canonical_error") is not None:
                operation["canonical_error"] = payload.get("canonical_error")
            if "next_actions" in payload:
                public["next_actions"] = list(payload["next_actions"])
            public["status"] = str(payload.get("case_status", "open"))
        elif kind == "OperationReconciled":
            operation = operations.setdefault(
                operation_id,
                {"operation_id": operation_id, "operation": ""},
            )
            operation["status"] = str(payload.get("status", "completed"))
            if operation["status"] in {"completed", "verified", "succeeded"}:
                name = str(operation.get("operation", ""))
                if name and name != "workflow.advance":
                    completed_counts[name] = completed_counts.get(name, 0) + 1
            operation["reconciled_revision"] = revision
            operation["summary"] = str(payload.get("summary", ""))
            if "target_epoch" in payload:
                operation["target_epoch"] = payload["target_epoch"]
            if operation["status"] == "mutation_outcome_unknown":
                unknown_mutations[operation_id] = {
                    "operation_id": operation_id,
                    "operation": str(operation.get("operation", "")),
                    "status": operation["status"],
                }
            else:
                unknown_mutations.pop(operation_id, None)
            step_id = str(operation.get("workflow_step_id", ""))
            cycle_id = str(operation.get("workflow_cycle_id", ""))
            if step_id and cycle_id == str(public.get("workflow_cycle_id", "")):
                workflow_step_states[step_id] = {
                    "kind": str(operation.get("workflow_step_kind", "operation")),
                    "name": str(operation.get("operation", "")),
                    "status": str(operation.get("status", "")),
                    "workflow_cycle_id": cycle_id,
                    "target_version": int(operation.get("target_version", 0)),
                    "target_id": str(operation.get("target_id", "")),
                    "operation_id": operation_id,
                }
                if "target_epoch" in operation:
                    workflow_step_states[step_id]["target_epoch"] = operation[
                        "target_epoch"
                    ]
            operation.pop("canonical_error", None)
            if payload.get("canonical_error") is not None:
                operation["canonical_error"] = payload.get("canonical_error")
            if "next_actions" in payload:
                public["next_actions"] = list(payload["next_actions"])
            public["status"] = str(payload.get("case_status", "open"))
        elif kind == "CaseClosed":
            public["closed"] = True
            public["status"] = "closed"

    active_statuses = {
        "accepted",
        "running",
        "waiting_external",
        "waiting_phase_record",
        "mutation_outcome_unknown",
    }
    while len(operations) > MAX_PROJECTED_OPERATIONS:
        removable = next(
            (
                operation_id
                for operation_id, operation in operations.items()
                if str(operation.get("status", "")) not in active_statuses
            ),
            next(iter(operations)),
        )
        operations.pop(removable, None)
    while len(evidence_by_id) > MAX_PROJECTED_EVIDENCE_REFS:
        evidence_by_id.popitem(last=False)
    if len(phases) > MAX_PROJECTED_PHASE_RECORDS:
        phases = phases[-MAX_PROJECTED_PHASE_RECORDS:]

    public["operations"] = list(operations.values())
    public["completed_operation_counts"] = completed_counts
    public["workflow_step_states"] = workflow_step_states
    public["workflow_step_attempts"] = workflow_step_attempts
    public["workflow_phase_values"] = workflow_phase_values
    public["mutation_outcome_unknown_operations"] = unknown_mutations
    public["operation_count"] = operation_count
    public["phase_records"] = phases
    public["phase_record_count"] = phase_record_count
    public["evidence_refs"] = list(evidence_by_id.values())
    public["evidence_ref_count"] = evidence_ref_count
    public["projection_truncated"] = bool(
        operation_count > len(operations)
        or phase_record_count > len(phases)
        or evidence_ref_count > len(evidence_by_id)
    )
    public["last_access"] = last_access
    return public


def project_case(
    case_id: str,
    events: Iterable[Mapping[str, object]],
    *,
    last_access: float = 0.0,
) -> dict[str, object]:
    return _apply_case_events(
        _empty_projection(case_id),
        events,
        last_access=last_access,
    )


class RuntimeRepository(Protocol):
    def load(self, case_id: str) -> dict[str, object] | None: ...

    def current_revision(self, case_id: str) -> int | None: ...

    def commit(
        self,
        case_id: str,
        *,
        expected_revision: int,
        events: Iterable[PendingCaseEvent],
    ) -> dict[str, object]: ...

    def claim_idempotency(
        self, case_id: str, key: str, fingerprint: str
    ) -> Mapping[str, object] | None: ...

    def complete_idempotency(
        self, case_id: str, key: str, receipt: Mapping[str, object]
    ) -> None: ...

    def abandon_idempotency(self, case_id: str, key: str) -> None: ...

    def bind_task(self, task_id: str, case_id: str) -> None: ...

    def case_for_task(self, task_id: str) -> str | None: ...

    def is_case_bound(self, case_id: str) -> bool: ...

    def unbind_task(self, task_id: str) -> None: ...

    def touch(self, case_id: str, *, at: float) -> None: ...

    def evidence_reference(
        self, case_id: str, evidence_id: str
    ) -> dict[str, object] | None: ...

    def delete_case(self, case_id: str) -> tuple[dict[str, object], ...]: ...

    def metadata(self) -> tuple[dict[str, object], ...]: ...

    def blob_reference_count(self, blob_id: str) -> int: ...

    def size_bytes(self) -> int: ...

    def status(self) -> dict[str, object]: ...


class InMemoryRuntimeRepository:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._events: dict[str, list[dict[str, object]]] = {}
        self._projections: dict[str, dict[str, object]] = {}
        self._idempotency: dict[tuple[str, str], dict[str, object]] = {}
        self._bindings: dict[str, str] = {}
        self._meta: dict[str, dict[str, object]] = {}
        self._clock = clock
        self._lock = threading.RLock()

    def _load_locked(self, case_id: str) -> dict[str, object] | None:
        events = self._events.get(case_id)
        if events is None:
            return None
        meta = self._meta.get(case_id, {})
        projection = self._projections.get(case_id)
        if projection is None:
            projection = project_case(
                case_id,
                events,
                last_access=float(meta.get("last_access", 0.0)),
            )
            self._projections[case_id] = projection
        public = json.loads(json.dumps(projection))
        public["last_access"] = float(meta.get("last_access", 0.0))
        return public

    def load(self, case_id: str) -> dict[str, object] | None:
        with self._lock:
            projection = self._load_locked(case_id)
            return json.loads(json.dumps(projection)) if projection is not None else None

    def current_revision(self, case_id: str) -> int | None:
        with self._lock:
            events = self._events.get(case_id)
            return len(events) if events is not None else None

    def commit(
        self,
        case_id: str,
        *,
        expected_revision: int,
        events: Iterable[PendingCaseEvent],
    ) -> dict[str, object]:
        pending = tuple(events)
        if not pending:
            projection = self.load(case_id)
            if projection is None:
                raise CaseNotFound(case_id)
            return projection
        with self._lock:
            existing = self._events.setdefault(case_id, [])
            current = len(existing)
            if current != expected_revision:
                raise RevisionConflict(
                    f"case {case_id} revision is {current}, expected {expected_revision}"
                )
            now = self._clock()
            base = self._projections.get(case_id)
            if base is None:
                base = (
                    project_case(case_id, existing)
                    if existing
                    else _empty_projection(case_id)
                )
            appended: list[dict[str, object]] = []
            for item in pending:
                revision = len(existing) + 1
                event = {
                    "revision": revision,
                    "kind": item.kind,
                    "operation_id": item.operation_id,
                    "payload": dict(item.payload),
                    "created_at": now,
                }
                existing.append(event)
                appended.append(event)
            projection = _apply_case_events(base, appended, last_access=now)
            self._projections[case_id] = json.loads(json.dumps(projection))
            self._meta[case_id] = {
                "last_access": now,
                "status": projection["status"],
                "created_at": self._meta.get(case_id, {}).get("created_at", now),
            }
            return json.loads(json.dumps(projection))

    def claim_idempotency(
        self, case_id: str, key: str, fingerprint: str
    ) -> Mapping[str, object] | None:
        identity = (case_id, key)
        with self._lock:
            existing = self._idempotency.get(identity)
            if existing is None:
                self._idempotency[identity] = {
                    "fingerprint": fingerprint,
                    "status": "pending",
                    "receipt": None,
                }
                return None
            if existing["fingerprint"] != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key {key} is already bound to different input"
                )
            receipt = existing.get("receipt")
            if isinstance(receipt, Mapping):
                return json.loads(json.dumps(receipt))
            raise OperationAlreadyInProgress(
                f"idempotency key {key} is already in progress"
            )

    def complete_idempotency(
        self, case_id: str, key: str, receipt: Mapping[str, object]
    ) -> None:
        with self._lock:
            self._idempotency[(case_id, key)]["status"] = "completed"
            self._idempotency[(case_id, key)]["receipt"] = dict(receipt)

    def abandon_idempotency(self, case_id: str, key: str) -> None:
        with self._lock:
            identity = (case_id, key)
            existing = self._idempotency.get(identity)
            if existing is not None and existing.get("status") == "pending":
                self._idempotency.pop(identity, None)

    def bind_task(self, task_id: str, case_id: str) -> None:
        with self._lock:
            self._bindings[task_id] = case_id

    def case_for_task(self, task_id: str) -> str | None:
        with self._lock:
            return self._bindings.get(task_id)

    def is_case_bound(self, case_id: str) -> bool:
        with self._lock:
            return any(bound == case_id for bound in self._bindings.values())

    def unbind_task(self, task_id: str) -> None:
        with self._lock:
            self._bindings.pop(task_id, None)

    def touch(self, case_id: str, *, at: float) -> None:
        with self._lock:
            if case_id not in self._events:
                raise CaseNotFound(case_id)
            self._meta.setdefault(case_id, {})["last_access"] = at

    def evidence_reference(
        self, case_id: str, evidence_id: str
    ) -> dict[str, object] | None:
        with self._lock:
            for event in reversed(self._events.get(case_id, [])):
                if event.get("kind") != "EvidenceAttached":
                    continue
                payload = event.get("payload", {})
                reference = (
                    payload.get("evidence") if isinstance(payload, Mapping) else None
                )
                if (
                    isinstance(reference, Mapping)
                    and str(reference.get("evidence_id", "")) == evidence_id
                ):
                    return json.loads(json.dumps(reference))
            return None

    def delete_case(self, case_id: str) -> tuple[dict[str, object], ...]:
        with self._lock:
            events = self._events.get(case_id)
            if events is None:
                return ()
            refs = tuple(
                dict(reference)
                for event in events
                if event.get("kind") == "EvidenceAttached"
                for payload in (event.get("payload", {}),)
                if isinstance(payload, Mapping)
                for reference in (payload.get("evidence"),)
                if isinstance(reference, Mapping)
            )
            self._events.pop(case_id, None)
            self._projections.pop(case_id, None)
            self._meta.pop(case_id, None)
            self._idempotency = {
                identity: value
                for identity, value in self._idempotency.items()
                if identity[0] != case_id
            }
            self._bindings = {
                task: bound
                for task, bound in self._bindings.items()
                if bound != case_id
            }
            return refs

    def metadata(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(
                {
                    "case_id": case_id,
                    "last_access": float(meta.get("last_access", 0.0)),
                    "status": str(meta.get("status", "open")),
                }
                for case_id, meta in self._meta.items()
            )

    def blob_reference_count(self, blob_id: str) -> int:
        with self._lock:
            return sum(
                1
                for events in self._events.values()
                for event in events
                if event.get("kind") == "EvidenceAttached"
                for payload in (event.get("payload", {}),)
                if isinstance(payload, Mapping)
                for reference in (payload.get("evidence"),)
                if isinstance(reference, Mapping)
                and reference.get("blob_id") == blob_id
            )

    def size_bytes(self) -> int:
        with self._lock:
            return len(
                _json_bytes(
                    {
                        "events": self._events,
                        "projections": self._projections,
                        "idempotency": {
                            f"{case_id}:{key}": value
                            for (case_id, key), value in self._idempotency.items()
                        },
                        "bindings": self._bindings,
                        "meta": self._meta,
                    }
                )
            )

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "adapter": "memory",
                "case_count": len(self._events),
                "idempotency_count": len(self._idempotency),
                "task_binding_count": len(self._bindings),
            }


class SQLiteRuntimeRepository:
    """SQLite WAL append-only case and idempotency repository."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        owner_is_active: Callable[[int, str], bool] = _process_owner_is_active,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._clock = clock
        self._owner_pid = os.getpid()
        self._owner_started = _process_start_marker(self._owner_pid)
        self._owner_token = uuid.uuid4().hex
        self._owner_is_active = owner_is_active
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS case_events (
                    case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (case_id, revision)
                );
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    last_access REAL NOT NULL,
                    status TEXT NOT NULL,
                    projection_json TEXT
                );
                CREATE TABLE IF NOT EXISTS idempotency (
                    case_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT,
                    updated_at REAL NOT NULL,
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    owner_started TEXT NOT NULL DEFAULT '',
                    owner_token TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (case_id, key)
                );
                CREATE TABLE IF NOT EXISTS task_bindings (
                    task_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    owner_pid INTEGER NOT NULL DEFAULT 0,
                    owner_started TEXT NOT NULL DEFAULT '',
                    owner_token TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS runtime_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            idempotency_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(idempotency)")
            }
            for name, declaration in (
                ("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
                ("owner_started", "TEXT NOT NULL DEFAULT ''"),
                ("owner_token", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in idempotency_columns:
                    connection.execute(
                        f"ALTER TABLE idempotency ADD COLUMN {name} {declaration}"
                    )
            binding_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(task_bindings)")
            }
            for name, declaration in (
                ("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
                ("owner_started", "TEXT NOT NULL DEFAULT ''"),
                ("owner_token", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in binding_columns:
                    connection.execute(
                        f"ALTER TABLE task_bindings ADD COLUMN {name} {declaration}"
                    )
            case_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cases)")
            }
            if "projection_json" not in case_columns:
                connection.execute("ALTER TABLE cases ADD COLUMN projection_json TEXT")
            row = connection.execute(
                "SELECT value FROM runtime_meta WHERE key = 'storage_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO runtime_meta (key, value) VALUES ('storage_version', ?)",
                    (str(CONTEXT_RUNTIME_STORAGE_VERSION),),
                )
            elif str(row["value"]) != str(CONTEXT_RUNTIME_STORAGE_VERSION):
                raise ContextRuntimeError(
                    "unsupported Context Runtime storage version: "
                    + str(row["value"])
                )

    @staticmethod
    def _load_from_connection(
        connection: sqlite3.Connection, case_id: str
    ) -> dict[str, object] | None:
        meta = connection.execute(
            "SELECT last_access, projection_json FROM cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if meta is not None and meta["projection_json"]:
            projection = json.loads(str(meta["projection_json"]))
            projection["last_access"] = float(meta["last_access"])
            return projection
        cursor = connection.execute(
            "SELECT revision, kind, operation_id, payload_json, created_at "
            "FROM case_events WHERE case_id = ? ORDER BY revision",
            (case_id,),
        )
        first = cursor.fetchone()
        if first is None:
            return None

        def events() -> Iterable[dict[str, object]]:
            row = first
            while row is not None:
                yield {
                    "revision": row["revision"],
                    "kind": row["kind"],
                    "operation_id": row["operation_id"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                row = cursor.fetchone()

        projection = project_case(
            case_id,
            events(),
            last_access=float(meta["last_access"]) if meta is not None else 0.0,
        )
        if meta is not None:
            connection.execute(
                "UPDATE cases SET projection_json = ? WHERE case_id = ?",
                (_json_bytes(projection).decode("utf-8"), case_id),
            )
        return projection

    def load(self, case_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            return self._load_from_connection(connection, case_id)

    def current_revision(self, case_id: str) -> int | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(revision) AS revision FROM case_events WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            revision = row["revision"] if row is not None else None
            return int(revision) if revision is not None else None

    def commit(
        self,
        case_id: str,
        *,
        expected_revision: int,
        events: Iterable[PendingCaseEvent],
    ) -> dict[str, object]:
        pending = tuple(events)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision "
                "FROM case_events WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            current = int(row["revision"])
            if current != expected_revision:
                raise RevisionConflict(
                    f"case {case_id} revision is {current}, expected {expected_revision}"
                )
            now = self._clock()
            base = self._load_from_connection(connection, case_id)
            if base is None:
                base = _empty_projection(case_id)
            appended: list[dict[str, object]] = []
            for offset, item in enumerate(pending, start=1):
                event = {
                    "revision": current + offset,
                    "kind": item.kind,
                    "operation_id": item.operation_id,
                    "payload": dict(item.payload),
                    "created_at": now,
                }
                connection.execute(
                    "INSERT INTO case_events "
                    "(case_id, revision, kind, operation_id, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        case_id,
                        current + offset,
                        item.kind,
                        item.operation_id,
                        _json_bytes(dict(item.payload)).decode("utf-8"),
                        now,
                    ),
                )
                appended.append(event)
            if not appended and current == 0:
                raise CaseNotFound(case_id)
            projection = _apply_case_events(base, appended, last_access=now)
            connection.execute(
                "INSERT INTO cases "
                "(case_id, created_at, last_access, status, projection_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(case_id) DO UPDATE SET "
                "last_access = excluded.last_access, status = excluded.status, "
                "projection_json = excluded.projection_json",
                (
                    case_id,
                    now,
                    now,
                    projection["status"],
                    _json_bytes(projection).decode("utf-8"),
                ),
            )
            connection.commit()
            projection["last_access"] = now
            return projection

    def claim_idempotency(
        self, case_id: str, key: str, fingerprint: str
    ) -> Mapping[str, object] | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, status, receipt_json, owner_pid, "
                "owner_started, owner_token FROM idempotency "
                "WHERE case_id = ? AND key = ?",
                (case_id, key),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO idempotency "
                    "(case_id, key, fingerprint, status, receipt_json, updated_at, "
                    "owner_pid, owner_started, owner_token) "
                    "VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?, ?)",
                    (
                        case_id,
                        key,
                        fingerprint,
                        self._clock(),
                        self._owner_pid,
                        self._owner_started,
                        self._owner_token,
                    ),
                )
                connection.commit()
                return None
            if row["fingerprint"] != fingerprint:
                raise IdempotencyConflict(
                    f"idempotency key {key} is already bound to different input"
                )
            if row["status"] == "completed" and row["receipt_json"]:
                return json.loads(row["receipt_json"])
            owner_pid = int(row["owner_pid"] or 0)
            owner_started = str(row["owner_started"] or "")
            owner_token = str(row["owner_token"] or "")
            if owner_token != self._owner_token and not self._owner_is_active(
                owner_pid, owner_started
            ):
                connection.execute(
                    "UPDATE idempotency SET status = 'pending', receipt_json = NULL, "
                    "updated_at = ?, owner_pid = ?, owner_started = ?, owner_token = ? "
                    "WHERE case_id = ? AND key = ?",
                    (
                        self._clock(),
                        self._owner_pid,
                        self._owner_started,
                        self._owner_token,
                        case_id,
                        key,
                    ),
                )
                connection.commit()
                return None
            raise OperationAlreadyInProgress(
                f"idempotency key {key} is already in progress"
            )

    def complete_idempotency(
        self, case_id: str, key: str, receipt: Mapping[str, object]
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE idempotency SET status = 'completed', receipt_json = ?, "
                "updated_at = ? WHERE case_id = ? AND key = ?",
                (
                    _json_bytes(dict(receipt)).decode("utf-8"),
                    self._clock(),
                    case_id,
                    key,
                ),
            )

    def abandon_idempotency(self, case_id: str, key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM idempotency WHERE case_id = ? AND key = ? "
                "AND status = 'pending'",
                (case_id, key),
            )

    def bind_task(self, task_id: str, case_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO task_bindings "
                "(task_id, case_id, updated_at, owner_pid, owner_started, owner_token) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
                "case_id = excluded.case_id, updated_at = excluded.updated_at, "
                "owner_pid = excluded.owner_pid, "
                "owner_started = excluded.owner_started, "
                "owner_token = excluded.owner_token",
                (
                    task_id,
                    case_id,
                    self._clock(),
                    self._owner_pid,
                    self._owner_started,
                    self._owner_token,
                ),
            )

    def case_for_task(self, task_id: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT case_id FROM task_bindings WHERE task_id = ?", (task_id,)
            ).fetchone()
            return str(row["case_id"]) if row is not None else None

    def is_case_bound(self, case_id: str) -> bool:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id, owner_pid, owner_started, owner_token "
                "FROM task_bindings WHERE case_id = ?",
                (case_id,),
            ).fetchall()
            active = False
            stale_task_ids: list[str] = []
            for row in rows:
                owner_token = str(row["owner_token"] or "")
                if owner_token == self._owner_token or self._owner_is_active(
                    int(row["owner_pid"] or 0),
                    str(row["owner_started"] or ""),
                ):
                    active = True
                else:
                    stale_task_ids.append(str(row["task_id"]))
            if stale_task_ids:
                connection.executemany(
                    "DELETE FROM task_bindings WHERE task_id = ?",
                    ((task_id,) for task_id in stale_task_ids),
                )
            return active

    def unbind_task(self, task_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM task_bindings WHERE task_id = ?", (task_id,))

    def touch(self, case_id: str, *, at: float) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE cases SET last_access = ? WHERE case_id = ?", (at, case_id)
            )
            if cursor.rowcount == 0:
                raise CaseNotFound(case_id)

    def evidence_reference(
        self, case_id: str, evidence_id: str
    ) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            for row in connection.execute(
                "SELECT payload_json FROM case_events "
                "WHERE case_id = ? AND kind = 'EvidenceAttached' "
                "ORDER BY revision DESC",
                (case_id,),
            ):
                payload = json.loads(row["payload_json"])
                reference = (
                    payload.get("evidence") if isinstance(payload, Mapping) else None
                )
                if (
                    isinstance(reference, Mapping)
                    and str(reference.get("evidence_id", "")) == evidence_id
                ):
                    return dict(reference)
            return None

    def delete_case(self, case_id: str) -> tuple[dict[str, object], ...]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM case_events WHERE case_id = ? LIMIT 1", (case_id,)
            ).fetchone()
            if exists is None:
                connection.rollback()
                return ()
            refs = tuple(
                dict(reference)
                for row in connection.execute(
                    "SELECT payload_json FROM case_events "
                    "WHERE case_id = ? AND kind = 'EvidenceAttached' "
                    "ORDER BY revision",
                    (case_id,),
                )
                for payload in (json.loads(row["payload_json"]),)
                if isinstance(payload, Mapping)
                for reference in (payload.get("evidence"),)
                if isinstance(reference, Mapping)
            )
            connection.execute("DELETE FROM case_events WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM cases WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM idempotency WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM task_bindings WHERE case_id = ?", (case_id,))
            connection.commit()
            return refs

    def metadata(self) -> tuple[dict[str, object], ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT case_id, last_access, status FROM cases ORDER BY last_access"
            ).fetchall()
            return tuple(dict(row) for row in rows)

    def blob_reference_count(self, blob_id: str) -> int:
        with self._lock, self._connect() as connection:
            return sum(
                1
                for row in connection.execute(
                    "SELECT payload_json FROM case_events "
                    "WHERE kind = 'EvidenceAttached'"
                )
                for payload in (json.loads(row["payload_json"]),)
                if isinstance(payload, Mapping)
                for reference in (payload.get("evidence"),)
                if isinstance(reference, Mapping)
                and reference.get("blob_id") == blob_id
            )

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.is_file():
                total += path.stat().st_size
        return total

    def status(self) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            case_count = connection.execute(
                "SELECT COUNT(*) AS count FROM cases"
            ).fetchone()["count"]
            idempotency_count = connection.execute(
                "SELECT COUNT(*) AS count FROM idempotency"
            ).fetchone()["count"]
            binding_count = connection.execute(
                "SELECT COUNT(*) AS count FROM task_bindings"
            ).fetchone()["count"]
        return {
            "adapter": "sqlite",
            "case_count": int(case_count),
            "idempotency_count": int(idempotency_count),
            "task_binding_count": int(binding_count),
            "database_bytes": self.size_bytes(),
        }


class ContextToolResult(dict[str, object]):
    """Legacy mapping for Python callers plus compact MCP envelope."""

    def __init__(
        self,
        legacy: Mapping[str, object],
        envelope: Mapping[str, object],
    ) -> None:
        super().__init__(legacy)
        self.envelope = dict(envelope)


def _summary_for(operation: str, value: Mapping[str, object]) -> str:
    for key in ("summary", "message", "next_action", "next_step"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if "completed" in value:
        if value.get("completed"):
            return f"{operation} completed"
        if value.get("partial"):
            return f"{operation} partially completed"
    if value.get("ok") is False:
        return f"{operation} completed with partial or failed evidence"
    return f"{operation} completed"


def _facts_for(value: Mapping[str, object], *, max_facts: int = 32) -> list[dict[str, object]]:
    priority = (
        "ok",
        "schema",
        "normalized_code",
        "code",
        "completed",
        "partial",
        "profile",
        "task",
        "ip",
        "target_id",
        "product_version",
    )
    facts: list[dict[str, object]] = []
    for key in priority:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            if key in value:
                facts.append({"key": key, "value": item})
        if len(facts) >= max_facts:
            break
    targets = value.get("targets")
    if isinstance(targets, list) and len(facts) < max_facts:
        facts.append({"key": "target_count", "value": len(targets)})
    journal = value.get("journal")
    if isinstance(journal, Mapping) and len(facts) < max_facts:
        stage = journal.get("stage")
        if isinstance(stage, str):
            facts.append({"key": "mutation_stage", "value": stage})
    return facts


def _next_actions(value: Mapping[str, object]) -> list[str]:
    actions: list[str] = []
    for key in ("next_action", "next_step"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            actions.append(candidate.strip())
    return actions[:8]


def bounded_envelope(value: Mapping[str, object], *, max_bytes: int) -> dict[str, object]:
    envelope = dict(value)
    if len(_json_bytes(envelope)) <= max_bytes:
        return envelope
    envelope["content_compacted"] = True
    facts = envelope.get("facts")
    if isinstance(facts, list):
        while facts and len(_json_bytes(envelope)) > max_bytes:
            facts.pop()
    evidence = envelope.get("evidence_refs")
    if isinstance(evidence, list):
        while len(evidence) > 8 and len(_json_bytes(envelope)) > max_bytes:
            evidence.pop()
    summary = envelope.get("summary")
    if isinstance(summary, str) and len(_json_bytes(envelope)) > max_bytes:
        envelope["summary"] = summary[:1024]
    error = envelope.get("canonical_error")
    if isinstance(error, Mapping) and len(_json_bytes(envelope)) > max_bytes:
        envelope["canonical_error"] = {
            "code": str(error.get("code", "internal_error")),
            "message": str(error.get("message", ""))[:512],
        }
    if len(_json_bytes(envelope)) > max_bytes:
        envelope = {
            "schema": envelope.get("schema", AGENT_ENVELOPE_SCHEMA),
            "case_id": envelope.get("case_id", ""),
            "revision": envelope.get("revision", 0),
            "operation": envelope.get("operation", {}),
            "status": envelope.get("status", "failed"),
            "summary": str(envelope.get("summary", ""))[:512],
            "evidence_refs": list(envelope.get("evidence_refs", []))[:2],
            "gaps": ["content_compacted"],
            "next_actions": list(envelope.get("next_actions", []))[:2],
            "canonical_error": envelope.get("canonical_error"),
            "continuation": envelope.get("continuation"),
            "capsule": envelope.get("capsule"),
            "content_compacted": True,
        }
        capsule = envelope.get("capsule")
        if isinstance(capsule, Mapping):
            targets = capsule.get("targets", [])
            envelope["capsule"] = {
                "schema": capsule.get("schema", ""),
                "case_id": capsule.get("case_id", ""),
                "case_revision": capsule.get("case_revision", 0),
                "status": capsule.get("status", ""),
                "intent": capsule.get("intent", ""),
                "target_count": len(targets) if isinstance(targets, list) else 0,
                "next_actions": list(capsule.get("next_actions", []))[:2],
            }
        continuation = envelope.get("continuation")
        if isinstance(continuation, Mapping) and len(_json_bytes(envelope)) > max_bytes:
            compact_continuation = dict(continuation)
            targets = compact_continuation.get("targets", [])
            if isinstance(targets, list):
                compact_continuation["target_count"] = len(targets)
                compact_continuation["targets"] = targets[:8]
            envelope["continuation"] = compact_continuation
    if len(_json_bytes(envelope)) > max_bytes:
        raise ValueError("agent envelope cannot be compacted below byte limit")
    return envelope


class ContextRuntime:
    """Case lifecycle and bounded response coordinator."""

    def __init__(
        self,
        catalog: OperationCatalog,
        *,
        repository: RuntimeRepository | None = None,
        blob_repository: BlobRepository | None = None,
        envelope_max_bytes: int = AGENT_ENVELOPE_MAX_BYTES,
        max_cached_projections: int = 64,
        max_cached_projection_bytes: int = DEFAULT_PROJECTION_CACHE_BYTES,
        retention_seconds: float = DEFAULT_CASE_RETENTION_SECONDS,
        storage_soft_limit_bytes: int = DEFAULT_STORAGE_SOFT_LIMIT_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if envelope_max_bytes <= 1024:
            raise ValueError("agent envelope byte limit is too small")
        if max_cached_projections <= 0:
            raise ValueError("projection cache size must be positive")
        if max_cached_projection_bytes <= 0:
            raise ValueError("projection cache byte limit must be positive")
        self.catalog = catalog
        self.repository = repository or InMemoryRuntimeRepository(clock=clock)
        self.blob_repository = blob_repository or InMemoryBlobRepository()
        self.envelope_max_bytes = envelope_max_bytes
        self.max_cached_projections = max_cached_projections
        self.max_cached_projection_bytes = int(max_cached_projection_bytes)
        self.retention_seconds = float(retention_seconds)
        self.storage_soft_limit_bytes = int(storage_soft_limit_bytes)
        self.clock = clock
        self._projection_cache: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._projection_cache_sizes: dict[str, int] = {}
        self._projection_cache_bytes = 0
        self._capsule_cache: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._metrics = {
            "invocations": 0,
            "idempotent_replays": 0,
            "warm_continuations": 0,
            "projection_cache_hits": 0,
            "projection_cache_stale": 0,
            "projection_evictions": 0,
            "projection_rebuilds": 0,
            "capsule_cache_hits": 0,
            "capsule_rebuilds": 0,
            "capsule_evictions": 0,
            "evidence_reads": 0,
            "evidence_bytes_read": 0,
            "evidence_bytes_written": 0,
            "envelope_bytes": 0,
            "peak_envelope_bytes": 0,
            "shadow_writes": 0,
            "shadow_write_failures": 0,
            "shadow_parity_mismatches": 0,
            "maintenance_evictions": 0,
        }
        self._lock = threading.RLock()

    def _cache(self, projection: Mapping[str, object]) -> dict[str, object]:
        public = json.loads(json.dumps(projection))
        case_id = str(public["case_id"])
        public_bytes = len(_json_bytes(public))
        with self._lock:
            if self._projection_cache.pop(case_id, None) is not None:
                self._projection_cache_bytes -= self._projection_cache_sizes.pop(
                    case_id, 0
                )
            cached_capsule = self._capsule_cache.get(case_id)
            if (
                cached_capsule is not None
                and int(cached_capsule.get("case_revision", -1)) != int(public["revision"])
            ):
                self._capsule_cache.pop(case_id, None)
            if public_bytes <= self.max_cached_projection_bytes:
                self._projection_cache[case_id] = public
                self._projection_cache_sizes[case_id] = public_bytes
                self._projection_cache_bytes += public_bytes
            while (
                len(self._projection_cache) > self.max_cached_projections
                or self._projection_cache_bytes > self.max_cached_projection_bytes
            ):
                evicted_case_id, _projection = self._projection_cache.popitem(
                    last=False
                )
                self._projection_cache_bytes -= self._projection_cache_sizes.pop(
                    evicted_case_id, 0
                )
                self._metrics["projection_evictions"] += 1
        return json.loads(json.dumps(public))

    def _load(self, case_id: str, *, touch: bool = False) -> dict[str, object] | None:
        with self._lock:
            cached = self._projection_cache.get(case_id)
            cached_revision = (
                int(cached.get("revision", -1)) if cached is not None else None
            )
        if cached is not None:
            repository_revision = self.repository.current_revision(case_id)
            if repository_revision == cached_revision:
                with self._lock:
                    cached = self._projection_cache.get(case_id)
                    if cached is not None:
                        self._projection_cache.move_to_end(case_id)
                        self._metrics["projection_cache_hits"] += 1
                        projection = json.loads(json.dumps(cached))
                    else:
                        projection = None
            else:
                with self._lock:
                    removed = self._projection_cache.pop(case_id, None)
                    if removed is not None:
                        self._projection_cache_bytes -= self._projection_cache_sizes.pop(
                            case_id, 0
                        )
                    self._capsule_cache.pop(case_id, None)
                    self._metrics["projection_cache_stale"] += 1
                projection = None
        else:
            projection = None
        if projection is None:
            projection = self.repository.load(case_id)
            if projection is None:
                return None
            self._metrics["projection_rebuilds"] += 1
            projection = self._cache(projection)
        if touch:
            now = self.clock()
            self.repository.touch(case_id, at=now)
            projection["last_access"] = now
            self._cache(projection)
        return projection

    def _capsule(self, projection: Mapping[str, object]) -> dict[str, object]:
        """Build the bounded, revision-bound model input projection."""

        case_id = str(projection["case_id"])
        revision = int(projection["revision"])
        with self._lock:
            cached = self._capsule_cache.get(case_id)
            if cached is not None and int(cached.get("case_revision", -1)) == revision:
                self._capsule_cache.move_to_end(case_id)
                self._metrics["capsule_cache_hits"] += 1
                return json.loads(json.dumps(cached))
        phase_records = [
            dict(item)
            for item in projection.get("phase_records", [])
            if isinstance(item, Mapping)
        ]
        evidence_refs = [
            dict(item)
            for item in projection.get("evidence_refs", [])
            if isinstance(item, Mapping)
        ]
        capsule = {
            "schema": f"{CONTEXT_RUNTIME_SCHEMA}/capsule",
            "case_id": case_id,
            "case_revision": revision,
            "status": projection.get("status", "open"),
            "intent": projection.get("intent", ""),
            "entry_operation": projection.get("entry_operation", ""),
            "final_purpose": projection.get("final_purpose", ""),
            "change_boundary": projection.get("change_boundary", ""),
            "delivery_strategy": projection.get("delivery_strategy", ""),
            "targets": list(projection.get("targets", [])),
            "target_version": int(projection.get("target_version", 1)),
            "target_epoch_floor": self._target_epoch_floor(
                projection,
                target_id=self._selected_target_id(projection),
            ),
            "target_epoch_floors": self._target_epoch_floors(projection),
            "workflow_cycle_id": str(
                projection.get("workflow_cycle_id", "cycle-1")
            ),
            "workflow_cycle_number": int(
                projection.get("workflow_cycle_number", 1)
            ),
            "target_generations": sorted(
                {
                    f"{item.get('target_id', '')}:{item.get('generation', '')}"
                    for item in evidence_refs
                }
            ),
            "source_revisions": sorted(
                {
                    str(item.get("source_revision", ""))
                    for item in phase_records
                    if item.get("source_revision")
                }
            ),
            "artifact_revisions": sorted(
                {
                    str(item.get("artifact_sha256", ""))
                    for item in phase_records
                    if item.get("artifact_sha256")
                }
            ),
            "evidence_ids": [
                str(item.get("evidence_id", "")) for item in evidence_refs[-32:]
            ],
            "next_actions": list(projection.get("next_actions", []))[:8],
        }
        capsule = bounded_envelope(capsule, max_bytes=self.envelope_max_bytes)
        with self._lock:
            self._capsule_cache.pop(case_id, None)
            self._capsule_cache[case_id] = capsule
            self._metrics["capsule_rebuilds"] += 1
            while len(self._capsule_cache) > self.max_cached_projections:
                self._capsule_cache.popitem(last=False)
                self._metrics["capsule_evictions"] += 1
        return json.loads(json.dumps(capsule))

    def _resolve_case_id(self, task_id: str, arguments: Mapping[str, object]) -> str:
        explicit = arguments.get("case_id")
        if isinstance(explicit, str) and explicit.strip():
            case_id = _safe_identifier(explicit, fallback="")
            if not case_id:
                raise ValueError("case_id must be a safe 1-128 character identifier")
        else:
            case_id = self.repository.case_for_task(task_id) or (
                "case-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
            )
        return case_id

    def _case_id(self, task_id: str, arguments: Mapping[str, object]) -> str:
        case_id = self._resolve_case_id(task_id, arguments)
        self.repository.bind_task(task_id, case_id)
        return case_id

    def _validate_expected_before_case_update(
        self,
        case_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object] | None:
        existing = self._load(case_id)
        expected = arguments.get("expected_revision")
        if expected is None:
            return existing
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise TypeError("expected_revision must be an integer")
        observed = int(existing["revision"]) if existing is not None else 0
        if expected != observed:
            raise RevisionConflict(
                f"case {case_id} revision is {observed}, expected {expected}"
            )
        return existing

    @staticmethod
    def _targets(arguments: Mapping[str, object]) -> list[dict[str, object]]:
        raw_targets = arguments.get("targets")
        if isinstance(raw_targets, list):
            return [
                {
                    "target_id": str(item.get("target_id", f"target-{index}")),
                    "role": str(item.get("role", "symmetric")),
                    "address": str(item.get("ip", "")),
                }
                for index, item in enumerate(raw_targets, start=1)
                if isinstance(item, Mapping)
            ]
        address = arguments.get("ip")
        if isinstance(address, str) and address.strip():
            return [
                {
                    "target_id": str(arguments.get("target_id", "target-1")),
                    "role": str(arguments.get("target_role", "candidate")),
                    "address": address.strip(),
                }
            ]
        return []

    def _open_case(
        self,
        case_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        existing = self._load(case_id)
        if existing is not None:
            supplied = {
                key: value
                for key, value in arguments.items()
                if key not in _CONTEXT_CONTROL_ARGUMENTS
                and not key.startswith("_")
            }
            if not supplied:
                return existing
            previous_inputs = existing.get("workflow_inputs", {})
            previous_input_map = (
                dict(previous_inputs)
                if isinstance(previous_inputs, Mapping)
                else {}
            )
            workflow_inputs = dict(previous_input_map)
            replaces_target = "targets" in supplied or (
                isinstance(supplied.get("ip"), str)
                and bool(str(supplied.get("ip", "")).strip())
            )
            if replaces_target:
                for name in _TARGET_REPLACEMENT_RESET_ARGUMENTS:
                    workflow_inputs.pop(name, None)
            workflow_inputs.update(_sanitize(supplied))
            payload: dict[str, object] = {}
            for name in (
                "intent",
                "final_purpose",
                "change_boundary",
                "delivery_strategy",
            ):
                if name in supplied and str(supplied[name]) != str(existing.get(name, "")):
                    payload[name] = str(supplied[name])
            updated_targets = self._targets(arguments) if replaces_target else []
            if replaces_target and updated_targets != existing.get("targets", []):
                payload["targets"] = updated_targets
            if workflow_inputs != previous_input_map:
                payload["workflow_inputs"] = workflow_inputs
            binding_changed = bool(
                replaces_target and updated_targets != existing.get("targets", [])
            )
            if not binding_changed:
                for name in _TARGET_VERSION_ARGUMENTS:
                    if workflow_inputs.get(name) != previous_input_map.get(name):
                        binding_changed = True
                        break
            if binding_changed:
                payload["target_changed"] = True
                payload["target_version"] = int(existing.get("target_version", 1)) + 1
            if not payload:
                return existing
            return self._cache(
                self.repository.commit(
                    case_id,
                    expected_revision=int(existing["revision"]),
                    events=(PendingCaseEvent("CaseUpdated", payload),),
                )
            )
        event = PendingCaseEvent(
            "CaseOpened",
            {
                "intent": str(arguments.get("intent", "diagnosis-only")),
                "entry_operation": str(
                    arguments.get("_context_entry_operation", "")
                ),
                "final_purpose": str(
                    arguments.get("final_purpose", arguments.get("problem", ""))
                ),
                "change_boundary": str(arguments.get("change_boundary", "")),
                "delivery_strategy": str(arguments.get("delivery_strategy", "")),
                "targets": self._targets(arguments),
                "target_version": 1,
                "workflow_cycle_id": "cycle-1",
                "workflow_cycle_number": 1,
                "workflow_inputs": _sanitize(
                    {
                        key: value
                        for key, value in arguments.items()
                        if key not in _CONTEXT_CONTROL_ARGUMENTS
                        and not key.startswith("_")
                    }
                ),
            },
        )
        return self._cache(
            self.repository.commit(case_id, expected_revision=0, events=(event,))
        )

    def _put_evidence(
        self,
        value: Mapping[str, object],
        *,
        case_id: str,
        operation_id: str,
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
    ) -> EvidenceRef:
        body = _json_bytes(_sanitize(value))
        blob_id = self.blob_repository.put(body)
        self._metrics["evidence_bytes_written"] += len(body)
        targets = self._targets(arguments)
        target_id = str(arguments.get("target_id", ""))
        if not target_id and len(targets) == 1:
            target_id = str(targets[0].get("target_id", "target-1"))
        if not target_id and descriptor.mutation:
            candidates = [
                target
                for target in targets
                if str(target.get("role", "")).strip().lower() == "candidate"
            ]
            if len(candidates) == 1:
                target_id = str(candidates[0].get("target_id", ""))
        generation = str(
            value.get(
                "target_epoch",
                value.get("generation", arguments.get("target_epoch", "unknown")),
            )
        )
        provenance = f"{descriptor.name}:{operation_id}"
        evidence_id = _fingerprint(
            {
                "blob_id": blob_id,
                "case_id": case_id,
                "target_id": target_id,
                "generation": generation,
                "provenance": provenance,
            }
        )
        return EvidenceRef(
            evidence_id=evidence_id,
            blob_id=blob_id,
            media_type="application/json",
            byte_count=len(body),
            target_id=target_id,
            generation=generation,
            provenance=provenance,
            observed_at=self.clock(),
        )

    def _envelope(
        self,
        *,
        case_id: str,
        revision: int,
        operation_id: str,
        operation: str,
        status: str,
        value: Mapping[str, object],
        evidence_refs: Iterable[Mapping[str, object]] = (),
        gaps: Iterable[str] = (),
        canonical_error: Mapping[str, object] | None = None,
        continuation: Mapping[str, object] | None = None,
        capsule: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        envelope = {
            "schema": AGENT_ENVELOPE_SCHEMA,
            "case_id": case_id,
            "revision": revision,
            "operation": {
                "operation_id": operation_id,
                "name": operation,
                "status": status,
            },
            "status": status,
            "summary": _summary_for(operation, value),
            "facts": _facts_for(value),
            "evidence_refs": [dict(item) for item in evidence_refs],
            "gaps": list(gaps),
            "next_actions": _next_actions(value),
            "canonical_error": (
                dict(canonical_error) if canonical_error is not None else None
            ),
        }
        if continuation is not None:
            envelope["continuation"] = dict(continuation)
        if capsule is not None:
            envelope["capsule"] = dict(capsule)
        comparison = value.get("comparison")
        diff_card = (
            comparison.get("diff_card")
            if isinstance(comparison, Mapping)
            else value.get("diff_card")
        )
        if isinstance(diff_card, Mapping):
            envelope["diff_card"] = dict(diff_card)
        bounded = bounded_envelope(envelope, max_bytes=self.envelope_max_bytes)
        encoded_bytes = len(_json_bytes(bounded))
        self._metrics["envelope_bytes"] += encoded_bytes
        self._metrics["peak_envelope_bytes"] = max(
            self._metrics["peak_envelope_bytes"], encoded_bytes
        )
        return bounded

    def _result_from_receipt(
        self,
        receipt: Mapping[str, object],
        *,
        mutation: bool = False,
    ) -> ContextToolResult:
        envelope = receipt.get("envelope")
        reference = receipt.get("legacy_result_ref")
        inline = receipt.get("legacy_value")
        if not isinstance(envelope, Mapping):
            raise EvidenceUnavailable("idempotency receipt is incomplete")
        if isinstance(reference, Mapping):
            raw = self.blob_repository.read(
                str(reference["blob_id"]), offset=0, limit=-1
            )
            value = json.loads(raw.decode("utf-8"))
        elif isinstance(inline, Mapping):
            value = dict(inline)
        else:
            raise EvidenceUnavailable("idempotency receipt has no readable result")
        if not isinstance(value, Mapping):
            raise EvidenceUnavailable("legacy result evidence is not an object")
        self._metrics["idempotent_replays"] += 1
        self._metrics["warm_continuations"] += 1
        public = dict(value)
        if mutation:
            public["idempotent_replay"] = True
        return ContextToolResult(public, envelope)

    @staticmethod
    def _mutation_is_terminal(value: Mapping[str, object]) -> bool:
        journal = value.get("journal")
        stage = str(journal.get("stage", "")) if isinstance(journal, Mapping) else ""
        return stage in {
            "verified",
            "rollback_verified",
            "replan_required",
            "verification_failed_terminal",
            "rollback_verification_failed_terminal",
        }

    @staticmethod
    def _observed_target_epoch(
        value: Mapping[str, object],
        *,
        target_id: str = "",
    ) -> int | None:
        direct = value.get(
            "target_epoch",
            value.get("epoch_after", value.get("generation")),
        )
        if isinstance(direct, int) and not isinstance(direct, bool) and direct >= 0:
            return direct
        observed = value.get("observed_target_epochs")
        if isinstance(observed, Mapping):
            selected = observed.get(target_id) if target_id else None
            if selected is None and len(observed) == 1:
                selected = next(iter(observed.values()))
            if isinstance(selected, int) and not isinstance(selected, bool) and selected >= 0:
                return selected
        containers: list[Mapping[str, object]] = [value]
        result = value.get("result")
        if isinstance(result, Mapping):
            containers.append(result)
        for container in containers:
            runtime = container.get("runtime")
            status = runtime.get("status") if isinstance(runtime, Mapping) else None
            targets = status.get("targets") if isinstance(status, Mapping) else None
            if not isinstance(targets, list):
                continue
            for target in targets:
                if not isinstance(target, Mapping):
                    continue
                epochs = target.get("epochs")
                epoch = epochs.get("target_epoch") if isinstance(epochs, Mapping) else None
                if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0:
                    return epoch
        return None

    @staticmethod
    def _selected_target_id(
        projection: Mapping[str, object],
        arguments: Mapping[str, object] | None = None,
    ) -> str:
        supplied = arguments or {}
        target_id = str(supplied.get("target_id", "")).strip()
        if target_id:
            return target_id
        supplied_targets = ContextRuntime._targets(supplied)
        if len(supplied_targets) == 1:
            return str(supplied_targets[0].get("target_id", "")).strip()
        workflow_inputs = projection.get("workflow_inputs", {})
        if isinstance(workflow_inputs, Mapping):
            target_id = str(workflow_inputs.get("target_id", "")).strip()
            if target_id:
                return target_id
        targets = projection.get("targets", [])
        if isinstance(targets, list) and len(targets) == 1:
            target = targets[0]
            if isinstance(target, Mapping):
                return str(target.get("target_id", "")).strip()
        return ""

    @classmethod
    def _preferred_target_id(
        cls,
        projection: Mapping[str, object],
        arguments: Mapping[str, object] | None = None,
    ) -> str:
        selected = cls._selected_target_id(projection, arguments)
        if selected:
            return selected
        supplied = arguments or {}
        targets = cls._targets(supplied)
        if not targets:
            raw_targets = projection.get("targets", [])
            if isinstance(raw_targets, list):
                targets = [
                    dict(target)
                    for target in raw_targets
                    if isinstance(target, Mapping)
                ]
        candidates = [
            target
            for target in targets
            if str(target.get("role", "")).strip().lower() == "candidate"
        ]
        if len(candidates) == 1:
            return str(candidates[0].get("target_id", "")).strip()
        return ""

    @classmethod
    def _target_epoch_floors(
        cls,
        projection: Mapping[str, object],
    ) -> dict[str, int]:
        target_version = int(projection.get("target_version", 1))
        targets = projection.get("targets", [])
        default_target_id = ""
        if isinstance(targets, list) and len(targets) == 1:
            target = targets[0]
            if isinstance(target, Mapping):
                default_target_id = str(target.get("target_id", "")).strip()
        floors: dict[str, int] = {}

        def retain_epoch(raw_target_id: object, raw_epoch: object) -> None:
            if (
                not isinstance(raw_epoch, int)
                or isinstance(raw_epoch, bool)
                or raw_epoch < 0
            ):
                return
            target_id = str(raw_target_id or "").strip() or default_target_id
            if not target_id:
                return
            floors[target_id] = max(floors.get(target_id, 0), raw_epoch)

        states = projection.get("workflow_step_states", {})
        for state in states.values() if isinstance(states, Mapping) else ():
            if not isinstance(state, Mapping):
                continue
            epoch = state.get("target_epoch")
            state_target_version = int(state.get("target_version", 0))
            if (
                isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 0
                and state_target_version == target_version
            ):
                retain_epoch(state.get("target_id"), epoch)
        for operation in projection.get("operations", []):
            if not isinstance(operation, Mapping):
                continue
            epoch = operation.get("target_epoch")
            operation_target_version = int(operation.get("target_version", 0))
            if (
                isinstance(epoch, int)
                and not isinstance(epoch, bool)
                and epoch >= 0
                and (
                    operation_target_version == target_version
                    or (target_version == 1 and operation_target_version == 0)
                )
            ):
                retain_epoch(operation.get("target_id"), epoch)
        return dict(sorted(floors.items()))

    @classmethod
    def _target_epoch_floor(
        cls,
        projection: Mapping[str, object],
        *,
        target_id: str = "",
    ) -> int:
        floors = cls._target_epoch_floors(projection)
        selected = target_id.strip() or cls._selected_target_id(projection)
        if selected:
            return floors.get(selected, 0)
        if len(floors) == 1:
            return next(iter(floors.values()))
        return 0

    def minimum_target_epoch(
        self,
        task_id: str,
        arguments: Mapping[str, object],
    ) -> int:
        case_id = self._resolve_case_id(task_id, arguments)
        projection = self._load(case_id)
        if projection is None:
            return 0
        supplied_targets = self._targets(arguments)
        if supplied_targets and supplied_targets != projection.get("targets", []):
            return 0
        return self._target_epoch_floor(
            projection,
            target_id=self._preferred_target_id(projection, arguments),
        )

    @classmethod
    def _compatible_workflow_operation(
        cls,
        projection: Mapping[str, object],
        operation: str,
        *,
        execution_target_id: str = "",
    ) -> dict[str, object]:
        for kind, name, step_id in cls._workflow_plan(projection):
            if cls._workflow_step_completed(
                projection,
                kind=kind,
                name=name,
                step_id=step_id,
                target_id=(
                    execution_target_id
                    if kind == "operation" and name == operation
                    else None
                ),
            ):
                continue
            if kind == "operation" and name == operation:
                expected_target_id = cls._workflow_step_target_id(
                    projection,
                    kind=kind,
                    name=name,
                    step_id=step_id,
                )
                if operation in {"live_patch_run", "upgrade_run"}:
                    expected_target_id = execution_target_id or expected_target_id
                if (
                    expected_target_id
                    and execution_target_id != expected_target_id
                ):
                    return {}
                return {
                    "workflow_cycle_id": str(
                        projection.get("workflow_cycle_id", "cycle-1")
                    ),
                    "workflow_step_id": step_id,
                    "workflow_step_kind": kind,
                    "target_version": int(projection.get("target_version", 1)),
                }
            break
        return {}

    @staticmethod
    def _domain_result_status(
        descriptor: OperationDescriptor, value: Mapping[str, object]
    ) -> str:
        if descriptor.mutation and not ContextRuntime._mutation_is_terminal(value):
            return "mutation_outcome_unknown"
        explicit = str(value.get("status", "")).strip().lower()
        if explicit in {
            "partial",
            "failed",
            "cancelled",
            "mutation_outcome_unknown",
        }:
            return explicit
        if value.get("ok") is False:
            code = str(
                value.get("normalized_code", value.get("code", ""))
            ).lower()
            if value.get("partial") is True or "partial" in code:
                return "partial"
            return "failed"
        return "completed"

    def _bounded_legacy_value(self, value: Mapping[str, object]) -> dict[str, object]:
        sanitized = _sanitize(value)
        if isinstance(sanitized, Mapping) and len(_json_bytes(sanitized)) <= self.envelope_max_bytes:
            return dict(sanitized)
        return {
            "ok": value.get("ok"),
            "summary": _summary_for("operation", value),
            "result_compacted": True,
            "next_action": (_next_actions(value) or [""])[0],
        }

    @classmethod
    def _case_status_after_domain(
        cls,
        projection: Mapping[str, object],
        descriptor: OperationDescriptor,
        value: Mapping[str, object],
        *,
        operation_id: str,
        reconciled: bool,
        target_id: str,
    ) -> str:
        terminal_event = {
            "revision": int(projection.get("revision", 0)) + 1,
            "kind": "OperationReconciled" if reconciled else "OperationTerminal",
            "operation_id": operation_id,
            "payload": {
                "status": "completed",
                "summary": _summary_for(descriptor.name, value),
                "case_status": "open",
                "target_epoch": cls._observed_target_epoch(
                    value,
                    target_id=target_id,
                ),
            },
            "created_at": 0.0,
        }
        simulated = _apply_case_events(
            projection,
            (terminal_event,),
            last_access=float(projection.get("last_access", 0.0)),
        )
        return (
            "terminal"
            if cls._continuation_for(simulated)["workflow_complete"]
            else "open"
        )

    def invoke_domain(
        self,
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        executor: Callable[[], Mapping[str, object]],
    ) -> ContextToolResult:
        self._metrics["invocations"] += 1
        case_arguments = dict(arguments)
        case_arguments.setdefault("_context_entry_operation", descriptor.name)
        case_id = self._case_id(task_id, case_arguments)
        self._validate_expected_before_case_update(case_id, case_arguments)
        projection = self._open_case(case_id, case_arguments)
        if projection.get("closed"):
            raise CaseClosed(f"case {case_id} is closed")
        execution_target_id = (
            self._preferred_target_id(projection, arguments)
            if descriptor.mutation or descriptor.name == "debug_collect"
            else self._selected_target_id(projection, arguments)
        )
        idempotency_key = _safe_identifier(
            arguments.get("idempotency_key", operation_id),
            fallback=operation_id,
        )
        resumed_fingerprint = arguments.get("_workflow_request_fingerprint")
        if resumed_fingerprint is not None and (
            not isinstance(resumed_fingerprint, str)
            or len(resumed_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in resumed_fingerprint
            )
        ):
            raise ValueError("invalid resumed workflow request fingerprint")
        request_fingerprint = resumed_fingerprint or _fingerprint(
            {
                "operation": descriptor.name,
                "arguments": _sanitize(
                    {
                        key: value
                        for key, value in arguments.items()
                        if key
                        not in {"case_id", "expected_revision", "idempotency_key"}
                    }
                ),
            }
        )
        reconciling = any(
            isinstance(item, Mapping)
            and item.get("operation_id") == operation_id
            and item.get("status")
            in {"accepted", "running", "mutation_outcome_unknown"}
            for item in projection.get("operations", [])
        )
        try:
            replay = self.repository.claim_idempotency(
                case_id, idempotency_key, request_fingerprint
            )
        except IdempotencyConflict:
            if descriptor.mutation:
                # MutationJournal owns mutation identity conflicts. Invoke the
                # domain only far enough for its durable journal to reject the
                # conflicting operation before any new remote effect.
                executor()
            raise
        if replay is not None:
            return self._result_from_receipt(replay, mutation=descriptor.mutation)
        revision = int(projection["revision"])
        if not reconciling:
            workflow_metadata = {
                "workflow_cycle_id": str(arguments.get("_workflow_cycle_id", "")),
                "workflow_step_id": str(arguments.get("_workflow_step_id", "")),
                "workflow_step_kind": str(arguments.get("_workflow_step_kind", "")),
                "target_version": int(
                    arguments.get(
                        "_workflow_target_version",
                        projection.get("target_version", 1),
                    )
                ),
                "target_id": execution_target_id,
            }
            if not workflow_metadata["workflow_step_id"]:
                workflow_metadata.update(
                    self._compatible_workflow_operation(
                        projection,
                        descriptor.name,
                        execution_target_id=execution_target_id,
                    )
                )
            try:
                projection = self.repository.commit(
                    case_id,
                    expected_revision=revision,
                    events=(
                        PendingCaseEvent(
                            "OperationAccepted",
                            {
                                "operation": descriptor.name,
                                "idempotency_key": idempotency_key,
                                "request_fingerprint": request_fingerprint,
                                **workflow_metadata,
                            },
                            operation_id,
                        ),
                        PendingCaseEvent("OperationStarted", {}, operation_id),
                    ),
                )
                self._cache(projection)
            except Exception:
                self.repository.abandon_idempotency(case_id, idempotency_key)
                raise
        try:
            raw_value = executor()
            if not isinstance(raw_value, Mapping):
                raise TypeError("domain operation must return an object")
            value = dict(raw_value)
            minimum_epoch = arguments.get("_minimum_target_epoch")
            if minimum_epoch is not None:
                if (
                    isinstance(minimum_epoch, bool)
                    or not isinstance(minimum_epoch, int)
                    or minimum_epoch < 0
                ):
                    raise TypeError("_minimum_target_epoch must be a non-negative integer")
                observed_epoch = self._observed_target_epoch(
                    value,
                    target_id=execution_target_id,
                )
                if observed_epoch is None or observed_epoch < minimum_epoch:
                    raise ValueError(
                        "fresh verification did not report the required target epoch: "
                        f"observed {observed_epoch}, required {minimum_epoch}"
                    )
        except Exception as exc:
            current = self.repository.load(case_id)
            terminal_projection: Mapping[str, object] | None = None
            error = {
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
            }
            if current is not None:
                terminal_status = (
                    "mutation_outcome_unknown" if descriptor.mutation else "failed"
                )
                try:
                    event = (
                        PendingCaseEvent(
                            "OperationProgressed",
                            {
                                "status": terminal_status,
                                "next_actions": [
                                    "reconcile the mutation journal before retrying"
                                    if descriptor.mutation
                                    else "resolve the error and retry with the same case"
                                ],
                            },
                            operation_id,
                        )
                        if reconciling
                        else PendingCaseEvent(
                            "OperationTerminal",
                            {
                                "status": terminal_status,
                                "summary": str(exc),
                                "canonical_error": error,
                                "next_actions": [
                                    "reconcile the mutation journal before retrying"
                                    if descriptor.mutation
                                    else "resolve the error and retry with the same case"
                                ],
                                "case_status": "open",
                            },
                            operation_id,
                        )
                    )
                    terminal_projection = self.repository.commit(
                        case_id,
                        expected_revision=int(current["revision"]),
                        events=(event,),
                    )
                    self._cache(terminal_projection)
                except Exception:
                    terminal_projection = None
            if descriptor.mutation or terminal_projection is None:
                self.repository.abandon_idempotency(case_id, idempotency_key)
                raise
            value = {
                "ok": False,
                "status": "failed",
                "error": str(exc),
                "canonical_error": error,
            }
            envelope = self._envelope(
                case_id=case_id,
                revision=int(terminal_projection["revision"]),
                operation_id=operation_id,
                operation=descriptor.name,
                status="failed",
                value=value,
                evidence_refs=(),
                canonical_error=error,
            )
            self.repository.complete_idempotency(
                case_id,
                idempotency_key,
                {
                    "envelope": envelope,
                    "legacy_value": self._bounded_legacy_value(value),
                },
            )
            return ContextToolResult(value, envelope)
        gaps: list[str] = []
        evidence: EvidenceRef | None = None
        try:
            evidence_arguments = dict(arguments)
            if execution_target_id:
                evidence_arguments.setdefault("target_id", execution_target_id)
            evidence = self._put_evidence(
                value,
                case_id=case_id,
                operation_id=operation_id,
                descriptor=descriptor,
                arguments=evidence_arguments,
            )
        except Exception as exc:
            gaps.append(f"evidence_not_persisted: {type(exc).__name__}: {exc}")
            if descriptor.mutation and not self._mutation_is_terminal(value):
                current = self.repository.load(case_id)
                message = f"cannot persist non-terminal mutation evidence: {exc}"
                if current is not None:
                    event_kind = "OperationReconciled" if reconciling else "OperationTerminal"
                    terminal = self.repository.commit(
                        case_id,
                        expected_revision=int(current["revision"]),
                        events=(
                            PendingCaseEvent(
                                event_kind,
                                {
                                    "status": "mutation_outcome_unknown",
                                    "summary": message,
                                    "canonical_error": {
                                        "code": "mutation_outcome_unknown",
                                        "message": message,
                                    },
                                    "next_actions": [
                                        "reconcile the mutation journal before retrying"
                                    ],
                                    "case_status": "open",
                                },
                                operation_id,
                            ),
                        ),
                    )
                    self._cache(terminal)
                self.repository.abandon_idempotency(case_id, idempotency_key)
                raise MutationOutcomeUnknown(message) from exc
        current = self.repository.load(case_id)
        if current is None:
            self.repository.abandon_idempotency(case_id, idempotency_key)
            raise CaseNotFound(case_id)
        pending: list[PendingCaseEvent] = []
        if evidence is not None:
            pending.append(
                PendingCaseEvent(
                    "EvidenceAttached",
                    {"evidence": evidence.to_public_dict()},
                    operation_id,
                )
            )
        result_status = self._domain_result_status(descriptor, value)
        case_status = (
            self._case_status_after_domain(
                current,
                descriptor,
                value,
                operation_id=operation_id,
                reconciled=reconciling,
                target_id=execution_target_id,
            )
            if result_status == "completed"
            else "open"
        )
        pending.append(
            PendingCaseEvent(
                "OperationReconciled" if reconciling else "OperationTerminal",
                {
                    "status": result_status,
                    "summary": _summary_for(descriptor.name, value),
                    "next_actions": _next_actions(value),
                    "case_status": case_status,
                    "target_epoch": self._observed_target_epoch(
                        value,
                        target_id=execution_target_id,
                    ),
                },
                operation_id,
            )
        )
        try:
            projection = self.repository.commit(
                case_id,
                expected_revision=int(current["revision"]),
                events=pending,
            )
        except Exception as exc:
            if descriptor.mutation:
                raise MutationOutcomeUnknown(
                    "mutation returned but its Case terminal receipt could not be committed"
                ) from exc
            raise
        self._cache(projection)
        public_evidence = [evidence.to_public_dict()] if evidence is not None else []
        envelope = self._envelope(
            case_id=case_id,
            revision=int(projection["revision"]),
            operation_id=operation_id,
            operation=descriptor.name,
            status=result_status,
            value=value,
            evidence_refs=public_evidence,
            gaps=gaps,
        )
        receipt = {
            "envelope": envelope,
            "legacy_result_ref": (
                evidence.to_public_dict() if evidence is not None else None
            ),
        }
        if evidence is None:
            receipt["legacy_value"] = self._bounded_legacy_value(value)
        if result_status == "mutation_outcome_unknown":
            self.repository.abandon_idempotency(case_id, idempotency_key)
            raise MutationOutcomeUnknown(
                "mutation returned without a terminal journal stage"
            )
        self.repository.complete_idempotency(case_id, idempotency_key, receipt)
        return ContextToolResult(value, envelope)

    def wrap_status(
        self,
        value: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult:
        case_id = self.repository.case_for_task(task_id) or ""
        projection = self._load(case_id) if case_id else None
        envelope = self._envelope(
            case_id=case_id,
            revision=int(projection["revision"]) if projection else 0,
            operation_id=operation_id,
            operation="runtime_status",
            status="completed",
            value=value,
        )
        envelope["api_version"] = value.get("api_version", RUNTIME_API_VERSION)
        envelope = bounded_envelope(envelope, max_bytes=self.envelope_max_bytes)
        return ContextToolResult(value, envelope)

    def wrap_read(
        self,
        value: Mapping[str, object],
        *,
        operation: str,
        operation_id: str,
        case_id: str,
        status: str = "completed",
    ) -> ContextToolResult:
        projection = self._load(case_id) if case_id else None
        continuation = (
            self._continuation_for(value) if operation == "case_read" else None
        )
        capsule = value.get("capsule") if operation == "case_read" else None
        envelope = self._envelope(
            case_id=case_id,
            revision=int(projection["revision"]) if projection else 0,
            operation_id=operation_id,
            operation=operation,
            status=status,
            value=value,
            evidence_refs=(
                value.get("evidence_refs", [])
                if isinstance(value.get("evidence_refs"), list)
                else ()
            ),
            continuation=continuation,
            capsule=capsule if isinstance(capsule, Mapping) else None,
        )
        return ContextToolResult(value, envelope)

    def error_result(
        self,
        exc: Exception,
        *,
        operation: str,
        arguments: Mapping[str, object],
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult:
        explicit_case = arguments.get("case_id")
        case_id = (
            str(explicit_case).strip()
            if isinstance(explicit_case, str) and explicit_case.strip()
            else (self.repository.case_for_task(task_id) or "")
        )
        projection = self._load(case_id) if case_id else None
        code = str(getattr(exc, "code", type(exc).__name__))
        status = (
            "mutation_outcome_unknown"
            if code == "mutation_outcome_unknown"
            else "failed"
        )
        canonical = {"code": code, "message": str(exc)[:2048]}
        legacy = {
            "ok": False,
            "error": type(exc).__name__,
            "code": code,
            "message": str(exc),
        }
        envelope = self._envelope(
            case_id=case_id,
            revision=int(projection["revision"]) if projection else 0,
            operation_id=operation_id,
            operation=operation,
            status=status,
            value=legacy,
            canonical_error=canonical,
        )
        return ContextToolResult(legacy, envelope)

    def shadow_domain(
        self,
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        value: Mapping[str, object],
    ) -> dict[str, object]:
        """Best-effort migration write that never changes the legacy result."""

        public = dict(value)
        try:
            recorded = self.invoke_domain(
                descriptor,
                arguments,
                task_id=task_id,
                operation_id=operation_id,
                executor=lambda: public,
            )
            self._metrics["shadow_writes"] += 1
            if _fingerprint(_sanitize(recorded)) != _fingerprint(_sanitize(public)):
                self._metrics["shadow_parity_mismatches"] += 1
                public["context_shadow_warning"] = "legacy/context result mismatch"
        except Exception as exc:
            self._metrics["shadow_write_failures"] += 1
            public["context_shadow_warning"] = (
                f"context shadow write failed: {type(exc).__name__}: {exc}"
            )
        return public

    def _control_identity(
        self,
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
        operation_id: str,
    ) -> tuple[str, str]:
        key = _safe_identifier(
            arguments.get("idempotency_key", operation_id), fallback=operation_id
        )
        fingerprint = _fingerprint(
            {
                "operation": descriptor.name,
                "arguments": _sanitize(
                    {
                        name: value
                        for name, value in arguments.items()
                        if name
                        not in {"case_id", "expected_revision", "idempotency_key"}
                    }
                ),
            }
        )
        return key, fingerprint

    def phase_record(
        self,
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult:
        case_id = self._case_id(task_id, arguments)
        projection = self._load(case_id)
        if projection is None:
            raise CaseNotFound(case_id)
        if projection.get("closed"):
            raise CaseClosed(f"case {case_id} is closed")
        expected = arguments.get("expected_revision", projection["revision"])
        phase_type = str(arguments.get("phase_type", "")).strip().lower()
        aliases = {
            "developer": "developer.change",
            "developer.edit": "developer.change",
            "developer:edit": "developer.change",
            "build": "build.artifact",
            "build.package": "build.artifact",
            "build:package": "build.artifact",
        }
        phase_type = aliases.get(phase_type, phase_type)
        if phase_type not in {"developer.change", "build.artifact"}:
            raise ValueError("phase_type must be developer.change or build.artifact")
        producer = str(arguments.get("producer_identity", "")).strip()
        if not producer:
            raise ValueError("producer_identity is required")
        expected_producer = {
            "developer.change": "openubmc-developer",
            "build.artifact": "openubmc-build",
        }[phase_type]
        compatible_producers = {
            "developer.change": {
                "openubmc-developer",
                "developer",
                "developer-skill",
            },
            "build.artifact": {
                "openubmc-build",
                "build",
                "build-skill",
            },
        }[phase_type]
        if producer not in compatible_producers:
            raise ValueError(
                f"{phase_type} producer_identity must be {expected_producer}"
            )
        producer = expected_producer
        status = str(arguments.get("status", "completed")).strip().lower()
        if status not in {"running", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported phase status")
        record: dict[str, object] = {
            "phase_type": phase_type,
            "producer_identity": producer,
            "status": status,
            "source_revision": str(arguments.get("source_revision", "")).strip(),
            "summary": str(arguments.get("summary", "")).strip(),
            "recorded_at": self.clock(),
        }
        if not record["source_revision"]:
            raise ValueError("source_revision is required")
        if not record["summary"]:
            raise ValueError("phase summary is required")
        raw_evidence_ids = arguments.get("evidence_ids", [])
        if not isinstance(raw_evidence_ids, list) or not all(
            isinstance(item, str) and item.strip() for item in raw_evidence_ids
        ):
            raise ValueError("evidence_ids must be an array of non-empty strings")
        record["evidence_ids"] = list(dict.fromkeys(raw_evidence_ids))
        if phase_type == "developer.change":
            authored = arguments.get("authored_files", [])
            if not isinstance(authored, list) or not all(
                isinstance(item, str) and item.strip() for item in authored
            ):
                raise ValueError("developer phase requires authored_files")
            verification = arguments.get("verification_plan", [])
            if not isinstance(verification, list) or not all(
                isinstance(item, str) and item.strip() for item in verification
            ):
                raise ValueError("verification_plan must be an array of strings")
            record.update(
                {
                    "authored_files": list(authored),
                    "verification_plan": list(verification),
                    "artifact_path": str(arguments.get("artifact_path", "")),
                    "remote_path": str(arguments.get("remote_path", "")),
                    "restart_scope": str(arguments.get("restart_scope", "none")),
                }
            )
        else:
            artifact_path = str(arguments.get("artifact_path", "")).strip()
            artifact_sha256 = str(arguments.get("artifact_sha256", "")).strip().lower()
            product_version = str(arguments.get("product_version", "")).strip()
            if status == "completed":
                if not artifact_path:
                    raise ValueError("build phase requires artifact_path")
                if len(artifact_sha256) != 64 or any(
                    character not in "0123456789abcdef"
                    for character in artifact_sha256
                ):
                    raise ValueError("build phase requires a SHA-256 artifact hash")
                if not product_version:
                    raise ValueError("build phase requires product_version")
            record.update(
                {
                    "artifact_path": artifact_path,
                    "artifact_sha256": artifact_sha256,
                    "product_version": product_version,
                }
            )
        key, fingerprint = self._control_identity(
            descriptor, arguments, operation_id
        )
        replay = self.repository.claim_idempotency(case_id, key, fingerprint)
        if replay is not None:
            return self._result_from_receipt(replay)
        if isinstance(expected, bool) or not isinstance(expected, int):
            self.repository.abandon_idempotency(case_id, key)
            raise TypeError("expected_revision must be an integer")
        if expected != projection["revision"]:
            self.repository.abandon_idempotency(case_id, key)
            raise RevisionConflict(
                f"case {case_id} revision is {projection['revision']}, expected {expected}"
            )
        step_id = next(
            (
                candidate_step_id
                for kind, name, candidate_step_id in self._workflow_plan(projection)
                if kind == "phase" and name == phase_type
            ),
            f"phase-{phase_type.replace('.', '-')}",
        )
        retained_step_ids: list[str] = []
        for _kind, _name, candidate_step_id in self._workflow_plan(projection):
            retained_step_ids.append(candidate_step_id)
            if candidate_step_id == step_id:
                break
        current_phase_values = projection.get("workflow_phase_values", {})
        current_phase = (
            current_phase_values.get(phase_type)
            if isinstance(current_phase_values, Mapping)
            else None
        )
        if isinstance(current_phase, Mapping) and current_phase.get("status") == "completed":
            if phase_type == "developer.change":
                next_cycle_number = int(projection.get("workflow_cycle_number", 1)) + 1
                projection = self.repository.commit(
                    case_id,
                    expected_revision=int(projection["revision"]),
                    events=(
                        PendingCaseEvent(
                            "WorkflowCycleStarted",
                            {
                                "workflow_cycle_number": next_cycle_number,
                                "workflow_cycle_id": f"cycle-{next_cycle_number}",
                                "reason": "new developer change",
                            },
                        ),
                    ),
                )
                self._cache(projection)
                expected = int(projection["revision"])
            else:
                projection = self.repository.commit(
                    case_id,
                    expected_revision=int(projection["revision"]),
                    events=(
                        PendingCaseEvent(
                            "WorkflowStepsInvalidated",
                            {
                                "after_step_id": step_id,
                                "retained_step_ids": retained_step_ids,
                            },
                        ),
                    ),
                )
                self._cache(projection)
                expected = int(projection["revision"])
        cycle_id = str(projection.get("workflow_cycle_id", "cycle-1"))
        record["workflow_cycle_id"] = cycle_id
        record["workflow_step_id"] = step_id
        record["phase_attempt"] = self._workflow_attempt(
            projection,
            kind="phase",
            step_id=step_id,
        ) + 1
        prior_running_operation_ids = [
            str(item.get("operation_id", ""))
            for item in projection.get("operations", [])
            if isinstance(item, Mapping)
            and str(item.get("operation_id", "")) != operation_id
            and str(item.get("workflow_step_id", "")) == step_id
            and str(item.get("workflow_cycle_id", "")) == cycle_id
            and str(item.get("status", "")) in {"accepted", "running"}
        ]
        try:
            evidence = self._put_evidence(
                record,
                case_id=case_id,
                operation_id=operation_id,
                descriptor=descriptor,
                arguments=arguments,
            )
            events: list[PendingCaseEvent] = []
            if status != "running":
                events.extend(
                    PendingCaseEvent(
                        "OperationTerminal",
                        {
                            "status": "superseded",
                            "summary": f"{phase_type} terminal record received",
                            "case_status": "open",
                        },
                        prior_operation_id,
                    )
                    for prior_operation_id in prior_running_operation_ids
                )
            events.extend(
                (
                    PendingCaseEvent(
                        "OperationAccepted",
                        {
                            "operation": descriptor.name,
                            "idempotency_key": key,
                            "request_fingerprint": fingerprint,
                            "workflow_cycle_id": cycle_id,
                            "workflow_step_id": step_id,
                            "workflow_step_kind": "phase",
                        },
                        operation_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, operation_id),
                    PendingCaseEvent(
                        "OperationProgressed",
                        {"status": status, "phase_record": record},
                        operation_id,
                    ),
                    PendingCaseEvent(
                        "EvidenceAttached",
                        {"evidence": evidence.to_public_dict()},
                        operation_id,
                    ),
                )
            )
            if status != "running":
                events.append(
                    PendingCaseEvent(
                        "OperationTerminal",
                        {
                            "status": status,
                            "summary": str(record["summary"]),
                            "case_status": "open",
                        },
                        operation_id,
                    )
                )
            projection = self.repository.commit(
                case_id,
                expected_revision=expected,
                events=events,
            )
        except Exception:
            self.repository.abandon_idempotency(case_id, key)
            raise
        self._cache(projection)
        envelope = self._envelope(
            case_id=case_id,
            revision=int(projection["revision"]),
            operation_id=operation_id,
            operation=descriptor.name,
            status=status,
            value=record,
            evidence_refs=(evidence.to_public_dict(),),
        )
        self.repository.complete_idempotency(
            case_id,
            key,
            {
                "envelope": envelope,
                "legacy_result_ref": evidence.to_public_dict(),
            },
        )
        return ContextToolResult(record, envelope)

    @staticmethod
    def _workflow_plan(
        projection: Mapping[str, object]
    ) -> list[tuple[str, str, str]]:
        intent = str(projection.get("intent", "diagnosis-only"))
        delivery = str(projection.get("delivery_strategy", "source-only")) or "source-only"
        if intent == "bundle-and-diagnose":
            raw = [("operation", "log_bundle_collect"), ("operation", "debug_run")]
        elif intent == "live-patch":
            raw = [("operation", "live_patch_run"), ("operation", "debug_collect")]
        elif intent == "upgrade-and-verify":
            raw = [("operation", "upgrade_run"), ("operation", "debug_collect")]
        elif intent == "diagnose-and-fix":
            raw = [("operation", "debug_run"), ("phase", "developer.change")]
            if delivery == "build-upgrade":
                raw.extend(
                    [
                        ("phase", "build.artifact"),
                        ("operation", "upgrade_run"),
                        ("operation", "debug_collect"),
                    ]
                )
            elif delivery == "live-patch":
                raw.extend(
                    [
                        ("operation", "live_patch_run"),
                        ("operation", "debug_collect"),
                    ]
                )
        elif (
            intent == "diagnosis-only"
            and projection.get("entry_operation") == "debug_collect"
        ):
            raw = [("operation", "debug_collect")]
        else:
            raw = [("operation", "debug_run")]
        return [
            (kind, name, f"step-{index:02d}-{name.replace('.', '-')}")
            for index, (kind, name) in enumerate(raw, start=1)
        ]

    @staticmethod
    def _completed_operation_counts(
        projection: Mapping[str, object]
    ) -> dict[str, int]:
        raw = projection.get("completed_operation_counts", {})
        if isinstance(raw, Mapping):
            counts = {
                str(name): int(count)
                for name, count in raw.items()
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0
            }
            if counts or projection.get("operation_count", 0) == 0:
                return counts
        counts: dict[str, int] = {}
        for operation in projection.get("operations", []):
            if (
                isinstance(operation, Mapping)
                and operation.get("status")
                in {"completed", "verified", "succeeded"}
                and operation.get("operation") != "workflow.advance"
            ):
                name = str(operation.get("operation", ""))
                if name:
                    counts[name] = counts.get(name, 0) + 1
        return counts

    @staticmethod
    def _completed_phases(projection: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
        current = projection.get("workflow_phase_values", {})
        if isinstance(current, Mapping):
            completed = {
                str(phase_type): dict(record)
                for phase_type, record in current.items()
                if isinstance(record, Mapping)
                and record.get("status") == "completed"
            }
            if completed:
                return completed
        cycle_id = str(projection.get("workflow_cycle_id", ""))
        latest: dict[str, Mapping[str, object]] = {}
        for record in projection.get("phase_records", []):
            if (
                isinstance(record, Mapping)
                and isinstance(record.get("phase_type"), str)
                and (
                    not cycle_id
                    or str(record.get("workflow_cycle_id", cycle_id)) == cycle_id
                )
            ):
                latest[str(record["phase_type"])] = record
        return {
            phase_type: record
            for phase_type, record in latest.items()
            if record.get("status") == "completed"
        }

    @staticmethod
    def _domain_arguments(
        projection: Mapping[str, object],
        operation: str,
        completed_phases: Mapping[str, Mapping[str, object]],
    ) -> dict[str, object]:
        raw = projection.get("workflow_inputs", {})
        arguments = dict(raw) if isinstance(raw, Mapping) else {}
        workflow = arguments.pop("workflow", {})
        domain = {
            "debug_run": "debug",
            "debug_collect": "debug",
            "log_bundle_collect": "log_analyzer",
            "live_patch_run": "live_patch",
            "upgrade_run": "upgrade",
        }.get(operation, "")
        if isinstance(workflow, Mapping):
            section = workflow.get(domain)
            if isinstance(section, Mapping):
                arguments.update(section)
        arguments["case_id"] = projection["case_id"]
        targets = projection.get("targets", [])
        if "ip" not in arguments and isinstance(targets, list) and targets:
            first = targets[0]
            if isinstance(first, Mapping) and first.get("address"):
                arguments["ip"] = first["address"]
        if operation == "debug_collect":
            arguments["profile"] = "freshness"
        if operation == "live_patch_run":
            developer = completed_phases.get("developer.change", {})
            for source, destination in (
                ("artifact_path", "local_path"),
                ("remote_path", "remote_path"),
                ("restart_scope", "restart_scope"),
                ("verification_plan", "verification_checks"),
            ):
                if source in developer and destination not in arguments:
                    arguments[destination] = developer[source]
        if operation == "upgrade_run":
            build = completed_phases.get("build.artifact", {})
            for name in ("artifact_path", "artifact_sha256", "product_version"):
                if name in build:
                    arguments[name] = build[name]
        return arguments

    @classmethod
    def _workflow_step_target_id(
        cls,
        projection: Mapping[str, object],
        *,
        kind: str,
        name: str,
        step_id: str,
    ) -> str:
        if kind != "operation":
            return ""
        states = projection.get("workflow_step_states", {})
        state = states.get(step_id) if isinstance(states, Mapping) else None
        if (
            isinstance(state, Mapping)
            and state.get("status") in {"completed", "verified", "succeeded"}
            and int(state.get("target_version", 0))
            == int(projection.get("target_version", 1))
        ):
            return str(state.get("target_id", ""))
        if name == "debug_collect":
            plan = cls._workflow_plan(projection)
            preceding = []
            for prior_kind, prior_name, prior_step_id in plan:
                if prior_step_id == step_id:
                    break
                preceding.append((prior_kind, prior_name, prior_step_id))
            for prior_kind, prior_name, prior_step_id in reversed(preceding):
                if prior_kind != "operation" or prior_name not in {
                    "live_patch_run",
                    "upgrade_run",
                }:
                    continue
                prior_state = (
                    states.get(prior_step_id)
                    if isinstance(states, Mapping)
                    else None
                )
                if (
                    isinstance(prior_state, Mapping)
                    and prior_state.get("status")
                    in {"completed", "verified", "succeeded"}
                    and int(prior_state.get("target_version", 0))
                    == int(projection.get("target_version", 1))
                ):
                    return str(prior_state.get("target_id", ""))
        if name in {"live_patch_run", "upgrade_run", "debug_collect"}:
            return cls._preferred_target_id(projection)
        return cls._selected_target_id(projection)

    @classmethod
    def _workflow_step_completed(
        cls,
        projection: Mapping[str, object],
        *,
        kind: str,
        name: str,
        step_id: str,
        target_id: str | None = None,
    ) -> bool:
        states = projection.get("workflow_step_states", {})
        state = states.get(step_id) if isinstance(states, Mapping) else None
        if not isinstance(state, Mapping):
            return False
        if state.get("status") not in {"completed", "verified", "succeeded"}:
            return False
        if kind == "operation":
            expected_target_id = (
                cls._workflow_step_target_id(
                    projection,
                    kind=kind,
                    name=name,
                    step_id=step_id,
                )
                if target_id is None
                else target_id
            )
            return (
                int(state.get("target_version", 0))
                == int(projection.get("target_version", 1))
                and str(state.get("target_id", "")) == expected_target_id
            )
        return True

    @staticmethod
    def _workflow_attempt(
        projection: Mapping[str, object],
        *,
        kind: str,
        step_id: str,
    ) -> int:
        cycle_id = str(projection.get("workflow_cycle_id", "cycle-1"))
        key = (
            f"{cycle_id}:{kind}:{step_id}:target-{int(projection.get('target_version', 1))}"
            if kind == "operation"
            else f"{cycle_id}:{kind}:{step_id}"
        )
        attempts = projection.get("workflow_step_attempts", {})
        value = attempts.get(key, 0) if isinstance(attempts, Mapping) else 0
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    @classmethod
    def _continuation_for(
        cls,
        projection: Mapping[str, object],
    ) -> dict[str, object]:
        required_kind = ""
        required_name = ""
        required_step_id = ""
        for kind, name, step_id in cls._workflow_plan(projection):
            if cls._workflow_step_completed(
                projection,
                kind=kind,
                name=name,
                step_id=step_id,
            ):
                continue
            required_kind = kind
            required_name = name
            required_step_id = step_id
            break
        blocked_operation = next(
            (
                item
                for item in reversed(list(projection.get("operations", [])))
                if isinstance(item, Mapping)
                and str(item.get("status", ""))
                in {"failed", "mutation_outcome_unknown"}
            ),
            None,
        )
        next_actions = projection.get("next_actions", [])
        next_action = (
            str(next_actions[0])
            if isinstance(next_actions, list) and next_actions
            else ""
        )
        if not next_action and required_name:
            next_action = (
                f"submit phase_record for {required_name}"
                if required_kind == "phase"
                else f"run {required_name}"
            )
        return {
            "intent": str(projection.get("intent", "")),
            "delivery_strategy": str(projection.get("delivery_strategy", "")),
            "targets": [
                dict(target)
                for target in projection.get("targets", [])
                if isinstance(target, Mapping)
            ],
            "target_version": int(projection.get("target_version", 1)),
            "target_epoch_floor": cls._target_epoch_floor(
                projection,
                target_id=cls._selected_target_id(projection),
            ),
            "target_epoch_floors": cls._target_epoch_floors(projection),
            "workflow_cycle_id": str(
                projection.get("workflow_cycle_id", "cycle-1")
            ),
            "workflow_cycle_number": int(
                projection.get("workflow_cycle_number", 1)
            ),
            "workflow_complete": not bool(required_name),
            "current_phase": required_name,
            "required_phase_type": required_name if required_kind == "phase" else "",
            "required_operation": required_name if required_kind == "operation" else "",
            "required_workflow_step_id": required_step_id,
            "blocked_operation_id": (
                str(blocked_operation.get("operation_id", ""))
                if isinstance(blocked_operation, Mapping)
                else ""
            ),
            "status": str(projection.get("status", "open")),
            "next_action": next_action,
        }

    def workflow_advance(
        self,
        descriptor: OperationDescriptor,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        domain_invoker: Callable[[str, Mapping[str, object], str], Mapping[str, object]],
    ) -> ContextToolResult:
        case_id = self._case_id(task_id, arguments)
        self._validate_expected_before_case_update(case_id, arguments)
        projection = self._open_case(case_id, arguments)
        if projection.get("closed"):
            raise CaseClosed(f"case {case_id} is closed")
        expected = int(projection["revision"])
        key, fingerprint = self._control_identity(
            descriptor, arguments, operation_id
        )
        replay = self.repository.claim_idempotency(case_id, key, fingerprint)
        if replay is not None:
            return self._result_from_receipt(replay)
        try:
            projection = self.repository.commit(
                case_id,
                expected_revision=expected,
                events=(
                    PendingCaseEvent(
                        "OperationAccepted",
                        {
                            "operation": descriptor.name,
                            "idempotency_key": key,
                            "request_fingerprint": fingerprint,
                        },
                        operation_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, operation_id),
                ),
            )
            self._cache(projection)
            max_steps = arguments.get("max_steps", 8)
            if isinstance(max_steps, bool) or not isinstance(max_steps, int):
                raise TypeError("max_steps must be an integer")
            if max_steps <= 0 or max_steps > 64:
                raise ValueError("max_steps must be between 1 and 64")
            plan = self._workflow_plan(projection)
            status = "completed"
            value: dict[str, object] = {}
            steps_run = 0
            while steps_run < max_steps:
                projection = self.repository.load(case_id)
                if projection is None:
                    raise CaseNotFound(case_id)
                tracked_unknown = projection.get(
                    "mutation_outcome_unknown_operations", {}
                )
                unknown_mutation = next(
                    (
                        item
                        for item in (
                            tracked_unknown.values()
                            if isinstance(tracked_unknown, Mapping)
                            else projection.get("operations", [])
                        )
                        if isinstance(item, Mapping)
                        and item.get("status") == "mutation_outcome_unknown"
                        and str(item.get("operation", "")) in self.catalog.names()
                        and self.catalog.require(
                            str(item.get("operation", ""))
                        ).mutation
                    ),
                    None,
                )
                if isinstance(unknown_mutation, Mapping):
                    status = "mutation_outcome_unknown"
                    value = {
                        "completed": False,
                        "status": status,
                        "steps_run": steps_run,
                        "blocked_operation_id": unknown_mutation.get(
                            "operation_id", ""
                        ),
                        "next_action": (
                            "reconcile the mutation journal explicitly before continuing"
                        ),
                    }
                    break
                completed_phases = self._completed_phases(projection)
                next_step: tuple[str, str, str] | None = None
                for kind, name, step_id in plan:
                    if not self._workflow_step_completed(
                        projection,
                        kind=kind,
                        name=name,
                        step_id=step_id,
                    ):
                        next_step = (kind, name, step_id)
                        break
                if next_step is None:
                    value = {
                        "completed": True,
                        "status": "completed",
                        "steps_run": steps_run,
                        "next_action": "",
                    }
                    status = "completed"
                    break
                kind, name, step_id = next_step
                if kind == "phase":
                    value = {
                        "completed": False,
                        "status": "waiting_phase_record",
                        "required_phase_type": name,
                        "required_workflow_step_id": step_id,
                        "workflow_cycle_id": projection.get(
                            "workflow_cycle_id", "cycle-1"
                        ),
                        "steps_run": steps_run,
                        "next_action": f"submit phase_record for {name}",
                    }
                    status = "waiting_phase_record"
                    break
                if name not in self.catalog.names():
                    value = {
                        "completed": False,
                        "status": "waiting_external",
                        "required_operation": name,
                        "steps_run": steps_run,
                        "next_action": f"install or provide operation {name}",
                    }
                    status = "waiting_external"
                    break
                domain_arguments = self._domain_arguments(
                    projection, name, completed_phases
                )
                required_target_id = self._workflow_step_target_id(
                    projection,
                    kind=kind,
                    name=name,
                    step_id=step_id,
                )
                if required_target_id:
                    domain_arguments["target_id"] = required_target_id
                target_epoch_floor = self._target_epoch_floor(
                    projection,
                    target_id=(
                        self._preferred_target_id(
                            projection,
                            domain_arguments,
                        )
                        if name in {
                            "live_patch_run",
                            "upgrade_run",
                            "debug_collect",
                        }
                        else self._selected_target_id(
                            projection,
                            domain_arguments,
                        )
                    ),
                )
                if name in {"live_patch_run", "upgrade_run"}:
                    domain_arguments["_minimum_target_epoch"] = target_epoch_floor
                if name == "debug_collect":
                    if target_epoch_floor:
                        domain_arguments["_minimum_target_epoch"] = target_epoch_floor
                cycle_id = str(projection.get("workflow_cycle_id", "cycle-1"))
                target_version = int(projection.get("target_version", 1))
                resumable_operation = next(
                    (
                        item
                        for item in reversed(list(projection.get("operations", [])))
                        if isinstance(item, Mapping)
                        and str(item.get("operation", "")) == name
                        and str(item.get("workflow_cycle_id", "")) == cycle_id
                        and str(item.get("workflow_step_id", "")) == step_id
                        and int(item.get("target_version", 0)) == target_version
                        and str(item.get("target_id", "")) == required_target_id
                        and str(item.get("status", "")) in {"accepted", "running"}
                    ),
                    None,
                )
                if isinstance(resumable_operation, Mapping):
                    derived_operation_id = str(
                        resumable_operation.get("operation_id", "")
                    )
                    resumed_request_fingerprint = str(
                        resumable_operation.get("request_fingerprint", "")
                    )
                    if resumed_request_fingerprint:
                        domain_arguments["_workflow_request_fingerprint"] = (
                            resumed_request_fingerprint
                        )
                else:
                    attempt = self._workflow_attempt(
                        projection,
                        kind=kind,
                        step_id=step_id,
                    ) + 1
                    derived_operation_id = (
                        "op-advance-"
                        + hashlib.sha256(
                            (
                                f"{case_id}:{cycle_id}:{target_version}:"
                                f"{step_id}:attempt-{attempt}"
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                    )
                domain_arguments["idempotency_key"] = derived_operation_id
                domain_arguments["_workflow_cycle_id"] = cycle_id
                domain_arguments["_workflow_step_id"] = step_id
                domain_arguments["_workflow_step_kind"] = kind
                domain_arguments["_workflow_target_version"] = target_version
                try:
                    domain_result = domain_invoker(
                        name, domain_arguments, derived_operation_id
                    )
                except Exception:
                    after_failure = self.repository.load(case_id)
                    failed_operation = None
                    if after_failure is not None:
                        failed_operation = next(
                            (
                                item
                                for item in reversed(after_failure.get("operations", []))
                                if isinstance(item, Mapping)
                                and item.get("operation_id") == derived_operation_id
                            ),
                            None,
                        )
                    failed_status = (
                        str(failed_operation.get("status", ""))
                        if isinstance(failed_operation, Mapping)
                        else ""
                    )
                    if failed_status in {"failed", "mutation_outcome_unknown"}:
                        status = failed_status
                        value = {
                            "completed": False,
                            "status": status,
                            "steps_run": steps_run,
                            "blocked_operation_id": derived_operation_id,
                            "next_action": (
                                "reconcile the mutation journal explicitly before continuing"
                                if status == "mutation_outcome_unknown"
                                else "resolve the failed operation and continue the same case"
                            ),
                        }
                        break
                    raise
                domain_envelope = getattr(domain_result, "envelope", {})
                domain_status = (
                    str(domain_envelope.get("status", "completed"))
                    if isinstance(domain_envelope, Mapping)
                    else "completed"
                )
                if domain_status not in {"completed", "verified", "succeeded"}:
                    steps_run += 1
                    status = domain_status
                    value = {
                        "completed": False,
                        "status": status,
                        "steps_run": steps_run,
                        "blocked_operation_id": derived_operation_id,
                        "next_action": (
                            "reconcile the mutation journal explicitly before continuing"
                            if status == "mutation_outcome_unknown"
                            else "inspect the operation evidence and resolve its incomplete result"
                        ),
                    }
                    break
                steps_run += 1
            else:
                status = "budget_exhausted"
                value = {
                    "completed": False,
                    "status": status,
                    "steps_run": steps_run,
                    "next_action": "call workflow.advance again to continue",
                }
            projection = self.repository.load(case_id)
            if projection is None:
                raise CaseNotFound(case_id)
            evidence = self._put_evidence(
                value,
                case_id=case_id,
                operation_id=operation_id,
                descriptor=descriptor,
                arguments=arguments,
            )
            projection = self.repository.commit(
                case_id,
                expected_revision=int(projection["revision"]),
                events=(
                    PendingCaseEvent(
                        "OperationProgressed",
                        {
                            "status": status,
                            "next_actions": _next_actions(value),
                        },
                        operation_id,
                    ),
                    PendingCaseEvent(
                        "EvidenceAttached",
                        {"evidence": evidence.to_public_dict()},
                        operation_id,
                    ),
                    PendingCaseEvent(
                        "OperationTerminal",
                        {
                            "status": status,
                            "summary": _summary_for(descriptor.name, value),
                            "next_actions": _next_actions(value),
                            "case_status": (
                                "terminal"
                                if status in {"completed", "failed", "cancelled"}
                                else status
                            ),
                        },
                        operation_id,
                    ),
                ),
            )
        except Exception:
            self.repository.abandon_idempotency(case_id, key)
            raise
        self._cache(projection)
        envelope = self._envelope(
            case_id=case_id,
            revision=int(projection["revision"]),
            operation_id=operation_id,
            operation=descriptor.name,
            status=status,
            value=value,
            evidence_refs=(evidence.to_public_dict(),),
        )
        self.repository.complete_idempotency(
            case_id,
            key,
            {
                "envelope": envelope,
                "legacy_result_ref": evidence.to_public_dict(),
            },
        )
        return ContextToolResult(value, envelope)

    def read_case(self, case_id: str) -> dict[str, object]:
        projection = self._load(case_id, touch=True)
        if projection is None:
            raise CaseNotFound(case_id)
        projection["capsule"] = self._capsule(projection)
        return projection

    def read_evidence(
        self,
        case_id: str,
        evidence_id: str,
        *,
        offset: int = 0,
        limit: int = DEFAULT_EVIDENCE_READ_BYTES,
        target_id: str = "",
        generation: str = "",
    ) -> dict[str, object]:
        if offset < 0:
            raise ValueError("evidence offset must be non-negative")
        if limit <= 0 or limit > MAX_EVIDENCE_READ_BYTES:
            raise ValueError(
                f"evidence limit must be between 1 and {MAX_EVIDENCE_READ_BYTES}"
            )
        projection = self._load(case_id, touch=True)
        if projection is None:
            raise CaseNotFound(case_id)
        reference = next(
            (
                item
                for item in projection["evidence_refs"]
                if item.get("evidence_id") == evidence_id
            ),
            None,
        )
        if not isinstance(reference, Mapping):
            reference = self.repository.evidence_reference(case_id, evidence_id)
        if not isinstance(reference, Mapping):
            raise EvidenceUnavailable(
                f"evidence {evidence_id} does not belong to case {case_id}"
            )
        if target_id and str(reference.get("target_id", "")) != target_id:
            raise EvidenceUnavailable(
                f"evidence {evidence_id} target does not match {target_id}"
            )
        if generation and str(reference.get("generation", "")) != generation:
            raise EvidenceUnavailable(
                f"evidence {evidence_id} generation does not match {generation}"
            )
        body = self.blob_repository.read(
            str(reference["blob_id"]), offset=offset, limit=limit
        )
        self._metrics["evidence_reads"] += 1
        self._metrics["evidence_bytes_read"] += len(body)
        return {
            "schema": f"{CONTEXT_RUNTIME_SCHEMA}/evidence-read",
            "case_id": case_id,
            "evidence": dict(reference),
            "offset": offset,
            "returned_bytes": len(body),
            "truncated": offset + len(body) < int(reference["byte_count"]),
            "body": body.decode("utf-8", errors="replace"),
        }

    def close_case(self, case_id: str, *, expected_revision: int) -> dict[str, object]:
        projection = self._load(case_id)
        if projection is None:
            raise CaseNotFound(case_id)
        if projection.get("closed"):
            return projection
        if expected_revision != projection["revision"]:
            raise RevisionConflict(
                f"case {case_id} revision is {projection['revision']}, expected {expected_revision}"
            )
        active = any(
            isinstance(operation, Mapping)
            and str(operation.get("status", ""))
            in {"accepted", "running", "mutation_outcome_unknown"}
            for operation in projection.get("operations", [])
        )
        unknown_mutations = projection.get(
            "mutation_outcome_unknown_operations", {}
        )
        workflow_complete = bool(
            self._continuation_for(projection).get("workflow_complete")
        )
        if (
            active
            or projection.get("status") == "mutation_outcome_unknown"
            or bool(unknown_mutations)
            or not workflow_complete
        ):
            raise CaseNotForgettable(f"case {case_id} is not terminal")
        closed = self.repository.commit(
            case_id,
            expected_revision=expected_revision,
            events=(PendingCaseEvent("CaseClosed", {}),),
        )
        return self._cache(closed)

    def forget_case(self, case_id: str) -> dict[str, object]:
        projection = self._load(case_id)
        if projection is None:
            return {"case_id": case_id, "forgotten": False}
        legacy_terminal = (
            projection.get("status") == "terminal"
            and not projection.get("workflow_step_states")
        )
        if (
            not projection.get("closed")
            and not self._continuation_for(projection).get("workflow_complete")
            and not legacy_terminal
        ) or projection.get("status") == "mutation_outcome_unknown":
            raise CaseNotForgettable(f"case {case_id} is not terminal")
        references = self.repository.delete_case(case_id)
        deleted_blobs = 0
        for reference in references:
            blob_id = str(reference.get("blob_id", ""))
            if blob_id and self.repository.blob_reference_count(blob_id) == 0:
                deleted_blobs += int(self.blob_repository.delete(blob_id))
        with self._lock:
            if self._projection_cache.pop(case_id, None) is not None:
                self._projection_cache_bytes -= self._projection_cache_sizes.pop(
                    case_id, 0
                )
            self._capsule_cache.pop(case_id, None)
        return {
            "case_id": case_id,
            "forgotten": True,
            "deleted_blobs": deleted_blobs,
        }

    def maintain(self) -> dict[str, object]:
        now = self.clock()
        evicted_cases = 0
        for meta in self.repository.metadata():
            status = str(meta.get("status", "open"))
            last_access = float(meta.get("last_access", 0.0))
            if (
                status in {"terminal", "closed"}
                and now - last_access >= self.retention_seconds
                and not self.repository.is_case_bound(str(meta["case_id"]))
            ):
                self.forget_case(str(meta["case_id"]))
                evicted_cases += 1
            elif (
                status not in {"terminal", "closed", "mutation_outcome_unknown"}
                and now - last_access >= self.retention_seconds * 4
                and not self.repository.is_case_bound(str(meta["case_id"]))
            ):
                case_id = str(meta["case_id"])
                projection = self._load(case_id)
                if projection is None:
                    continue
                try:
                    self.close_case(
                        case_id,
                        expected_revision=int(projection["revision"]),
                    )
                    self.forget_case(case_id)
                except CaseNotForgettable:
                    continue
                evicted_cases += 1
        if (
            self.blob_repository.size_bytes() + self.repository.size_bytes()
            > self.storage_soft_limit_bytes
        ):
            for meta in sorted(
                self.repository.metadata(), key=lambda item: float(item["last_access"])
            ):
                if (
                    self.blob_repository.size_bytes() + self.repository.size_bytes()
                    <= self.storage_soft_limit_bytes
                ):
                    break
                case_id = str(meta["case_id"])
                status = str(meta.get("status"))
                if status in {"terminal", "closed"} and not self.repository.is_case_bound(
                    case_id
                ):
                    self.forget_case(case_id)
                elif (
                    status != "mutation_outcome_unknown"
                    and not self.repository.is_case_bound(case_id)
                ):
                    projection = self._load(case_id)
                    if projection is None:
                        continue
                    try:
                        self.close_case(
                            case_id,
                            expected_revision=int(projection["revision"]),
                        )
                        self.forget_case(case_id)
                    except CaseNotForgettable:
                        continue
                else:
                    continue
                evicted_cases += 1
        self._metrics["maintenance_evictions"] += evicted_cases
        return {"evicted_cases": evicted_cases}

    def status(self) -> dict[str, object]:
        with self._lock:
            cache_count = len(self._projection_cache)
            cache_bytes = self._projection_cache_bytes
            capsule_count = len(self._capsule_cache)
            metrics = dict(self._metrics)
        return {
            "schema": CONTEXT_RUNTIME_SCHEMA,
            "repository": self.repository.status(),
            "blob_bytes": self.blob_repository.size_bytes(),
            "repository_bytes": self.repository.size_bytes(),
            "storage_bytes": (
                self.blob_repository.size_bytes() + self.repository.size_bytes()
            ),
            "projection_cache_count": cache_count,
            "projection_cache_limit": self.max_cached_projections,
            "projection_cache_bytes": cache_bytes,
            "projection_cache_byte_limit": self.max_cached_projection_bytes,
            "capsule_cache_count": capsule_count,
            "capsule_cache_limit": self.max_cached_projections,
            "envelope_max_bytes": self.envelope_max_bytes,
            "retention_seconds": self.retention_seconds,
            "storage_soft_limit_bytes": self.storage_soft_limit_bytes,
            "metrics": metrics,
        }
