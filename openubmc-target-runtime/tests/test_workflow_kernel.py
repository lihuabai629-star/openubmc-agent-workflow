from __future__ import annotations

from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.workflow import (  # noqa: E402
    DEFAULT_PHASE_REGISTRY,
    DEFAULT_WORKFLOW_KERNEL,
    PhaseDescriptor,
    PhaseRegistry,
    StepIdentity,
    WorkflowDefinition,
    WorkflowKernel,
    WorkflowRegistry,
)


class WorkflowKernelTests(unittest.TestCase):
    def test_narrow_debug_collect_is_one_versioned_step(self) -> None:
        projection = {
            "intent": "diagnosis-only",
            "entry_domain": "debug",
            "entry_operation": "debug_collect",
            "workflow_cycle_id": "cycle-1",
            "target_version": 1,
        }

        definition = DEFAULT_WORKFLOW_KERNEL.definition_for(projection)

        self.assertEqual(definition.version, 1)
        self.assertEqual(definition.definition_id, "diagnosis-only.debug-collect")
        self.assertEqual(
            [(step.kind, step.name, step.owner) for step in definition.steps],
            [("operation", "debug_collect", "openubmc-debug")],
        )
        self.assertEqual(
            WorkflowDefinition.from_public_dict(
                definition.to_public_dict()
            ).fingerprint,
            definition.fingerprint,
        )

    def test_source_only_definition_uses_typed_phase_contract(self) -> None:
        definition = DEFAULT_WORKFLOW_KERNEL.definition_for(
            {
                "intent": "diagnose-and-fix",
                "entry_domain": "debug",
                "entry_operation": "debug_run",
                "delivery_strategy": "source-only",
            }
        )

        self.assertEqual(
            [(step.kind, step.name) for step in definition.steps],
            [
                ("operation", "debug_run"),
                ("phase", "developer.change"),
            ],
        )
        phase = definition.steps[1]
        self.assertEqual(phase.owner, "openubmc-developer")
        self.assertTrue(phase.receipt_schema.endswith("developer-change-receipt-v1"))

    def test_recorded_definition_is_pinned_and_tamper_evident(self) -> None:
        current = DEFAULT_WORKFLOW_KERNEL.definition_for(
            {
                "intent": "diagnosis-only",
                "entry_domain": "debug",
                "entry_operation": "debug_collect",
            }
        ).to_public_dict()
        projection = {
            "intent": "diagnose-and-fix",
            "delivery_strategy": "build-upgrade",
            "workflow_definition": current,
        }

        pinned = DEFAULT_WORKFLOW_KERNEL.definition_for(projection)
        self.assertEqual([step.name for step in pinned.steps], ["debug_collect"])

        current["steps"][0]["name"] = "debug_run"
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            DEFAULT_WORKFLOW_KERNEL.definition_for(projection)

    def test_step_identity_changes_for_input_workflow_attempt_and_epoch(self) -> None:
        projection = {
            "intent": "diagnosis-only",
            "entry_domain": "debug",
            "entry_operation": "debug_collect",
            "workflow_cycle_id": "cycle-1",
            "target_version": 1,
        }
        step = DEFAULT_WORKFLOW_KERNEL.definition_for(projection).steps[0]
        base = DEFAULT_WORKFLOW_KERNEL.step_identity(
            projection,
            step=step,
            attempt=1,
            input_fingerprint="a" * 64,
            target_epoch=1,
        )
        identities = {
            base.execution_id,
            DEFAULT_WORKFLOW_KERNEL.step_identity(
                projection,
                step=step,
                attempt=1,
                input_fingerprint="b" * 64,
                target_epoch=1,
            ).execution_id,
            DEFAULT_WORKFLOW_KERNEL.step_identity(
                projection,
                step=step,
                attempt=2,
                input_fingerprint="a" * 64,
                target_epoch=1,
            ).execution_id,
            DEFAULT_WORKFLOW_KERNEL.step_identity(
                projection,
                step=step,
                attempt=1,
                input_fingerprint="a" * 64,
                target_epoch=2,
            ).execution_id,
        }
        self.assertEqual(len(identities), 4)
        self.assertEqual(
            StepIdentity(**base.to_public_dict()).execution_id,
            base.execution_id,
        )

    def test_phase_registry_rejects_conflicts_and_invalid_receipts(self) -> None:
        descriptor = DEFAULT_PHASE_REGISTRY.require("developer.change")
        receipt = {
            "source_revision": "abc123",
            "summary": "fixed",
            "authored_files": ["src/fix.lua"],
            "verification_plan": ["unit"],
        }
        self.assertEqual(
            DEFAULT_PHASE_REGISTRY.validate_receipt(
                "developer.change",
                producer="openubmc-developer",
                receipt=receipt,
            ),
            descriptor,
        )
        with self.assertRaisesRegex(ValueError, "producer_identity"):
            DEFAULT_PHASE_REGISTRY.validate_receipt(
                "developer.change",
                producer="openubmc-build",
                receipt=receipt,
            )
        with self.assertRaisesRegex(ValueError, "verification_plan"):
            DEFAULT_PHASE_REGISTRY.validate_receipt(
                "developer.change",
                producer="openubmc-developer",
                receipt={**receipt, "verification_plan": []},
            )
        with self.assertRaisesRegex(ValueError, "duplicate phase"):
            PhaseRegistry((descriptor, descriptor))

    def test_test_domain_registers_without_kernel_changes(self) -> None:
        phases = PhaseRegistry(
            (
                PhaseDescriptor(
                    "test.review",
                    "test-skill",
                    "test/review-receipt-v1",
                    ("summary",),
                ),
            )
        )
        registry = WorkflowRegistry(
            phases=phases,
            operation_owners={"debug_run": "test-debug"},
        )
        kernel = WorkflowKernel(registry)

        definition = registry.resolve(
            intent="diagnosis-only",
            entry_domain="debug",
            entry_operation="debug_run",
        )

        self.assertEqual(kernel.definition_for(definition.to_public_dict()).steps[0].owner, "test-debug")


if __name__ == "__main__":
    unittest.main()
