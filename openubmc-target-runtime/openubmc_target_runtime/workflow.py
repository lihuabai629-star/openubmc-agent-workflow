"""Canonical versioned workflow definitions and execution identities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import re

from .contracts import RUNTIME_API_VERSION


WORKFLOW_DEFINITION_SCHEMA = f"{RUNTIME_API_VERSION}/workflow-definition-v1"
WORKFLOW_DEFINITION_VERSION = 1
_STEP_KINDS = frozenset({"operation", "phase"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PhaseDescriptor:
    name: str
    owner: str
    receipt_schema: str
    required_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.name):
            raise ValueError("phase name must be a safe identifier")
        if not _SAFE_ID.fullmatch(self.owner):
            raise ValueError("phase owner must be a safe identifier")
        if not self.receipt_schema.strip():
            raise ValueError("phase receipt_schema must not be empty")
        if len(self.required_fields) != len(set(self.required_fields)):
            raise ValueError(f"phase {self.name} has duplicate required fields")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "receipt_schema": self.receipt_schema,
            "required_fields": list(self.required_fields),
        }


class PhaseRegistry:
    """Canonical phase ownership and receipt contract registry."""

    def __init__(self, descriptors: Sequence[PhaseDescriptor]) -> None:
        registered: dict[str, PhaseDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.name in registered:
                raise ValueError(f"duplicate phase descriptor: {descriptor.name}")
            registered[descriptor.name] = descriptor
        self._descriptors = registered

    def require(self, name: str) -> PhaseDescriptor:
        try:
            return self._descriptors[name]
        except KeyError as exc:
            raise ValueError(f"unregistered workflow phase: {name}") from exc

    def validate_receipt(
        self,
        name: str,
        *,
        producer: str,
        receipt: Mapping[str, object],
    ) -> PhaseDescriptor:
        descriptor = self.require(name)
        if producer != descriptor.owner:
            raise ValueError(f"{name} producer_identity must be {descriptor.owner}")
        missing = []
        for field in descriptor.required_fields:
            value = receipt.get(field)
            if value is None or value == "" or value == () or value == []:
                missing.append(field)
        if missing:
            raise ValueError(
                f"{name} receipt omits required fields: {', '.join(missing)}"
            )
        return descriptor

    def to_public_dict(self) -> dict[str, object]:
        return {
            name: descriptor.to_public_dict()
            for name, descriptor in sorted(self._descriptors.items())
        }


@dataclass(frozen=True)
class WorkflowStepDefinition:
    step_id: str
    kind: str
    name: str
    owner: str
    receipt_schema: str = ""

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.step_id):
            raise ValueError("workflow step_id must be a safe identifier")
        if self.kind not in _STEP_KINDS:
            raise ValueError(f"unsupported workflow step kind: {self.kind}")
        if not _SAFE_ID.fullmatch(self.name):
            raise ValueError("workflow step name must be a safe identifier")
        if not _SAFE_ID.fullmatch(self.owner):
            raise ValueError("workflow step owner must be a safe identifier")
        if self.kind == "phase" and not self.receipt_schema:
            raise ValueError("phase workflow steps require a receipt schema")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "name": self.name,
            "owner": self.owner,
            "receipt_schema": self.receipt_schema,
        }

    @classmethod
    def from_public_dict(
        cls, value: Mapping[str, object]
    ) -> "WorkflowStepDefinition":
        return cls(
            step_id=str(value.get("step_id", "")),
            kind=str(value.get("kind", "")),
            name=str(value.get("name", "")),
            owner=str(value.get("owner", "")),
            receipt_schema=str(value.get("receipt_schema", "")),
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    definition_id: str
    version: int
    intent: str
    entry_domain: str
    entry_operation: str
    delivery_strategy: str
    steps: tuple[WorkflowStepDefinition, ...]

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.definition_id):
            raise ValueError("workflow definition_id must be a safe identifier")
        if isinstance(self.version, bool) or self.version <= 0:
            raise ValueError("workflow definition version must be positive")
        if not self.intent.strip():
            raise ValueError("workflow intent must not be empty")
        if not self.steps:
            raise ValueError("workflow definition requires at least one step")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow definition step IDs must be unique")

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.identity_facts())

    def identity_facts(self) -> dict[str, object]:
        return {
            "schema": WORKFLOW_DEFINITION_SCHEMA,
            "definition_id": self.definition_id,
            "version": self.version,
            "intent": self.intent,
            "entry_domain": self.entry_domain,
            "entry_operation": self.entry_operation,
            "delivery_strategy": self.delivery_strategy,
            "steps": [step.to_public_dict() for step in self.steps],
        }

    def to_public_dict(self) -> dict[str, object]:
        return {**self.identity_facts(), "fingerprint": self.fingerprint}

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "WorkflowDefinition":
        if value.get("schema") != WORKFLOW_DEFINITION_SCHEMA:
            raise ValueError("unsupported workflow definition schema")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("workflow definition steps must be an array")
        definition = cls(
            definition_id=str(value.get("definition_id", "")),
            version=int(value.get("version", 0)),
            intent=str(value.get("intent", "")),
            entry_domain=str(value.get("entry_domain", "")),
            entry_operation=str(value.get("entry_operation", "")),
            delivery_strategy=str(value.get("delivery_strategy", "")),
            steps=tuple(
                WorkflowStepDefinition.from_public_dict(step)
                for step in raw_steps
                if isinstance(step, Mapping)
            ),
        )
        recorded = str(value.get("fingerprint", ""))
        if recorded and recorded != definition.fingerprint:
            raise ValueError("workflow definition fingerprint mismatch")
        return definition


@dataclass(frozen=True)
class StepIdentity:
    workflow_definition_id: str
    workflow_version: int
    workflow_fingerprint: str
    cycle_id: str
    step_id: str
    attempt: int
    input_fingerprint: str
    target_version: int
    target_epoch: int

    def __post_init__(self) -> None:
        if self.attempt <= 0:
            raise ValueError("step attempt must be positive")
        if self.target_version <= 0:
            raise ValueError("target version must be positive")
        if self.target_epoch < 0:
            raise ValueError("target epoch must be non-negative")
        for name, value in (
            ("workflow_fingerprint", self.workflow_fingerprint),
            ("input_fingerprint", self.input_fingerprint),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a SHA-256 fingerprint")

    @property
    def execution_id(self) -> str:
        return "step-" + _fingerprint(self.to_public_dict())[:32]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "workflow_definition_id": self.workflow_definition_id,
            "workflow_version": self.workflow_version,
            "workflow_fingerprint": self.workflow_fingerprint,
            "cycle_id": self.cycle_id,
            "step_id": self.step_id,
            "attempt": self.attempt,
            "input_fingerprint": self.input_fingerprint,
            "target_version": self.target_version,
            "target_epoch": self.target_epoch,
        }


class WorkflowRegistry:
    """Resolve one immutable WorkflowDefinition from public Case facts."""

    def __init__(
        self,
        *,
        phases: PhaseRegistry,
        operation_owners: Mapping[str, str],
    ) -> None:
        self.phases = phases
        self.operation_owners = dict(operation_owners)

    def _step(self, index: int, kind: str, name: str) -> WorkflowStepDefinition:
        step_id = f"step-{index:02d}-{name.replace('.', '-')}"
        if kind == "phase":
            phase = self.phases.require(name)
            return WorkflowStepDefinition(
                step_id,
                kind,
                name,
                phase.owner,
                phase.receipt_schema,
            )
        try:
            owner = self.operation_owners[name]
        except KeyError as exc:
            raise ValueError(f"workflow operation has no owner: {name}") from exc
        return WorkflowStepDefinition(step_id, kind, name, owner)

    def resolve(
        self,
        *,
        intent: str,
        entry_domain: str = "",
        entry_operation: str = "",
        delivery_strategy: str = "",
    ) -> WorkflowDefinition:
        normalized_intent = str(intent or "diagnosis-only").strip().lower().replace("_", "-")
        domain = str(entry_domain).strip().lower().replace("-", "_")
        operation = str(entry_operation).strip()
        delivery = (
            str(delivery_strategy or "source-only")
            .strip()
            .lower()
            .replace("_", "-")
        )
        if normalized_intent == "bundle-and-diagnose":
            raw = (("operation", "log_bundle_collect"), ("operation", "debug_run"))
        elif normalized_intent in {"live-patch", "rollback"}:
            raw = (("operation", "live_patch_run"), ("operation", "debug_collect"))
        elif normalized_intent == "upgrade-and-verify":
            raw = (("operation", "upgrade_run"), ("operation", "debug_collect"))
        elif normalized_intent == "diagnose-and-fix":
            items: list[tuple[str, str]] = [
                ("operation", "debug_run"),
                ("phase", "developer.change"),
            ]
            if delivery == "build-upgrade":
                items.extend(
                    (
                        ("phase", "build.artifact"),
                        ("operation", "upgrade_run"),
                        ("operation", "debug_collect"),
                    )
                )
            elif delivery == "live-patch":
                items.extend(
                    (
                        ("operation", "live_patch_run"),
                        ("operation", "debug_collect"),
                    )
                )
            raw = tuple(items)
        elif normalized_intent == "diagnosis-only" and operation == "debug_collect":
            raw = (("operation", "debug_collect"),)
        elif normalized_intent == "diagnosis-only" and domain == "log_analyzer":
            raw = (("operation", "log_bundle_collect"),)
        else:
            raw = (("operation", "debug_run"),)
        steps = tuple(
            self._step(index, kind, name)
            for index, (kind, name) in enumerate(raw, start=1)
        )
        route = operation or domain or steps[0].name
        definition_id = ".".join(
            part
            for part in (
                normalized_intent,
                route.replace("_", "-"),
                delivery if normalized_intent == "diagnose-and-fix" else "",
            )
            if part
        )
        return WorkflowDefinition(
            definition_id=definition_id,
            version=WORKFLOW_DEFINITION_VERSION,
            intent=normalized_intent,
            entry_domain=domain,
            entry_operation=operation,
            delivery_strategy=(
                delivery if normalized_intent == "diagnose-and-fix" else ""
            ),
            steps=steps,
        )


class WorkflowKernel:
    """Deep module that owns workflow definition and step identity semantics."""

    def __init__(self, registry: WorkflowRegistry) -> None:
        self.registry = registry

    def definition_for(self, projection: Mapping[str, object]) -> WorkflowDefinition:
        recorded = projection.get("workflow_definition")
        if isinstance(recorded, Mapping) and recorded:
            return WorkflowDefinition.from_public_dict(recorded)
        intent = str(projection.get("intent", "diagnosis-only"))
        normalized_intent = intent.strip().lower().replace("_", "-")
        if normalized_intent in {"", "diagnosis-only"}:
            operations = projection.get("operations", ())
            operation_names = {
                str(item.get("operation", ""))
                for item in operations
                if isinstance(item, Mapping)
            }
            if "upgrade_run" in operation_names:
                intent = "upgrade-and-verify"
            elif "live_patch_run" in operation_names:
                rollback = any(
                    isinstance(item, Mapping)
                    and item.get("operation") == "live_patch_run"
                    and isinstance(item.get("inputs"), Mapping)
                    and str(item["inputs"].get("action", "")).strip().lower()
                    == "rollback"
                    for item in operations
                )
                intent = "rollback" if rollback else "live-patch"
            elif {"log_bundle_collect", "debug_run"}.issubset(operation_names):
                intent = "bundle-and-diagnose"
        return self.registry.resolve(
            intent=intent,
            entry_domain=str(projection.get("entry_domain", "")),
            entry_operation=str(projection.get("entry_operation", "")),
            delivery_strategy=str(projection.get("delivery_strategy", "")),
        )

    def plan(self, projection: Mapping[str, object]) -> tuple[WorkflowStepDefinition, ...]:
        return self.definition_for(projection).steps

    def step_identity(
        self,
        projection: Mapping[str, object],
        *,
        step: WorkflowStepDefinition,
        attempt: int,
        input_fingerprint: str,
        target_epoch: int,
    ) -> StepIdentity:
        definition = self.definition_for(projection)
        return StepIdentity(
            workflow_definition_id=definition.definition_id,
            workflow_version=definition.version,
            workflow_fingerprint=definition.fingerprint,
            cycle_id=str(projection.get("workflow_cycle_id", "cycle-1")),
            step_id=step.step_id,
            attempt=attempt,
            input_fingerprint=input_fingerprint,
            target_version=int(projection.get("target_version", 1)),
            target_epoch=target_epoch,
        )

    def semantic_cursor(
        self,
        projection: Mapping[str, object],
        *,
        nodes: Sequence[Mapping[str, object]],
        acceptance_plan_id: str,
        context_facts: Mapping[str, object] | None = None,
    ) -> str:
        definition = self.definition_for(projection)
        return _fingerprint(
            {
                "schema": f"{WORKFLOW_DEFINITION_SCHEMA}/semantic-cursor",
                "workflow_definition_id": definition.definition_id,
                "workflow_version": definition.version,
                "workflow_fingerprint": definition.fingerprint,
                "workflow_cycle_id": str(
                    projection.get("workflow_cycle_id", "cycle-1")
                ),
                "target_version": int(projection.get("target_version", 1)),
                "acceptance_plan_id": acceptance_plan_id,
                "context_facts": dict(context_facts or {}),
                "nodes": list(nodes),
            }
        )


DEFAULT_PHASE_REGISTRY = PhaseRegistry(
    (
        PhaseDescriptor(
            "developer.change",
            "openubmc-developer",
            f"{RUNTIME_API_VERSION}/developer-change-receipt-v1",
            ("source_revision", "summary", "authored_files", "verification_plan"),
        ),
        PhaseDescriptor(
            "build.artifact",
            "openubmc-build",
            f"{RUNTIME_API_VERSION}/build-artifact-receipt-v1",
            (
                "source_revision",
                "summary",
                "artifact_path",
                "artifact_sha256",
                "product_version",
            ),
        ),
    )
)

DEFAULT_WORKFLOW_REGISTRY = WorkflowRegistry(
    phases=DEFAULT_PHASE_REGISTRY,
    operation_owners={
        "debug_run": "openubmc-debug",
        "debug_collect": "openubmc-debug",
        "log_bundle_collect": "openubmc-log-analyzer",
        "live_patch_run": "openubmc-live-patch",
        "upgrade_run": "openubmc-upgrade",
    },
)

DEFAULT_WORKFLOW_KERNEL = WorkflowKernel(DEFAULT_WORKFLOW_REGISTRY)
