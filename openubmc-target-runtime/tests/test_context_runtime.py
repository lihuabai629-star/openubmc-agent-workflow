from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    AGENT_ENVELOPE_MAX_BYTES,
    EvidenceUnavailable,
    FilesystemBlobRepository,
    IdempotencyConflict,
    InMemoryBlobRepository,
    InMemoryRuntimeRepository,
    JsonRpcMcpEndpoint,
    MutationOutcomeUnknown,
    OperationAlreadyInProgress,
    PendingCaseEvent,
    RevisionConflict,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
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
        minimum_epoch = arguments.get("_minimum_target_epoch", 0)
        value["target_epoch"] = (
            int(minimum_epoch)
            if isinstance(minimum_epoch, int) and not isinstance(minimum_epoch, bool)
            else 0
        )
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


class PartialDebugBackend(FullFakeBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": False,
            "code": "workflow_partial_failure",
            "normalized_code": "workflow_partial_failure",
            "returncode": 1,
            "error": "collection failed",
        }


class DeterministicFailureBackend(FullFakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        self.attempts += 1
        raise ValueError("deterministic debug failure")


class FailOnceDebugBackend(FullFakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        self.attempts += 1
        if self.attempts == 1:
            raise ValueError("temporary debug failure")
        return super().debug_run(task, arguments, context)


class RawEvidenceBackend(FullFakeBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["credential_status"] = {
            "password_policy": "development-default",
            "token_state": "present",
        }
        return value


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

    def test_step_invalidation_uses_explicit_plan_membership_not_text_order(self) -> None:
        repository = InMemoryRuntimeRepository()
        events = [
            PendingCaseEvent(
                "CaseOpened",
                {
                    "intent": "diagnose-and-fix",
                    "targets": [],
                    "target_version": 1,
                },
            )
        ]
        for operation_id, step_id in (
            ("build", "step-2-build"),
            ("upgrade", "step-10-upgrade"),
        ):
            events.extend(
                (
                    PendingCaseEvent(
                        "OperationAccepted",
                        {
                            "operation": operation_id,
                            "workflow_cycle_id": "cycle-1",
                            "workflow_step_id": step_id,
                            "workflow_step_kind": "operation",
                            "target_version": 1,
                        },
                        operation_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, operation_id),
                    PendingCaseEvent(
                        "OperationTerminal",
                        {"status": "completed", "case_status": "open"},
                        operation_id,
                    ),
                )
            )
        events.append(
            PendingCaseEvent(
                "WorkflowStepsInvalidated",
                {
                    "after_step_id": "step-2-build",
                    "retained_step_ids": ["step-2-build"],
                },
            )
        )

        projection = repository.commit(
            "ordered-invalidation", expected_revision=0, events=events
        )

        self.assertEqual(
            set(projection["workflow_step_states"]),
            {"step-2-build"},
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

    def test_long_case_projection_is_bounded_without_losing_history_indexes(self) -> None:
        for repository in self.repositories():
            with self.subTest(adapter=repository.status()["adapter"]):
                events = [
                    PendingCaseEvent(
                        "CaseOpened",
                        {
                            "intent": "diagnosis-only",
                            "final_purpose": "diagnose",
                            "targets": [],
                        },
                    )
                ]
                for index in range(300):
                    operation_id = f"operation-{index}"
                    operation = "debug_run" if index == 0 else "runtime_status"
                    reference = {
                        "evidence_id": f"evidence-{index}",
                        "blob_id": f"{index:064x}",
                        "media_type": "application/json",
                        "byte_count": index + 1,
                        "target_id": "candidate",
                        "generation": "1",
                        "provenance": operation,
                        "observed_at": float(index),
                    }
                    accepted_payload = {
                        "operation": operation,
                        "idempotency_key": operation_id,
                    }
                    if index == 0:
                        accepted_payload.update(
                            {
                                "workflow_cycle_id": "cycle-1",
                                "workflow_step_id": "step-01-debug_run",
                                "workflow_step_kind": "operation",
                                "target_version": 1,
                            }
                        )
                    events.extend(
                        (
                            PendingCaseEvent(
                                "OperationAccepted",
                                accepted_payload,
                                operation_id,
                            ),
                            PendingCaseEvent("OperationStarted", {}, operation_id),
                            PendingCaseEvent(
                                "EvidenceAttached",
                                {"evidence": reference},
                                operation_id,
                            ),
                            PendingCaseEvent(
                                "OperationTerminal",
                                {
                                    "status": "completed",
                                    "summary": "done",
                                    "case_status": "terminal",
                                },
                                operation_id,
                            ),
                        )
                    )

                projection = repository.commit(
                    "long-case",
                    expected_revision=0,
                    events=events,
                )

                self.assertEqual(projection["operation_count"], 300)
                self.assertEqual(projection["evidence_ref_count"], 300)
                self.assertLessEqual(len(projection["operations"]), 128)
                self.assertLessEqual(len(projection["evidence_refs"]), 256)
                self.assertTrue(projection["projection_truncated"])
                self.assertEqual(
                    projection["completed_operation_counts"]["debug_run"], 1
                )
                self.assertEqual(
                    repository.evidence_reference("long-case", "evidence-0")[
                        "blob_id"
                    ],
                    "0" * 64,
                )
                references = repository.delete_case("long-case")
                self.assertEqual(len(references), 300)

    def test_workflow_uses_completed_operation_counts_after_recent_window_trims(self) -> None:
        for repository in self.repositories():
            with self.subTest(adapter=repository.status()["adapter"]):
                events = [
                    PendingCaseEvent(
                        "CaseOpened",
                        {
                            "intent": "diagnosis-only",
                            "final_purpose": "diagnose",
                            "targets": [],
                        },
                    )
                ]
                for index in range(160):
                    operation_id = f"history-{index}"
                    operation = "debug_run" if index == 0 else "runtime_status"
                    accepted_payload = {
                        "operation": operation,
                        "idempotency_key": operation_id,
                    }
                    if index == 0:
                        accepted_payload.update(
                            {
                                "workflow_cycle_id": "cycle-1",
                                "workflow_step_id": "step-01-debug_run",
                                "workflow_step_kind": "operation",
                                "target_version": 1,
                            }
                        )
                    events.extend(
                        (
                            PendingCaseEvent(
                                "OperationAccepted",
                                accepted_payload,
                                operation_id,
                            ),
                            PendingCaseEvent("OperationStarted", {}, operation_id),
                            PendingCaseEvent(
                                "OperationTerminal",
                                {
                                    "status": "completed",
                                    "summary": "done",
                                    "case_status": "terminal",
                                },
                                operation_id,
                            ),
                        )
                    )
                repository.commit("workflow-history", expected_revision=0, events=events)
                backend = FullFakeBackend()
                service = RuntimeMcpService(backend, context_repository=repository)
                try:
                    result = service.call_tool(
                        "workflow.advance",
                        {"case_id": "workflow-history"},
                        task_id=f"workflow-{repository.status()['adapter']}",
                        operation_id="advance-history",
                    )
                finally:
                    service.close()

                self.assertEqual(result["status"], "completed")
                self.assertEqual(backend.calls, [])


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

    def test_filesystem_range_read_does_not_materialize_the_full_blob(self) -> None:
        body = b"x" * (16 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as raw:
            repository = FilesystemBlobRepository(Path(raw))
            blob_id = repository.put(body)
            tracemalloc.start()
            returned = repository.read(blob_id, offset=1024, limit=65536)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        self.assertEqual(returned, body[1024 : 1024 + 65536])
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_filesystem_put_repairs_an_existing_corrupt_blob(self) -> None:
        body = json.dumps({"value": "repair-me"}).encode("utf-8")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = FilesystemBlobRepository(root)
            blob_id = repository.put(body)
            path = root / blob_id[:2] / f"{blob_id}.json.gz"
            path.write_bytes(b"corrupt")

            self.assertEqual(repository.put(body), blob_id)
            self.assertEqual(repository.read(blob_id, offset=0, limit=-1), body)


class ContextRuntimeIntegrationTests(unittest.TestCase):
    def test_partial_domain_result_stops_workflow_before_developer_phase(self) -> None:
        backend = PartialDebugBackend()
        service = RuntimeMcpService(backend)
        try:
            direct = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.70"},
                task_id="partial-direct",
                operation_id="partial-direct-op",
            )
            advanced = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.71",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="partial-advance",
                operation_id="partial-advance-op",
            )
        finally:
            service.close()

        self.assertEqual(direct.envelope["status"], "partial")
        self.assertEqual(advanced["status"], "partial")
        self.assertNotIn("required_phase_type", advanced)
        self.assertEqual(len(backend.calls), 2)

    def test_failed_domain_step_retries_with_a_new_attempt_on_continue(self) -> None:
        backend = FailOnceDebugBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_tool(
                "workflow.advance",
                {"ip": "192.0.2.72", "intent": "diagnosis-only"},
                task_id="failed-advance",
                operation_id="failed-advance-one",
            )
            second = service.call_tool(
                "workflow.advance",
                {"case_id": first.envelope["case_id"]},
                task_id="failed-advance",
                operation_id="failed-advance-two",
            )
        finally:
            service.close()

        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(backend.attempts, 2)

    def test_build_phase_failure_without_artifact_can_retry_in_the_same_case(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.73", "intent": "diagnose-and-fix"},
                task_id="build-retry",
                operation_id="debug-before-build",
            )
            case_id = opened.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="build-retry",
                operation_id="read-before-failure",
            )
            failed = service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "build-failed",
                    "phase_type": "build.artifact",
                    "producer_identity": "openubmc-build",
                    "status": "failed",
                    "source_revision": "abc123",
                    "summary": "build failed before producing an artifact",
                },
                task_id="build-retry",
                operation_id="build-failed-op",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="build-retry",
                operation_id="read-before-retry",
            )
            completed = service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "build-completed",
                    "phase_type": "build.artifact",
                    "producer_identity": "openubmc-build",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "retry produced an artifact",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "1.2.3",
                },
                task_id="build-retry",
                operation_id="build-completed-op",
            )
        finally:
            service.close()

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["phase_attempt"], 1)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["phase_attempt"], 2)

    def test_sqlite_reclaims_a_pending_claim_owned_by_an_inactive_process(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "runtime.sqlite3"
            first = SQLiteRuntimeRepository(path)
            self.assertIsNone(first.claim_idempotency("case-a", "key-a", "fingerprint"))
            second = SQLiteRuntimeRepository(
                path,
                owner_is_active=lambda _pid, _started: False,
            )
            self.assertIsNone(
                second.claim_idempotency("case-a", "key-a", "fingerprint")
            )

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
            "artifact_path": "/tmp/test.hpm",
            "artifact_sha256": "a" * 64,
            "product_version": "1.2.3",
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

    def test_large_result_is_blob_backed_and_mcp_envelope_is_bounded(self) -> None:
        backend = FullFakeBackend(large_bytes=1024 * 1024 + 123)
        service = RuntimeMcpService(backend)
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
            self.assertEqual(verification[-1]["profile"], "freshness")
        finally:
            service.close()

    def test_preexisting_collect_does_not_satisfy_post_upgrade_verification(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        try:
            opened = service.call_tool(
                "debug_collect",
                {
                    "ip": "192.0.2.80",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="fresh-verification",
                operation_id="preexisting-collect",
            )
            case_id = opened.envelope["case_id"]
            waiting = service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="fresh-verification",
                operation_id="advance-diagnose",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="fresh-verification",
                operation_id="read-developer",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "fresh-developer",
                    "phase_type": "developer.change",
                    "producer_identity": "developer",
                    "status": "completed",
                    "source_revision": "abc",
                    "summary": "fixed",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["upgrade", "verify"],
                },
                task_id="fresh-verification",
                operation_id="fresh-developer",
            )
            service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="fresh-verification",
                operation_id="advance-build",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="fresh-verification",
                operation_id="read-build",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "fresh-build",
                    "phase_type": "build.artifact",
                    "producer_identity": "build",
                    "status": "completed",
                    "source_revision": "abc",
                    "summary": "built",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": "b" * 64,
                    "product_version": "2",
                },
                task_id="fresh-verification",
                operation_id="fresh-build",
            )
            final = service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="fresh-verification",
                operation_id="advance-verify",
            )
        finally:
            service.close()

        self.assertEqual(waiting["required_phase_type"], "developer.change")
        self.assertTrue(final["completed"])
        collects = [args for name, args in backend.calls if name == "debug_collect"]
        self.assertEqual(len(collects), 2)
        self.assertEqual(collects[-1]["_minimum_target_epoch"], 3)

    def test_target_switch_updates_case_and_followup_delivery_target(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.81",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="target-switch",
                operation_id="advance-a",
            )
            case_id = first.envelope["case_id"]
            service.call_tool(
                "debug_run",
                {"case_id": case_id, "ip": "192.0.2.82"},
                task_id="target-switch",
                operation_id="manual-b",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="target-switch",
                operation_id="read-b",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "switch-developer",
                    "phase_type": "developer.change",
                    "producer_identity": "developer",
                    "status": "completed",
                    "source_revision": "abc",
                    "summary": "fixed",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["upgrade", "verify"],
                },
                task_id="target-switch",
                operation_id="switch-developer",
            )
            service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="target-switch",
                operation_id="advance-b-debug",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="target-switch",
                operation_id="read-switch-build",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "switch-build",
                    "phase_type": "build.artifact",
                    "producer_identity": "build",
                    "status": "completed",
                    "source_revision": "abc",
                    "summary": "built",
                    "artifact_path": "/tmp/product.hpm",
                    "artifact_sha256": "c" * 64,
                    "product_version": "3",
                },
                task_id="target-switch",
                operation_id="switch-build",
            )
            final = service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="target-switch",
                operation_id="advance-b-final",
            )
        finally:
            service.close()

        self.assertEqual(case["targets"][0]["address"], "192.0.2.82")
        self.assertTrue(final["completed"])
        upgrade = [args for name, args in backend.calls if name == "upgrade_run"]
        self.assertEqual(upgrade[-1]["ip"], "192.0.2.82")

    def test_existing_target_selection_does_not_advance_target_version(self) -> None:
        service = RuntimeMcpService(FullFakeBackend())
        try:
            opened = service.call_tool(
                "debug_run",
                {
                    "targets": [
                        {
                            "ip": "192.0.2.86",
                            "target_id": "reference",
                            "role": "reference",
                        },
                        {
                            "ip": "192.0.2.87",
                            "target_id": "candidate",
                            "role": "candidate",
                        },
                    ]
                },
                task_id="target-selector",
                operation_id="compare",
            )
            case_id = opened.envelope["case_id"]
            service.call_tool(
                "debug_collect",
                {"case_id": case_id, "target_id": "candidate"},
                task_id="target-selector",
                operation_id="candidate-read",
            )
            selected = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="target-selector",
                operation_id="read-selection",
            )
            service.call_tool(
                "debug_collect",
                {"case_id": case_id, "target_id": "reference"},
                task_id="target-selector",
                operation_id="reference-read",
            )
            switched = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="target-selector",
                operation_id="read-switched-selection",
            )
        finally:
            service.close()

        self.assertEqual(selected["target_version"], 1)
        self.assertEqual(switched["target_version"], 1)
        self.assertEqual(switched["workflow_inputs"]["target_id"], "reference")

    def test_new_developer_change_starts_a_new_delivery_cycle(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_tool(
                "workflow.advance",
                {"ip": "192.0.2.83", "intent": "diagnose-and-fix"},
                task_id="second-cycle",
                operation_id="cycle-one-debug",
            )
            case_id = first.envelope["case_id"]

            def submit_developer(key: str, revision: int):
                return service.call_tool(
                    "phase_record",
                    {
                        "case_id": case_id,
                        "expected_revision": revision,
                        "idempotency_key": key,
                        "phase_type": "developer.change",
                        "producer_identity": "developer",
                        "status": "completed",
                        "source_revision": key,
                        "summary": key,
                        "authored_files": ["src/unit.lua"],
                        "verification_plan": ["verify"],
                    },
                    task_id="second-cycle",
                    operation_id=key,
                )

            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="second-cycle",
                operation_id="read-cycle-one",
            )
            first_phase = submit_developer("developer-cycle-one", case["revision"])
            service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="second-cycle",
                operation_id="finish-cycle-one",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="second-cycle",
                operation_id="read-cycle-two",
            )
            second_phase = submit_developer("developer-cycle-two", case["revision"])
            final = service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="second-cycle",
                operation_id="finish-cycle-two",
            )
        finally:
            service.close()

        self.assertEqual(first_phase["workflow_cycle_id"], "cycle-1")
        self.assertEqual(second_phase["workflow_cycle_id"], "cycle-2")
        self.assertTrue(final["completed"])
        self.assertEqual(
            len([name for name, _args in backend.calls if name == "debug_run"]),
            2,
        )

    def test_persisted_evidence_keeps_internal_development_fields(self) -> None:
        service = RuntimeMcpService(RawEvidenceBackend())
        try:
            result = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.84"},
                task_id="raw-evidence",
                operation_id="raw-evidence",
            )
            reference = result.envelope["evidence_refs"][0]
            evidence = service.call_tool(
                "evidence_read",
                {
                    "case_id": result.envelope["case_id"],
                    "evidence_id": reference["evidence_id"],
                },
                task_id="raw-evidence",
                operation_id="read-raw-evidence",
            )
        finally:
            service.close()

        body = json.loads(evidence["body"])
        self.assertEqual(
            body["credential_status"]["password_policy"],
            "development-default",
        )
        self.assertEqual(body["credential_status"]["token_state"], "present")

    def test_service_maintenance_evicts_expired_unbound_terminal_case(self) -> None:
        now = [0.0]
        repository = InMemoryRuntimeRepository(clock=lambda: now[0])
        service = RuntimeMcpService(
            FullFakeBackend(),
            context_repository=repository,
            context_retention_seconds=5,
            context_maintenance_interval_seconds=0,
        )
        try:
            result = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.85"},
                task_id="maintenance-case",
                operation_id="maintenance-debug",
            )
            case_id = result.envelope["case_id"]
            service.complete_task("maintenance-case")
            now[0] = 6.0
            service.call_tool(
                "runtime_status",
                {},
                task_id="maintenance-status",
                operation_id="maintenance-status",
            )
        finally:
            service.close()

        self.assertIsNone(repository.load(case_id))

    def test_service_status_reports_context_maintenance_failure(self) -> None:
        service = RuntimeMcpService(
            FullFakeBackend(),
            context_maintenance_interval_seconds=0,
        )

        def fail_maintenance():
            raise OSError("maintenance store unavailable")

        service.context_runtime.maintain = fail_maintenance
        try:
            status = service.call_tool(
                "runtime_status",
                {},
                task_id="maintenance-failure",
                operation_id="maintenance-failure-status",
            )
        finally:
            service.close()

        self.assertEqual(status["context_maintenance"]["attempts"], 1)
        self.assertEqual(status["context_maintenance"]["failures"], 1)
        self.assertIn(
            "maintenance store unavailable",
            status["context_maintenance"]["last_error"],
        )

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

    def test_projection_cache_also_respects_the_total_byte_budget(self) -> None:
        service = RuntimeMcpService(
            FullFakeBackend(),
            context_max_cached_projections=64,
            context_max_cached_projection_bytes=4096,
        )
        try:
            for index in range(5):
                service.call_tool(
                    "debug_run",
                    {
                        "ip": f"192.0.2.{80 + index}",
                        "problem": "large-context-" + "x" * 4096,
                    },
                    task_id=f"byte-cache-{index}",
                    operation_id=f"byte-cache-op-{index}",
                )
            status = service.context_runtime.status()
        finally:
            service.close()

        self.assertLessEqual(
            status["projection_cache_bytes"],
            status["projection_cache_byte_limit"],
        )

    def test_trimmed_evidence_remains_readable_and_is_deleted_with_the_case(self) -> None:
        repositories = [InMemoryRuntimeRepository()]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repositories.append(
            SQLiteRuntimeRepository(Path(temporary.name) / "runtime.sqlite3")
        )
        for repository in repositories:
            with self.subTest(adapter=repository.status()["adapter"]):
                blobs = InMemoryBlobRepository()
                body = b'{"old":"evidence"}'
                blob_id = blobs.put(body)
                events = [
                    PendingCaseEvent(
                        "CaseOpened",
                        {
                            "intent": "diagnosis-only",
                            "final_purpose": "diagnose",
                            "targets": [],
                        },
                    )
                ]
                for index in range(300):
                    operation_id = f"evidence-operation-{index}"
                    events.extend(
                        (
                            PendingCaseEvent(
                                "OperationAccepted",
                                {
                                    "operation": "runtime_status",
                                    "idempotency_key": operation_id,
                                },
                                operation_id,
                            ),
                            PendingCaseEvent("OperationStarted", {}, operation_id),
                            PendingCaseEvent(
                                "EvidenceAttached",
                                {
                                    "evidence": {
                                        "evidence_id": f"old-evidence-{index}",
                                        "blob_id": blob_id,
                                        "media_type": "application/json",
                                        "byte_count": len(body),
                                        "target_id": "candidate",
                                        "generation": "1",
                                        "provenance": "runtime_status",
                                        "observed_at": float(index),
                                    }
                                },
                                operation_id,
                            ),
                            PendingCaseEvent(
                                "OperationTerminal",
                                {
                                    "status": "completed",
                                    "summary": "done",
                                    "case_status": "terminal",
                                },
                                operation_id,
                            ),
                        )
                    )
                repository.commit("evidence-history", expected_revision=0, events=events)
                service = RuntimeMcpService(
                    FullFakeBackend(),
                    context_repository=repository,
                    blob_repository=blobs,
                )
                try:
                    read = service.context_runtime.read_evidence(
                        "evidence-history", "old-evidence-0"
                    )
                    forgotten = service.context_runtime.forget_case("evidence-history")
                finally:
                    service.close()

                self.assertEqual(read["body"], body.decode("utf-8"))
                self.assertEqual(forgotten["deleted_blobs"], 1)
                with self.assertRaises(EvidenceUnavailable):
                    blobs.read(blob_id, offset=0, limit=-1)

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
            service.complete_task("shared-task")
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
