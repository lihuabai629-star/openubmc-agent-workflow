"""Canonical Runtime operation metadata shared by MCP, SDK, and Workflow."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .capability import CapabilityDescriptor
from .catalog import OperationDescriptor
from .contracts import RUNTIME_API_VERSION


OPERATION_CONTRACT_SCHEMA = f"{RUNTIME_API_VERSION}/operation-contract-v1"


@dataclass(frozen=True)
class RuntimeOperationContract:
    name: str
    domain: str = ""
    lifecycle: str = "invoke"
    handler_name: str | None = None
    mutation: bool = False
    workflow_entry: bool = False
    credential_values: bool = False
    capability: str = ""
    owner_skill: str = ""
    timeout_seconds: float = 0.0
    evidence_types: tuple[str, ...] = ()
    orchestration_phase: str = ""
    closeout_stage: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Runtime operation name is required")
        if self.domain:
            required = {
                "handler_name": self.handler_name,
                "capability": self.capability,
                "owner_skill": self.owner_skill,
                "orchestration_phase": self.orchestration_phase,
                "closeout_stage": self.closeout_stage,
            }
            missing = [name for name, value in required.items() if not value]
            if missing or self.timeout_seconds <= 0 or not self.evidence_types:
                raise ValueError(
                    f"domain operation {self.name} has incomplete metadata: "
                    + ", ".join(missing)
                )
        elif any(
            (
                self.handler_name,
                self.capability,
                self.owner_skill,
                self.timeout_seconds,
                self.evidence_types,
                self.orchestration_phase,
                self.closeout_stage,
                self.workflow_entry,
                self.credential_values,
                self.mutation,
            )
        ):
            raise ValueError(
                f"control operation {self.name} must not declare domain capability metadata"
            )

    def operation_descriptor(
        self,
        definition: Mapping[str, object],
    ) -> OperationDescriptor:
        return OperationDescriptor.from_tool_definition(
            definition,
            lifecycle=self.lifecycle,
            handler_name=self.handler_name,
            mutation=self.mutation,
        )

    def capability_descriptor(
        self,
        input_schema: Mapping[str, object],
    ) -> CapabilityDescriptor:
        if not self.domain:
            raise ValueError(f"control operation {self.name} has no Runtime capability")
        return CapabilityDescriptor(
            operation=self.name,
            capability=self.capability,
            owner_skill=self.owner_skill,
            input_schema=input_schema,
            output_schema={"type": "object", "additionalProperties": True},
            timeout_seconds=self.timeout_seconds,
            evidence_types=self.evidence_types,
            mutation=self.mutation,
        )


class RuntimeOperationContractRegistry:
    """One metadata interface for transport, workflow, and SDK consumers."""

    def __init__(self, contracts: Iterable[RuntimeOperationContract]) -> None:
        ordered: list[RuntimeOperationContract] = []
        by_name: dict[str, RuntimeOperationContract] = {}
        for contract in contracts:
            if contract.name in by_name:
                raise ValueError(f"duplicate Runtime operation contract: {contract.name}")
            ordered.append(contract)
            by_name[contract.name] = contract
        if not ordered:
            raise ValueError("Runtime operation contract registry must not be empty")
        self._ordered = tuple(ordered)
        self._by_name = by_name

    def contracts(self) -> tuple[RuntimeOperationContract, ...]:
        return self._ordered

    def require(self, name: str) -> RuntimeOperationContract:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValueError(f"unknown Runtime operation contract: {name}") from exc

    def domain_contracts(self) -> tuple[RuntimeOperationContract, ...]:
        return tuple(contract for contract in self._ordered if contract.domain)

    def operation_owners(self) -> dict[str, str]:
        return {
            contract.name: contract.owner_skill
            for contract in self.domain_contracts()
        }

    def operation_domains(self) -> dict[str, str]:
        return {
            contract.name: contract.domain
            for contract in self.domain_contracts()
        }

    def domain_to_entry_operation(self) -> dict[str, str]:
        return {
            contract.domain: contract.name
            for contract in self.domain_contracts()
            if contract.workflow_entry
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": OPERATION_CONTRACT_SCHEMA,
            "operations": [
                {
                    "name": contract.name,
                    "domain": contract.domain,
                    "lifecycle": contract.lifecycle,
                    "handler_name": contract.handler_name,
                    "mutation": contract.mutation,
                    "workflow_entry": contract.workflow_entry,
                    "credential_values": contract.credential_values,
                    "capability": contract.capability,
                    "owner_skill": contract.owner_skill,
                    "timeout_seconds": contract.timeout_seconds,
                    "evidence_types": list(contract.evidence_types),
                    "orchestration_phase": contract.orchestration_phase,
                    "closeout_stage": contract.closeout_stage,
                }
                for contract in self._ordered
            ],
        }


DEFAULT_OPERATION_CONTRACTS = RuntimeOperationContractRegistry(
    (
        RuntimeOperationContract(
            "debug_run",
            domain="debug",
            handler_name="debug_run",
            workflow_entry=True,
            credential_values=True,
            capability="openubmc.debug.diagnose",
            owner_skill="openubmc-debug",
            timeout_seconds=300.0,
            evidence_types=("diagnosis", "runtime-observation"),
            orchestration_phase="diagnosis",
            closeout_stage="diagnosis",
        ),
        RuntimeOperationContract(
            "debug_collect",
            domain="debug",
            handler_name="debug_collect",
            credential_values=True,
            capability="openubmc.debug.verify",
            owner_skill="openubmc-debug",
            timeout_seconds=180.0,
            evidence_types=("runtime-verification", "acceptance-outcome"),
            orchestration_phase="fresh_verification",
            closeout_stage="verification",
        ),
        RuntimeOperationContract(
            "log_bundle_collect",
            domain="log_analyzer",
            handler_name="log_bundle_collect",
            workflow_entry=True,
            credential_values=True,
            capability="openubmc.logs.bundle",
            owner_skill="openubmc-log-analyzer",
            timeout_seconds=600.0,
            evidence_types=("diagnostic-bundle", "collection-outcome"),
            orchestration_phase="bundle",
            closeout_stage="bundle",
        ),
        RuntimeOperationContract(
            "live_patch_run",
            domain="live_patch",
            handler_name="live_patch_run",
            mutation=True,
            workflow_entry=True,
            credential_values=True,
            capability="openubmc.delivery.live-patch",
            owner_skill="openubmc-live-patch",
            timeout_seconds=600.0,
            evidence_types=("mutation-journal", "deployment-verification"),
            orchestration_phase="mutation",
            closeout_stage="live_patch",
        ),
        RuntimeOperationContract(
            "upgrade_run",
            domain="upgrade",
            handler_name="upgrade_run",
            mutation=True,
            workflow_entry=True,
            credential_values=True,
            capability="openubmc.delivery.upgrade",
            owner_skill="openubmc-upgrade",
            timeout_seconds=1800.0,
            evidence_types=("mutation-journal", "deployment-identity"),
            orchestration_phase="mutation",
            closeout_stage="upgrade",
        ),
        RuntimeOperationContract("case_read", lifecycle="read"),
        RuntimeOperationContract("evidence_read", lifecycle="read"),
        RuntimeOperationContract("case_replay_export", lifecycle="read"),
        RuntimeOperationContract("case_replay_run", lifecycle="read"),
        RuntimeOperationContract("session_outcome_record"),
        RuntimeOperationContract("session_outcome_summary", lifecycle="read"),
        RuntimeOperationContract("session_outcome_transition"),
        RuntimeOperationContract("session_outcome_promote"),
        RuntimeOperationContract("case_close", lifecycle="close"),
        RuntimeOperationContract("case_forget", lifecycle="close"),
        RuntimeOperationContract("phase_record"),
        RuntimeOperationContract("workflow.advance"),
        RuntimeOperationContract("workflow.next"),
        RuntimeOperationContract("runtime_status", lifecycle="status"),
    )
)
