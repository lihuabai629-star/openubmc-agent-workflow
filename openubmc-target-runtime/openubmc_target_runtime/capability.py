"""Declarative Runtime SDK capability routing and typed domain receipts."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .catalog import OperationCatalogError, validate_json_schema
from .contracts import RUNTIME_API_VERSION


CAPABILITY_REGISTRY_SCHEMA = f"{RUNTIME_API_VERSION}/capability-registry-v1"
DOMAIN_RECEIPT_SCHEMA = f"{RUNTIME_API_VERSION}/domain-receipt-v1"
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
        explicit = str(value.get("outcome_status", "")).strip().lower()
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
        effect_classes: Mapping[str, EffectClass] | None = None,
        read_attempts: int = 2,
    ) -> None:
        if read_attempts <= 0:
            raise ValueError("read_attempts must be positive")
        self.registry = registry
        self.adapters = dict(adapters)
        self.effect_classes = dict(effect_classes or {})
        self.read_attempts = read_attempts
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

    def execute(
        self,
        operation: str,
        *,
        context: RuntimeSDKContext,
        arguments: Mapping[str, object],
    ) -> DomainReceipt:
        descriptor = self.registry.require(operation)
        adapter = self.adapters[operation]
        policy = self.policy_for(operation)
        last_error: BaseException | None = None
        for attempt in range(1, policy.max_attempts + 1):
            try:
                raw = adapter.execute(context, arguments)
                return _validated_domain_receipt(operation, descriptor, raw)
            except (ConnectionError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt >= policy.max_attempts:
                    raise
        assert last_error is not None
        raise last_error
