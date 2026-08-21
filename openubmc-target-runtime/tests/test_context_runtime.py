from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    AGENT_ENVELOPE_MAX_BYTES,
    EffectClass,
    FilesystemBlobRepository,
    IdempotencyConflict,
    InMemoryBlobRepository,
    InMemoryRuntimeRepository,
    JsonRpcMcpEndpoint,
    MutationOutcomeUnknown,
    MutationAuthorizationDenied,
    OperationAlreadyInProgress,
    PendingCaseEvent,
    RevisionConflict,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)
from openubmc_target_runtime.effect_runner import (  # noqa: E402
    EffectIntent,
    EffectSettlementMode,
)


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.closed = False


class FullFakeBackend:
    def __init__(self, *, large_bytes: int = 0) -> None:
        self.created: list[FakeTask] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.large_bytes = large_bytes

    def open_task(self, task_id: str) -> FakeTask:
        task = FakeTask(task_id)
        self.created.append(task)
        return task

    @staticmethod
    def close_task(task: FakeTask) -> None:
        task.closed = True

    @staticmethod
    def maintain_task(_task: FakeTask) -> int:
        return 0

    @staticmethod
    def task_status(task: FakeTask) -> dict[str, object]:
        return {"task_id": task.task_id, "closed": task.closed}

    def _result(self, name: str, task: FakeTask, arguments) -> dict[str, object]:
        captured = dict(arguments)
        self.calls.append((name, captured))
        value: dict[str, object] = {
            "ok": True,
            "schema": f"test/{name}",
            "task": task.task_id,
            "ip": captured.get("ip"),
        }
        if self.large_bytes:
            value["raw"] = "x" * self.large_bytes
        return value

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return self._result("debug_run", task, arguments)

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._result("debug_collect", task, arguments)
        value["profile"] = arguments.get("profile", "standard")
        minimum_epoch = arguments.get("_minimum_target_epoch")
        if isinstance(minimum_epoch, int) and not isinstance(minimum_epoch, bool):
            value["target_epoch"] = minimum_epoch
        return value

    def log_bundle_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return self._result("log_bundle_collect", task, arguments)

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._result("live_patch_run", task, arguments)
        value["journal"] = {"stage": "verified"}
        value["target_epoch"] = 2
        return value

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._result("upgrade_run", task, arguments)
        value["journal"] = {"stage": "verified"}
        value["target_epoch"] = 3
        return value


class FailingBindRepository(InMemoryRuntimeRepository):
    def bind_task(self, task_id: str, case_id: str) -> None:
        raise OSError("shadow repository unavailable")


class FailingBlobRepository(InMemoryBlobRepository):
    def put(self, body: bytes) -> str:
        raise OSError("blob repository unavailable")


class FailTerminalCommitRepository(InMemoryRuntimeRepository):
    def commit(self, case_id, *, expected_revision, events):
        pending = tuple(events)
        if any(item.kind in {"OperationTerminal", "OperationReconciled"} for item in pending):
            raise OSError("terminal commit unavailable")
        return super().commit(
            case_id,
            expected_revision=expected_revision,
            events=pending,
        )


class FailOnceReceiptRepository(InMemoryRuntimeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_receipt = True

    def complete_idempotency(self, case_id, key, receipt) -> None:
        if self.fail_next_receipt:
            self.fail_next_receipt = False
            raise OSError("receipt completion interrupted")
        super().complete_idempotency(case_id, key, receipt)


class FailOnceUpgradeBackend(FullFakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.upgrade_attempts = 0

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        self.upgrade_attempts += 1
        if self.upgrade_attempts == 1:
            raise OSError("upload connection lost")
        return super().upgrade_run(task, arguments, context)


class ExplodingDebugBackend(FullFakeBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        raise ValueError("failure-" + "x" * (1024 * 1024))


class RepositoryContractTests(unittest.TestCase):
    def repositories(self):
        yield InMemoryRuntimeRepository()
        with tempfile.TemporaryDirectory() as raw:
            yield SQLiteRuntimeRepository(Path(raw) / "runtime.sqlite3")

    def test_append_revision_projection_and_conflict(self) -> None:
        for repository in self.repositories():
            with self.subTest(adapter=repository.status()["adapter"]):
                opened = repository.commit(
                    "case-a",
                    expected_revision=0,
                    events=(
                        PendingCaseEvent(
                            "CaseOpened",
                            {
                                "intent": "diagnosis-only",
                                "final_purpose": "diagnose",
                                "targets": [],
                            },
                        ),
                    ),
                )
                self.assertEqual(opened["revision"], 1)
                completed = repository.commit(
                    "case-a",
                    expected_revision=1,
                    events=(
                        PendingCaseEvent(
                            "OperationAccepted",
                            {"operation": "debug_run", "idempotency_key": "one"},
                            "op-one",
                        ),
                        PendingCaseEvent("OperationStarted", {}, "op-one"),
                        PendingCaseEvent(
                            "OperationTerminal",
                            {"status": "completed", "summary": "done"},
                            "op-one",
                        ),
                    ),
                )
                self.assertEqual(completed["revision"], 4)
                self.assertEqual(len(completed["operations"]), 1)
                self.assertEqual(completed["operations"][0]["status"], "completed")
                with self.assertRaises(RevisionConflict):
                    repository.commit(
                        "case-a",
                        expected_revision=1,
                        events=(PendingCaseEvent("CaseClosed", {}),),
                    )

    def test_idempotency_claim_and_conflict(self) -> None:
        for repository in self.repositories():
            with self.subTest(adapter=repository.status()["adapter"]):
                self.assertIsNone(
                    repository.claim_idempotency("case-a", "same", "fingerprint-a")
                )
                repository.complete_idempotency(
                    "case-a",
                    "same",
                    {"envelope": {"status": "completed"}, "legacy_value": {"ok": True}},
                )
                replay = repository.claim_idempotency(
                    "case-a", "same", "fingerprint-a"
                )
                self.assertEqual(replay["legacy_value"], {"ok": True})
                with self.assertRaises(IdempotencyConflict):
                    repository.claim_idempotency(
                        "case-a", "same", "fingerprint-b"
                    )


class BlobRepositoryContractTests(unittest.TestCase):
    def test_content_addressing_range_and_hash(self) -> None:
        body = json.dumps({"value": "evidence"}).encode("utf-8")
        repositories = [InMemoryBlobRepository()]
        with tempfile.TemporaryDirectory() as raw:
            repositories.append(FilesystemBlobRepository(Path(raw)))
            for repository in repositories:
                with self.subTest(adapter=type(repository).__name__):
                    first = repository.put(body)
                    second = repository.put(body)
                    self.assertEqual(first, second)
                    self.assertEqual(first, hashlib.sha256(body).hexdigest())
                    self.assertEqual(repository.read(first, offset=2, limit=5), body[2:7])
                    self.assertGreater(repository.size_bytes(), 0)


class ContextRuntimeIntegrationTests(unittest.TestCase):
    def test_legacy_domain_invocation_requires_an_observed_target_epoch(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        try:
            result = service.context_runtime.invoke_domain(
                service.catalog.require("debug_collect"),
                {
                    "ip": "192.0.2.70",
                    "_minimum_target_epoch": 4,
                },
                task_id="legacy-missing-target-epoch",
                operation_id="legacy-missing-target-epoch-collect",
                executor=lambda: {
                    "ok": True,
                    "business_acceptance": "passed",
                },
            )
        finally:
            service.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result.envelope["status"], "failed")
        self.assertIn(
            "fresh verification did not report the required target epoch",
            result["error"],
        )
        self.assertNotIn("target_epoch", result)

    def test_legacy_domain_invocation_uses_the_selected_targets_epoch(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        try:
            result = service.context_runtime.invoke_domain(
                service.catalog.require("debug_collect"),
                {
                    "ip": "192.0.2.71",
                    "target_id": "candidate",
                    "_minimum_target_epoch": 4,
                },
                task_id="legacy-target-scoped-epoch",
                operation_id="legacy-target-scoped-epoch-collect",
                executor=lambda: {
                    "ok": True,
                    "business_acceptance": "passed",
                    "result": {
                        "runtime": {
                            "status": {
                                "targets": [
                                    {
                                        "target_id": "reference",
                                        "epochs": {"target_epoch": 9},
                                    },
                                    {
                                        "target_id": "candidate",
                                        "epochs": {"target_epoch": 3},
                                    },
                                ]
                            }
                        }
                    },
                },
            )
        finally:
            service.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result.envelope["status"], "failed")
        self.assertIn("observed 3, required 4", result["error"])

    def test_legacy_domain_invocation_rejects_a_foreign_observed_epoch(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        try:
            result = service.context_runtime.invoke_domain(
                service.catalog.require("debug_collect"),
                {
                    "ip": "192.0.2.72",
                    "target_id": "candidate",
                    "_minimum_target_epoch": 4,
                },
                task_id="legacy-foreign-target-epoch",
                operation_id="legacy-foreign-target-epoch-collect",
                executor=lambda: {
                    "ok": True,
                    "business_acceptance": "passed",
                    "observed_target_epochs": {"reference": 9},
                },
            )
        finally:
            service.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result.envelope["status"], "failed")
        self.assertIn(
            "fresh verification did not report the required target epoch",
            result["error"],
        )

    def test_effect_transition_rejects_a_foreign_observed_epoch(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        effect_id = "typed-foreign-target-epoch-collect"
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.73", "deadline": 10},
                task_id="typed-foreign-target-epoch",
                operation_id="typed-foreign-target-epoch-open",
            )
            run_id = opened.envelope["case_id"]
            projection = service.context_runtime.read_case(run_id)
            service.context_runtime.repository.commit(
                run_id,
                expected_revision=int(projection["revision"]),
                events=(
                    PendingCaseEvent(
                        "OperationAccepted",
                        {
                            "operation": "debug_collect",
                            "idempotency_key": effect_id,
                            "request_fingerprint": "a" * 64,
                            "target_id": "candidate",
                        },
                        effect_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, effect_id),
                ),
            )
            events = service.context_runtime.prepare_effect_transition(
                EffectIntent(
                    run_id=run_id,
                    effect_id=effect_id,
                    operation="debug_collect",
                    effect_class=EffectClass.READ_ONLY,
                    request_fingerprint="a" * 64,
                    arguments={
                        "target_id": "candidate",
                        "_minimum_target_epoch": 4,
                    },
                ),
                result={
                    "ok": True,
                    "business_acceptance": "passed",
                    "observed_target_epochs": {"reference": 9},
                },
                error=None,
                settlement_mode=EffectSettlementMode.DISPATCH,
            )
        finally:
            service.close()

        self.assertEqual(events[0].kind, "OperationTerminal")
        self.assertEqual(events[0].payload["status"], "failed")
        self.assertIn(
            "fresh verification did not report the required target epoch",
            events[0].payload["summary"],
        )

    def test_effect_transition_keeps_terminal_mutation_recoverable_when_evidence_fails(
        self,
    ) -> None:
        service = RuntimeMcpService(
            FullFakeBackend(),
            blob_repository=FailingBlobRepository(),
        )
        effect_id = "typed-terminal-mutation-evidence-failure"
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.74", "deadline": 10},
                task_id="typed-terminal-mutation",
                operation_id="typed-terminal-mutation-open",
            )
            run_id = opened.envelope["case_id"]
            projection = service.context_runtime.read_case(run_id)
            service.context_runtime.repository.commit(
                run_id,
                expected_revision=int(projection["revision"]),
                events=(
                    PendingCaseEvent(
                        "OperationAccepted",
                        {
                            "operation": "live_patch_run",
                            "idempotency_key": effect_id,
                            "request_fingerprint": "b" * 64,
                            "target_id": "candidate",
                        },
                        effect_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, effect_id),
                ),
            )
            events = service.context_runtime.prepare_effect_transition(
                EffectIntent(
                    run_id=run_id,
                    effect_id=effect_id,
                    operation="live_patch_run",
                    effect_class=EffectClass.RECONCILABLE_MUTATION,
                    request_fingerprint="b" * 64,
                    arguments={"target_id": "candidate"},
                ),
                result={
                    "ok": True,
                    "journal": {"stage": "verified"},
                    "target_epoch": 2,
                },
                error=None,
                settlement_mode=EffectSettlementMode.DISPATCH,
            )
        finally:
            service.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "OperationTerminal")
        self.assertEqual(events[0].payload["status"], "mutation_outcome_unknown")
        self.assertIn("cannot persist mutation evidence", events[0].payload["summary"])

    def test_json_rpc_error_uses_mutation_journal_outcome_classification(self) -> None:
        class RollbackFailedBackend(FullFakeBackend):
            def live_patch_run(self, task, arguments, context):
                error = ValueError("rollback transport failed")
                error.mutation_outcome = "unknown"
                error.mutation_journal_stage = "rollback_failed"
                error.mutation_effects_started = True
                raise error

        service = RuntimeMcpService(
            RollbackFailedBackend(), interface_profile="compatibility"
        )
        endpoint = JsonRpcMcpEndpoint(
            service,
            session_task_id="rollback-failed-task",
        )
        try:
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "live_patch_run",
                        "arguments": {
                            "ip": "192.0.2.20",
                            "intent": "live-patch",
                            "local_path": "/tmp/unit.lua",
                            "remote_path": "/opt/bmc/apps/demo/unit.lua",
                            "deadline": 10,
                        },
                    },
                }
            )
        finally:
            service.close()

        self.assertIsNotNone(response)
        result = response["result"]
        self.assertTrue(result["isError"])
        structured = result["structuredContent"]
        self.assertEqual(structured["status"], "mutation_outcome_unknown")
        self.assertEqual(structured["code"], "mutation_outcome_unknown")
        self.assertIn("reconcile", structured["next_action"])
        self.assertNotIn("修正工具参数", result["content"][0]["text"])

    def test_authorization_rejection_is_not_recorded_as_unknown_mutation(self) -> None:
        class AuthorizationRejectedBackend(FullFakeBackend):
            def live_patch_run(self, task, arguments, context):
                raise MutationAuthorizationDenied("rollback requires explicit intent")

        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            AuthorizationRejectedBackend(),
            context_repository=repository,
        )
        try:
            with self.assertRaises(MutationAuthorizationDenied):
                service.call_tool(
                    "live_patch_run",
                    {
                        "ip": "192.0.2.45",
                        "intent": "live-patch",
                        "local_path": "/tmp/unit.lua",
                        "remote_path": "/opt/bmc/apps/demo/unit.lua",
                        "deadline": 10,
                    },
                    task_id="authorization-rejected-task",
                    operation_id="authorization-rejected",
                )
            case_id = repository.case_for_task("authorization-rejected-task")
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="authorization-rejected-reader",
                operation_id="authorization-rejected-read",
            )
        finally:
            service.close()

        operation = next(
            item
            for item in case["operations"]
            if item["operation_id"] == "authorization-rejected"
        )
        self.assertEqual(operation["status"], "failed")

    def test_shadow_mode_never_breaks_the_legacy_read_result(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(
            backend,
            context_repository=FailingBindRepository(),
            context_mode="shadow",
        )
        try:
            result = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.20", "deadline": 10},
                task_id="shadow-task",
                operation_id="shadow-one",
            )
            status = service.context_runtime.status()
        finally:
            service.close()
        self.assertTrue(result["ok"])
        self.assertIn("context shadow write failed", result["context_shadow_warning"])
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(status["metrics"]["shadow_write_failures"], 1)

    def test_mcp_error_uses_a_bounded_canonical_envelope_and_text(self) -> None:
        service = RuntimeMcpService(ExplodingDebugBackend())
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="error-task")
        try:
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "debug_run",
                        "arguments": {"ip": "192.0.2.21", "deadline": 10},
                    },
                }
            )
        finally:
            service.close()
        result = response["result"]
        envelope = result["structuredContent"]
        self.assertTrue(result["isError"])
        self.assertEqual(envelope["status"], "failed")
        self.assertEqual(envelope["canonical_error"]["code"], "ValueError")
        self.assertLessEqual(len(json.dumps(envelope).encode("utf-8")), 24_576)
        self.assertLessEqual(
            len(result["content"][0]["text"].encode("utf-8")), 4096
        )

    def test_read_only_blob_failure_returns_once_with_warning_and_no_fake_ref(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(
            backend,
            blob_repository=FailingBlobRepository(),
        )
        arguments = {
            "ip": "192.0.2.22",
            "deadline": 10,
            "idempotency_key": "blob-failure",
        }
        try:
            first = service.call_tool(
                "debug_run",
                arguments,
                task_id="blob-task",
                operation_id="blob-one",
            )
            replay = service.call_tool(
                "debug_run",
                arguments,
                task_id="blob-task",
                operation_id="blob-two",
            )
        finally:
            service.close()
        self.assertEqual(first.envelope["evidence_refs"], [])
        self.assertIn("evidence_not_persisted", first.envelope["gaps"][0])
        self.assertTrue(replay["ok"])
        self.assertEqual(len(backend.calls), 1)

    def test_direct_terminal_mutation_evidence_failure_stays_unknown(self) -> None:
        backend = FullFakeBackend()
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            backend,
            context_repository=repository,
            blob_repository=FailingBlobRepository(),
        )
        arguments = {
            "ip": "192.0.2.75",
            "intent": "live-patch",
            "delivery_strategy": "live-patch",
            "local_path": "/tmp/unit.lua",
            "remote_path": "/opt/bmc/apps/demo/unit.lua",
            "restart_scope": "none",
            "deadline": 10,
            "idempotency_key": "terminal-evidence-failure",
        }
        try:
            with self.assertRaisesRegex(
                MutationOutcomeUnknown,
                "cannot persist mutation evidence",
            ):
                service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="terminal-evidence-failure-task",
                    operation_id="terminal-evidence-failure-operation",
                )
            case_id = repository.case_for_task("terminal-evidence-failure-task")
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="terminal-evidence-failure-reader",
                operation_id="terminal-evidence-failure-read",
            )
        finally:
            service.close()

        mutation = next(
            item
            for item in case["operations"]
            if item["operation_id"] == "terminal-evidence-failure-operation"
        )
        self.assertEqual(mutation["status"], "mutation_outcome_unknown")
        self.assertEqual([name for name, _ in backend.calls], ["live_patch_run"])

    def test_unknown_mutation_blocks_advance_but_explicit_reconciliation_recovers(self) -> None:
        backend = FailOnceUpgradeBackend()
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(backend, context_repository=repository)
        arguments = {
            "ip": "192.0.2.23",
            "intent": "upgrade-and-verify",
            "artifact_path": "/tmp/test.hpm",
            "artifact_sha256": "a" * 64,
            "product_version": "1.2.3",
            "deadline": 10,
            "idempotency_key": "mutation-one",
        }
        try:
            with self.assertRaisesRegex(OSError, "upload connection lost"):
                service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="mutation-task",
                    operation_id="mutation-one",
                )
            case_id = repository.case_for_task("mutation-task")
            blocked = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "advance-blocked",
                    "deadline": 10,
                },
                task_id="mutation-task",
                operation_id="advance-blocked",
            )
            self.assertEqual(blocked["status"], "mutation_outcome_unknown")
            self.assertEqual(backend.upgrade_attempts, 1)
            recovered = service.call_tool(
                "upgrade_run",
                {**arguments, "case_id": case_id},
                task_id="mutation-task",
                operation_id="mutation-one",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="reader",
                operation_id="read-reconciled",
            )
        finally:
            service.close()
        self.assertTrue(recovered["ok"])
        mutation = next(
            item for item in case["operations"] if item["operation_id"] == "mutation-one"
        )
        self.assertEqual(mutation["status"], "completed")
        self.assertEqual(backend.upgrade_attempts, 2)

    def test_mutation_terminal_commit_failure_never_reexecutes_effect(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(
            backend,
            context_repository=FailTerminalCommitRepository(),
        )
        arguments = {
            "ip": "192.0.2.24",
            "artifact_path": "/tmp/openubmc.hpm",
            "artifact_sha256": "a" * 64,
            "product_version": "2.0.0",
            "deadline": 10,
            "idempotency_key": "commit-failure",
        }
        try:
            with self.assertRaises(MutationOutcomeUnknown):
                service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="commit-task",
                    operation_id="commit-one",
                )
            with self.assertRaises(OperationAlreadyInProgress):
                service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="commit-task",
                    operation_id="commit-two",
                )
        finally:
            service.close()
        self.assertEqual([name for name, _ in backend.calls], ["upgrade_run"])

    def test_terminal_case_repairs_a_pending_idempotency_receipt(self) -> None:
        backend = FullFakeBackend()
        repository = FailOnceReceiptRepository()
        service = RuntimeMcpService(backend, context_repository=repository)
        arguments = {
            "ip": "192.0.2.25",
            "intent": "live-patch",
            "delivery_strategy": "live-patch",
            "local_path": "/tmp/unit.lua",
            "remote_path": "/opt/bmc/apps/demo/unit.lua",
            "restart_scope": "none",
            "deadline": 10,
            "idempotency_key": "receipt-repair",
        }
        try:
            with self.assertRaisesRegex(OSError, "receipt completion interrupted"):
                service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="receipt-repair-task",
                    operation_id="receipt-repair-operation",
                )
            case_id = repository.case_for_task("receipt-repair-task")
            repaired = service.call_tool(
                "live_patch_run",
                {**arguments, "case_id": case_id},
                task_id="receipt-repair-task",
                operation_id="receipt-repair-retry",
            )
            status = service.call_tool(
                "runtime_status",
                {},
                task_id="receipt-repair-task",
                operation_id="receipt-repair-status",
            )
        finally:
            service.close()

        self.assertEqual([name for name, _ in backend.calls], ["live_patch_run"])
        self.assertTrue(repaired["idempotent_replay"])
        self.assertIn("closeout", repaired)
        self.assertEqual(
            status["context_runtime"]["metrics"][
                "idempotency_receipt_repairs"
            ],
            1,
        )

    def test_completed_retries_ignore_stale_revision_but_new_work_does_not(self) -> None:
        backend = FullFakeBackend()
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(backend, context_repository=repository)
        domain_arguments = {
            "ip": "192.0.2.26",
            "intent": "diagnosis-only",
            "deadline": 10,
            "idempotency_key": "stale-domain-retry",
        }
        workflow_arguments = {
            "ip": "192.0.2.27",
            "intent": "diagnosis-only",
            "deadline": 10,
            "idempotency_key": "stale-workflow-retry",
        }
        try:
            domain_first = service.call_tool(
                "debug_run",
                domain_arguments,
                task_id="stale-domain-task",
                operation_id="stale-domain-first",
            )
            domain_case_id = domain_first.envelope["case_id"]
            domain_replay = service.call_tool(
                "debug_run",
                {
                    **domain_arguments,
                    "case_id": domain_case_id,
                    "expected_revision": 1,
                },
                task_id="stale-domain-task",
                operation_id="stale-domain-replay",
            )
            with self.assertRaises(RevisionConflict):
                service.call_tool(
                    "debug_run",
                    {
                        **domain_arguments,
                        "case_id": domain_case_id,
                        "expected_revision": 1,
                        "idempotency_key": "new-domain-work",
                    },
                    task_id="stale-domain-task",
                    operation_id="new-domain-work",
                )

            workflow_first = service.call_tool(
                "workflow.advance",
                workflow_arguments,
                task_id="stale-workflow-task",
                operation_id="stale-workflow-first",
            )
            workflow_case_id = workflow_first.envelope["case_id"]
            workflow_replay = service.call_tool(
                "workflow.advance",
                {
                    **workflow_arguments,
                    "case_id": workflow_case_id,
                    "expected_revision": 1,
                },
                task_id="stale-workflow-task",
                operation_id="stale-workflow-replay",
            )
            with self.assertRaises(RevisionConflict):
                service.call_tool(
                    "workflow.advance",
                    {
                        **workflow_arguments,
                        "case_id": workflow_case_id,
                        "expected_revision": 1,
                        "idempotency_key": "new-workflow-work",
                    },
                    task_id="stale-workflow-task",
                    operation_id="new-workflow-work",
                )
        finally:
            service.close()

        self.assertTrue(domain_replay["ok"])
        self.assertTrue(workflow_replay["completed"])
        self.assertEqual(
            [name for name, _ in backend.calls].count("debug_run"),
            2,
        )

    def test_password_env_selector_is_preserved_and_affects_idempotency(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(
            backend, interface_profile="compatibility"
        )
        arguments = {
            "ip": "192.0.2.28",
            "intent": "diagnosis-only",
            "ssh_password_env": "BMC_PASSWORD_A",
            "deadline": 10,
            "idempotency_key": "selector-identity",
        }
        try:
            service.call_tool(
                "debug_run",
                arguments,
                task_id="selector-task",
                operation_id="selector-first",
            )
            with self.assertRaises(IdempotencyConflict):
                service.call_tool(
                    "debug_run",
                    {
                        **arguments,
                        "ssh_password_env": "BMC_PASSWORD_B",
                    },
                    task_id="selector-task",
                    operation_id="selector-second",
                )
        finally:
            service.close()

        self.assertEqual(backend.calls[0][1]["ssh_password_env"], "BMC_PASSWORD_A")

    def test_large_result_is_blob_backed_and_mcp_envelope_is_bounded(self) -> None:
        backend = FullFakeBackend(large_bytes=1024 * 1024 + 123)
        service = RuntimeMcpService(
            backend, interface_profile="compatibility"
        )
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="large-task")
        try:
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "debug_run",
                        "arguments": {"ip": "192.0.2.1", "deadline": 10},
                    },
                }
            )
            envelope = response["result"]["structuredContent"]
            self.assertLessEqual(
                len(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                AGENT_ENVELOPE_MAX_BYTES,
            )
            self.assertNotIn("raw", envelope)
            legacy_bytes = len(
                json.dumps(
                    {"raw": "x" * (1024 * 1024 + 123)},
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            envelope_bytes = len(
                json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            )
            self.assertLess(envelope_bytes, legacy_bytes * 0.2)
            reference = envelope["evidence_refs"][0]
            evidence = service.call_tool(
                "evidence_read",
                {
                    "case_id": envelope["case_id"],
                    "evidence_id": reference["evidence_id"],
                    "limit": 128,
                },
                task_id="large-task",
                operation_id="read-1",
            )
            self.assertEqual(evidence["returned_bytes"], 128)
            self.assertTrue(evidence["truncated"])
        finally:
            service.close()

    def test_sqlite_restart_restores_case_and_idempotent_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository_path = root / "runtime.sqlite3"
            blob_root = root / "blobs"
            first_backend = FullFakeBackend()
            first_service = RuntimeMcpService(
                first_backend,
                context_repository=SQLiteRuntimeRepository(repository_path),
                blob_repository=FilesystemBlobRepository(blob_root),
            )
            first = first_service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.2",
                    "idempotency_key": "stable-run",
                    "deadline": 10,
                },
                task_id="restart-task",
                operation_id="first",
            )
            case_id = first.envelope["case_id"]
            first_service.close()

            second_backend = FullFakeBackend()
            second_service = RuntimeMcpService(
                second_backend,
                context_repository=SQLiteRuntimeRepository(repository_path),
                blob_repository=FilesystemBlobRepository(blob_root),
            )
            try:
                recovered = second_service.call_tool(
                    "case_read",
                    {"case_id": case_id},
                    task_id="new-host-task",
                    operation_id="read",
                )
                replayed = second_service.call_tool(
                    "debug_run",
                    {
                        "case_id": case_id,
                        "ip": "192.0.2.2",
                        "idempotency_key": "stable-run",
                        "deadline": 10,
                    },
                    task_id="new-host-task",
                    operation_id="second",
                )
            finally:
                second_service.close()
        self.assertGreater(recovered["revision"], 1)
        self.assertEqual(replayed["ip"], "192.0.2.2")
        self.assertEqual(second_backend.calls, [])

    def test_phase_record_and_advance_complete_build_upgrade_flow(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.3",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "final_purpose": "fix and verify",
                    "idempotency_key": "advance-1",
                    "deadline": 10,
                },
                task_id="workflow-task",
                operation_id="advance-1",
            )
            self.assertEqual(first["status"], "waiting_phase_record")
            self.assertEqual(first["required_phase_type"], "developer.change")
            case_id = first.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="workflow-task",
                operation_id="case-1",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "developer-one",
                    "phase_type": "developer.change",
                    "producer_identity": "developer-skill",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "implemented fix",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["build", "upgrade", "verify"],
                },
                task_id="workflow-task",
                operation_id="phase-developer",
            )
            second = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "advance-2",
                    "deadline": 10,
                },
                task_id="workflow-task",
                operation_id="advance-2",
            )
            self.assertEqual(second["required_phase_type"], "build.artifact")
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="workflow-task",
                operation_id="case-2",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "build-one",
                    "phase_type": "build.artifact",
                    "producer_identity": "build-skill",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "built hpm",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "1.2.3",
                },
                task_id="workflow-task",
                operation_id="phase-build",
            )
            final = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": "advance-3",
                    "deadline": 10,
                },
                task_id="workflow-task",
                operation_id="advance-3",
            )
            self.assertTrue(final["completed"])
            upgrade = next(args for name, args in backend.calls if name == "upgrade_run")
            self.assertEqual(upgrade["artifact_sha256"], "a" * 64)
            self.assertEqual(upgrade["product_version"], "1.2.3")
            verification = [
                args for name, args in backend.calls if name == "debug_collect"
            ]
            self.assertEqual(verification[-1]["profile"], "standard")
        finally:
            service.close()

    def test_workflow_advance_runs_direct_rollback_then_fresh_verification(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        try:
            result = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.44",
                    "intent": "rollback",
                    "action": "rollback",
                    "backup_path": "/tmp/unit.lua.bak",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "final_purpose": "restore the previous runtime file",
                    "idempotency_key": "rollback-advance",
                    "deadline": 10,
                },
                task_id="rollback-workflow-task",
                operation_id="rollback-advance",
            )
        finally:
            service.close()

        self.assertTrue(result["completed"])
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["live_patch_run", "debug_collect"],
        )
        self.assertEqual(backend.calls[0][1]["action"], "rollback")

    def _advance_after_terminal_phase(
        self, *, phase_type: str, status: str
    ) -> tuple[dict[str, object], list[str]]:
        label = f"{phase_type.replace('.', '-')}-{status}"
        delivery_strategy = (
            "live-patch" if phase_type == "developer.change" else "build-upgrade"
        )
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        try:
            waiting = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.31",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": delivery_strategy,
                    "final_purpose": "fix and verify",
                    "idempotency_key": f"advance-{label}-1",
                    "deadline": 10,
                },
                task_id=f"{label}-task",
                operation_id=f"advance-{label}-1",
            )
            self.assertEqual(waiting["required_phase_type"], "developer.change")
            case_id = waiting.envelope["case_id"]
            if phase_type == "build.artifact":
                case = service.call_tool(
                    "case_read",
                    {"case_id": case_id},
                    task_id=f"{label}-task",
                    operation_id=f"read-{label}-developer",
                )
                service.call_tool(
                    "phase_record",
                    {
                        "case_id": case_id,
                        "expected_revision": case["revision"],
                        "idempotency_key": f"developer-completed-{label}",
                        "phase_type": "developer.change",
                        "producer_identity": "openubmc-developer",
                        "status": "completed",
                        "source_revision": "abc123",
                        "summary": "implemented source fix",
                        "authored_files": ["src/unit.lua"],
                        "verification_plan": ["build", "upgrade", "verify"],
                    },
                    task_id=f"{label}-task",
                    operation_id=f"phase-{label}-developer",
                )
                waiting = service.call_tool(
                    "workflow.advance",
                    {
                        "case_id": case_id,
                        "idempotency_key": f"advance-{label}-2",
                        "deadline": 10,
                    },
                    task_id=f"{label}-task",
                    operation_id=f"advance-{label}-2",
                )
                self.assertEqual(waiting["required_phase_type"], "build.artifact")
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id=f"{label}-task",
                operation_id=f"read-{label}-terminal",
            )
            producer = (
                "openubmc-developer"
                if phase_type == "developer.change"
                else "openubmc-build"
            )
            phase = service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": f"phase-terminal-{label}",
                    "phase_type": phase_type,
                    "producer_identity": producer,
                    "status": status,
                    "source_revision": "abc123",
                    "summary": f"{phase_type} {status}",
                },
                task_id=f"{label}-task",
                operation_id=f"phase-terminal-{label}",
            )
            self.assertEqual(phase["status"], status)
            blocked = service.call_tool(
                "workflow.next",
                {"case_id": case_id},
                task_id=f"{label}-task",
                operation_id=f"next-{label}-terminal",
            )
            self.assertEqual(blocked["status"], status)
            self.assertEqual(blocked["blocked_phase_type"], phase_type)
            retried = service.call_tool(
                "workflow.advance",
                {
                    "case_id": case_id,
                    "idempotency_key": f"advance-{label}-terminal",
                    "deadline": 10,
                },
                task_id=f"{label}-task",
                operation_id=f"advance-{label}-terminal",
            )
            retry_case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id=f"{label}-task",
                operation_id=f"read-{label}-blocked",
            )
        finally:
            service.close()
        self.assertEqual(retry_case["status"], "waiting_phase_record")
        return retried, [name for name, _ in backend.calls]

    def test_workflow_advance_opens_new_developer_phase_attempt(self) -> None:
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                retried, calls = self._advance_after_terminal_phase(
                    phase_type="developer.change", status=status
                )
                self.assertFalse(retried["completed"])
                self.assertEqual(retried["status"], "waiting_phase_record")
                self.assertEqual(
                    retried["required_phase_type"], "developer.change"
                )
                self.assertEqual(calls, ["debug_run"])

    def test_workflow_advance_opens_new_build_phase_attempt(self) -> None:
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                retried, calls = self._advance_after_terminal_phase(
                    phase_type="build.artifact", status=status
                )
                self.assertFalse(retried["completed"])
                self.assertEqual(retried["status"], "waiting_phase_record")
                self.assertEqual(retried["required_phase_type"], "build.artifact")
                self.assertEqual(calls, ["debug_run"])

    def test_task_completion_closes_connection_but_case_remains(self) -> None:
        backend = FullFakeBackend()
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(backend, context_repository=repository)
        result = service.call_tool(
            "debug_run",
            {"ip": "192.0.2.4", "deadline": 10},
            task_id="completion-task",
            operation_id="one",
        )
        case_id = result.envelope["case_id"]
        self.assertTrue(service.complete_task("completion-task"))
        self.assertTrue(backend.created[0].closed)
        self.assertIsNone(repository.case_for_task("completion-task"))
        recovered = service.call_tool(
            "case_read",
            {"case_id": case_id},
            task_id="another-task",
            operation_id="read",
        )
        self.assertEqual(recovered["case_id"], case_id)
        service.close()

    def test_close_stops_advance_and_forget_removes_a_terminal_case(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            FullFakeBackend(), context_repository=repository
        )
        try:
            result = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.25", "deadline": 10},
                task_id="close-task",
                operation_id="debug-close",
            )
            case_id = result.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="close-task",
                operation_id="read-close",
            )
            closed = service.call_tool(
                "case_close",
                {"case_id": case_id, "expected_revision": case["revision"]},
                task_id="close-task",
                operation_id="close-case",
            )
            with self.assertRaisesRegex(Exception, "closed"):
                service.call_tool(
                    "workflow.advance",
                    {"case_id": case_id, "deadline": 10},
                    task_id="close-task",
                    operation_id="advance-closed",
                )
            forgotten = service.call_tool(
                "case_forget",
                {"case_id": case_id},
                task_id="close-task",
                operation_id="forget-case",
            )
        finally:
            service.close()
        self.assertTrue(closed["closed"])
        self.assertTrue(forgotten["forgotten"])
        self.assertIsNone(repository.load(case_id))

    def test_waiting_external_case_is_not_storage_evicted(self) -> None:
        repository = InMemoryRuntimeRepository()
        service = RuntimeMcpService(
            FullFakeBackend(),
            context_repository=repository,
            context_storage_soft_limit_bytes=1,
        )
        try:
            waiting = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.26",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "deadline": 10,
                },
                task_id="waiting-task",
                operation_id="advance-waiting",
            )
            service.context_runtime.maintain()
        finally:
            service.close()
        self.assertEqual(waiting["status"], "waiting_phase_record")
        self.assertIsNotNone(repository.load(waiting.envelope["case_id"]))

    def test_projection_cache_is_bounded_and_rebuilds(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        service.context_runtime.max_cached_projections = 2
        try:
            case_ids = []
            for index in range(5):
                result = service.call_tool(
                    "debug_run",
                    {"ip": f"192.0.2.{10 + index}", "deadline": 10},
                    task_id=f"cache-task-{index}",
                    operation_id=f"op-{index}",
                )
                case_ids.append(result.envelope["case_id"])
            status = service.context_runtime.status()
            self.assertLessEqual(status["projection_cache_count"], 2)
            service.call_tool(
                "case_read",
                {"case_id": case_ids[0]},
                task_id="reader",
                operation_id="read-old",
            )
            self.assertGreater(
                service.context_runtime.status()["metrics"]["projection_rebuilds"],
                0,
            )
        finally:
            service.close()

    def test_phase_record_replay_precedes_terminal_transition_validation(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.30", "intent": "diagnose-and-fix", "deadline": 10},
                task_id="phase-replay-task",
                operation_id="debug",
            )
            case_id = opened.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="phase-replay-task",
                operation_id="read-phase",
            )
            phase_arguments = {
                "case_id": case_id,
                "expected_revision": case["revision"],
                "idempotency_key": "developer-stable",
                "phase_type": "developer.change",
                "producer_identity": "openubmc-developer",
                "status": "completed",
                "source_revision": "abc123",
                "summary": "fixed source",
                "authored_files": ["src/unit.lua"],
                "verification_plan": ["build"],
            }
            first = service.call_tool(
                "phase_record",
                phase_arguments,
                task_id="phase-replay-task",
                operation_id="phase-one",
            )
            replay = service.call_tool(
                "phase_record",
                phase_arguments,
                task_id="phase-replay-task",
                operation_id="phase-two",
            )
        finally:
            service.close()
        self.assertEqual(first["summary"], replay["summary"])
        self.assertEqual(first.envelope, replay.envelope)

    def test_ttl_access_refresh_lru_shared_blob_and_capsule_rebuild(self) -> None:
        now = [0.0]
        repository = InMemoryRuntimeRepository(clock=lambda: now[0])
        blobs = InMemoryBlobRepository()
        service = RuntimeMcpService(
            FullFakeBackend(),
            context_repository=repository,
            blob_repository=blobs,
            context_retention_seconds=5,
        )
        service.context_runtime.clock = lambda: now[0]
        try:
            first = service.call_tool(
                "debug_run",
                {
                    "case_id": "case-old",
                    "ip": "192.0.2.31",
                    "intent": "diagnosis-only",
                    "deadline": 10,
                },
                task_id="shared-task",
                operation_id="old-debug",
            )
            first_ref = first.envelope["evidence_refs"][0]
            size_after_one = service.context_runtime.status()["storage_bytes"]
            now[0] = 2.0
            second = service.call_tool(
                "debug_run",
                {
                    "case_id": "case-new",
                    "ip": "192.0.2.31",
                    "intent": "diagnosis-only",
                    "deadline": 10,
                },
                task_id="shared-task",
                operation_id="new-debug",
            )
            second_ref = second.envelope["evidence_refs"][0]
            self.assertEqual(first_ref["blob_id"], second_ref["blob_id"])
            self.assertNotEqual(first_ref["evidence_id"], second_ref["evidence_id"])
            size_after_two = service.context_runtime.status()["storage_bytes"]
            service.context_runtime.storage_soft_limit_bytes = (
                size_after_one + size_after_two
            ) // 2
            now[0] = 3.0
            first_case = service.call_tool(
                "case_read",
                {"case_id": "case-old"},
                task_id="reader",
                operation_id="touch-old",
            )
            first_capsule_revision = first_case["capsule"]["case_revision"]
            now[0] = 4.0
            service.call_tool(
                "case_read",
                {"case_id": "case-old"},
                task_id="reader",
                operation_id="touch-old-again",
            )
            self.assertGreater(
                service.context_runtime.status()["metrics"]["capsule_cache_hits"],
                0,
            )
            service.context_runtime.maintain()
            self.assertIsNotNone(repository.load("case-old"))
            self.assertIsNone(repository.load("case-new"))
            self.assertEqual(
                blobs.read(first_ref["blob_id"], offset=0, limit=-1),
                blobs.read(second_ref["blob_id"], offset=0, limit=-1),
            )
            now[0] = 10.0
            service.context_runtime.storage_soft_limit_bytes = 1024 * 1024
            service.context_runtime.maintain()
            self.assertIsNone(repository.load("case-old"))
            self.assertGreater(first_capsule_revision, 0)
        finally:
            service.close()

    def test_evidence_target_generation_and_corruption_are_canonical_failures(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            service = RuntimeMcpService(
                FullFakeBackend(),
                blob_repository=FilesystemBlobRepository(root / "blobs"),
            )
            try:
                result = service.call_tool(
                    "debug_run",
                    {
                        "ip": "192.0.2.40",
                        "target_id": "candidate-a",
                        "target_epoch": 7,
                        "deadline": 10,
                    },
                    task_id="evidence-task",
                    operation_id="evidence-one",
                )
                reference = result.envelope["evidence_refs"][0]
                with self.assertRaisesRegex(Exception, "target does not match"):
                    service.call_tool(
                        "evidence_read",
                        {
                            "case_id": result.envelope["case_id"],
                            "evidence_id": reference["evidence_id"],
                            "target_id": "candidate-b",
                        },
                        task_id="reader",
                        operation_id="wrong-target",
                    )
                blob_path = (
                    root
                    / "blobs"
                    / reference["blob_id"][:2]
                    / f"{reference['blob_id']}.json.gz"
                )
                blob_path.write_bytes(b"corrupt")
                with self.assertRaisesRegex(Exception, "unavailable"):
                    service.call_tool(
                        "evidence_read",
                        {
                            "case_id": result.envelope["case_id"],
                            "evidence_id": reference["evidence_id"],
                        },
                        task_id="reader",
                        operation_id="corrupt-read",
                    )
            finally:
                service.close()

    def test_sqlite_rejects_an_unknown_storage_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "runtime.sqlite3"
            SQLiteRuntimeRepository(path)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE runtime_meta SET value = '999' WHERE key = 'storage_version'"
                )
            with self.assertRaisesRegex(Exception, "unsupported.*storage version"):
                SQLiteRuntimeRepository(path)


if __name__ == "__main__":
    unittest.main()
