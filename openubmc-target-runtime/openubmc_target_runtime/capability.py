"""Declarative Runtime SDK capability routing and typed domain receipts."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
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
        receipt = (
            raw
            if isinstance(raw, DomainReceipt)
            else DomainReceipt.from_value(operation, raw)
        )
        if receipt.operation != descriptor.operation:
            raise ValueError("domain receipt operation does not match capability")
        return receipt
