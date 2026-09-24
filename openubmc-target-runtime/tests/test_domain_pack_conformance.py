from __future__ import annotations

import hashlib
from enum import Enum
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    ArtifactContract,
    ArtifactRef,
    CallableDomainAdapter,
    CapabilityDescriptor,
    CapabilityRegistry,
    DomainExecutor,
    DomainPack,
    DomainPackAuthorContract,
    DomainPackConformanceExample,
    DomainPackConformanceSuite,
    DomainPackWorkflow,
    DomainReceipt,
    EffectClass,
    EffectRecoveryMode,
    MutationJournal,
    MutationRecoveryDisposition,
    RUNTIME_EFFECT_RECOVERY_ARGUMENT,
    RuntimeMcpService,
    RuntimeSDKContext,
    effect_recovery_mode,
    mutation_receipt_verifier,
    mutation_recovery_route,
)
from openubmc_target_runtime.domain_packs import (  # noqa: E402
    builtin_domain_pack_contracts,
    builtin_domain_packs,
)
from openubmc_target_runtime.operation_contracts import (  # noqa: E402
    DEFAULT_OPERATION_CONTRACTS,
)


def descriptor(
    operation: str = "fake_mutation",
    *,
    mutation: bool = True,
    effect_class: EffectClass | None = None,
) -> CapabilityDescriptor:
    values: dict[str, object] = {
        "operation": operation,
        "capability": f"test.{operation}",
        "owner_skill": "test-domain-pack",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object", "additionalProperties": True},
        "timeout_seconds": 10,
        "evidence_types": ("test-result",),
        "mutation": mutation,
    }
    if effect_class is not None:
        values["effect_class"] = effect_class
    return CapabilityDescriptor(**values)


def example(
    operation: str,
    *,
    status: str = "succeeded",
    arguments: dict[str, object] | None = None,
    value: dict[str, object] | None = None,
) -> DomainPackConformanceExample:
    return DomainPackConformanceExample(
        arguments=dict(arguments or {}),
        receipt=DomainReceipt(
            operation=operation,
            status=status,
            value=dict(value or {"ok": True}),
        ),
    )


class Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class ForeignEffectRecoveryMode(str, Enum):
    RECONCILE = "reconcile"


class ForeignMutationRecoveryDisposition(str, Enum):
    TERMINAL = "terminal"
    RECOVER = "recover"
    NEW = "new"
    INVALID = "invalid"


class Backend:
    @staticmethod
    def open_task(task_id: str) -> Task:
        return Task(task_id)

    @staticmethod
    def close_task(_task: Task) -> None:
        return None

    @staticmethod
    def maintain_task(_task: Task) -> int:
        return 0

    @staticmethod
    def task_status(task: Task) -> dict[str, object]:
        return {"task_id": task.task_id}

    @staticmethod
    def debug_run(_task, _arguments, _context) -> dict[str, object]:
        return {"ok": True}

    debug_collect = debug_run


class DomainPackConformanceTests(unittest.TestCase):
    def test_conformance_examples_are_typed_data_and_cannot_execute_io(self) -> None:
        with self.assertRaisesRegex(TypeError, "typed data"):
            DomainPackConformanceExample(
                arguments=lambda _context: {},
                receipt=lambda _action: DomainReceipt(
                    operation="fake_read",
                    status="succeeded",
                    value={"ok": True},
                ),
            )
        with self.assertRaisesRegex(TypeError, "typed data"):
            DomainPackConformanceExample(
                arguments={"probe": lambda: None},
                receipt=DomainReceipt(
                    operation="fake_read",
                    status="succeeded",
                    value={"ok": True},
                ),
            )

    def test_builtin_packs_use_the_public_author_contract(self) -> None:
        operations = (
            "live_patch_run",
            "upgrade_run",
            "log_bundle_index",
            "log_bundle_query",
            "log_bundle_export",
        )
        descriptors = tuple(
            descriptor(
                operation,
                mutation=operation in {"live_patch_run", "upgrade_run"},
            )
            for operation in operations
        )
        registry = CapabilityRegistry(descriptors)
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        contracts = builtin_domain_pack_contracts(
            registry,
            {operation: adapter for operation in operations}
        )

        self.assertTrue(
            all(
                isinstance(contract, DomainPackAuthorContract)
                for contract in contracts
            )
        )
        packs = DomainPackConformanceSuite().bind(registry, contracts)
        self.assertEqual(
            {pack.descriptor.operation for pack in packs},
            set(operations),
        )

    def test_author_contract_builds_a_read_only_pack_from_the_registry_seam(self) -> None:
        pack_descriptor = descriptor("fake_read", mutation=False)
        registry = CapabilityRegistry((pack_descriptor,))
        adapter = CallableDomainAdapter(
            lambda _context, _arguments: DomainReceipt(
                operation="fake_read",
                status="succeeded",
                value={"ok": True},
            )
        )
        contract = DomainPackAuthorContract(
            descriptor=pack_descriptor,
            name="fake-read",
            version="1",
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, receipt: receipt.status == "succeeded",
            conformance_example=example("fake_read"),
        )

        pack = contract.build(registry)

        self.assertEqual(pack.descriptor, pack_descriptor)
        self.assertEqual(pack.name, "fake-read")
        self.assertIs(pack.effect_class, EffectClass.READ_ONLY)
        self.assertIs(pack.adapter, adapter)

    def test_author_contract_rejects_effect_and_recovery_mismatches(self) -> None:
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        with self.assertRaisesRegex(ValueError, "mutation flag"):
            descriptor(
                "unsafe_idempotent",
                mutation=False,
                effect_class=EffectClass.IDEMPOTENT_MUTATION,
            )
        with self.assertRaisesRegex(ValueError, "mutation recovery"):
            DomainPackAuthorContract(
                descriptor=descriptor("fake_read", mutation=False),
                name="invalid-read",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                reconciler=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("first_read"),
            )
        with self.assertRaisesRegex(ValueError, "reconciler and journal action"):
            DomainPackAuthorContract(
                descriptor=descriptor("fake_mutation"),
                name="invalid-mutation",
                version="1",
                effect_class=EffectClass.RECONCILABLE_MUTATION,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("second_read"),
            )
        contract = DomainPackAuthorContract(
            descriptor=descriptor("fake_mutation"),
            name="mismatched",
            version="1",
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, _receipt: True,
            conformance_example=example("fake_mutation"),
        )
        with self.assertRaisesRegex(ValueError, "Effect class"):
            contract.build(CapabilityRegistry((descriptor("fake_mutation"),)))
        with self.assertRaisesRegex(ValueError, "redacted.*reference"):
            DomainPackAuthorContract(
                descriptor=descriptor("fake_read", mutation=False),
                name="invalid-artifact",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("second_read"),
                artifact_contract=ArtifactContract(
                    path_fields=("artifact_path",),
                    artifact_kind="redacted-input",
                    require_redacted=True,
                ),
            )
        idempotent_descriptor = descriptor(
            "idempotent_collect",
            mutation=True,
            effect_class=EffectClass.IDEMPOTENT_MUTATION,
        )
        with self.assertRaisesRegex(ValueError, "Effect class"):
            DomainPackAuthorContract(
                descriptor=idempotent_descriptor,
                name="unsafe-read-downgrade",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("idempotent_collect"),
            ).build(CapabilityRegistry((idempotent_descriptor,)))

    def test_mutation_workflow_requires_distinct_read_only_verification(self) -> None:
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        mutation_descriptor = descriptor("fake_mutation_route")
        verification_descriptor = descriptor(
            "fake_verification",
            mutation=False,
        )

        def contract(verification_operation: str) -> DomainPackAuthorContract:
            return DomainPackAuthorContract(
                descriptor=mutation_descriptor,
                name="fake-mutation-route",
                version="1",
                effect_class=EffectClass.RECONCILABLE_MUTATION,
                adapter=adapter,
                reconciler=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("fake_mutation_route"),
                journal_action=lambda _arguments: "live_patch",
                closeout_stage="live_patch",
                workflow=DomainPackWorkflow(
                    intent="live-patch",
                    verification_operation=verification_operation,
                ),
            )

        with self.assertRaisesRegex(ValueError, "distinct operation"):
            DomainPackConformanceSuite().bind(
                CapabilityRegistry((mutation_descriptor,)),
                (contract("fake_mutation_route"),),
            )

        mutation_verification = descriptor("fake_verification")
        with self.assertRaisesRegex(ValueError, "READ_ONLY"):
            DomainPackConformanceSuite().bind(
                CapabilityRegistry(
                    (mutation_descriptor, mutation_verification)
                ),
                (contract("fake_verification"),),
            )

        with self.assertRaisesRegex(ValueError, "intent is invalid"):
            DomainPackWorkflow(
                intent="arbitrary-flow",
                verification_operation=verification_descriptor.operation,
            )

    def test_pack_set_conformance_rejects_duplicate_identity_and_artifact_phase(self) -> None:
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        contracts = (
            DomainPackAuthorContract(
                descriptor=descriptor("first_read", mutation=False),
                name="duplicate-pack",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("first_read"),
                artifact_contract=ArtifactContract(
                    path_fields=("_artifact_path",),
                    artifact_kind="first-artifact",
                    reference_required=True,
                ),
                artifact_phase="shared.phase",
            ),
            DomainPackAuthorContract(
                descriptor=descriptor("second_read", mutation=False),
                name="duplicate-pack",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("second_read"),
                artifact_contract=ArtifactContract(
                    path_fields=("_artifact_path",),
                    artifact_kind="second-artifact",
                    reference_required=True,
                ),
                artifact_phase="shared.phase",
            ),
        )
        registry = CapabilityRegistry(
            (
                descriptor("first_read", mutation=False),
                descriptor("second_read", mutation=False),
            )
        )

        with self.assertRaisesRegex(ValueError, "duplicate Domain Pack identity"):
            DomainPackConformanceSuite().bind(registry, contracts)

        distinct = (
            contracts[0],
            DomainPackAuthorContract(
                descriptor=descriptor("second_read", mutation=False),
                name="second-pack",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("second_read"),
                artifact_contract=contracts[1].artifact_contract,
                artifact_phase="shared.phase",
            ),
        )
        with self.assertRaisesRegex(ValueError, "artifact phase"):
            DomainPackConformanceSuite().bind(registry, distinct)

        duplicate_operation = (
            contracts[0],
            DomainPackAuthorContract(
                descriptor=descriptor("first_read", mutation=False),
                name="another-first-pack",
                version="1",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("first_read"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "duplicate Domain Pack operation"):
            DomainPackConformanceSuite().bind(registry, duplicate_operation)

        missing_capability = DomainPackAuthorContract(
            descriptor=descriptor("first_read", mutation=False),
            name="missing-capability",
            version="1",
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, _receipt: True,
            conformance_example=example("first_read"),
            capability_requirements=("test.not-registered",),
        )
        with self.assertRaisesRegex(ValueError, "unregistered capability"):
            DomainPackConformanceSuite().bind(registry, (missing_capability,))

        legacy_pack = DomainPack(
            name="legacy",
            version="1",
            descriptor=registry.require("first_read"),
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, _receipt: True,
        )
        with self.assertRaisesRegex(TypeError, "author contract"):
            DomainPackConformanceSuite().bind(registry, (legacy_pack,))

    def test_pack_set_bind_executes_each_typed_behavioral_example(self) -> None:
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        contract = DomainPackAuthorContract(
            descriptor=descriptor("fake_mutation"),
            name="invalid-recovery-example",
            version="1",
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=adapter,
            reconciler=adapter,
            verifier=lambda _action, receipt: receipt.status == "verified",
            journal_action=lambda _arguments: "",
            conformance_example=example(
                "fake_mutation",
                status="verified",
            ),
        )

        with self.assertRaisesRegex(ValueError, "journal action"):
            DomainPackConformanceSuite().bind(
                CapabilityRegistry((descriptor("fake_mutation"),)),
                (contract,),
            )

    def test_behavioral_conformance_verifies_identity_receipt_and_effect_classification(self) -> None:
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        context = RuntimeSDKContext(
            task_id="conformance-run",
            operation_id="effect-conformance",
            timeout_seconds=10,
            target_id="192.0.2.1",
        )
        suite = DomainPackConformanceSuite(read_attempts=2)
        read_pack = DomainPackAuthorContract(
            descriptor=descriptor("fake_read", mutation=False),
            name="fake-read",
            version="1",
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, receipt: receipt.status == "succeeded",
            conformance_example=example("fake_read"),
        ).build(CapabilityRegistry((descriptor("fake_read", mutation=False),)))

        read_report = suite.verify_example(
            read_pack,
            context=context,
            arguments={"query": "health"},
            receipt=DomainReceipt(
                operation="fake_read",
                status="succeeded",
                value={"ok": True},
            ),
        )

        self.assertEqual(read_report["effect_identity"], "stable")
        self.assertEqual(read_report["receipt_binding"], "verified")
        self.assertEqual(read_report["retry_classification"], "bounded-read-retry:2")
        self.assertEqual(read_report["recovery_classification"], "none")

        mutation_pack = DomainPackAuthorContract(
            descriptor=descriptor("fake_mutation"),
            name="fake-mutation",
            version="1",
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=adapter,
            reconciler=adapter,
            verifier=lambda _action, receipt: receipt.status == "verified",
            conformance_example=example(
                "fake_mutation",
                status="verified",
            ),
            journal_action=lambda _arguments: "mutate",
        ).build(CapabilityRegistry((descriptor("fake_mutation"),)))
        mutation_report = suite.verify_example(
            mutation_pack,
            context=context,
            arguments={"value": 1},
            receipt=DomainReceipt(
                operation="fake_mutation",
                status="verified",
                value={"ok": True},
            ),
        )
        self.assertEqual(mutation_report["retry_classification"], "single-attempt")
        self.assertEqual(
            mutation_report["recovery_classification"],
            "reconcile-same-effect",
        )

        with self.assertRaisesRegex(ValueError, "receipt operation"):
            suite.verify_example(
                read_pack,
                context=context,
                arguments={},
                receipt=DomainReceipt(
                    operation="another_read",
                    status="succeeded",
                    value={"ok": True},
                ),
            )
        non_boolean_verifier = DomainPackAuthorContract(
            descriptor=descriptor("fake_read", mutation=False),
            name="non-boolean-verifier",
            version="1",
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, _receipt: "yes",
            conformance_example=example("fake_read"),
        ).build(CapabilityRegistry((descriptor("fake_read", mutation=False),)))
        with self.assertRaisesRegex(ValueError, "boolean"):
            suite.verify_example(
                non_boolean_verifier,
                context=context,
                arguments={},
                receipt=DomainReceipt(
                    operation="fake_read",
                    status="succeeded",
                    value={"ok": True},
                ),
            )


    def test_log_bundle_stage_contracts_are_internal_and_artifact_bound(self) -> None:
        operations = {
            "log_bundle_index": ("openubmc-log-bundle", "openubmc-log-index"),
            "log_bundle_query": ("openubmc-log-index", "openubmc-log-query"),
            "log_bundle_export": ("openubmc-log-query", "openubmc-log-report"),
        }
        descriptors = tuple(
            descriptor(operation, mutation=False) for operation in operations
        )
        registry = CapabilityRegistry(descriptors)
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})

        registered = builtin_domain_packs(
            registry,
            {item.operation: adapter for item in descriptors},
        )
        packs = {pack.descriptor.operation: pack for pack in registered}

        self.assertEqual(set(packs), set(operations))
        for operation, (input_kind, output_kind) in operations.items():
            with self.subTest(operation=operation):
                contract = DEFAULT_OPERATION_CONTRACTS.require(operation)
                self.assertEqual(contract.exposure, "internal")
                self.assertEqual(contract.audience, "internal")
                self.assertIs(packs[operation].effect_class, EffectClass.READ_ONLY)
                self.assertEqual(
                    packs[operation].artifact_contract.artifact_kind,
                    input_kind,
                )
                self.assertTrue(packs[operation].artifact_contract.reference_required)
                self.assertEqual(
                    packs[operation].result_artifact_contract.artifact_kind,
                    output_kind,
                )

    def test_builtin_mutation_packs_own_artifact_phase_and_kind_metadata(self) -> None:
        descriptors = (descriptor("live_patch_run"), descriptor("upgrade_run"))
        registry = CapabilityRegistry(descriptors)
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})

        registered = builtin_domain_packs(
            registry,
            {item.operation: adapter for item in descriptors},
        )
        packs = {pack.descriptor.operation: pack for pack in registered}
        executor = DomainExecutor(registry, {}, packs=registered)

        self.assertEqual(packs["live_patch_run"].artifact_phase, "developer.change")
        self.assertEqual(
            packs["live_patch_run"].artifact_contract.artifact_kind,
            "openubmc-live-patch",
        )
        self.assertEqual(packs["upgrade_run"].artifact_phase, "build.artifact")
        self.assertEqual(
            packs["upgrade_run"].artifact_contract.artifact_kind,
            "openubmc-hpm",
        )
        self.assertEqual(
            executor.artifact_metadata_for_phase("build.artifact"),
            {
                "mutation": True,
                "owner_skill": "test-domain-pack",
                "timeout_seconds": 10,
                "closeout_stage": "upgrade",
                "artifact_phase": "build.artifact",
                "artifact_kind": "openubmc-hpm",
                "artifact_requires_version": True,
            },
        )

    def test_artifact_contract_materializes_runtime_arguments_without_gateway_branches(self) -> None:
        contract = ArtifactContract(
            path_fields=("artifact_path",),
            digest_field="artifact_sha256",
            version_field="product_version",
            artifact_kind="openubmc-hpm",
            required=True,
        )
        reference = ArtifactRef(
            handle="/tmp/product.hpm",
            digest="a" * 64,
            kind="openubmc-hpm",
            size=8,
            provenance="openubmc-build",
            version="2.0.0",
            target="192.0.2.1",
            run_id="run-1",
        )

        self.assertEqual(
            contract.runtime_arguments(reference, Path("/verified/product.hpm")),
            {
                "artifact_path": "/verified/product.hpm",
                "artifact_sha256": "a" * 64,
                "product_version": "2.0.0",
            },
        )

    def test_domain_pack_binds_legacy_mutation_receipts_with_pack_owned_action(self) -> None:
        pack_descriptor = descriptor("live_patch_run")
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        pack = DomainPack(
            name="live-patch",
            version="1",
            descriptor=pack_descriptor,
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=adapter,
            reconciler=adapter,
            verifier=lambda _action, _receipt: True,
            journal_action=lambda arguments: (
                "rollback" if arguments.get("action") == "rollback" else "live_patch"
            ),
        )

        translated = pack.bind_compatibility_receipt(
            {
                "journal": {"stage": "verified"},
                "executions": [
                    {
                        "operation_id": "nested-effect",
                        "value": {"journal": {"stage": "verified"}},
                    }
                ],
            },
            operation_id="effect-1",
            arguments={"action": "rollback"},
        )

        self.assertEqual(
            translated["_runtime_compatibility_receipt"],
            {"operation_id": "effect-1", "action": "rollback"},
        )
        self.assertEqual(
            translated["executions"][0]["value"]["_runtime_compatibility_receipt"],
            {"operation_id": "nested-effect", "action": "rollback"},
        )

    def test_mutation_receipt_stage_contract_is_owned_by_the_journal(self) -> None:
        self.assertIn("planned", MutationJournal.VALID_STAGES)
        self.assertIn("replan_required", MutationJournal.VALID_STAGES)
        self.assertTrue(
            MutationJournal.VALID_STAGES.issuperset(
                MutationJournal.TERMINAL_STAGES
            )
        )
        with self.assertRaisesRegex(ValueError, "unsupported mutation journal stage"):
            MutationJournal(
                task_id="invalid-stage",
                operation_id="effect-invalid-stage",
                operation_fingerprint="a" * 64,
                action="live_patch",
                original_intent="live-patch",
                target_fingerprint="b" * 64,
                target_identity=None,
                epoch_before=0,
                stage="future_stage",
            )

    def test_mutation_journal_owns_a_typed_recovery_disposition(self) -> None:
        journal = MutationJournal(
            task_id="typed-recovery",
            operation_id="effect-typed-recovery",
            operation_fingerprint="a" * 64,
            action="live_patch",
            original_intent="live-patch",
            target_fingerprint="b" * 64,
            target_identity=None,
            epoch_before=0,
        )

        self.assertIs(
            journal.recovery_disposition,
            MutationRecoveryDisposition.RECOVER,
        )

    def test_shared_mutation_recovery_route_selects_terminal_pending_and_replan(self) -> None:
        terminal = SimpleNamespace(
            operation_id="effect-1",
            action="live_patch",
            stage="verified",
            terminal=True,
            recovery_disposition=MutationRecoveryDisposition.TERMINAL,
            operation_fingerprint="match",
        )
        pending = SimpleNamespace(
            operation_id="effect-2",
            action="live_patch",
            stage="applying",
            terminal=False,
            recovery_disposition=MutationRecoveryDisposition.RECOVER,
            operation_fingerprint="match",
        )
        replan = SimpleNamespace(
            operation_id="effect-3",
            action="live_patch",
            stage="replan_required",
            terminal=False,
            recovery_disposition=MutationRecoveryDisposition.NEW,
            operation_fingerprint="match",
        )

        self.assertEqual(
            mutation_recovery_route(
                EffectRecoveryMode.RECONCILE,
                lambda: (terminal,),
                operation_id="effect-1",
                action="live_patch",
                label="Live Patch",
                matches=lambda journal: journal.operation_fingerprint == "match",
            ).disposition,
            "terminal",
        )
        self.assertEqual(
            mutation_recovery_route(
                EffectRecoveryMode.RECONCILE,
                lambda: (pending,),
                operation_id="effect-2",
                action="live_patch",
                label="Live Patch",
                matches=lambda journal: journal.operation_fingerprint == "match",
            ).disposition,
            "recover",
        )
        self.assertEqual(
            mutation_recovery_route(
                EffectRecoveryMode.RECONCILE,
                lambda: (replan,),
                operation_id="effect-3",
                action="live_patch",
                label="Live Patch",
                matches=lambda journal: journal.operation_fingerprint == "match",
            ).disposition,
            "new",
        )

    def test_effect_recovery_mode_accepts_equivalent_enum_from_adapter_boundary(self) -> None:
        self.assertIs(
            effect_recovery_mode(
                {
                    RUNTIME_EFFECT_RECOVERY_ARGUMENT: (
                        ForeignEffectRecoveryMode.RECONCILE
                    )
                }
            ),
            EffectRecoveryMode.RECONCILE,
        )

    def test_mutation_recovery_route_accepts_equivalent_enum_from_adapter_boundary(
        self,
    ) -> None:
        for stage, raw, expected in (
            (
                "verified",
                ForeignMutationRecoveryDisposition.TERMINAL,
                MutationRecoveryDisposition.TERMINAL,
            ),
            (
                "applying",
                ForeignMutationRecoveryDisposition.RECOVER,
                MutationRecoveryDisposition.RECOVER,
            ),
            (
                "replan_required",
                ForeignMutationRecoveryDisposition.NEW,
                MutationRecoveryDisposition.NEW,
            ),
        ):
            with self.subTest(stage=stage):
                journal = SimpleNamespace(
                    operation_id="cross-module-effect",
                    action="live_patch",
                    stage=stage,
                    recovery_disposition=raw,
                )
                route = mutation_recovery_route(
                    EffectRecoveryMode.RECONCILE,
                    lambda: (journal,),
                    operation_id="cross-module-effect",
                    action="live_patch",
                    label="Live Patch",
                    matches=lambda _journal: True,
                )
                self.assertIs(route.disposition, expected)

        for invalid in ("terminal", ForeignMutationRecoveryDisposition.INVALID):
            with self.subTest(invalid=invalid):
                journal.recovery_disposition = invalid
                with self.assertRaisesRegex(ValueError, "valid recovery disposition"):
                    mutation_recovery_route(
                        EffectRecoveryMode.RECONCILE,
                        lambda: (journal,),
                        operation_id="cross-module-effect",
                        action="live_patch",
                        label="Live Patch",
                        matches=lambda _journal: True,
                    )

    def test_mutation_verifier_authenticates_failed_receipts_without_claiming_success(self) -> None:
        pack_descriptor = descriptor("live_patch_run")
        adapter = CallableDomainAdapter(
            lambda _context, _arguments: DomainReceipt(
                operation="live_patch_run",
                status="failed",
                value={
                    "operation_id": "effect-failed-1",
                    "journal": {
                        "schema": "openubmc.target-runtime.v1/mutation-journal",
                        "task_id": "failed-receipt",
                        "operation_id": "effect-failed-1",
                        "operation_fingerprint": "f" * 64,
                        "target_fingerprint": "b" * 64,
                        "action": "live_patch",
                        "stage": "verification_failed_terminal",
                        "effects_started": True,
                    }
                },
            )
        )
        executor = DomainExecutor(
            CapabilityRegistry((pack_descriptor,)),
            {},
            packs=(
                DomainPack(
                    name="live-patch",
                    version="1",
                    descriptor=pack_descriptor,
                    effect_class=EffectClass.RECONCILABLE_MUTATION,
                    adapter=adapter,
                    reconciler=adapter,
                    verifier=lambda action, receipt: mutation_receipt_verifier(
                        action,
                        receipt,
                        journal_action="live_patch",
                    ),
                ),
            ),
        )

        result = executor.execute(
            "live_patch_run",
            context=RuntimeSDKContext(
                task_id="failed-receipt",
                operation_id="effect-failed-1",
                timeout_seconds=5,
            ),
            arguments={},
        )

        self.assertTrue(result.verified)
        self.assertEqual(result.status, "failed")

    def test_mutation_verifier_rejects_wrong_modern_identity_and_artifact_binding(self) -> None:
        pack_descriptor = descriptor("live_patch_run")
        action_context = RuntimeSDKContext(
            task_id="strict-receipt-task",
            operation_id="strict-effect-1",
            timeout_seconds=5,
        )
        pack = DomainPack(
            name="strict-live-patch",
            version="1",
            descriptor=pack_descriptor,
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=CallableDomainAdapter(lambda _context, _arguments: {}),
            reconciler=CallableDomainAdapter(lambda _context, _arguments: {}),
            verifier=lambda action, receipt: mutation_receipt_verifier(
                action,
                receipt,
                journal_action="live_patch",
            ),
            artifact_contract=ArtifactContract(
                path_fields=("local_path",),
                digest_field="artifact_sha256",
                required=True,
            ),
        )
        action = pack.action(
            action_context,
            {
                "local_path": "/tmp/strict.lua",
                "artifact_sha256": "a" * 64,
            },
        )
        base = {
            "schema": "openubmc.target-runtime.v1/mutation-journal",
            "task_id": action_context.task_id,
            "operation_id": action_context.operation_id,
            "operation_fingerprint": "f" * 64,
            "target_fingerprint": "b" * 64,
            "action": "live_patch",
            "stage": "verified",
            "expected_checksum": "a" * 64,
        }
        for field, value in (
            ("task_id", "another-task"),
            ("operation_id", "another-effect"),
            ("operation_fingerprint", ""),
            ("target_fingerprint", ""),
            ("expected_checksum", "b" * 64),
        ):
            with self.subTest(field=field):
                journal = dict(base)
                journal[field] = value
                receipt = DomainReceipt(
                    operation="live_patch_run",
                    status="verified",
                    value={
                        "operation_id": action_context.operation_id,
                        "journal": journal,
                    },
                )
                self.assertFalse(
                    mutation_receipt_verifier(
                        action,
                        receipt,
                        journal_action="live_patch",
                    )
                )

    def test_schema_less_compatibility_receipt_still_requires_identity_action_and_stage(self) -> None:
        pack_descriptor = descriptor("upgrade_run")
        context = RuntimeSDKContext(
            task_id="compat-receipt-task",
            operation_id="compat-effect-1",
            timeout_seconds=5,
        )
        pack = DomainPack(
            name="compat-upgrade",
            version="1",
            descriptor=pack_descriptor,
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=CallableDomainAdapter(lambda _context, _arguments: {}),
            reconciler=CallableDomainAdapter(lambda _context, _arguments: {}),
            verifier=lambda action, receipt: mutation_receipt_verifier(
                action,
                receipt,
                journal_action="upgrade",
            ),
        )
        action = pack.action(context, {})
        valid = DomainReceipt(
            operation="upgrade_run",
            status="verified",
            value={
                "operation_id": context.operation_id,
                "journal": {
                    "operation_id": context.operation_id,
                    "action": "upgrade",
                    "stage": "verified",
                },
            },
        )
        self.assertTrue(
            mutation_receipt_verifier(action, valid, journal_action="upgrade")
        )
        for journal in (
            {"operation_id": "other", "action": "upgrade", "stage": "verified"},
            {"operation_id": context.operation_id, "action": "live_patch", "stage": "verified"},
            {"operation_id": context.operation_id, "action": "upgrade", "stage": ""},
        ):
            with self.subTest(journal=journal):
                receipt = DomainReceipt(
                    operation="upgrade_run",
                    status="verified",
                    value={"operation_id": context.operation_id, "journal": journal},
                )
                self.assertFalse(
                    mutation_receipt_verifier(
                        action,
                        receipt,
                        journal_action="upgrade",
                    )
                )

    def test_fake_pack_executes_and_reconciles_without_gateway_or_run_engine_changes(self) -> None:
        calls: list[tuple[str, EffectRecoveryMode | None]] = []
        verifications: list[str] = []

        def execute(context, arguments):
            calls.append(("execute", context.recovery_mode))
            return DomainReceipt(
                operation="fake_mutation",
                status="verified",
                value={"projected": arguments["value"]},
                evidence_ids=("evidence-fake",),
            )

        def reconcile(context, arguments):
            calls.append(("reconcile", context.recovery_mode))
            return {
                "outcome_status": "verified",
                "projected": arguments["value"],
                "reconciled": True,
            }

        def verify(action, receipt):
            verifications.append(action.effect_id)
            return receipt.status == "verified" and bool(receipt.value.get("projected"))

        pack_descriptor = descriptor()
        pack = DomainPack(
            name="fake-pack",
            version="1.0",
            descriptor=pack_descriptor,
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=CallableDomainAdapter(execute),
            reconciler=CallableDomainAdapter(reconcile),
            verifier=verify,
            artifact_contract=ArtifactContract(
                path_fields=("artifact_path",),
                digest_field="artifact_sha256",
                version_field="artifact_version",
                artifact_kind="test-artifact",
                required=True,
            ),
        )
        executor = DomainExecutor(
            CapabilityRegistry((pack_descriptor,)),
            {},
            packs=(pack,),
        )
        context = RuntimeSDKContext(
            task_id="fake-workflow",
            operation_id="effect-fake-1",
            timeout_seconds=5,
            target_id="target-1",
        )
        arguments = {
            "value": 42,
            "artifact_path": "/tmp/fake.bin",
            "artifact_sha256": "a" * 64,
            "artifact_version": "1.0.0",
            "artifact_ref": {
                "handle": "/tmp/fake.bin",
                "digest": "sha256:" + "a" * 64,
                "kind": "test-artifact",
                "size": 0,
                "provenance": "unit-test",
                "retention_hint": "run-lifetime",
                "version": "1.0.0",
                "target": "target-1",
                "run_id": "fake-workflow",
            },
        }

        executed = executor.execute(
            "fake_mutation", context=context, arguments=arguments
        )
        replay_identity = pack.action(context, arguments).effect_id
        recovered = executor.reconcile(
            "fake_mutation", context=context, arguments=arguments
        )

        self.assertEqual(executed.action.effect_id, replay_identity)
        self.assertIsInstance(executed.action.artifact, ArtifactRef)
        self.assertEqual(executed.action.artifact.digest, "a" * 64)
        self.assertTrue(executed.verified)
        self.assertEqual(executed.value["projected"], 42)
        self.assertEqual(recovered.value["reconciled"], True)
        self.assertTrue(recovered.recovery)
        self.assertEqual(
            calls,
            [
                ("execute", None),
                ("reconcile", EffectRecoveryMode.RECONCILE),
            ],
        )
        self.assertEqual(len(verifications), 2)
        self.assertEqual(
            executed.to_public_dict()["action"]["artifact_ref"]["version"],
            "1.0.0",
        )
        self.assertNotIn("artifact", executed.to_public_dict()["action"])

        public_pack = pack.to_public_dict()
        self.assertEqual(public_pack["artifact_phase"], "")

    def test_mutation_pack_never_retries_an_unknown_result(self) -> None:
        attempts = 0

        def unknown(_context, _arguments):
            nonlocal attempts
            attempts += 1
            raise TimeoutError("mutation outcome unknown")

        pack_descriptor = descriptor("fake_unknown")
        adapter = CallableDomainAdapter(unknown)
        executor = DomainExecutor(
            CapabilityRegistry((pack_descriptor,)),
            {},
            packs=(
                DomainPack(
                    name="fake-unknown-pack",
                    version="1",
                    descriptor=pack_descriptor,
                    effect_class=EffectClass.RECONCILABLE_MUTATION,
                    adapter=adapter,
                    reconciler=adapter,
                    verifier=lambda _action, _receipt: True,
                ),
            ),
            read_attempts=3,
        )

        with self.assertRaises(TimeoutError):
            executor.execute(
                "fake_unknown",
                context=RuntimeSDKContext(
                    task_id="fake-unknown",
                    operation_id="fake-unknown-1",
                    timeout_seconds=5,
                ),
                arguments={},
            )

        self.assertEqual(attempts, 1)

    def test_reconcile_keeps_the_original_effect_identity(self) -> None:
        pack_descriptor = descriptor("fake_identity")
        adapter = CallableDomainAdapter(
            lambda _context, _arguments: {
                "outcome_status": "verified",
                "projected": True,
            }
        )
        pack = DomainPack(
            name="fake-identity-pack",
            version="1",
            descriptor=pack_descriptor,
            effect_class=EffectClass.RECONCILABLE_MUTATION,
            adapter=adapter,
            reconciler=adapter,
            verifier=lambda _action, receipt: receipt.status == "verified",
        )
        executor = DomainExecutor(
            CapabilityRegistry((pack_descriptor,)),
            {},
            packs=(pack,),
        )
        context = RuntimeSDKContext(
            task_id="identity",
            operation_id="stable-operation",
            timeout_seconds=5,
            target_id="target-1",
        )
        executed = executor.execute(
            "fake_identity", context=context, arguments={"value": 42}
        )
        recovered = executor.reconcile(
            "fake_identity",
            context=context,
            arguments={
                "value": 42,
                "_runtime_effect_recovery": "reconcile",
            },
        )

        self.assertEqual(executed.action.effect_id, recovered.action.effect_id)
        self.assertNotIn(
            "_runtime_effect_recovery", recovered.action.to_public_dict()["arguments"]
        )

    def test_runtime_registers_live_patch_and_upgrade_through_domain_packs(self) -> None:
        class FullBackend(Backend):
            live_patch_run = Backend.debug_run
            upgrade_run = Backend.debug_run

        service = RuntimeMcpService(FullBackend(), interface_profile="operator")
        try:
            status = service.call_exposed_tool(
                "runtime_status",
                {},
                task_id="domain-pack-status",
                operation_id="domain-pack-status",
            )
            packs = {
                item["operation"]: item
                for item in status["domain_packs"]
            }
        finally:
            service.close()

        self.assertEqual(set(packs), {"live_patch_run", "upgrade_run"})
        for operation in packs:
            self.assertEqual(
                packs[operation]["effect_class"],
                EffectClass.RECONCILABLE_MUTATION.value,
            )
            self.assertTrue(packs[operation]["capability_requirements"])

    def test_log_collection_keeps_its_idempotent_effect_policy(self) -> None:
        class FullBackend(Backend):
            log_bundle_collect = Backend.debug_run

        service = RuntimeMcpService(FullBackend())
        try:
            policy = service._test.domain_executor.policy_for("log_bundle_collect")
            descriptor = service._test.capability_registry.require(
                "log_bundle_collect"
            )
        finally:
            service.close()

        self.assertIs(policy.effect_class, EffectClass.IDEMPOTENT_MUTATION)
        self.assertEqual(policy.max_attempts, 1)
        self.assertIs(
            descriptor.effect_class,
            EffectClass.IDEMPOTENT_MUTATION,
        )

    def test_agent_entry_operation_cannot_downgrade_a_mutation_pack(self) -> None:
        class FullBackend(Backend):
            live_patch_run = Backend.debug_run

        service = RuntimeMcpService(FullBackend())
        try:
            with self.assertRaisesRegex(ValueError, "READ_ONLY"):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.92",
                        "intent": "diagnosis-only",
                        "entry_operation": "live_patch_run",
                        "purpose": "must remain read-only",
                    },
                    task_id="unsafe-entry-run",
                    operation_id="unsafe-entry-start",
                )
        finally:
            service.close()

    def test_entry_arguments_cannot_override_runtime_workflow_control(self) -> None:
        service = RuntimeMcpService(Backend())
        try:
            with self.assertRaisesRegex(ValueError, "Runtime-owned"):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.92",
                        "intent": "diagnosis-only",
                        "entry_operation": "debug_run",
                        "entry_arguments": {
                            "workflow": {
                                "verification": {"profile": "injected"}
                            }
                        },
                        "purpose": "workflow control remains Runtime-owned",
                    },
                    task_id="entry-arguments-run",
                    operation_id="entry-arguments-start",
                )
        finally:
            service.close()

    def test_injected_distinct_pack_extends_defaults_and_participates_in_a_real_workflow(self) -> None:
        class FullBackend(Backend):
            live_patch_run = Backend.debug_run

            @staticmethod
            def debug_run(_task, _arguments, _context) -> dict[str, object]:
                raise AssertionError("the injected debug Pack must execute")

            @staticmethod
            def debug_collect(_task, arguments, _context) -> dict[str, object]:
                value: dict[str, object] = {
                    "ok": True,
                    "summary": "fake verification completed",
                    "target_epoch": 1,
                }
                if arguments.get("profile") == "freshness" or arguments.get(
                    "_minimum_target_epoch"
                ):
                    value["business_acceptance"] = "passed"
                return value

        calls: list[str] = []

        def pack_extensions(registry, _adapters):
            pack_descriptor = registry.require("debug_run")

            def execute(context, arguments):
                calls.append(context.operation_id)
                return DomainReceipt(
                    operation="debug_run",
                    status="succeeded",
                    value={
                        "ok": True,
                        "summary": "fake debug completed",
                        "root_cause": "the injected diagnostic path completed",
                        "observed_at": "2026-08-25T00:00:00Z",
                        "freshness": {"status": "fresh"},
                        "target_epoch": 1,
                    },
                )

            adapter = CallableDomainAdapter(execute)
            return (
                DomainPackAuthorContract(
                    descriptor=pack_descriptor,
                    name="fake-debug",
                    version="1",
                    effect_class=EffectClass.READ_ONLY,
                    adapter=adapter,
                    verifier=lambda _action, receipt: (
                        receipt.status == "succeeded"
                        and receipt.value.get("summary") == "fake debug completed"
                    ),
                    conformance_example=example(
                        "debug_run",
                        value={
                            "ok": True,
                            "summary": "fake debug completed",
                            "root_cause": "the injected diagnostic path completed",
                            "observed_at": "2026-08-25T00:00:00Z",
                            "freshness": {"status": "fresh"},
                            "target_epoch": 1,
                        },
                    ),
                ),
            )

        service = RuntimeMcpService(
            FullBackend(),
            domain_pack_extensions=pack_extensions,
        )
        operator = RuntimeMcpService(
            FullBackend(),
            domain_pack_extensions=pack_extensions,
            interface_profile="operator",
        )
        try:
            status = operator.call_exposed_tool(
                "runtime_status",
                {},
                task_id="fake-pack-status",
                operation_id="fake-pack-status",
            )
            registered = {
                item["operation"] for item in status["domain_packs"]
            }
            conformance = status["domain_pack_conformance"]
            completed = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.90",
                    "intent": "diagnosis-only",
                    "purpose": "exercise a distinct fake Domain Pack",
                },
                task_id="fake-pack-workflow",
                operation_id="fake-pack-start",
            )
        finally:
            service.close()
            operator.close()

        self.assertEqual(completed["state"], "waiting_response", completed)
        self.assertEqual(completed["gate"]["name"], "diagnosis.acceptance")
        self.assertEqual(len(calls), 1)
        self.assertEqual(registered, {"debug_run", "live_patch_run"})
        self.assertTrue(conformance["valid"])
        self.assertEqual(
            conformance["operations"],
            ["debug_run", "live_patch_run"],
        )

    def test_runtime_rejects_an_extension_that_bypasses_the_author_contract(self) -> None:
        class FullBackend(Backend):
            log_bundle_collect = Backend.debug_run

        def raw_pack_extension(registry, adapters):
            return (
                DomainPack(
                    name="raw-log-bundle",
                    version="1",
                    descriptor=registry.require("log_bundle_collect"),
                    effect_class=EffectClass.READ_ONLY,
                    adapter=adapters["log_bundle_collect"],
                    verifier=lambda _action, _receipt: True,
                ),
            )

        with self.assertRaisesRegex(TypeError, "author contracts"):
            RuntimeMcpService(
                FullBackend(),
                domain_pack_extensions=raw_pack_extension,
            )

    def test_extension_contributes_a_new_capability_without_mcp_wiring(self) -> None:
        new_descriptor = descriptor("fake_health", mutation=False)
        adapter = CallableDomainAdapter(
            lambda _context, _arguments: DomainReceipt(
                operation="fake_health",
                status="succeeded",
                value={
                    "health": "ok",
                    "summary": "target health is ok",
                    "root_cause": "the target health probe found no fault",
                    "observed_at": "2026-08-25T00:00:00Z",
                    "freshness": {"status": "fresh"},
                },
            )
        )

        def extension(_registry, _adapters):
            return (
                DomainPackAuthorContract(
                    descriptor=new_descriptor,
                    name="fake-health",
                    version="1",
                    effect_class=EffectClass.READ_ONLY,
                    adapter=adapter,
                    verifier=lambda _action, receipt: (
                        receipt.value.get("health") == "ok"
                    ),
                    closeout_stage="diagnosis",
                    conformance_example=DomainPackConformanceExample(
                        arguments={},
                        receipt=DomainReceipt(
                            operation="fake_health",
                            status="succeeded",
                            value={
                                "health": "ok",
                                "summary": "target health is ok",
                                "root_cause": "the target health probe found no fault",
                                "observed_at": "2026-08-25T00:00:00Z",
                                "freshness": {"status": "fresh"},
                            },
                        ),
                    ),
                ),
            )

        service = RuntimeMcpService(
            Backend(),
            domain_pack_extensions=extension,
        )
        operator = RuntimeMcpService(
            Backend(),
            domain_pack_extensions=extension,
            interface_profile="operator",
        )
        try:
            completed = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.91",
                    "intent": "diagnosis-only",
                    "entry_operation": "fake_health",
                    "purpose": "run a newly contributed Domain Pack",
                },
                task_id="new-capability-run",
                operation_id="new-capability-start",
            )
            status = operator.call_exposed_tool(
                "runtime_status",
                {},
                task_id="new-capability-status",
                operation_id="new-capability-status",
            )
        finally:
            service.close()
            operator.close()

        capabilities = {
            item["operation"]
            for item in status["capability_registry"]["capabilities"]
        }
        self.assertIn("fake_health", capabilities)
        self.assertIn("fake_health", status["domain_pack_conformance"]["operations"])
        self.assertEqual(completed["state"], "waiting_response", completed)
        self.assertEqual(completed["gate"]["name"], "diagnosis.acceptance")
        self.assertEqual(
            completed["diagnostic_receipt"]["operation"],
            "fake_health",
        )
        self.assertEqual(
            completed["diagnostic_receipt"]["status"],
            "complete",
        )

    def test_extension_contributes_a_mutation_route_with_fresh_verification(self) -> None:
        seen_arguments: list[dict[str, object]] = []
        seen_verification_arguments: list[dict[str, object]] = []

        mutation_descriptor = descriptor("fake_mutation_route")
        verification_descriptor = descriptor(
            "fake_mutation_verification",
            mutation=False,
        )

        def execute_mutation(context, arguments):
            seen_arguments.append(dict(arguments))
            return DomainReceipt(
                operation="fake_mutation_route",
                status="verified",
                value={
                    "summary": "fake mutation verified",
                    "target_epoch": 1,
                    "journal": {
                        "schema": "openubmc.target-runtime.v1/mutation-journal",
                        "task_id": context.task_id,
                        "operation_id": context.operation_id,
                        "operation_fingerprint": "a" * 64,
                        "target_fingerprint": "b" * 64,
                        "action": "live_patch",
                        "stage": "verified",
                        "effects_started": True,
                        "epoch_after": 1,
                        "expected_checksum": "c" * 64,
                        "observed_checksum": "c" * 64,
                        "root_mount_restored": True,
                    },
                },
            )

        adapter = CallableDomainAdapter(execute_mutation)
        verification_adapter = CallableDomainAdapter(
            lambda _context, arguments: (
                seen_verification_arguments.append(dict(arguments))
                or DomainReceipt(
                    operation="fake_mutation_verification",
                    status="succeeded",
                    value={
                        "ok": True,
                        "summary": "fresh verification passed",
                        "business_acceptance": "passed",
                        "target_epoch": int(
                            arguments.get("_minimum_target_epoch", 0)
                        ),
                    },
                )
            )
        )

        def extension(_registry, _adapters):
            return (
                DomainPackAuthorContract(
                    descriptor=mutation_descriptor,
                    name="fake-mutation-route",
                    version="1",
                    effect_class=EffectClass.RECONCILABLE_MUTATION,
                    adapter=adapter,
                    reconciler=adapter,
                    verifier=lambda action, receipt: mutation_receipt_verifier(
                        action,
                        receipt,
                        journal_action="live_patch",
                    ),
                    journal_action=lambda _arguments: "live_patch",
                    closeout_stage="live_patch",
                    workflow=DomainPackWorkflow(
                        intent="live-patch",
                        verification_operation="fake_mutation_verification",
                    ),
                    conformance_example=DomainPackConformanceExample(
                        arguments={"ip": "conformance-target"},
                        receipt=DomainReceipt(
                            operation="fake_mutation_route",
                            status="verified",
                            value={
                                "summary": "fake mutation verified",
                                "target_epoch": 1,
                                "journal": {
                                    "schema": "openubmc.target-runtime.v1/mutation-journal",
                                    "task_id": "conformance-fake_mutation_route",
                                    "operation_id": (
                                        "effect-conformance-fake_mutation_route"
                                    ),
                                    "operation_fingerprint": "a" * 64,
                                    "target_fingerprint": "b" * 64,
                                    "action": "live_patch",
                                    "stage": "verified",
                                    "effects_started": True,
                                    "epoch_after": 1,
                                    "expected_checksum": "c" * 64,
                                    "observed_checksum": "c" * 64,
                                    "root_mount_restored": True,
                                },
                            },
                        ),
                    ),
                ),
                DomainPackAuthorContract(
                    descriptor=verification_descriptor,
                    name="fake-mutation-verification",
                    version="1",
                    effect_class=EffectClass.READ_ONLY,
                    adapter=verification_adapter,
                    verifier=lambda _action, receipt: (
                        receipt.value.get("business_acceptance") == "passed"
                    ),
                    closeout_stage="verification",
                    conformance_example=DomainPackConformanceExample(
                        arguments={"_minimum_target_epoch": 1},
                        receipt=DomainReceipt(
                            operation="fake_mutation_verification",
                            status="succeeded",
                            value={
                                "ok": True,
                                "summary": "fresh verification passed",
                                "business_acceptance": "passed",
                                "target_epoch": 1,
                            },
                        ),
                    ),
                ),
            )

        service = RuntimeMcpService(
            Backend(),
            domain_pack_extensions=extension,
        )
        try:
            with self.assertRaisesRegex(ValueError, "typed route"):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.93",
                        "intent": "upgrade-and-verify",
                        "entry_operation": "fake_mutation_route",
                        "purpose": "reject a mismatched contributed route",
                    },
                    task_id="wrong-mutation-route",
                    operation_id="wrong-mutation-start",
                )
            completed = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.93",
                    "intent": "live-patch",
                    "entry_operation": "fake_mutation_route",
                    "entry_arguments": {"repair_scope": "fan-zone-1"},
                    "purpose": "run a contributed mutation route",
                },
                task_id="new-mutation-run",
                operation_id="new-mutation-start",
            )
        finally:
            service.close()

        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(
            [fact["name"] for fact in completed["facts"]],
            ["fake_mutation_route", "fake_mutation_verification"],
        )
        self.assertEqual(seen_arguments[0]["repair_scope"], "fan-zone-1")
        self.assertEqual(
            seen_verification_arguments[0]["_minimum_target_epoch"],
            1,
        )
        self.assertNotIn("repair_scope", seen_verification_arguments[0])


if __name__ == "__main__":
    unittest.main()
