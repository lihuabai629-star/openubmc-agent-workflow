from __future__ import annotations

from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    ArtifactContract,
    CallableDomainAdapter,
    CapabilityDescriptor,
    CapabilityRegistry,
    DomainExecutor,
    DomainPack,
    DomainReceipt,
    EffectClass,
    EffectRecoveryMode,
    RuntimeMcpService,
    RuntimeSDKContext,
)


def descriptor(operation: str = "fake_mutation") -> CapabilityDescriptor:
    return CapabilityDescriptor(
        operation=operation,
        capability=f"test.{operation}",
        owner_skill="test-domain-pack",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        timeout_seconds=10,
        evidence_types=("test-result",),
        mutation=True,
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
        }

        executed = executor.execute(
            "fake_mutation", context=context, arguments=arguments
        )
        replay_identity = pack.action(context, arguments).effect_id
        recovered = executor.reconcile(
            "fake_mutation", context=context, arguments=arguments
        )

        self.assertEqual(executed.action.effect_id, replay_identity)
        self.assertEqual(executed.action.artifact.sha256, "a" * 64)
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
            executed.to_public_dict()["action"]["artifact"]["version"],
            "1.0.0",
        )

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

        service = RuntimeMcpService(FullBackend())
        try:
            packs = {
                item["operation"]: item
                for item in service.domain_executor.pack_descriptors()
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


if __name__ == "__main__":
    unittest.main()
