from __future__ import annotations

from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CallableDomainAdapter,
    CapabilityDescriptor,
    CapabilityRegistry,
    DomainExecutor,
    DomainReceipt,
    EffectClass,
    OperationCatalogError,
    RUNTIME_API_VERSION,
    RuntimeMcpService,
    RuntimeSDK,
    RuntimeSDKContext,
)


def _descriptor(operation: str = "test_probe", **overrides) -> CapabilityDescriptor:
    values = {
        "operation": operation,
        "capability": "test.probe",
        "owner_skill": "test-skill",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object", "additionalProperties": True},
        "timeout_seconds": 10.0,
        "evidence_types": ("test-observation",),
    }
    values.update(overrides)
    return CapabilityDescriptor(**values)


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _Backend:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def open_task(task_id: str) -> _Task:
        return _Task(task_id)

    @staticmethod
    def close_task(_task: _Task) -> None:
        return None

    @staticmethod
    def maintain_task(_task: _Task) -> int:
        return 0

    @staticmethod
    def task_status(task: _Task) -> dict[str, object]:
        return {"task_id": task.task_id}

    def debug_run(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls += 1
        return {
            "ok": True,
            "profile": arguments.get("profile", "standard"),
            "root_cause": "the requested diagnostic capability is available",
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {"status": "fresh"},
        }

    def debug_collect(self, _task, arguments, context) -> dict[str, object]:
        return self.debug_run(_task, arguments, context)


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_rejects_missing_schema_conflicts_and_runtime_mismatch(self) -> None:
        with self.assertRaisesRegex(OperationCatalogError, "non-empty"):
            _descriptor(input_schema={})
        descriptor = _descriptor()
        with self.assertRaisesRegex(OperationCatalogError, "duplicate"):
            CapabilityRegistry((descriptor, descriptor))
        with self.assertRaisesRegex(OperationCatalogError, "incompatible"):
            _descriptor(runtime_api_version="openubmc.target-runtime.v0")

    def test_new_domain_executes_through_the_sdk_without_kernel_changes(self) -> None:
        sdk = RuntimeSDK(CapabilityRegistry((_descriptor(),)))
        receipt = sdk.execute(
            "test_probe",
            context=RuntimeSDKContext(
                task_id="test-task",
                operation_id="test-operation",
                timeout_seconds=5,
            ),
            arguments={"value": 42},
            adapter=CallableDomainAdapter(
                lambda _context, arguments: DomainReceipt(
                    operation="test_probe",
                    status="succeeded",
                    value={"observed": arguments["value"]},
                    evidence_ids=("evidence-test",),
                )
            ),
        )

        self.assertEqual(receipt.value["observed"], 42)
        self.assertEqual(receipt.evidence_ids, ("evidence-test",))

    def test_debug_entry_is_declared_and_routed_behind_execute(self) -> None:
        backend = _Backend()
        service = RuntimeMcpService(backend)
        operator = RuntimeMcpService(backend, interface_profile="operator")
        try:
            result = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.75",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {"profile": "mdb"},
                },
                task_id="registry-debug",
                operation_id="registry-debug",
            )
            status = operator.call_exposed_tool(
                "runtime_status",
                {},
                task_id="registry-debug",
                operation_id="registry-status",
            )
        finally:
            service.close()
            operator.close()

        descriptor = next(
            item
            for item in status["capability_registry"]["capabilities"]
            if item["operation"] == "debug_run"
        )
        self.assertEqual(descriptor["owner_skill"], "openubmc-debug")
        self.assertEqual(descriptor["runtime_api_version"], RUNTIME_API_VERSION)
        self.assertEqual(result["state"], "waiting_response")
        self.assertEqual(result["gate"]["name"], "diagnosis.acceptance")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(descriptor["capability"], "openubmc.debug.diagnose")

    def test_domain_executor_retries_transient_read_failures(self) -> None:
        attempts = 0

        def transient_read(_context, arguments):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary transport failure")
            return {"ok": True, "value": arguments["value"]}

        registry = CapabilityRegistry((_descriptor(),))
        executor = DomainExecutor(
            registry,
            {"test_probe": CallableDomainAdapter(transient_read)},
            read_attempts=2,
        )

        receipt = executor.execute(
            "test_probe",
            context=RuntimeSDKContext(
                task_id="read-retry",
                operation_id="read-retry-1",
                timeout_seconds=5,
            ),
            arguments={"value": 42},
        )

        self.assertEqual(receipt.value["value"], 42)
        self.assertEqual(attempts, 2)
        self.assertEqual(
            executor.policy_for("test_probe").effect_class,
            EffectClass.READ_ONLY,
        )

    def test_domain_executor_never_blindly_retries_a_mutation(self) -> None:
        attempts = 0

        def interrupted_mutation(_context, _arguments):
            nonlocal attempts
            attempts += 1
            raise TimeoutError("mutation result is unknown")

        descriptor = _descriptor(operation="test_mutation", mutation=True)
        executor = DomainExecutor(
            CapabilityRegistry((descriptor,)),
            {"test_mutation": CallableDomainAdapter(interrupted_mutation)},
            read_attempts=3,
        )

        with self.assertRaises(TimeoutError):
            executor.execute(
                "test_mutation",
                context=RuntimeSDKContext(
                    task_id="mutation-once",
                    operation_id="mutation-once-1",
                    timeout_seconds=5,
                ),
                arguments={"value": 42},
            )

        self.assertEqual(attempts, 1)
        policy = executor.policy_for("test_mutation")
        self.assertEqual(policy.effect_class, EffectClass.RECONCILABLE_MUTATION)
        self.assertEqual(policy.max_attempts, 1)


if __name__ == "__main__":
    unittest.main()
