"""Built-in Domain Pack registrations for proven Runtime mutation domains."""

from __future__ import annotations

from collections.abc import Mapping

from .capability import (
    ArtifactContract,
    CapabilityRegistry,
    DomainAdapter,
    DomainPack,
    DomainPackAuthorContract,
    DomainPackConformanceSuite,
    EffectClass,
    ResultArtifactContract,
    mutation_receipt_verifier,
)
from .operation_contracts import LOG_BUNDLE_STAGE_CONTRACTS


def _live_patch_journal_action(arguments: Mapping[str, object]) -> str:
    action = str(arguments.get("action", "")).strip().lower().replace("-", "_")
    return "rollback" if action == "rollback" else "live_patch"


def _upgrade_journal_action(_arguments: Mapping[str, object]) -> str:
    return "upgrade"


def builtin_domain_pack_contracts(
    adapters: Mapping[str, DomainAdapter],
) -> tuple[DomainPackAuthorContract, ...]:
    """Author Runtime-owned mutation and local Artifact stage contracts."""

    definitions = {
        "live_patch_run": {
            "name": "live-patch",
            "artifact_phase": "developer.change",
            "artifact_contract": ArtifactContract(
                path_fields=("local_path", "backup_path"),
                digest_field="artifact_sha256",
                artifact_kind="openubmc-live-patch",
            ),
            "journal_action": _live_patch_journal_action,
        },
        "upgrade_run": {
            "name": "upgrade",
            "artifact_phase": "build.artifact",
            "artifact_contract": ArtifactContract(
                path_fields=("artifact_path",),
                digest_field="artifact_sha256",
                version_field="product_version",
                artifact_kind="openubmc-hpm",
                required=True,
            ),
            "journal_action": _upgrade_journal_action,
        },
    }
    contracts: list[DomainPackAuthorContract] = []
    for operation, definition in definitions.items():
        adapter = adapters.get(operation)
        if adapter is None:
            continue
        journal_action = definition["journal_action"]
        contracts.append(
            DomainPackAuthorContract(
                name=str(definition["name"]),
                version="1",
                operation=operation,
                effect_class=EffectClass.RECONCILABLE_MUTATION,
                adapter=adapter,
                reconciler=adapter,
                verifier=(
                    lambda action, receipt, resolve=journal_action: (
                        mutation_receipt_verifier(
                            action,
                            receipt,
                            journal_action=resolve(action.arguments),
                        )
                    )
                ),
                artifact_contract=definition["artifact_contract"],
                artifact_phase=str(definition["artifact_phase"]),
                journal_action=journal_action,
            )
        )
    for stage in LOG_BUNDLE_STAGE_CONTRACTS:
        adapter = adapters.get(stage.operation)
        if adapter is None:
            continue
        result_contract = ResultArtifactContract(
            stage.output_kind,
            require_redacted=stage.output_redacted,
        )
        contracts.append(
            DomainPackAuthorContract(
                name=stage.operation.replace("_", "-"),
                version="1",
                operation=stage.operation,
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=(
                    lambda action, receipt, contract=result_contract: (
                        contract.bind(action, receipt) is not None
                    )
                ),
                artifact_contract=ArtifactContract(
                    path_fields=("_artifact_path",),
                    digest_field="_artifact_sha256",
                    artifact_kind=stage.input_kind,
                    required=True,
                    reference_required=True,
                    require_redacted=stage.input_redacted,
                ),
                result_artifact_contract=result_contract,
            )
        )
    return tuple(contracts)


def builtin_domain_packs(
    registry: CapabilityRegistry,
    adapters: Mapping[str, DomainAdapter],
) -> tuple[DomainPack, ...]:
    """Bind built-in author contracts through the public conformance seam."""

    return DomainPackConformanceSuite().bind(
        registry,
        builtin_domain_pack_contracts(adapters),
    )
