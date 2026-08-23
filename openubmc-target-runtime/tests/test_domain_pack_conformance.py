from __future__ import annotations

import hashlib
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
    DomainReceipt,
    EffectClass,
    EffectRecoveryMode,
    MutationJournal,
    MutationRecoveryDisposition,
    RuntimeMcpService,
    RuntimeSDKContext,
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
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        operation=operation,
        capability=f"test.{operation}",
        owner_skill="test-domain-pack",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        timeout_seconds=10,
        evidence_types=("test-result",),
        mutation=mutation,
    )


def example(
    operation: str,
    *,
    status: str = "succeeded",
    arguments: dict[str, object] | None = None,
    value: dict[str, object] | None = None,
) -> DomainPackConformanceExample:
    return DomainPackConformanceExample(
        arguments=lambda _context: dict(arguments or {}),
        receipt=lambda _action: DomainReceipt(
            operation=operation,
            status=status,
            value=dict(value or {"ok": True}),
        ),
    )


class Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


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
            name="fake-read",
            version="1",
            operation="fake_read",
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
        with self.assertRaisesRegex(ValueError, "mutation recovery"):
            DomainPackAuthorContract(
                name="invalid-read",
                version="1",
                operation="fake_read",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                reconciler=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("first_read"),
            )
        with self.assertRaisesRegex(ValueError, "reconciler and journal action"):
            DomainPackAuthorContract(
                name="invalid-mutation",
                version="1",
                operation="fake_mutation",
                effect_class=EffectClass.RECONCILABLE_MUTATION,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("second_read"),
            )
        contract = DomainPackAuthorContract(
            name="mismatched",
            version="1",
            operation="fake_mutation",
            effect_class=EffectClass.READ_ONLY,
            adapter=adapter,
            verifier=lambda _action, _receipt: True,
            conformance_example=example("fake_mutation"),
        )
        with self.assertRaisesRegex(ValueError, "Effect class"):
            contract.build(CapabilityRegistry((descriptor("fake_mutation"),)))
        with self.assertRaisesRegex(ValueError, "redacted.*reference"):
            DomainPackAuthorContract(
                name="invalid-artifact",
                version="1",
                operation="fake_read",
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

    def test_pack_set_conformance_rejects_duplicate_identity_and_artifact_phase(self) -> None:
        adapter = CallableDomainAdapter(lambda _context, _arguments: {})
        contracts = (
            DomainPackAuthorContract(
                name="duplicate-pack",
                version="1",
                operation="first_read",
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
                name="duplicate-pack",
                version="1",
                operation="second_read",
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
                name="second-pack",
                version="1",
                operation="second_read",
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
                name="another-first-pack",
                version="1",
                operation="first_read",
                effect_class=EffectClass.READ_ONLY,
                adapter=adapter,
                verifier=lambda _action, _receipt: True,
                conformance_example=example("first_read"),
            ),
        )
        with self.assertRaisesRegex(ValueError, "duplicate Domain Pack operation"):
            DomainPackConformanceSuite().bind(registry, duplicate_operation)

        missing_capability = DomainPackAuthorContract(
            name="missing-capability",
            version="1",
            operation="first_read",
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
            name="invalid-recovery-example",
            version="1",
            operation="fake_mutation",
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
            name="fake-read",
            version="1",
            operation="fake_read",
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
            name="fake-mutation",
            version="1",
            operation="fake_mutation",
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
            name="non-boolean-verifier",
            version="1",
            operation="fake_read",
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

    def test_injected_distinct_pack_extends_defaults_and_participates_in_a_real_workflow(self) -> None:
        class FullBackend(Backend):
            live_patch_run = Backend.debug_run

            @staticmethod
            def log_bundle_collect(_task, _arguments, _context) -> dict[str, object]:
                raise AssertionError("the injected log bundle Pack must execute")

            @staticmethod
            def debug_run(_task, arguments, _context) -> dict[str, object]:
                value: dict[str, object] = {
                    "ok": True,
                    "summary": "fake debug completed",
                    "target_epoch": 1,
                }
                if arguments.get("profile") == "freshness" or arguments.get(
                    "_minimum_target_epoch"
                ):
                    value["business_acceptance"] = "passed"
                return value

            debug_collect = debug_run

        calls: list[str] = []

        def pack_extensions(registry, _adapters):
            registry.require("log_bundle_collect")

            def execute(context, arguments):
                calls.append(context.operation_id)
                return DomainReceipt(
                    operation="log_bundle_collect",
                    status="succeeded",
                    value={
                        "ok": True,
                        "summary": "fake log bundle collected",
                        "bundle": arguments.get("problem", ""),
                    },
                )

            adapter = CallableDomainAdapter(execute)
            return (
                DomainPackAuthorContract(
                    name="fake-log-bundle",
                    version="1",
                    operation="log_bundle_collect",
                    effect_class=EffectClass.READ_ONLY,
                    adapter=adapter,
                    verifier=lambda _action, receipt: (
                        receipt.status == "succeeded"
                        and receipt.value.get("summary") == "fake log bundle collected"
                    ),
                    conformance_example=example(
                        "log_bundle_collect",
                        value={
                            "ok": True,
                            "summary": "fake log bundle collected",
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
                    "intent": "bundle-and-diagnose",
                    "purpose": "exercise a distinct fake Domain Pack",
                },
                task_id="fake-pack-workflow",
                operation_id="fake-pack-start",
            )
        finally:
            service.close()
            operator.close()

        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(registered, {"live_patch_run", "log_bundle_collect"})
        self.assertTrue(conformance["valid"])
        self.assertEqual(
            conformance["operations"],
            ["live_patch_run", "log_bundle_collect"],
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


if __name__ == "__main__":
    unittest.main()
