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
from openubmc_target_runtime.redaction import (  # noqa: E402
    register_secret_values,
    secret_redaction_request,
)
from openubmc_target_runtime.effect_runner import (  # noqa: E402
    EffectIntent,
    EffectSettlementMode,
)


from diagnosis_fixtures import persist_terminal_diagnosis


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


class VersionedUpgradeBackend(FullFakeBackend):
    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        value = super().upgrade_run(task, arguments, context)
        value["installed_version"] = str(arguments["product_version"])
        return value


class ConflictingVersionUpgradeBackend(FullFakeBackend):
    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        value = super().upgrade_run(task, arguments, context)
        value["product_version"] = "backend-reported-alias"
        value["installed_version"] = str(arguments["product_version"])
        return value


class ArtifactAliasOnlyUpgradeBackend(FullFakeBackend):
    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        value = super().upgrade_run(task, arguments, context)
        value["product_version"] = str(arguments["product_version"])
        return value


class SecretShapedVersionUpgradeBackend(FullFakeBackend):
    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        value = super().upgrade_run(task, arguments, context)
        value["installed_version"] = "token=must-not-persist"
        return value


class MultiTargetDebugBackend(FullFakeBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {
            "ok": True,
            "targets": [
                {
                    "target_id": target["target_id"],
                    "operation_id": "token=must-not-persist",
                    "result": {"firmware_version": f"version-{index}"},
                }
                for index, target in enumerate(arguments["targets"], start=1)
            ],
        }


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

    def test_persistence_boundaries_redact_events_and_retry_receipts(self) -> None:
        secret = "fixture-persistence-secret"
        for repository in self.repositories():
            with self.subTest(adapter=repository.status()["adapter"]):
                with secret_redaction_request():
                    register_secret_values({"password": secret})
                    repository.commit(
                        "case-secret-boundary",
                        expected_revision=0,
                        events=(
                            PendingCaseEvent(
                                "CaseOpened",
                                {
                                    "intent": "diagnosis-only",
                                    "final_purpose": "diagnose",
                                    "targets": [],
                                    "authorization": {
                                        "original_intent": "diagnosis-only",
                                        "delivery_strategy": "",
                                        "allowed_actions": [],
                                        "authorized_exceptions": {},
                                        "allow_insecure_tls": False,
                                        "parse_count": 1,
                                    },
                                    "http": {"authorization": secret},
                                    "password": secret,
                                    "note": f"remote echoed {secret}",
                                },
                            ),
                        ),
                    )
                    repository.claim_idempotency(
                        "case-secret-boundary",
                        "secret-retry",
                        "fingerprint-secret-retry",
                    )
                    repository.complete_idempotency(
                        "case-secret-boundary",
                        "secret-retry",
                        {
                            "password": secret,
                            "message": f"ordinary backend output: {secret}",
                            "credential_source": "local-active-revision",
                        },
                    )

                events = repository.events("case-secret-boundary")
                replay = repository.claim_idempotency(
                    "case-secret-boundary",
                    "secret-retry",
                    "fingerprint-secret-retry",
                )
                persisted = json.dumps(
                    {"events": events, "replay": replay}, sort_keys=True
                )

                self.assertNotIn(secret, persisted)
                self.assertNotIn('"password"', persisted)
                self.assertNotIn('"authorization": "', persisted)
                self.assertIn("<redacted>", persisted)
                self.assertEqual(
                    events[0]["payload"]["authorization"]["original_intent"],
                    "diagnosis-only",
                )
                self.assertEqual(
                    replay["credential_source"], "local-active-revision"
                )

    def test_request_secret_redaction_cleans_unlabelled_exception_text(self) -> None:
        secret = "fixture-unlabelled-exception-secret"

        with self.assertRaises(RuntimeError) as raised:
            with secret_redaction_request():
                register_secret_values({"password": secret})
                raise RuntimeError(f"remote backend echoed {secret}")

        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("<redacted>", str(raised.exception))


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
            result = service._test.context_runtime.invoke_domain(
                service._test.catalog.require("debug_collect"),
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
            result = service._test.context_runtime.invoke_domain(
                service._test.catalog.require("debug_collect"),
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
            result = service._test.context_runtime.invoke_domain(
                service._test.catalog.require("debug_collect"),
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
            projection = service._test.context_runtime.read_case(run_id)
            service._test.context_runtime.repository.commit(
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
            events = service._test.context_runtime.prepare_effect_transition(
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
            projection = service._test.context_runtime.read_case(run_id)
            service._test.context_runtime.repository.commit(
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
            events = service._test.context_runtime.prepare_effect_transition(
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

    def test_effect_transition_keeps_read_only_effect_retryable_when_evidence_fails(
        self,
    ) -> None:
        service = RuntimeMcpService(
            FullFakeBackend(),
            blob_repository=FailingBlobRepository(),
        )
        effect_id = "typed-read-only-evidence-failure"
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.76", "deadline": 10},
                task_id="typed-read-only",
                operation_id="typed-read-only-open",
            )
            run_id = opened.envelope["case_id"]
            projection = service._test.context_runtime.read_case(run_id)
            service._test.context_runtime.repository.commit(
                run_id,
                expected_revision=int(projection["revision"]),
                events=(
                    PendingCaseEvent(
                        "OperationAccepted",
                        {
                            "operation": "debug_collect",
                            "idempotency_key": effect_id,
                            "request_fingerprint": "c" * 64,
                            "target_id": "candidate",
                        },
                        effect_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, effect_id),
                ),
            )
            events = service._test.context_runtime.prepare_effect_transition(
                EffectIntent(
                    run_id=run_id,
                    effect_id=effect_id,
                    operation="debug_collect",
                    effect_class=EffectClass.READ_ONLY,
                    request_fingerprint="c" * 64,
                    arguments={"target_id": "candidate"},
                ),
                result={"ok": True, "summary": "fresh observation completed"},
                error=None,
                settlement_mode=EffectSettlementMode.DISPATCH,
            )
        finally:
            service.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "OperationProgressed")
        self.assertEqual(events[0].payload["status"], "running")
        self.assertEqual(
            events[0].payload["canonical_error"]["code"],
            "evidence_not_persisted",
        )
        self.assertEqual(events[0].payload["evidence_retry_generation"], 1)
        self.assertIn("retry", events[0].payload["next_actions"][0])

    def test_internal_domain_error_uses_mutation_journal_outcome_classification(self) -> None:
        class RollbackFailedBackend(FullFakeBackend):
            def live_patch_run(self, task, arguments, context):
                error = ValueError("rollback transport failed")
                error.mutation_outcome = "unknown"
                error.mutation_journal_stage = "rollback_failed"
                error.mutation_effects_started = True
                raise error

        service = RuntimeMcpService(
            RollbackFailedBackend()
        )
        try:
            arguments = {
                "ip": "192.0.2.20",
                "intent": "live-patch",
                "local_path": "/tmp/unit.lua",
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "deadline": 10,
            }
            with self.assertRaises(ValueError) as raised:
                service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="rollback-failed-task",
                    operation_id="rollback-failed-operation",
                )
            structured = service.error_result(
                raised.exception,
                name="live_patch_run",
                arguments=arguments,
                task_id="rollback-failed-task",
                operation_id="rollback-failed-operation",
            )
        finally:
            service.close()

        self.assertFalse(structured["ok"])
        self.assertEqual(structured["code"], "mutation_outcome_unknown")
        self.assertIn("reconcile", structured["next_action"])

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
            status = service._test.context_runtime.status()
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


    def test_password_env_selector_is_preserved_and_affects_idempotency(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(
            backend
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
            backend
        )
        try:
            result = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.1", "deadline": 10},
                task_id="large-task",
                operation_id="large-task-debug",
            )
            envelope = result.envelope
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



    def test_projection_cache_is_bounded_and_rebuilds(self) -> None:
        backend = FullFakeBackend()
        service = RuntimeMcpService(backend)
        service._test.context_runtime.max_cached_projections = 2
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
            status = service._test.context_runtime.status()
            self.assertLessEqual(status["projection_cache_count"], 2)
            service.call_tool(
                "case_read",
                {"case_id": case_ids[0]},
                task_id="reader",
                operation_id="read-old",
            )
            self.assertGreater(
                service._test.context_runtime.status()["metrics"]["projection_rebuilds"],
                0,
            )
        finally:
            service.close()


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
        service._test.context_runtime.clock = lambda: now[0]
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
            persist_terminal_diagnosis(repository, "case-old")
            first_ref = first.envelope["evidence_refs"][0]
            size_after_one = service._test.context_runtime.status()["storage_bytes"]
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
            persist_terminal_diagnosis(repository, "case-new")
            second_ref = second.envelope["evidence_refs"][0]
            self.assertEqual(first_ref["blob_id"], second_ref["blob_id"])
            self.assertNotEqual(first_ref["evidence_id"], second_ref["evidence_id"])
            size_after_two = service._test.context_runtime.status()["storage_bytes"]
            service._test.context_runtime.storage_soft_limit_bytes = (
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
                service._test.context_runtime.status()["metrics"]["capsule_cache_hits"],
                0,
            )
            service._test.context_runtime.maintain()
            self.assertIsNotNone(repository.load("case-old"))
            self.assertIsNone(repository.load("case-new"))
            self.assertEqual(
                blobs.read(first_ref["blob_id"], offset=0, limit=-1),
                blobs.read(second_ref["blob_id"], offset=0, limit=-1),
            )
            now[0] = 10.0
            service._test.context_runtime.storage_soft_limit_bytes = 1024 * 1024
            service._test.context_runtime.maintain()
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
                self.assertEqual(reference["target_address"], "192.0.2.40")
                self.assertEqual(reference["operation"], "debug_run")
                self.assertEqual(reference["operation_id"], "evidence-one")
                self.assertEqual(reference["case_id"], result.envelope["case_id"])
                self.assertGreater(reference["observed_at"], 0)
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
                with self.assertRaisesRegex(Exception, "address does not match"):
                    service.call_tool(
                        "evidence_read",
                        {
                            "case_id": result.envelope["case_id"],
                            "evidence_id": reference["evidence_id"],
                            "target_address": "192.0.2.41",
                        },
                        task_id="reader",
                        operation_id="wrong-address",
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

    def test_multi_target_diagnosis_evidence_binds_each_target_identity(self) -> None:
        service = RuntimeMcpService(MultiTargetDebugBackend())
        try:
            result = service.call_tool(
                "debug_run",
                {
                    "targets": [
                        {"target_id": "bmc-a", "ip": "192.0.2.48"},
                        {"target_id": "bmc-b", "ip": "192.0.2.49"},
                    ],
                    "deadline": 10,
                },
                task_id="multi-target-evidence",
                operation_id="multi-target-debug",
            )
            projection = service._test.context_runtime.read_case(
                result.envelope["case_id"]
            )
        finally:
            service.close()

        reference = result.envelope["evidence_refs"][0]
        operation = next(
            item
            for item in projection["operations"]
            if item["operation_id"] == "multi-target-debug"
        )
        self.assertEqual(
            [target["target_id"] for target in operation["inputs"]["targets"]],
            ["bmc-a", "bmc-b"],
        )
        self.assertEqual(
            reference["target_bindings"],
            [
                {
                    "target_id": "bmc-a",
                    "target_address": "192.0.2.48",
                    "operation_id": "multi-target-debug",
                    "expected_product_version": "",
                    "observed_product_version": "version-1",
                },
                {
                    "target_id": "bmc-b",
                    "target_address": "192.0.2.49",
                    "operation_id": "multi-target-debug",
                    "expected_product_version": "",
                    "observed_product_version": "version-2",
                },
            ],
        )

    def test_upgrade_evidence_binds_expected_and_observed_product_versions(self) -> None:
        service = RuntimeMcpService(VersionedUpgradeBackend())
        try:
            result = service.call_tool(
                "upgrade_run",
                {
                    "ip": "192.0.2.45",
                    "target_id": "candidate-a",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "12.00.05.03",
                    "deadline": 10,
                    "idempotency_key": "version-bound-upgrade",
                },
                task_id="version-bound-evidence",
                operation_id="upgrade-version-bound",
            )
            reference = result.envelope["evidence_refs"][0]
            self.assertEqual(reference["expected_product_version"], "12.00.05.03")
            self.assertEqual(reference["observed_product_version"], "12.00.05.03")
            loaded = service.call_tool(
                "evidence_read",
                {
                    "case_id": result.envelope["case_id"],
                    "evidence_id": reference["evidence_id"],
                    "target_address": "192.0.2.45",
                    "expected_product_version": "12.00.05.03",
                    "observed_product_version": "12.00.05.03",
                    "operation": "upgrade_run",
                    "operation_id": "upgrade-version-bound",
                },
                task_id="version-bound-reader",
                operation_id="read-version-bound",
            )
            self.assertEqual(
                loaded["evidence"]["observed_product_version"],
                "12.00.05.03",
            )
            with self.assertRaisesRegex(Exception, "version does not match"):
                service.call_tool(
                    "evidence_read",
                    {
                        "case_id": result.envelope["case_id"],
                        "evidence_id": reference["evidence_id"],
                        "observed_product_version": "12.00.05.04",
                    },
                    task_id="version-bound-reader",
                    operation_id="read-wrong-version",
                )
        finally:
            service.close()

    def test_expected_version_comes_from_the_request_not_a_backend_alias(self) -> None:
        service = RuntimeMcpService(ConflictingVersionUpgradeBackend())
        try:
            result = service.call_tool(
                "upgrade_run",
                {
                    "ip": "192.0.2.46",
                    "target_id": "candidate-a",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "b" * 64,
                    "product_version": "12.00.05.03",
                    "deadline": 10,
                    "idempotency_key": "request-version-authoritative",
                },
                task_id="request-version-authoritative",
                operation_id="upgrade-request-version",
            )
        finally:
            service.close()

        reference = result.envelope["evidence_refs"][0]
        self.assertEqual(reference["expected_product_version"], "12.00.05.03")
        self.assertEqual(reference["observed_product_version"], "12.00.05.03")

    def test_artifact_product_version_is_not_device_observation(self) -> None:
        service = RuntimeMcpService(ArtifactAliasOnlyUpgradeBackend())
        try:
            result = service.call_tool(
                "upgrade_run",
                {
                    "ip": "192.0.2.50",
                    "target_id": "candidate-a",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "d" * 64,
                    "product_version": "12.00.05.03",
                    "deadline": 10,
                    "idempotency_key": "artifact-alias-is-not-observation",
                },
                task_id="artifact-alias-is-not-observation",
                operation_id="upgrade-artifact-alias",
            )
        finally:
            service.close()

        reference = result.envelope["evidence_refs"][0]
        self.assertEqual(reference["expected_product_version"], "12.00.05.03")
        self.assertEqual(reference["observed_product_version"], "")

    def test_secret_shaped_backend_version_is_not_persisted_as_identity(self) -> None:
        service = RuntimeMcpService(SecretShapedVersionUpgradeBackend())
        try:
            result = service.call_tool(
                "upgrade_run",
                {
                    "ip": "192.0.2.47",
                    "target_id": "candidate-a",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "c" * 64,
                    "product_version": "12.00.05.03",
                    "deadline": 10,
                    "idempotency_key": "secret-shaped-version",
                },
                task_id="secret-shaped-version",
                operation_id="upgrade-secret-version",
            )
        finally:
            service.close()

        reference = result.envelope["evidence_refs"][0]
        self.assertEqual(reference["expected_product_version"], "12.00.05.03")
        self.assertEqual(reference["observed_product_version"], "")
        self.assertNotIn("must-not-persist", json.dumps(result.envelope))

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
