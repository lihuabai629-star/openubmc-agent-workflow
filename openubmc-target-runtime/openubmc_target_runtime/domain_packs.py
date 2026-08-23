"""Built-in Domain Pack registrations for proven Runtime mutation domains."""

from __future__ import annotations

from collections.abc import Mapping

from .capability import (
    ArtifactContract,
    CapabilityRegistry,
    DomainAdapter,
    DomainPack,
    EffectClass,
    ResultArtifactContract,
    mutation_receipt_verifier,
)


def _live_patch_journal_action(arguments: Mapping[str, object]) -> str:
    action = str(arguments.get("action", "")).strip().lower().replace("-", "_")
    return "rollback" if action == "rollback" else "live_patch"


def _upgrade_journal_action(_arguments: Mapping[str, object]) -> str:
    return "upgrade"


def builtin_domain_packs(
    registry: CapabilityRegistry,
    adapters: Mapping[str, DomainAdapter],
) -> tuple[DomainPack, ...]:
    """Register Runtime-owned mutation and local Artifact stage contracts."""

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
    packs: list[DomainPack] = []
    for operation, definition in definitions.items():
        adapter = adapters.get(operation)
        if adapter is None:
            continue
        journal_action = definition["journal_action"]
        packs.append(
            DomainPack(
                name=str(definition["name"]),
                version="1",
                descriptor=registry.require(operation),
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
    stage_definitions = {
        "log_bundle_index": (
            "openubmc-log-bundle",
            "openubmc-log-index",
            False,
            False,
        ),
        "log_bundle_query": (
            "openubmc-log-index",
            "openubmc-log-query",
            False,
            True,
        ),
        "log_bundle_export": (
            "openubmc-log-query",
            "openubmc-log-report",
            True,
            True,
        ),
    }
    for operation, (
        input_kind,
        output_kind,
        input_redacted,
        output_redacted,
    ) in stage_definitions.items():
        adapter = adapters.get(operation)
        if adapter is None:
            continue
        result_contract = ResultArtifactContract(
            output_kind,
            require_redacted=output_redacted,
        )
        packs.append(
            DomainPack(
                name=operation.replace("_", "-"),
                version="1",
                descriptor=registry.require(operation),
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
                    artifact_kind=input_kind,
                    required=True,
                    reference_required=True,
                    require_redacted=input_redacted,
                ),
                result_artifact_contract=result_contract,
            )
        )
    return tuple(packs)
