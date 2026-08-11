from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    AcceptancePlan,
    CaseNotFound,
    FilesystemBlobRepository,
    InMemoryBlobRepository,
    InMemoryRuntimeRepository,
    JsonRpcMcpEndpoint,
    OperationDescriptor,
    OrchestratedMcpBackend,
    PendingCaseEvent,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)


WORKFLOW_NEXT = OperationDescriptor(
    name="workflow.next",
    description="Advance one existing Case to its next external workflow gate.",
    input_schema={
        "type": "object",
        "properties": {
            "case_id": {"type": "string", "minLength": 1},
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 64},
            "include_closeout_bundle": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
)


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def open_task(task_id: str) -> FakeTask:
        return FakeTask(task_id)

    @staticmethod
    def close_task(_task: FakeTask) -> None:
        return None

    @staticmethod
    def maintain_task(_task: FakeTask) -> int:
        return 0

    @staticmethod
    def task_status(task: FakeTask) -> dict[str, object]:
        return {"task_id": task.task_id}

    def _result(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        self.calls.append((name, dict(arguments)))
        value: dict[str, object] = {
            "ok": True,
            "summary": f"{name} completed",
            "target_epoch": len(self.calls),
        }
        if name in {"live_patch_run", "upgrade_run"}:
            value["journal"] = {"stage": "verified"}
        if name == "debug_collect":
            value["profile"] = arguments.get("profile", "freshness")
        return value

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        del task
        context.raise_if_stopped()
        return self._result("debug_run", arguments)

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        del task
        context.raise_if_stopped()
        return self._result("debug_collect", arguments)

    def log_bundle_collect(self, task, arguments, context) -> dict[str, object]:
        del task
        context.raise_if_stopped()
        return self._result("log_bundle_collect", arguments)

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        del task
        context.raise_if_stopped()
        return self._result("live_patch_run", arguments)

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        del task
        context.raise_if_stopped()
        return self._result("upgrade_run", arguments)


class WorkflowNextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryRuntimeRepository()
        self.blobs = InMemoryBlobRepository()
        self.backend = RecordingBackend()
        self.service = RuntimeMcpService(
            self.backend,
            context_repository=self.repository,
            blob_repository=self.blobs,
        )

    def tearDown(self) -> None:
        self.service.close()

    def open_case(
        self,
        case_id: str,
        *,
        intent: str = "diagnose-and-fix",
        delivery_strategy: str = "source-only",
    ) -> dict[str, object]:
        arguments = {
            "case_id": case_id,
            "ip": "192.0.2.80",
            "intent": intent,
            "delivery_strategy": delivery_strategy,
            "final_purpose": "verify workflow.next",
        }
        return self.repository.commit(
            case_id,
            expected_revision=0,
            events=(
                PendingCaseEvent(
                    "CaseOpened",
                    {
                        "intent": intent,
                        "final_purpose": arguments["final_purpose"],
                        "change_boundary": "unit-test",
                        "delivery_strategy": delivery_strategy,
                        "acceptance_plan": AcceptancePlan.freeze(
                            arguments,
                            frozen_at=1.0,
                        ).to_public_dict(),
                        "targets": [
                            {
                                "target_id": "target-1",
                                "role": "candidate",
                                "address": arguments["ip"],
                            }
                        ],
                        "workflow_inputs": arguments,
                    },
                ),
            ),
        )

    def call_next(
        self,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
        invocations: list[str] | None = None,
    ):
        def invoke(
            operation: str,
            domain_arguments: Mapping[str, object],
            derived_id: str,
        ) -> Mapping[str, object]:
            if invocations is not None:
                invocations.append(operation)
            return self.service.call_tool(
                operation,
                domain_arguments,
                task_id=task_id,
                operation_id=derived_id,
                _context_workflow_step=True,
            )

        return self.service.context_runtime.workflow_next(
            WORKFLOW_NEXT,
            arguments,
            task_id=task_id,
            operation_id=operation_id,
            domain_invoker=invoke,
        )

    def test_existing_only_and_narrow_arguments_do_not_create_or_bind(self) -> None:
        before = self.repository.status()
        with self.assertRaises(CaseNotFound):
            self.call_next({}, task_id="unbound-task", operation_id="next-unbound")
        with self.assertRaises(CaseNotFound):
            self.call_next(
                {"case_id": "missing-case"},
                task_id="missing-task",
                operation_id="next-missing",
            )
        self.assertEqual(self.repository.status(), before)
        self.assertIsNone(self.repository.load("missing-case"))
        self.assertIsNone(self.repository.case_for_task("missing-task"))

        self.open_case("case-strict")
        strict_before = self.repository.load("case-strict")
        for forbidden in (
            {"case_id": "case-strict", "expected_revision": 1},
            {"case_id": "case-strict", "idempotency_key": "caller-key"},
            {"case_id": "case-strict", "delivery_strategy": "live-patch"},
            {"case_id": "case-strict", "authorized_exceptions": {"force_path": True}},
        ):
            with self.subTest(arguments=forbidden):
                with self.assertRaises(TypeError):
                    self.call_next(
                        forbidden,
                        task_id="strict-task",
                        operation_id="next-strict",
                    )
        self.assertEqual(self.repository.load("case-strict"), strict_before)
        self.assertIsNone(self.repository.case_for_task("strict-task"))

    def test_catalog_and_json_rpc_expose_the_strict_resume_contract(self) -> None:
        definition = next(
            item
            for item in self.service.tool_definitions()
            if item["name"] == "workflow.next"
        )
        schema = definition["inputSchema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]),
            {"case_id", "max_steps", "include_closeout_bundle"},
        )

        self.open_case("case-json-rpc")
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="json-rpc-next-task",
        )
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "workflow.next",
                    "arguments": {"case_id": "case-json-rpc"},
                },
            }
        )

        assert response is not None
        structured = response["result"]["structuredContent"]
        self.assertEqual(structured["status"], "waiting_phase_record")
        self.assertEqual(structured["required_skill"], "openubmc-developer")
        self.assertEqual(
            structured["handoff_arguments"]["phase_record_contract"]["phase_type"],
            "developer.change",
        )
        self.assertEqual(
            structured["agent_envelope"]["operation"]["name"],
            "workflow.next",
        )

    def test_build_gate_returns_a_ready_skill_handoff_without_user_reprompt(self) -> None:
        self.open_case("case-build-handoff", delivery_strategy="build-upgrade")
        first = self.call_next(
            {"case_id": "case-build-handoff"},
            task_id="build-handoff-task",
            operation_id="build-handoff-debug",
        )
        self.assertEqual(first["required_skill"], "openubmc-developer")
        current = self.repository.load("case-build-handoff")
        assert current is not None
        self.service.call_tool(
            "phase_record",
            {
                "case_id": "case-build-handoff",
                "expected_revision": current["revision"],
                "idempotency_key": "build-handoff-developer",
                "phase_type": "developer.change",
                "producer_identity": "openubmc-developer",
                "status": "completed",
                "source_revision": "source-revision",
                "summary": "source change completed",
                "authored_files": ["src/fix.lua"],
                "verification_plan": ["unit regression"],
            },
            task_id="build-handoff-task",
            operation_id="build-handoff-developer",
        )

        waiting = self.call_next(
            {},
            task_id="build-handoff-task",
            operation_id="build-handoff-next",
        )

        self.assertEqual(waiting["status"], "waiting_phase_record")
        self.assertEqual(waiting["required_phase_type"], "build.artifact")
        self.assertEqual(waiting["required_skill"], "openubmc-build")
        handoff = waiting["handoff_arguments"]
        self.assertEqual(handoff["case_id"], "case-build-handoff")
        self.assertEqual(handoff["delivery_strategy"], "build-upgrade")
        self.assertEqual(
            handoff["phase_record_contract"]["producer_identity"],
            "openubmc-build",
        )
        self.assertIn("developer.change", handoff["completed_phases"])

    def test_case_and_derived_domains_reuse_one_frozen_authorization_policy(self) -> None:
        self.service.call_tool(
            "debug_run",
            {
                "ip": "192.0.2.81",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "final_purpose": "verify frozen authorization",
                "authorized_exceptions": {"no_backup": True},
                "allow_insecure_tls": True,
                "workflow": {
                    "upgrade": {
                        "allow_insecure_tls": False,
                        "authorized_exceptions": {"no_backup": False},
                    }
                },
            },
            task_id="authorization-case-task",
            operation_id="authorization-debug",
        )
        case_id = self.repository.case_for_task("authorization-case-task")
        assert case_id is not None
        projection = self.repository.load(case_id)
        assert projection is not None

        self.assertEqual(projection["entry_domain"], "debug")
        self.assertEqual(projection["workflow_inputs"]["entry_domain"], "debug")
        policy = projection["authorization"]
        self.assertEqual(policy["allowed_actions"], ["upgrade"])
        self.assertTrue(policy["allow_insecure_tls"])
        self.assertTrue(policy["authorized_exceptions"]["no_backup"])

        derived = self.service.context_runtime._domain_arguments(
            projection,
            "upgrade_run",
            {},
        )
        self.assertTrue(derived["allow_insecure_tls"])
        self.assertTrue(derived["authorized_exceptions"]["no_backup"])
        self.assertEqual(derived["intent"], "diagnose-and-fix")
        self.assertEqual(derived["entry_domain"], "debug")
        self.assertEqual(derived["delivery_strategy"], "build-upgrade")

    def test_case_freezes_delivery_inferred_from_workflow_sections(self) -> None:
        for section, delivery_strategy, allowed_action in (
            ("live_patch", "live-patch", "live_patch"),
            ("build", "build-upgrade", "upgrade"),
        ):
            with self.subTest(section=section):
                task_id = f"workflow-delivery-{section}"
                self.service.call_tool(
                    "debug_run",
                    {
                        "ip": "192.0.2.85",
                        "intent": "diagnose-and-fix",
                        "final_purpose": "freeze the inferred delivery route",
                        "workflow": {
                            "developer": {},
                            section: {},
                        },
                    },
                    task_id=task_id,
                    operation_id=f"{task_id}-debug",
                )
                case_id = self.repository.case_for_task(task_id)
                assert case_id is not None
                projection = self.repository.load(case_id)
                assert projection is not None

                self.assertEqual(
                    projection["delivery_strategy"],
                    delivery_strategy,
                )
                self.assertEqual(
                    projection["authorization"]["allowed_actions"],
                    [allowed_action],
                )

    def test_new_task_explicit_case_preserves_legacy_entry_domain(self) -> None:
        for delivery_strategy, mutation_operation in (
            ("live-patch", "live_patch_run"),
            ("build-upgrade", "upgrade_run"),
        ):
            with self.subTest(delivery_strategy=delivery_strategy):
                repository = InMemoryRuntimeRepository()
                blobs = InMemoryBlobRepository()
                domain = RecordingBackend()
                backend = OrchestratedMcpBackend(
                    {
                        "debug_run": domain,
                        "debug_collect": domain,
                        "live_patch_run": domain,
                        "upgrade_run": domain,
                    }
                )
                service = RuntimeMcpService(
                    backend,
                    context_repository=repository,
                    blob_repository=blobs,
                )
                case_id = f"case-cross-task-{delivery_strategy}"
                opened_arguments = {
                    "case_id": case_id,
                    "ip": "192.0.2.83",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": delivery_strategy,
                    "final_purpose": "resume the original workflow in a new task",
                }
                repository.commit(
                    case_id,
                    expected_revision=0,
                    events=(
                        PendingCaseEvent(
                            "CaseOpened",
                            {
                                "intent": opened_arguments["intent"],
                                "final_purpose": opened_arguments["final_purpose"],
                                "change_boundary": "legacy-case",
                                "delivery_strategy": delivery_strategy,
                                "acceptance_plan": AcceptancePlan.freeze(
                                    opened_arguments,
                                    frozen_at=1.0,
                                ).to_public_dict(),
                                "targets": [
                                    {
                                        "target_id": "target-1",
                                        "role": "candidate",
                                        "address": opened_arguments["ip"],
                                    }
                                ],
                                "workflow_inputs": opened_arguments,
                            },
                        ),
                    ),
                )
                try:
                    waiting = service.call_tool(
                        "workflow.next",
                        {"case_id": case_id},
                        task_id="original-task",
                        operation_id=f"{case_id}-diagnose",
                    )
                    self.assertEqual(waiting["status"], "waiting_phase_record")

                    current = repository.load(case_id)
                    assert current is not None
                    service.call_tool(
                        "phase_record",
                        {
                            "case_id": case_id,
                            "expected_revision": current["revision"],
                            "idempotency_key": f"{case_id}-developer",
                            "phase_type": "developer.change",
                            "producer_identity": "openubmc-developer",
                            "status": "completed",
                            "source_revision": "source-revision",
                            "summary": "source change completed",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh verification"],
                            "artifact_path": "/tmp/fix.lua",
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                        task_id="original-task",
                        operation_id=f"{case_id}-developer",
                    )
                    if delivery_strategy == "build-upgrade":
                        current = repository.load(case_id)
                        assert current is not None
                        service.call_tool(
                            "phase_record",
                            {
                                "case_id": case_id,
                                "expected_revision": current["revision"],
                                "idempotency_key": f"{case_id}-build",
                                "phase_type": "build.artifact",
                                "producer_identity": "openubmc-build",
                                "status": "completed",
                                "source_revision": "source-revision",
                                "summary": "firmware artifact completed",
                                "artifact_path": "/tmp/product.hpm",
                                "artifact_sha256": "a" * 64,
                                "product_version": "1.2.3",
                            },
                            task_id="original-task",
                            operation_id=f"{case_id}-build",
                        )

                    completed = service.call_tool(
                        "workflow.next",
                        {"case_id": case_id},
                        task_id="new-task",
                        operation_id=f"{case_id}-resume",
                    )
                    self.assertTrue(completed["completed"])
                    self.assertEqual(
                        [name for name, _arguments in domain.calls],
                        ["debug_run", mutation_operation, "debug_collect"],
                    )
                    projected = repository.load(case_id)
                    assert projected is not None
                    self.assertEqual(projected["entry_domain"], "debug")
                    mutation_arguments = next(
                        arguments
                        for name, arguments in domain.calls
                        if name == mutation_operation
                    )
                    self.assertEqual(
                        mutation_arguments["_task_authorization_policy"][
                            "original_intent"
                        ],
                        "diagnose-and-fix",
                    )
                finally:
                    service.close()

    def test_semantic_cursor_replays_one_gate_and_changes_after_progress(self) -> None:
        self.open_case("case-cursor")
        first = self.call_next(
            {"case_id": "case-cursor"},
            task_id="cursor-task",
            operation_id="next-first",
        )
        self.assertEqual(first["status"], "waiting_phase_record")
        self.assertEqual([name for name, _ in self.backend.calls], ["debug_run"])
        after_first = self.repository.load("case-cursor")
        assert after_first is not None
        first_next = [
            item
            for item in after_first["operations"]
            if item["operation"] == "workflow.next"
        ]
        self.assertEqual(len(first_next), 1)

        second = self.call_next(
            {},
            task_id="cursor-task",
            operation_id="next-second",
        )
        self.assertEqual(second["status"], "waiting_phase_record")
        after_second = self.repository.load("case-cursor")
        assert after_second is not None
        second_next = [
            item
            for item in after_second["operations"]
            if item["operation"] == "workflow.next"
        ]
        self.assertEqual(len(second_next), 2)
        self.assertNotIn(
            "workflow.next",
            self.service.context_runtime._operation_records(after_second),
        )
        self.assertNotEqual(
            first_next[0]["idempotency_key"],
            second_next[1]["idempotency_key"],
        )

        replay = self.call_next(
            {},
            task_id="cursor-task",
            operation_id="next-replay",
        )
        self.assertEqual(replay["status"], "waiting_phase_record")
        after_replay = self.repository.load("case-cursor")
        self.assertEqual(after_replay, after_second)
        self.assertEqual([name for name, _ in self.backend.calls], ["debug_run"])

        current = self.repository.load("case-cursor")
        assert current is not None
        self.service.call_tool(
            "phase_record",
            {
                "case_id": "case-cursor",
                "expected_revision": current["revision"],
                "idempotency_key": "developer-result",
                "phase_type": "developer.change",
                "producer_identity": "openubmc-developer",
                "status": "completed",
                "source_revision": "abc123",
                "summary": "source fix completed",
                "authored_files": ["src/unit.lua"],
                "verification_plan": ["unit regression"],
            },
            task_id="cursor-task",
            operation_id="developer-result",
        )
        final = self.call_next(
            {},
            task_id="cursor-task",
            operation_id="next-final",
        )
        self.assertTrue(final["completed"])
        final_case = self.repository.load("case-cursor")
        assert final_case is not None
        final_next = [
            item
            for item in final_case["operations"]
            if item["operation"] == "workflow.next"
        ]
        self.assertEqual(len(final_next), 3)
        self.assertNotEqual(
            second_next[1]["idempotency_key"],
            final_next[2]["idempotency_key"],
        )

    def test_explicit_case_replay_binds_the_new_task(self) -> None:
        self.open_case("case-replay-binding")
        self.call_next(
            {"case_id": "case-replay-binding"},
            task_id="binding-owner",
            operation_id="binding-first",
        )
        waiting = self.call_next(
            {},
            task_id="binding-owner",
            operation_id="binding-gate",
        )
        self.assertEqual(waiting["status"], "waiting_phase_record")
        before_replay = self.repository.load("case-replay-binding")

        replay = self.call_next(
            {"case_id": "case-replay-binding"},
            task_id="binding-reader",
            operation_id="binding-replay",
        )

        self.assertEqual(replay["status"], "waiting_phase_record")
        self.assertEqual(
            self.repository.case_for_task("binding-reader"),
            "case-replay-binding",
        )
        self.assertEqual(
            self.repository.load("case-replay-binding"),
            before_replay,
        )
        continued = self.call_next(
            {},
            task_id="binding-reader",
            operation_id="binding-bare-continue",
        )
        self.assertEqual(continued["status"], "waiting_phase_record")

    def test_legacy_diagnosis_only_case_recovers_domain_from_operations(self) -> None:
        cases = (
            ("log_bundle_collect", "log_analyzer", []),
            ("live_patch_run", "live_patch", ["debug_collect"]),
            ("upgrade_run", "upgrade", ["debug_collect"]),
        )
        for recorded_operation, entry_domain, expected_calls in cases:
            with self.subTest(recorded_operation=recorded_operation):
                repository = InMemoryRuntimeRepository()
                domain = RecordingBackend()
                service = RuntimeMcpService(
                    OrchestratedMcpBackend(
                        {
                            "debug_run": domain,
                            "debug_collect": domain,
                            "log_bundle_collect": domain,
                            "live_patch_run": domain,
                            "upgrade_run": domain,
                        }
                    ),
                    context_repository=repository,
                    blob_repository=InMemoryBlobRepository(),
                )
                case_id = f"legacy-{recorded_operation}"
                plan_arguments = {
                    "intent": "diagnosis-only",
                    "entry_domain": entry_domain,
                    "final_purpose": "resume a legacy Case safely",
                }
                operation_id = f"legacy-operation-{recorded_operation}"
                repository.commit(
                    case_id,
                    expected_revision=0,
                    events=(
                        PendingCaseEvent(
                            "CaseOpened",
                            {
                                "intent": "diagnosis-only",
                                "final_purpose": plan_arguments["final_purpose"],
                                "change_boundary": "legacy-case",
                                "delivery_strategy": "",
                                "acceptance_plan": AcceptancePlan.freeze(
                                    plan_arguments,
                                    frozen_at=1.0,
                                ).to_public_dict(),
                                "targets": [
                                    {
                                        "target_id": "target-1",
                                        "role": "candidate",
                                        "address": "192.0.2.84",
                                    }
                                ],
                                "workflow_inputs": {
                                    "intent": "diagnosis-only",
                                    "ip": "192.0.2.84",
                                    "final_purpose": plan_arguments[
                                        "final_purpose"
                                    ],
                                },
                            },
                        ),
                        PendingCaseEvent(
                            "OperationAccepted",
                            {
                                "operation": recorded_operation,
                                "idempotency_key": operation_id,
                                "request_fingerprint": operation_id,
                            },
                            operation_id,
                        ),
                        PendingCaseEvent(
                            "OperationStarted",
                            {},
                            operation_id,
                        ),
                        PendingCaseEvent(
                            "OperationTerminal",
                            {
                                "status": "completed",
                                "summary": f"{recorded_operation} completed",
                                "case_status": "open",
                            },
                            operation_id,
                        ),
                    ),
                )
                try:
                    projected = repository.load(case_id)
                    assert projected is not None
                    self.assertEqual(projected["entry_domain"], entry_domain)
                    derived = service.context_runtime._domain_arguments(
                        projected,
                        "debug_collect",
                        {},
                    )
                    expected_intent = {
                        "log_bundle_collect": "diagnosis-only",
                        "live_patch_run": "live-patch",
                        "upgrade_run": "upgrade-and-verify",
                    }[recorded_operation]
                    self.assertEqual(derived["intent"], expected_intent)

                    result = service.call_tool(
                        "workflow.next",
                        {"case_id": case_id},
                        task_id=f"reader-{recorded_operation}",
                        operation_id=f"resume-{recorded_operation}",
                    )
                    self.assertTrue(result["completed"])
                    self.assertEqual(
                        [name for name, _arguments in domain.calls],
                        expected_calls,
                    )
                finally:
                    service.close()

    def test_sqlite_restart_reuses_task_binding_and_gate_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = SQLiteRuntimeRepository(root / "context.sqlite3")
            blobs = FilesystemBlobRepository(root / "blobs")
            case_id = "case-restart-next"
            arguments = {
                "case_id": case_id,
                "ip": "192.0.2.82",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "final_purpose": "resume after MCP restart",
            }
            repository.commit(
                case_id,
                expected_revision=0,
                events=(
                    PendingCaseEvent(
                        "CaseOpened",
                        {
                            "intent": arguments["intent"],
                            "final_purpose": arguments["final_purpose"],
                            "change_boundary": "restart-test",
                            "delivery_strategy": arguments[
                                "delivery_strategy"
                            ],
                            "acceptance_plan": AcceptancePlan.freeze(
                                arguments,
                                frozen_at=1.0,
                            ).to_public_dict(),
                            "targets": [
                                {
                                    "target_id": "target-1",
                                    "role": "candidate",
                                    "address": arguments["ip"],
                                }
                            ],
                            "workflow_inputs": arguments,
                        },
                    ),
                ),
            )
            first_backend = RecordingBackend()
            first_service = RuntimeMcpService(
                first_backend,
                context_repository=repository,
                blob_repository=blobs,
            )
            try:
                first_service.call_tool(
                    "workflow.next",
                    {"case_id": case_id},
                    task_id="restart-next-task",
                    operation_id="restart-next-first",
                )
                waiting = first_service.call_tool(
                    "workflow.next",
                    {},
                    task_id="restart-next-task",
                    operation_id="restart-next-gate",
                )
                self.assertEqual(waiting["status"], "waiting_phase_record")
                before_restart = repository.load(case_id)
            finally:
                first_service.close()

            second_backend = RecordingBackend()
            second_repository = SQLiteRuntimeRepository(
                root / "context.sqlite3"
            )
            second_service = RuntimeMcpService(
                second_backend,
                context_repository=second_repository,
                blob_repository=FilesystemBlobRepository(root / "blobs"),
            )
            try:
                replay = second_service.call_tool(
                    "workflow.next",
                    {},
                    task_id="restart-next-task",
                    operation_id="restart-next-replay",
                )
            finally:
                second_service.close()

            self.assertEqual(replay["status"], "waiting_phase_record")
            self.assertEqual(second_backend.calls, [])
            self.assertEqual(second_repository.load(case_id), before_restart)

    def test_terminal_and_closed_results_are_persistent_zero_write_replays(self) -> None:
        self.open_case("case-terminal")
        waiting = self.call_next(
            {"case_id": "case-terminal"},
            task_id="terminal-owner",
            operation_id="next-waiting",
        )
        self.assertEqual(waiting["status"], "waiting_phase_record")
        current = self.repository.load("case-terminal")
        assert current is not None
        self.service.call_tool(
            "phase_record",
            {
                "case_id": "case-terminal",
                "expected_revision": current["revision"],
                "idempotency_key": "terminal-development",
                "phase_type": "developer.change",
                "producer_identity": "openubmc-developer",
                "status": "completed",
                "source_revision": "def456",
                "summary": "terminal source fix",
                "authored_files": ["src/fix.lua"],
                "verification_plan": ["unit regression"],
            },
            task_id="terminal-owner",
            operation_id="terminal-development",
        )
        completed = self.call_next(
            {},
            task_id="terminal-owner",
            operation_id="next-completed",
        )
        self.assertTrue(completed["completed"])

        terminal_before = self.repository.load("case-terminal")
        terminal_status_before = self.repository.status()
        terminal_blob_bytes = self.blobs.size_bytes()
        first_replay = self.call_next(
            {"case_id": "case-terminal"},
            task_id="terminal-reader",
            operation_id="terminal-replay-one",
        )
        second_replay = self.call_next(
            {"case_id": "case-terminal"},
            task_id="terminal-reader",
            operation_id="terminal-replay-two",
        )
        self.assertTrue(first_replay["terminal_replay"])
        self.assertTrue(second_replay["terminal_replay"])
        self.assertEqual(self.repository.load("case-terminal"), terminal_before)
        self.assertEqual(self.repository.status(), terminal_status_before)
        self.assertEqual(self.blobs.size_bytes(), terminal_blob_bytes)
        self.assertIsNone(self.repository.case_for_task("terminal-reader"))

        assert terminal_before is not None
        self.service.context_runtime.close_case(
            "case-terminal",
            expected_revision=int(terminal_before["revision"]),
        )
        closed_before = self.repository.load("case-terminal")
        closed_status_before = self.repository.status()
        closed_blob_bytes = self.blobs.size_bytes()
        closed = self.call_next(
            {"case_id": "case-terminal"},
            task_id="closed-reader",
            operation_id="closed-replay",
        )
        self.assertEqual(closed["case_status"], "closed")
        self.assertTrue(closed["terminal_replay"])
        self.assertEqual(self.repository.load("case-terminal"), closed_before)
        self.assertEqual(self.repository.status(), closed_status_before)
        self.assertEqual(self.blobs.size_bytes(), closed_blob_bytes)
        self.assertIsNone(self.repository.case_for_task("closed-reader"))

    def test_blocked_and_unknown_gates_never_invoke_domain_or_gain_authority(self) -> None:
        for status in ("blocked", "mutation_outcome_unknown"):
            with self.subTest(status=status):
                case_id = f"case-{status}"
                self.open_case(
                    case_id,
                    intent="upgrade-and-verify",
                    delivery_strategy="build-upgrade",
                )
                current = self.repository.load(case_id)
                assert current is not None
                self.repository.commit(
                    case_id,
                    expected_revision=int(current["revision"]),
                    events=(
                        PendingCaseEvent(
                            "OperationAccepted",
                            {
                                "operation": "upgrade_run",
                                "idempotency_key": f"upgrade-{status}",
                                "request_fingerprint": f"fingerprint-{status}",
                            },
                            f"upgrade-{status}",
                        ),
                        PendingCaseEvent(
                            "OperationStarted",
                            {},
                            f"upgrade-{status}",
                        ),
                        PendingCaseEvent(
                            "OperationTerminal",
                            {
                                "status": status,
                                "summary": status,
                                "case_status": "open",
                            },
                            f"upgrade-{status}",
                        ),
                    ),
                )
                invocations: list[str] = []
                first = self.call_next(
                    {"case_id": case_id},
                    task_id=f"task-{status}",
                    operation_id=f"next-{status}-one",
                    invocations=invocations,
                )
                self.assertEqual(first["status"], status)
                self.assertEqual(invocations, [])
                after_first = self.repository.load(case_id)
                second = self.call_next(
                    {},
                    task_id=f"task-{status}",
                    operation_id=f"next-{status}-two",
                    invocations=invocations,
                )
                self.assertEqual(second["status"], status)
                self.assertEqual(invocations, [])
                self.assertEqual(self.repository.load(case_id), after_first)


if __name__ == "__main__":
    unittest.main()
