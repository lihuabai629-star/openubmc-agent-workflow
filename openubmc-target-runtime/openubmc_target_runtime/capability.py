"""Declarative Runtime SDK capability routing and typed domain receipts."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol

from .catalog import OperationCatalogError, validate_json_schema
from .contracts import RUNTIME_API_VERSION
from .mutation import MutationJournal, MutationRecoveryDisposition
from .semantic_runtime import ArtifactRef


CAPABILITY_REGISTRY_SCHEMA = f"{RUNTIME_API_VERSION}/capability-registry-v1"
DOMAIN_RECEIPT_SCHEMA = f"{RUNTIME_API_VERSION}/domain-receipt-v1"
DOMAIN_ACTION_SCHEMA = f"{RUNTIME_API_VERSION}/domain-action-v1"
DOMAIN_RESULT_SCHEMA = f"{RUNTIME_API_VERSION}/domain-result-v1"
DOMAIN_PACK_SCHEMA = f"{RUNTIME_API_VERSION}/domain-pack-v1"
DOMAIN_PACK_CONFORMANCE_SCHEMA = f"{RUNTIME_API_VERSION}/domain-pack-conformance-v1"
RUNTIME_EFFECT_RECOVERY_ARGUMENT = "_runtime_effect_recovery"
_OUTCOME_STATUSES = frozenset(
    {
        "succeeded",
        "modified",
        "verified",
        "running",
        "skipped",
        "unavailable",
        "failed",
        "blocked",
        "mutation_outcome_unknown",
    }
)
_PACK_VERSION = re.compile(r"[1-9][0-9]{0,8}(?:\.[0-9]+){0,2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class CapabilityDescriptor:
    operation: str
    capability: str
    owner_skill: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    timeout_seconds: float
    evidence_types: tuple[str, ...]
    runtime_api_version: str = RUNTIME_API_VERSION
    mutation: bool = False

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise OperationCatalogError("capability operation is required")
        if not self.capability.strip():
            raise OperationCatalogError(
                f"operation {self.operation} requires a capability"
            )
        if not self.owner_skill.strip():
            raise OperationCatalogError(
                f"operation {self.operation} requires an owning Skill"
            )
        validate_json_schema(self.input_schema, path=f"{self.operation}.input")
        validate_json_schema(self.output_schema, path=f"{self.operation}.output")
        if self.input_schema.get("type") != "object":
            raise OperationCatalogError(
                f"operation {self.operation} input schema must be an object"
            )
        if self.output_schema.get("type") != "object":
            raise OperationCatalogError(
                f"operation {self.operation} output schema must be an object"
            )
        if self.timeout_seconds <= 0:
            raise OperationCatalogError(
                f"operation {self.operation} timeout must be positive"
            )
        if not self.evidence_types or any(
            not str(item).strip() for item in self.evidence_types
        ):
            raise OperationCatalogError(
                f"operation {self.operation} requires Evidence types"
            )
        if self.runtime_api_version != RUNTIME_API_VERSION:
            raise OperationCatalogError(
                f"operation {self.operation} targets incompatible Runtime "
                f"{self.runtime_api_version}; expected {RUNTIME_API_VERSION}"
            )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_REGISTRY_SCHEMA,
            "operation": self.operation,
            "capability": self.capability,
            "owner_skill": self.owner_skill,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "timeout_seconds": self.timeout_seconds,
            "evidence_types": list(self.evidence_types),
            "runtime_api_version": self.runtime_api_version,
            "mutation": self.mutation,
        }


class CapabilityRegistry:
    """Canonical operation-to-capability contract registry."""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor]) -> None:
        by_operation: dict[str, CapabilityDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.operation in by_operation:
                raise OperationCatalogError(
                    f"duplicate capability operation: {descriptor.operation}"
                )
            by_operation[descriptor.operation] = descriptor
        if not by_operation:
            raise OperationCatalogError("capability registry must not be empty")
        self._by_operation = by_operation

    def require(self, operation: str) -> CapabilityDescriptor:
        try:
            return self._by_operation[operation]
        except KeyError as exc:
            raise ValueError(f"unregistered Runtime capability: {operation}") from exc

    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._by_operation.values())

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": CAPABILITY_REGISTRY_SCHEMA,
            "capabilities": [
                descriptor.to_public_dict()
                for descriptor in self._by_operation.values()
            ],
        }


@dataclass(frozen=True)
class RuntimeSDKContext:
    task_id: str
    operation_id: str
    timeout_seconds: float
    target_id: str = ""
    minimum_target_epoch: int = 0
    recovery_mode: "EffectRecoveryMode | None" = None


@dataclass(frozen=True)
class DomainReceipt:
    operation: str
    status: str
    value: Mapping[str, object]
    evidence_ids: tuple[str, ...] = ()
    suggested_events: tuple[Mapping[str, object], ...] = ()
    outcome: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.status not in _OUTCOME_STATUSES:
            raise ValueError(f"unsupported domain receipt status: {self.status}")

    @classmethod
    def from_value(
        cls,
        operation: str,
        value: Mapping[str, object],
    ) -> "DomainReceipt":
        explicit = str(value.get("outcome_status") or "").strip().lower()
        compatibility_status = str(value.get("status") or "").strip().lower()
        if not explicit and compatibility_status in _OUTCOME_STATUSES:
            explicit = compatibility_status
        status = explicit or (
            "failed" if value.get("ok") is False else "succeeded"
        )
        if status not in _OUTCOME_STATUSES:
            status = "failed"
        raw_evidence = value.get("evidence_ids", [])
        evidence_ids = (
            tuple(
                str(item)
                for item in raw_evidence
                if isinstance(item, str) and item
            )
            if isinstance(raw_evidence, Sequence)
            and not isinstance(raw_evidence, (str, bytes, bytearray))
            else ()
        )
        raw_events = value.get("suggested_events", [])
        suggested_events = (
            tuple(dict(item) for item in raw_events if isinstance(item, Mapping))
            if isinstance(raw_events, Sequence)
            and not isinstance(raw_events, (str, bytes, bytearray))
            else ()
        )
        outcome = value.get("outcome")
        return cls(
            operation=operation,
            status=status,
            value=dict(value),
            evidence_ids=evidence_ids,
            suggested_events=suggested_events,
            outcome=dict(outcome) if isinstance(outcome, Mapping) else None,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": DOMAIN_RECEIPT_SCHEMA,
            "operation": self.operation,
            "status": self.status,
            "value": dict(self.value),
            "evidence_ids": list(self.evidence_ids),
            "suggested_events": [dict(item) for item in self.suggested_events],
            "outcome": dict(self.outcome) if self.outcome is not None else None,
        }


class DomainAdapter(Protocol):
    def execute(
        self,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
    ) -> DomainReceipt | Mapping[str, object]: ...


@dataclass(frozen=True)
class CallableDomainAdapter:
    callback: Callable[
        [RuntimeSDKContext, Mapping[str, object]],
        DomainReceipt | Mapping[str, object],
    ]

    def execute(
        self,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
    ) -> DomainReceipt | Mapping[str, object]:
        return self.callback(context, arguments)


def _validated_domain_receipt(
    operation: str,
    descriptor: CapabilityDescriptor,
    raw: DomainReceipt | Mapping[str, object],
) -> DomainReceipt:
    receipt = (
        raw
        if isinstance(raw, DomainReceipt)
        else DomainReceipt.from_value(operation, raw)
    )
    if receipt.operation != descriptor.operation:
        raise ValueError("domain receipt operation does not match capability")
    return receipt


class RuntimeSDK:
    """Deep module routing typed domain execution through one registry seam."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def execute(
        self,
        operation: str,
        *,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
        adapter: DomainAdapter,
    ) -> DomainReceipt:
        descriptor = self.registry.require(operation)
        if context.timeout_seconds <= 0:
            raise ValueError("Runtime SDK execution timeout must be positive")
        raw = adapter.execute(context, arguments)
        return _validated_domain_receipt(operation, descriptor, raw)


class EffectClass(str, Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT_MUTATION = "idempotent_mutation"
    RECONCILABLE_MUTATION = "reconcilable_mutation"
    IRREVERSIBLE_MUTATION = "irreversible_mutation"


class EffectRecoveryMode(str, Enum):
    RECONCILE = "reconcile"


@dataclass(frozen=True)
class MutationRecoveryRoute:
    journals: tuple[object, ...]
    journal: object | None
    disposition: MutationRecoveryDisposition


def mutation_recovery_route(
    mode: EffectRecoveryMode | None,
    load_journals: Callable[[], Iterable[object]],
    *,
    operation_id: str,
    action: str,
    label: str,
    matches: Callable[[object], bool],
) -> MutationRecoveryRoute:
    """Select one durable Mutation journal before any Domain re-execution."""

    journals = tuple(load_journals())
    journal = next(
        (
            candidate
            for candidate in journals
            if str(getattr(candidate, "operation_id", "")) == operation_id
            and str(getattr(candidate, "action", "")) == action
            and matches(candidate)
        ),
        None,
    )
    if journal is not None:
        disposition = getattr(journal, "recovery_disposition", None)
        if not isinstance(disposition, MutationRecoveryDisposition):
            raise ValueError(
                "MutationJournal did not provide a valid recovery disposition"
            )
        return MutationRecoveryRoute(journals, journal, disposition)
    require_effect_recovery_journal(
        mode,
        journals,
        operation_id=operation_id,
        action=action,
        label=label,
    )
    return MutationRecoveryRoute(
        journals,
        None,
        MutationRecoveryDisposition.NEW,
    )


@dataclass(frozen=True)
class ArtifactContract:
    path_fields: tuple[str, ...]
    digest_field: str = "artifact_sha256"
    version_field: str = ""
    artifact_kind: str = "runtime-artifact"
    required: bool = False
    reference_required: bool = False
    require_redacted: bool = False

    def __post_init__(self) -> None:
        if not self.path_fields or any(not field.strip() for field in self.path_fields):
            raise ValueError("Artifact contract requires path fields")
        if not self.digest_field.strip():
            raise ValueError("Artifact contract requires a digest field")
        if not self.artifact_kind.strip():
            raise ValueError("Artifact contract requires an ArtifactRef kind")

    def bind(
        self,
        arguments: Mapping[str, object],
    ) -> ArtifactRef | None:
        raw_reference = arguments.get("artifact_ref")
        if isinstance(raw_reference, Mapping) and raw_reference:
            reference = ArtifactRef.from_public_dict(raw_reference)
            if reference.kind != self.artifact_kind:
                raise ValueError("Domain Action ArtifactRef has the wrong kind")
            return reference
        if self.reference_required:
            raise ValueError("Domain Action requires an ArtifactRef")
        path = next(
            (
                str(arguments.get(field, "")).strip()
                for field in self.path_fields
                if str(arguments.get(field, "")).strip()
            ),
            "",
        )
        digest = str(arguments.get(self.digest_field, "")).strip().lower()
        version = (
            str(arguments.get(self.version_field, "")).strip()
            if self.version_field
            else ""
        )
        if not path and not digest and not version and not self.required:
            return None
        if not path:
            raise ValueError("Domain Action is missing its Artifact path")
        if not digest and not self.required:
            return None
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("Domain Action Artifact digest must be SHA-256")
        if self.version_field and not version:
            raise ValueError("Domain Action is missing its Artifact version")
        return None

    def runtime_arguments(
        self,
        reference: ArtifactRef,
        path: Path,
    ) -> dict[str, object]:
        """Materialize a verified ArtifactRef for the registered Domain Adapter."""

        if reference.kind != self.artifact_kind:
            raise ValueError("Domain Action ArtifactRef has the wrong kind")
        arguments: dict[str, object] = {
            self.path_fields[0]: str(path),
            self.digest_field: reference.digest,
        }
        if self.version_field:
            if not reference.version:
                raise ValueError("Domain Action is missing its Artifact version")
            arguments[self.version_field] = reference.version
        return arguments


@dataclass(frozen=True)
class ResultArtifactContract:
    artifact_kind: str
    require_redacted: bool = False

    def __post_init__(self) -> None:
        if not self.artifact_kind.strip():
            raise ValueError("Domain Result ArtifactRef kind is required")

    def bind(self, action: "DomainAction", receipt: "DomainReceipt") -> ArtifactRef:
        raw_reference = receipt.value.get("artifact_ref")
        if not isinstance(raw_reference, Mapping):
            result = receipt.value.get("result")
            raw_reference = (
                result.get("artifact_ref")
                if isinstance(result, Mapping)
                else None
            )
        if not isinstance(raw_reference, Mapping):
            raise ValueError("Domain Result omits its ArtifactRef")
        reference = ArtifactRef.from_public_dict(raw_reference)
        if reference.kind != self.artifact_kind:
            raise ValueError("Domain Result ArtifactRef has the wrong kind")
        if reference.run_id != action.context.task_id:
            raise ValueError("Domain Result ArtifactRef belongs to another Run")
        expected_target = str(action.arguments.get("ip", "")).strip()
        if expected_target and reference.target != expected_target:
            raise ValueError("Domain Result ArtifactRef targets another BMC")
        return reference


@dataclass(frozen=True)
class DomainAction:
    operation: str
    pack: str
    pack_version: str
    context: RuntimeSDKContext
    arguments: Mapping[str, object]
    artifact: ArtifactRef | None = None

    @property
    def effect_id(self) -> str:
        return "effect-" + _fingerprint(
            {
                "operation": self.operation,
                "pack": self.pack,
                "pack_version": self.pack_version,
                "operation_id": self.context.operation_id,
                "target_id": self.context.target_id,
                "arguments": dict(self.arguments),
            }
        )[:32]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": DOMAIN_ACTION_SCHEMA,
            "effect_id": self.effect_id,
            "operation": self.operation,
            "pack": self.pack,
            "pack_version": self.pack_version,
            "arguments": dict(self.arguments),
            "artifact_ref": (
                self.artifact.to_public_dict()
                if self.artifact is not None
                else None
            ),
        }


DomainVerifier = Callable[[DomainAction, DomainReceipt], bool]


def _mutation_journal_receipt_valid(
    journal: Mapping[str, object],
    *,
    expected_operation_id: str,
    expected_action: str,
    expected_task_id: str = "",
    expected_artifact_digest: str = "",
    compatibility_identity: Mapping[str, object] | None = None,
) -> bool:
    schema = str(journal.get("schema", ""))
    operation_id = str(journal.get("operation_id", ""))
    compatible_operation_id = str(
        (compatibility_identity or {}).get("operation_id", "")
    )
    if operation_id and operation_id != expected_operation_id:
        return False
    if not operation_id and (
        schema or compatible_operation_id != expected_operation_id
    ):
        return False
    action = str(journal.get("action", ""))
    compatible_action = str((compatibility_identity or {}).get("action", ""))
    if action and action != expected_action:
        return False
    if not action and (schema or compatible_action != expected_action):
        return False
    if str(journal.get("stage", "")) not in MutationJournal.VALID_STAGES:
        return False
    if schema:
        if schema != f"{RUNTIME_API_VERSION}/mutation-journal":
            return False
        if expected_task_id and str(journal.get("task_id", "")) != expected_task_id:
            return False
        if _SHA256.fullmatch(str(journal.get("operation_fingerprint", ""))) is None:
            return False
        if _SHA256.fullmatch(str(journal.get("target_fingerprint", ""))) is None:
            return False
    expected_checksum = str(journal.get("expected_checksum", ""))
    if expected_artifact_digest:
        if schema and expected_checksum != expected_artifact_digest:
            return False
        if expected_checksum and expected_checksum != expected_artifact_digest:
            return False
    return True


def mutation_receipt_verifier(
    action: DomainAction,
    receipt: DomainReceipt,
    *,
    journal_action: str,
) -> bool:
    """Authenticate a Mutation receipt without equating validity with success."""

    journal = receipt.value.get("journal")
    expected_artifact_digest = (
        action.artifact.digest
        if action.artifact is not None
        else str(action.arguments.get("artifact_sha256", ""))
        .strip()
        .lower()
        .removeprefix("sha256:")
    )
    if not isinstance(journal, Mapping):
        executions = receipt.value.get("executions")
        if (
            str(receipt.value.get("schema", ""))
            == f"{RUNTIME_API_VERSION}/task-orchestration"
            and isinstance(executions, Sequence)
            and not isinstance(executions, (str, bytes, bytearray))
        ):
            expected_domain = (
                "live_patch"
                if action.operation == "live_patch_run"
                else "upgrade"
            )
            return any(
                isinstance(item, Mapping)
                and str(item.get("domain", "")) == expected_domain
                and str(item.get("phase", "")) == "mutation"
                and str(item.get("status", ""))
                in {
                    "succeeded",
                    "modified",
                    "verified",
                    "failed",
                    "blocked",
                    "not_executed",
                }
                and isinstance(item.get("value"), Mapping)
                and isinstance(item["value"].get("journal"), Mapping)
                and _mutation_journal_receipt_valid(
                    item["value"]["journal"],
                    expected_operation_id=str(item.get("operation_id", "")),
                    expected_action=journal_action,
                    expected_artifact_digest=expected_artifact_digest,
                    compatibility_identity=(
                        item["value"].get("_runtime_compatibility_receipt")
                        if isinstance(
                            item["value"].get("_runtime_compatibility_receipt"),
                            Mapping,
                        )
                        else None
                    ),
                )
                for item in executions
            )
        return (
            receipt.status == "running"
            and str(receipt.value.get("status", "")).strip().lower() == "running"
            and str(receipt.value.get("operation_id", ""))
            == action.context.operation_id
        )
    public_operation_id = str(receipt.value.get("operation_id", ""))
    if public_operation_id and public_operation_id != action.context.operation_id:
        return False
    return _mutation_journal_receipt_valid(
        journal,
        expected_operation_id=action.context.operation_id,
        expected_action=journal_action,
        expected_task_id=action.context.task_id,
        expected_artifact_digest=expected_artifact_digest,
        compatibility_identity=(
            receipt.value.get("_runtime_compatibility_receipt")
            if isinstance(
                receipt.value.get("_runtime_compatibility_receipt"),
                Mapping,
            )
            else None
        ),
    )


@dataclass(frozen=True)
class DomainPack:
    name: str
    version: str
    descriptor: CapabilityDescriptor
    effect_class: EffectClass
    adapter: DomainAdapter
    verifier: DomainVerifier
    reconciler: DomainAdapter | None = None
    artifact_contract: ArtifactContract | None = None
    result_artifact_contract: ResultArtifactContract | None = None
    artifact_phase: str = ""
    capability_requirements: tuple[str, ...] = ()
    journal_action: Callable[[Mapping[str, object]], str] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Domain Pack name is required")
        if _PACK_VERSION.fullmatch(self.version) is None:
            raise ValueError("Domain Pack version must be numeric")
        requirements = self.capability_requirements or (
            self.descriptor.capability,
        )
        if any(not item.strip() for item in requirements):
            raise ValueError("Domain Pack capability requirements are invalid")
        object.__setattr__(self, "capability_requirements", tuple(requirements))
        if (
            self.effect_class is EffectClass.RECONCILABLE_MUTATION
            and self.reconciler is None
        ):
            raise ValueError("reconcilable Domain Pack requires a reconciler")

    def action(
        self,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
    ) -> DomainAction:
        action_arguments = {
            name: value
            for name, value in arguments.items()
            if name != RUNTIME_EFFECT_RECOVERY_ARGUMENT
        }
        artifact = (
            self.artifact_contract.bind(action_arguments)
            if self.artifact_contract is not None
            else None
        )
        return DomainAction(
            operation=self.descriptor.operation,
            pack=self.name,
            pack_version=self.version,
            context=context,
            arguments=action_arguments,
            artifact=artifact,
        )

    def bind_compatibility_receipt(
        self,
        value: Mapping[str, object],
        *,
        operation_id: str,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        """Bind schema-less legacy journals to this Pack's mutation identity."""

        translated = dict(value)
        if self.journal_action is None:
            return translated
        action = self.journal_action(arguments).strip()
        if not action:
            raise ValueError("Domain Pack journal action must not be empty")
        journal = translated.get("journal")
        if isinstance(journal, Mapping) and not str(journal.get("schema", "")):
            translated["_runtime_compatibility_receipt"] = {
                "operation_id": operation_id,
                "action": action,
            }
            translated.setdefault("operation_id", operation_id)
        executions = translated.get("executions")
        if isinstance(executions, list):
            bound_executions: list[object] = []
            for raw_execution in executions:
                if not isinstance(raw_execution, Mapping):
                    bound_executions.append(raw_execution)
                    continue
                execution = dict(raw_execution)
                execution_value = execution.get("value")
                if isinstance(execution_value, Mapping):
                    nested = dict(execution_value)
                    nested_journal = nested.get("journal")
                    if isinstance(nested_journal, Mapping) and not str(
                        nested_journal.get("schema", "")
                    ):
                        nested["_runtime_compatibility_receipt"] = {
                            "operation_id": str(execution.get("operation_id", "")),
                            "action": action,
                        }
                        execution["value"] = nested
                bound_executions.append(execution)
            translated["executions"] = bound_executions
        return translated

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": DOMAIN_PACK_SCHEMA,
            "name": self.name,
            "version": self.version,
            "operation": self.descriptor.operation,
            "effect_class": self.effect_class.value,
            "capability_requirements": list(self.capability_requirements),
            "artifact_contract": (
                {
                    "path_fields": list(self.artifact_contract.path_fields),
                    "digest_field": self.artifact_contract.digest_field,
                    "version_field": self.artifact_contract.version_field,
                    "artifact_kind": self.artifact_contract.artifact_kind,
                    "required": self.artifact_contract.required,
                    "reference_required": self.artifact_contract.reference_required,
                    "require_redacted": self.artifact_contract.require_redacted,
                }
                if self.artifact_contract is not None
                else None
            ),
            "result_artifact_contract": (
                {
                    "artifact_kind": self.result_artifact_contract.artifact_kind,
                    "require_redacted": self.result_artifact_contract.require_redacted,
                }
                if self.result_artifact_contract is not None
                else None
            ),
            "artifact_phase": self.artifact_phase,
        }


@dataclass(frozen=True)
class DomainPackAuthorContract:
    """One typed authoring interface for an internal Runtime Domain Pack."""

    name: str
    version: str
    operation: str
    effect_class: EffectClass
    adapter: DomainAdapter
    verifier: DomainVerifier
    reconciler: DomainAdapter | None = None
    artifact_contract: ArtifactContract | None = None
    result_artifact_contract: ResultArtifactContract | None = None
    artifact_phase: str = ""
    capability_requirements: tuple[str, ...] = ()
    journal_action: Callable[[Mapping[str, object]], str] | None = None

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise ValueError("Domain Pack author contract requires an operation")
        if self.effect_class not in {
            EffectClass.READ_ONLY,
            EffectClass.RECONCILABLE_MUTATION,
        }:
            raise ValueError(
                "Domain Pack authors may declare only read-only or reconcilable mutation Effects"
            )
        if self.effect_class is EffectClass.READ_ONLY and (
            self.reconciler is not None or self.journal_action is not None
        ):
            raise ValueError("read-only Domain Pack cannot declare mutation recovery")
        if self.effect_class is EffectClass.RECONCILABLE_MUTATION and (
            self.reconciler is None or self.journal_action is None
        ):
            raise ValueError(
                "reconcilable mutation Domain Pack requires reconciler and journal action"
            )
        if self.artifact_phase and self.artifact_contract is None:
            raise ValueError("Domain Pack artifact phase requires an Artifact contract")
        if (
            self.artifact_contract is not None
            and self.artifact_contract.require_redacted
            and not self.artifact_contract.reference_required
        ):
            raise ValueError(
                "redacted Domain Pack Artifact input requires an ArtifactRef reference"
            )

    def build(self, registry: CapabilityRegistry) -> DomainPack:
        descriptor = registry.require(self.operation)
        mutation = self.effect_class is EffectClass.RECONCILABLE_MUTATION
        if descriptor.mutation != mutation:
            raise ValueError(
                "Domain Pack Effect class does not match its capability descriptor"
            )
        return DomainPack(
            name=self.name,
            version=self.version,
            descriptor=descriptor,
            effect_class=self.effect_class,
            adapter=self.adapter,
            verifier=self.verifier,
            reconciler=self.reconciler,
            artifact_contract=self.artifact_contract,
            result_artifact_contract=self.result_artifact_contract,
            artifact_phase=self.artifact_phase,
            capability_requirements=self.capability_requirements,
            journal_action=self.journal_action,
        )


class DomainPackConformanceSuite:
    """Bind authored Packs and verify cross-Pack and behavioral invariants."""

    def __init__(self, *, read_attempts: int = 2) -> None:
        if read_attempts <= 0:
            raise ValueError("Domain Pack read attempts must be positive")
        self.read_attempts = read_attempts

    @staticmethod
    def _require_unique(packs: Sequence[DomainPack]) -> None:
        operations = [pack.descriptor.operation for pack in packs]
        if len(set(operations)) != len(operations):
            raise ValueError("duplicate Domain Pack operation")
        identities = [(pack.name, pack.version) for pack in packs]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate Domain Pack identity")
        phases = [pack.artifact_phase for pack in packs if pack.artifact_phase]
        if len(set(phases)) != len(phases):
            raise ValueError("multiple Domain Packs bind the same artifact phase")

    @staticmethod
    def _require_registry_alignment(
        registry: CapabilityRegistry,
        packs: Sequence[DomainPack],
    ) -> None:
        available = {
            descriptor.capability for descriptor in registry.descriptors()
        }
        for pack in packs:
            if registry.require(pack.descriptor.operation) != pack.descriptor:
                raise ValueError(
                    f"Domain Pack {pack.name} does not match the capability registry"
                )
            mutation = pack.effect_class is EffectClass.RECONCILABLE_MUTATION
            if pack.descriptor.mutation != mutation:
                raise ValueError(
                    "Domain Pack Effect class does not match its capability descriptor"
                )
            if any(
                requirement not in available
                for requirement in pack.capability_requirements
            ):
                raise ValueError(
                    f"Domain Pack {pack.name} requires an unregistered capability"
                )
            if pack.artifact_phase and pack.artifact_contract is None:
                raise ValueError("Domain Pack artifact phase requires an Artifact contract")

    def validate(
        self,
        registry: CapabilityRegistry,
        packs: Iterable[DomainPack],
    ) -> dict[str, object]:
        selected = tuple(packs)
        self._require_unique(selected)
        self._require_registry_alignment(registry, selected)
        return {
            "schema": DOMAIN_PACK_CONFORMANCE_SCHEMA,
            "valid": True,
            "pack_count": len(selected),
            "operations": sorted(
                pack.descriptor.operation for pack in selected
            ),
            "read_attempts": self.read_attempts,
        }

    def bind(
        self,
        registry: CapabilityRegistry,
        contracts: Iterable[DomainPackAuthorContract | DomainPack],
    ) -> tuple[DomainPack, ...]:
        packs = tuple(
            contract
            if isinstance(contract, DomainPack)
            else contract.build(registry)
            for contract in contracts
        )
        self.validate(registry, packs)
        return packs

    def verify_example(
        self,
        pack: DomainPack,
        *,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
        receipt: DomainReceipt,
    ) -> dict[str, str]:
        action = pack.action(context, arguments)
        replayed = pack.action(context, arguments)
        if action.effect_id != replayed.effect_id:
            raise ValueError("Domain Pack Effect identity is unstable")
        if receipt.operation != pack.descriptor.operation:
            raise ValueError("domain receipt operation does not match capability")
        verified = pack.verifier(action, receipt)
        if not isinstance(verified, bool):
            raise ValueError("Domain Pack verifier must return a boolean")
        if not verified:
            raise ValueError("Domain Pack verifier rejected its conformance example")
        retry = (
            f"bounded-read-retry:{self.read_attempts}"
            if pack.effect_class is EffectClass.READ_ONLY
            else "single-attempt"
        )
        recovery = "none"
        if pack.effect_class is EffectClass.RECONCILABLE_MUTATION:
            if pack.journal_action is None or not pack.journal_action(arguments).strip():
                raise ValueError(
                    "reconcilable mutation Domain Pack produced an invalid journal action"
                )
            recovery_action = pack.action(
                replace(context, recovery_mode=EffectRecoveryMode.RECONCILE),
                arguments,
            )
            if recovery_action.effect_id != action.effect_id:
                raise ValueError("Domain Pack recovery changed Effect identity")
            recovery = "reconcile-same-effect"
        return {
            "effect_identity": "stable",
            "receipt_binding": "verified",
            "retry_classification": retry,
            "recovery_classification": recovery,
        }


@dataclass(frozen=True)
class DomainResult:
    action: DomainAction
    receipt: DomainReceipt
    verified: bool
    recovery: bool = False

    @property
    def operation(self) -> str:
        return self.receipt.operation

    @property
    def status(self) -> str:
        return self.receipt.status

    @property
    def value(self) -> Mapping[str, object]:
        return self.receipt.value

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return self.receipt.evidence_ids

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": DOMAIN_RESULT_SCHEMA,
            "action": self.action.to_public_dict(),
            "receipt": self.receipt.to_public_dict(),
            "verified": self.verified,
            "recovery": self.recovery,
        }


def effect_recovery_mode(
    arguments: Mapping[str, object],
) -> EffectRecoveryMode | None:
    raw = arguments.get(RUNTIME_EFFECT_RECOVERY_ARGUMENT)
    if raw is None or raw == "":
        return None
    if isinstance(raw, EffectRecoveryMode):
        return raw
    try:
        return EffectRecoveryMode(str(raw).strip().lower())
    except ValueError as exc:
        raise ValueError("unsupported Runtime Effect recovery mode") from exc


def require_effect_recovery_journal(
    mode: EffectRecoveryMode | None,
    journals: Iterable[object],
    *,
    operation_id: str,
    action: str,
    label: str,
) -> None:
    if mode is not EffectRecoveryMode.RECONCILE:
        return
    if any(
        str(getattr(journal, "operation_id", "")) == operation_id
        and str(getattr(journal, "action", "")) == action
        for journal in journals
    ):
        return
    raise OSError(
        f"{label} recovery found no durable mutation journal; "
        "refusing to create a replacement Effect"
    )


@dataclass(frozen=True)
class DomainExecutionPolicy:
    effect_class: EffectClass
    max_attempts: int

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("Domain execution attempts must be positive")


class DomainExecutor:
    """Execute registered Domain Adapters with Runtime-owned Effect policy."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        adapters: Mapping[str, DomainAdapter],
        *,
        packs: Iterable[DomainPack] = (),
        effect_classes: Mapping[str, EffectClass] | None = None,
        read_attempts: int = 2,
        conformance_report: Mapping[str, object] | None = None,
    ) -> None:
        if read_attempts <= 0:
            raise ValueError("read_attempts must be positive")
        self.registry = registry
        registered_packs = tuple(packs)
        self.packs = {
            pack.descriptor.operation: pack for pack in registered_packs
        }
        if len(self.packs) != len(registered_packs):
            raise ValueError("duplicate Domain Pack operation")
        overlap = set(adapters) & set(self.packs)
        if overlap:
            raise ValueError(
                "Domain adapters and Packs overlap: " + ", ".join(sorted(overlap))
            )
        for operation, pack in self.packs.items():
            if registry.require(operation) != pack.descriptor:
                raise ValueError(
                    f"Domain Pack {pack.name} does not match the capability registry"
                )
        self.adapters = {
            **dict(adapters),
            **{operation: pack.adapter for operation, pack in self.packs.items()},
        }
        self.effect_classes = dict(effect_classes or {})
        self.effect_classes.update(
            {
                operation: pack.effect_class
                for operation, pack in self.packs.items()
            }
        )
        self.read_attempts = read_attempts
        self._conformance_report = dict(
            conformance_report
            or DomainPackConformanceSuite(read_attempts=read_attempts).validate(
                registry,
                registered_packs,
            )
        )
        missing = [
            descriptor.operation
            for descriptor in registry.descriptors()
            if descriptor.operation not in self.adapters
        ]
        if missing:
            raise ValueError(
                "DomainExecutor is missing adapters: " + ", ".join(sorted(missing))
            )

    def policy_for(self, operation: str) -> DomainExecutionPolicy:
        descriptor = self.registry.require(operation)
        effect_class = self.effect_classes.get(operation)
        if effect_class is None:
            effect_class = (
                EffectClass.RECONCILABLE_MUTATION
                if descriptor.mutation
                else EffectClass.READ_ONLY
            )
        return DomainExecutionPolicy(
            effect_class=effect_class,
            max_attempts=(
                self.read_attempts
                if effect_class is EffectClass.READ_ONLY
                else 1
            ),
        )

    def pack_for(self, operation: str) -> DomainPack | None:
        """Return the registered Pack that owns operation-specific contracts."""

        return self.packs.get(operation)

    def metadata_for(self, operation: str) -> Mapping[str, object]:
        """Return Pack-owned metadata needed by deterministic Run progression."""

        pack = self.packs.get(operation)
        if pack is None:
            return {}
        return {
            "mutation": pack.effect_class is EffectClass.RECONCILABLE_MUTATION,
            "artifact_phase": pack.artifact_phase,
            "artifact_kind": (
                pack.artifact_contract.artifact_kind
                if pack.artifact_contract is not None
                else ""
            ),
            "artifact_requires_version": bool(
                pack.artifact_contract is not None
                and pack.artifact_contract.version_field
            ),
        }

    def artifact_metadata_for_phase(
        self,
        phase_type: str,
    ) -> Mapping[str, object]:
        matches = [
            self.metadata_for(operation)
            for operation, pack in self.packs.items()
            if pack.artifact_phase == phase_type and pack.artifact_contract is not None
        ]
        if len(matches) > 1:
            raise ValueError(
                f"multiple Domain Packs bind artifacts to phase {phase_type}"
            )
        return matches[0] if matches else {}

    def execute(
        self,
        operation: str,
        *,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
    ) -> DomainResult:
        descriptor = self.registry.require(operation)
        pack = self.packs.get(operation)
        adapter = self.adapters[operation]
        action = (
            pack.action(context, arguments)
            if pack is not None
            else DomainAction(
                operation=operation,
                pack="runtime-core",
                pack_version="1",
                context=context,
                arguments=dict(arguments),
            )
        )
        policy = self.policy_for(operation)
        last_error: BaseException | None = None
        for attempt in range(1, policy.max_attempts + 1):
            try:
                raw = adapter.execute(context, arguments)
                receipt = _validated_domain_receipt(operation, descriptor, raw)
                verified = pack.verifier(action, receipt) if pack is not None else True
                if not verified:
                    raise ValueError("Domain Pack verifier rejected its Result")
                return DomainResult(action=action, receipt=receipt, verified=verified)
            except (ConnectionError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt >= policy.max_attempts:
                    raise
        assert last_error is not None
        raise last_error

    def reconcile(
        self,
        operation: str,
        *,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
    ) -> DomainResult:
        pack = self.packs.get(operation)
        if pack is None or pack.reconciler is None:
            raise ValueError(f"operation {operation} has no Domain Pack reconciler")
        if pack.effect_class is not EffectClass.RECONCILABLE_MUTATION:
            raise ValueError(f"operation {operation} is not reconcilable")
        recovery_context = replace(
            context,
            recovery_mode=EffectRecoveryMode.RECONCILE,
        )
        action = pack.action(recovery_context, arguments)
        raw = pack.reconciler.execute(recovery_context, arguments)
        receipt = _validated_domain_receipt(operation, pack.descriptor, raw)
        verified = pack.verifier(action, receipt)
        if not verified:
            raise ValueError("Domain Pack verifier rejected its reconciled Result")
        return DomainResult(
            action=action,
            receipt=receipt,
            verified=True,
            recovery=True,
        )

    def pack_descriptors(self) -> tuple[dict[str, object], ...]:
        return tuple(
            pack.to_public_dict()
            for pack in sorted(self.packs.values(), key=lambda item: item.name)
        )

    def conformance_report(self) -> dict[str, object]:
        return dict(self._conformance_report)
