from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    CaseNotForgettable,
    ContextRuntime,
    InMemoryBlobRepository,
    JsonRpcMcpEndpoint,
    OrchestratedMcpBackend,
    PendingCaseEvent,
    RevisionConflict,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.closed = False


class _DomainBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def open_task(self, task_id: str) -> _Task:
        return _Task(task_id)

    @staticmethod
    def close_task(task: _Task) -> None:
        task.closed = True

    @staticmethod
    def maintain_task(_task: _Task) -> int:
        return 0

    @staticmethod
    def task_status(task: _Task) -> dict[str, object]:
        return {"task_id": task.task_id, "closed": task.closed}

    def _capture(self, name: str, arguments) -> dict[str, object]:
        captured = dict(arguments)
        self.calls.append((name, captured))
        return {
            "ok": True,
            "schema": f"test/{name}",
            "ip": captured.get("ip"),
        }

    def debug_run(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._capture("debug_run", arguments)
        minimum = arguments.get("_minimum_target_epoch", 0)
        if arguments.get("profile") == "freshness":
            value["target_epoch"] = int(minimum)
        return value

    def debug_collect(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._capture("debug_collect", arguments)
        minimum = arguments.get("_minimum_target_epoch", 0)
        value["target_epoch"] = int(minimum)
        return value

    def live_patch_run(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._capture("live_patch_run", arguments)
        minimum = arguments.get("_minimum_target_epoch", 0)
        value.update(
            {
                "journal": {"stage": "verified"},
                "epoch_before": int(minimum),
                "epoch_after": int(minimum) + 1,
                "target_epoch": int(minimum) + 1,
            }
        )
        return value

    def upgrade_run(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._capture("upgrade_run", arguments)
        minimum = arguments.get("_minimum_target_epoch", 0)
        value.update(
            {
                "journal": {"stage": "verified"},
                "epoch_before": int(minimum),
                "epoch_after": int(minimum) + 1,
                "target_epoch": int(minimum) + 1,
            }
        )
        return value


class _FailingCollectBackend(_DomainBackend):
    def debug_collect(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._capture("debug_collect", arguments)
        value.update({"ok": False, "status": "failed"})
        return value


class _StaleCollectBackend(_DomainBackend):
    def debug_collect(self, _task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        value = self._capture("debug_collect", arguments)
        value["target_epoch"] = 0
        return value


class _CrashDuringCollectBackend(_DomainBackend):
    def debug_collect(self, _task, _arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        raise SystemExit("simulated process interruption")


class _CrashDuringPatchBackend(_DomainBackend):
    def live_patch_run(self, _task, _arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        raise SystemExit("simulated mutation interruption")


class WorkflowRegressionTests(unittest.TestCase):
    def test_direct_debug_satisfies_the_first_workflow_step(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            opened = service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.10",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="direct-debug",
                operation_id="debug-one",
            )
            advanced = service.call_tool(
                "workflow.advance",
                {"case_id": opened.envelope["case_id"]},
                task_id="direct-debug",
                operation_id="advance-one",
            )
        finally:
            service.close()

        self.assertEqual(advanced["status"], "waiting_phase_record")
        self.assertEqual(advanced["required_phase_type"], "developer.change")
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["debug_run"],
        )

    def test_narrow_debug_collect_does_not_expand_on_continue(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            collected = service.call_tool(
                "debug_collect",
                {
                    "ip": "192.0.2.13",
                    "profile": "mdb",
                },
                task_id="narrow-debug",
                operation_id="collect-one",
            )
            advanced = service.call_tool(
                "workflow.advance",
                {"case_id": collected.envelope["case_id"]},
                task_id="narrow-debug",
                operation_id="advance-narrow",
            )
        finally:
            service.close()

        self.assertTrue(advanced["completed"])
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["debug_collect"],
        )

    def test_failed_narrow_debug_collect_retries_the_same_operation(self) -> None:
        backend = _FailingCollectBackend()
        service = RuntimeMcpService(backend)
        try:
            collected = service.call_tool(
                "debug_collect",
                {
                    "ip": "192.0.2.23",
                    "profile": "mdb",
                },
                task_id="failed-narrow-debug",
                operation_id="collect-failed-one",
            )
            advanced = service.call_tool(
                "workflow.advance",
                {"case_id": collected.envelope["case_id"]},
                task_id="failed-narrow-debug",
                operation_id="advance-failed-narrow",
            )
        finally:
            service.close()

        self.assertEqual(advanced["status"], "failed")
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["debug_collect", "debug_collect"],
        )

    def test_authoritative_mode_does_not_start_the_legacy_embedded_workflow(self) -> None:
        domain = _DomainBackend()
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {"debug_run": domain, "live_patch_run": domain}
            ),
            context_mode="authoritative",
        )
        try:
            service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.11",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                    "workflow": {
                        "developer": {
                            "component_roots": ["/src/component"],
                            "authored_files": ["src/fix.lua"],
                            "change_summary": "fix the diagnosed issue",
                            "runtime_artifact": "/tmp/fix.lua",
                            "restart_scope": "none",
                            "verification_checks": ["state is ready"],
                        },
                        "live_patch": {
                            "remote_path": "/opt/bmc/fix.lua",
                        },
                        "verification": {"profile": "freshness"},
                    },
                },
                task_id="authoritative-routing",
                operation_id="debug-authoritative",
            )
        finally:
            service.close()

        self.assertEqual(
            [name for name, _arguments in domain.calls],
            ["debug_run"],
        )
        self.assertNotIn("workflow", domain.calls[0][1])

    def test_authoritative_mode_infers_delivery_before_stripping_legacy_workflow(self) -> None:
        domain = _DomainBackend()
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {"debug_run": domain, "live_patch_run": domain}
            ),
            context_mode="authoritative",
        )
        try:
            opened = service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.12",
                    "intent": "diagnose-and-fix",
                    "workflow": {
                        "developer": {
                            "authored_files": ["src/fix.lua"],
                        },
                        "live_patch": {
                            "remote_path": "/opt/bmc/fix.lua",
                        },
                    },
                },
                task_id="authoritative-inference",
                operation_id="debug-inference",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": opened.envelope["case_id"]},
                task_id="authoritative-inference",
                operation_id="read-inference",
            )
        finally:
            service.close()

        self.assertEqual(case["delivery_strategy"], "live-patch")
        self.assertEqual(
            case["capsule"]["delivery_strategy"],
            "live-patch",
        )
        self.assertNotIn("workflow", domain.calls[0][1])

    def test_authoritative_mode_disables_legacy_live_patch_auto_verification(self) -> None:
        domain = _DomainBackend()
        service = RuntimeMcpService(
            OrchestratedMcpBackend(
                {
                    "debug_collect": domain,
                    "live_patch_run": domain,
                }
            ),
            context_mode="authoritative",
        )
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.26",
                    "local_path": "/tmp/fix.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="authoritative-live-patch",
                operation_id="patch-authoritative",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": patched.envelope["case_id"]},
                task_id="authoritative-live-patch",
                operation_id="read-authoritative-patch",
            )
        finally:
            service.close()

        self.assertEqual(
            [name for name, _arguments in domain.calls],
            ["live_patch_run"],
        )
        self.assertEqual(
            case.envelope["continuation"]["required_operation"],
            "debug_collect",
        )

    def test_mutation_case_cannot_close_before_fresh_verification(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.27",
                    "local_path": "/tmp/fix.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="close-before-verification",
                operation_id="patch-before-close",
            )
            case_id = patched.envelope["case_id"]
            waiting = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="close-before-verification",
                operation_id="read-before-close",
            )
            with self.assertRaises(CaseNotForgettable):
                service.call_tool(
                    "case_close",
                    {
                        "case_id": case_id,
                        "expected_revision": waiting["revision"],
                    },
                    task_id="close-before-verification",
                    operation_id="close-too-early",
                )
            completed = service.call_tool(
                "workflow.advance",
                {"case_id": case_id},
                task_id="close-before-verification",
                operation_id="verify-before-close",
            )
            terminal = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="close-before-verification",
                operation_id="read-after-verification",
            )
            closed = service.call_tool(
                "case_close",
                {
                    "case_id": case_id,
                    "expected_revision": terminal["revision"],
                },
                task_id="close-before-verification",
                operation_id="close-after-verification",
            )
        finally:
            service.close()

        self.assertEqual(waiting["status"], "open")
        self.assertFalse(waiting.envelope["continuation"]["workflow_complete"])
        self.assertTrue(completed["completed"])
        self.assertEqual(terminal["status"], "terminal")
        self.assertTrue(closed["closed"])

    def test_storage_pressure_does_not_forget_unresolved_workflow(self) -> None:
        backend = _DomainBackend()
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(
                    Path(temp_dir) / "context.sqlite3"
                ),
                blob_repository=InMemoryBlobRepository(),
                context_storage_soft_limit_bytes=1,
                context_maintenance_interval_seconds=0,
            )
            try:
                waiting = service.call_tool(
                    "workflow.advance",
                    {
                        "ip": "192.0.2.28",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id="storage-pressure-case",
                    operation_id="advance-to-developer",
                )
                case_id = waiting.envelope["case_id"]
                service.complete_task("storage-pressure-case")
                service.call_tool(
                    "runtime_status",
                    {},
                    task_id="storage-pressure-status",
                    operation_id="maintain-under-pressure",
                )
                retained = service.call_tool(
                    "case_read",
                    {"case_id": case_id},
                    task_id="storage-pressure-read",
                    operation_id="read-retained-case",
                )
            finally:
                service.close()

        self.assertEqual(
            retained.envelope["continuation"]["required_phase_type"],
            "developer.change",
        )
        self.assertFalse(retained.envelope["continuation"]["workflow_complete"])

    def test_json_rpc_case_read_returns_typed_continuation_and_capsule(self) -> None:
        service = RuntimeMcpService(_DomainBackend())
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="continuation-task")
        try:
            waiting = service.call_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.12",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "final_purpose": "repair and verify",
                },
                task_id="continuation-task",
                operation_id="advance-continuation",
            )
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "case_read",
                        "arguments": {"case_id": waiting.envelope["case_id"]},
                    },
                }
            )
        finally:
            service.close()

        envelope = response["result"]["structuredContent"]
        continuation = envelope["continuation"]
        self.assertEqual(continuation["intent"], "diagnose-and-fix")
        self.assertEqual(continuation["delivery_strategy"], "build-upgrade")
        self.assertEqual(continuation["targets"][0]["address"], "192.0.2.12")
        self.assertEqual(continuation["required_phase_type"], "developer.change")
        self.assertEqual(
            continuation["required_workflow_step_id"],
            "step-02-developer-change",
        )
        self.assertEqual(envelope["capsule"]["case_id"], waiting.envelope["case_id"])

    def test_stale_revision_cannot_update_target_before_conflict(self) -> None:
        service = RuntimeMcpService(_DomainBackend())
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.13"},
                task_id="revision-task",
                operation_id="debug-revision",
            )
            case_id = opened.envelope["case_id"]
            before = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="revision-task",
                operation_id="read-before-conflict",
            )
            with self.assertRaises(RevisionConflict):
                service.call_tool(
                    "debug_run",
                    {
                        "case_id": case_id,
                        "expected_revision": before["revision"] - 1,
                        "ip": "192.0.2.99",
                    },
                    task_id="revision-task",
                    operation_id="debug-stale",
                )
            after = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="revision-task",
                operation_id="read-after-conflict",
            )
        finally:
            service.close()

        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["targets"], before["targets"])

    def test_running_phase_is_finished_by_its_terminal_record(self) -> None:
        service = RuntimeMcpService(_DomainBackend())
        try:
            opened = service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.14",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="phase-lifecycle",
                operation_id="debug-phase",
            )
            case_id = opened.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="phase-lifecycle",
                operation_id="read-running",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "developer-running",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "running",
                    "source_revision": "abc123",
                    "summary": "editing source",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["unit tests"],
                },
                task_id="phase-lifecycle",
                operation_id="phase-running",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="phase-lifecycle",
                operation_id="read-completed",
            )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "developer-completed",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "source_revision": "def456",
                    "summary": "source change completed",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["unit tests"],
                },
                task_id="phase-lifecycle",
                operation_id="phase-completed",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="phase-lifecycle",
                operation_id="read-close",
            )
            closed = service.call_tool(
                "case_close",
                {"case_id": case_id, "expected_revision": case["revision"]},
                task_id="phase-lifecycle",
                operation_id="close-phase-case",
            )
        finally:
            service.close()

        self.assertTrue(closed["closed"])

    def test_interrupted_collect_resumes_the_same_attempt_and_can_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "context.sqlite3"
            blobs = InMemoryBlobRepository()
            interrupted = RuntimeMcpService(
                _CrashDuringCollectBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=blobs,
            )
            try:
                patched = interrupted.call_tool(
                    "live_patch_run",
                    {
                        "ip": "192.0.2.29",
                        "local_path": "/tmp/fix.lua",
                        "remote_path": "/opt/bmc/fix.lua",
                    },
                    task_id="interrupted-collect",
                    operation_id="patch-before-interruption",
                )
                case_id = patched.envelope["case_id"]
                with self.assertRaisesRegex(
                    SystemExit, "simulated process interruption"
                ):
                    interrupted.call_tool(
                        "debug_collect",
                        {"case_id": case_id, "profile": "freshness"},
                        task_id="interrupted-collect",
                        operation_id="interrupted-verification",
                    )
            finally:
                interrupted.close()

            recovered = RuntimeMcpService(
                _DomainBackend(),
                context_repository=SQLiteRuntimeRepository(
                    database,
                    owner_is_active=lambda _pid, _started: False,
                ),
                blob_repository=blobs,
            )
            try:
                completed = recovered.call_tool(
                    "workflow.advance",
                    {"case_id": case_id},
                    task_id="recovered-collect",
                    operation_id="continue-after-interruption",
                )
                case = recovered.call_tool(
                    "case_read",
                    {"case_id": case_id},
                    task_id="recovered-collect",
                    operation_id="read-after-recovery",
                )
                closed = recovered.call_tool(
                    "case_close",
                    {"case_id": case_id, "expected_revision": case["revision"]},
                    task_id="recovered-collect",
                    operation_id="close-after-recovery",
                )
            finally:
                recovered.close()

        verification_operations = [
            item
            for item in case["operations"]
            if item.get("operation") == "debug_collect"
        ]
        self.assertTrue(completed["completed"])
        self.assertEqual(len(verification_operations), 1)
        self.assertEqual(
            verification_operations[0]["operation_id"],
            "interrupted-verification",
        )
        self.assertEqual(verification_operations[0]["status"], "completed")
        self.assertTrue(closed["closed"])

    def test_interrupted_mutation_resumes_the_same_journal_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "context.sqlite3"
            blobs = InMemoryBlobRepository()
            first_repository = SQLiteRuntimeRepository(database)
            interrupted = RuntimeMcpService(
                _CrashDuringPatchBackend(),
                context_repository=first_repository,
                blob_repository=blobs,
            )
            try:
                with self.assertRaisesRegex(
                    SystemExit, "simulated mutation interruption"
                ):
                    interrupted.call_tool(
                        "live_patch_run",
                        {
                            "ip": "192.0.2.33",
                            "local_path": "/tmp/fix.lua",
                            "remote_path": "/opt/bmc/fix.lua",
                        },
                        task_id="interrupted-patch",
                        operation_id="interrupted-mutation",
                    )
                case_id = first_repository.case_for_task("interrupted-patch")
                self.assertIsNotNone(case_id)
            finally:
                interrupted.close()

            backend = _DomainBackend()
            recovered = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(
                    database,
                    owner_is_active=lambda _pid, _started: False,
                ),
                blob_repository=blobs,
            )
            try:
                completed = recovered.call_tool(
                    "workflow.advance",
                    {"case_id": str(case_id)},
                    task_id="recovered-patch",
                    operation_id="continue-interrupted-patch",
                )
                case = recovered.call_tool(
                    "case_read",
                    {"case_id": str(case_id)},
                    task_id="recovered-patch",
                    operation_id="read-recovered-patch",
                )
            finally:
                recovered.close()

        mutation_operations = [
            item
            for item in case["operations"]
            if item.get("operation") == "live_patch_run"
        ]
        self.assertTrue(completed["completed"])
        self.assertEqual(len(mutation_operations), 1)
        self.assertEqual(
            mutation_operations[0]["operation_id"],
            "interrupted-mutation",
        )
        self.assertEqual(mutation_operations[0]["status"], "completed")
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["live_patch_run", "debug_collect"],
        )

    def test_phase_producer_is_constrained_and_build_evidence_is_preserved(self) -> None:
        service = RuntimeMcpService(_DomainBackend())
        try:
            opened = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.15"},
                task_id="build-provenance",
                operation_id="debug-build",
            )
            case_id = opened.envelope["case_id"]
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="build-provenance",
                operation_id="read-build",
            )
            with self.assertRaisesRegex(ValueError, "openubmc-build"):
                service.call_tool(
                    "phase_record",
                    {
                        "case_id": case_id,
                        "expected_revision": case["revision"],
                        "idempotency_key": "bad-build-producer",
                        "phase_type": "build.artifact",
                        "producer_identity": "not-build",
                        "status": "completed",
                        "source_revision": "abc123",
                        "summary": "built image",
                        "artifact_path": "/tmp/openubmc.hpm",
                        "artifact_sha256": "a" * 64,
                        "product_version": "1.2.3",
                    },
                    task_id="build-provenance",
                    operation_id="bad-build-phase",
                )
            service.call_tool(
                "phase_record",
                {
                    "case_id": case_id,
                    "expected_revision": case["revision"],
                    "idempotency_key": "good-build-producer",
                    "phase_type": "build.artifact",
                    "producer_identity": "openubmc-build",
                    "status": "completed",
                    "source_revision": "abc123",
                    "summary": "built image",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "1.2.3",
                    "evidence_ids": ["build-log", "artifact-checksum"],
                },
                task_id="build-provenance",
                operation_id="good-build-phase",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="build-provenance",
                operation_id="read-build-result",
            )
        finally:
            service.close()

        build = case["workflow_phase_values"]["build.artifact"]
        self.assertEqual(build["producer_identity"], "openubmc-build")
        self.assertEqual(
            build["evidence_ids"],
            ["build-log", "artifact-checksum"],
        )

    def test_case_epoch_is_monotonic_across_mutation_domains(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.16",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/fix.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="patch-domain",
                operation_id="patch-one",
            )
            upgraded = service.call_tool(
                "upgrade_run",
                {
                    "case_id": patched.envelope["case_id"],
                    "ip": "192.0.2.16",
                    "intent": "upgrade-and-verify",
                    "delivery_strategy": "build-upgrade",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "b" * 64,
                    "product_version": "2.0",
                },
                task_id="upgrade-domain",
                operation_id="upgrade-one",
            )
        finally:
            service.close()

        self.assertEqual(patched["target_epoch"], 1)
        self.assertEqual(upgraded["target_epoch"], 2)
        upgrade_arguments = next(
            arguments for name, arguments in backend.calls if name == "upgrade_run"
        )
        self.assertEqual(upgrade_arguments["_minimum_target_epoch"], 1)

    def test_direct_mutation_tools_infer_case_intent_and_delivery(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.19",
                    "local_path": "/tmp/fix.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="default-patch-intent",
                operation_id="patch-default-intent",
            )
            patch_case = service.call_tool(
                "case_read",
                {"case_id": patched.envelope["case_id"]},
                task_id="default-patch-intent",
                operation_id="read-patch-default-intent",
            )
            upgraded = service.call_tool(
                "upgrade_run",
                {
                    "ip": "192.0.2.20",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "d" * 64,
                    "product_version": "4.0",
                },
                task_id="default-upgrade-intent",
                operation_id="upgrade-default-intent",
            )
            upgrade_case = service.call_tool(
                "case_read",
                {"case_id": upgraded.envelope["case_id"]},
                task_id="default-upgrade-intent",
                operation_id="read-upgrade-default-intent",
            )
        finally:
            service.close()

        self.assertEqual(patch_case["intent"], "live-patch")
        self.assertEqual(patch_case["delivery_strategy"], "live-patch")
        self.assertEqual(
            patch_case.envelope["continuation"]["required_operation"],
            "debug_collect",
        )
        self.assertEqual(upgrade_case["intent"], "upgrade-and-verify")
        self.assertEqual(upgrade_case["delivery_strategy"], "build-upgrade")
        self.assertEqual(
            upgrade_case.envelope["continuation"]["required_operation"],
            "debug_collect",
        )

    def test_case_epoch_floor_is_isolated_per_comparison_target(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        targets = [
            {
                "ip": "192.0.2.21",
                "target_id": "reference",
                "role": "reference",
            },
            {
                "ip": "192.0.2.22",
                "target_id": "candidate",
                "role": "candidate",
            },
        ]
        try:
            reference_patch = service.call_tool(
                "live_patch_run",
                {
                    "targets": targets,
                    "target_id": "reference",
                    "intent": "live-patch",
                    "delivery_strategy": "live-patch",
                    "local_path": "/tmp/reference.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="dual-target-epochs",
                operation_id="patch-reference",
            )
            candidate_patch = service.call_tool(
                "live_patch_run",
                {
                    "case_id": reference_patch.envelope["case_id"],
                    "target_id": "candidate",
                    "local_path": "/tmp/candidate.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="dual-target-epochs",
                operation_id="patch-candidate",
            )
            candidate_upgrade = service.call_tool(
                "upgrade_run",
                {
                    "case_id": reference_patch.envelope["case_id"],
                    "target_id": "candidate",
                    "intent": "upgrade-and-verify",
                    "delivery_strategy": "build-upgrade",
                    "artifact_path": "/tmp/candidate.hpm",
                    "artifact_sha256": "c" * 64,
                    "product_version": "3.0",
                },
                task_id="dual-target-epochs",
                operation_id="upgrade-candidate",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": reference_patch.envelope["case_id"]},
                task_id="dual-target-epochs",
                operation_id="read-dual-target-epochs",
            )
        finally:
            service.close()

        self.assertEqual(reference_patch["target_epoch"], 1)
        self.assertEqual(candidate_patch["target_epoch"], 1)
        self.assertEqual(candidate_upgrade["target_epoch"], 2)
        mutation_calls = [
            arguments
            for name, arguments in backend.calls
            if name in {"live_patch_run", "upgrade_run"}
        ]
        self.assertEqual(mutation_calls[0]["_minimum_target_epoch"], 0)
        self.assertEqual(mutation_calls[1]["_minimum_target_epoch"], 0)
        self.assertEqual(mutation_calls[2]["_minimum_target_epoch"], 1)
        self.assertEqual(
            case["capsule"]["target_epoch_floors"],
            {"candidate": 2, "reference": 1},
        )

    def test_unique_candidate_uses_the_same_implicit_target_for_epoch_tracking(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        targets = [
            {
                "ip": "192.0.2.24",
                "target_id": "reference",
                "role": "reference",
            },
            {
                "ip": "192.0.2.25",
                "target_id": "candidate",
                "role": "candidate",
            },
        ]
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "targets": targets,
                    "local_path": "/tmp/candidate.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="implicit-candidate-epoch",
                operation_id="patch-implicit-candidate",
            )
            upgraded = service.call_tool(
                "upgrade_run",
                {
                    "case_id": patched.envelope["case_id"],
                    "artifact_path": "/tmp/candidate.hpm",
                    "artifact_sha256": "e" * 64,
                    "product_version": "5.0",
                },
                task_id="implicit-candidate-epoch",
                operation_id="upgrade-implicit-candidate",
            )
            verified = service.call_tool(
                "debug_collect",
                {
                    "case_id": patched.envelope["case_id"],
                    "profile": "freshness",
                },
                task_id="implicit-candidate-epoch",
                operation_id="verify-implicit-candidate",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": patched.envelope["case_id"]},
                task_id="implicit-candidate-epoch",
                operation_id="read-implicit-candidate",
            )
        finally:
            service.close()

        self.assertEqual(patched["target_epoch"], 1)
        self.assertEqual(upgraded["target_epoch"], 2)
        self.assertEqual(verified["target_epoch"], 2)
        upgrade_arguments = next(
            arguments
            for name, arguments in backend.calls
            if name == "upgrade_run"
        )
        self.assertEqual(upgrade_arguments["_minimum_target_epoch"], 1)
        verify_arguments = next(
            arguments
            for name, arguments in backend.calls
            if name == "debug_collect"
        )
        self.assertEqual(verify_arguments["_minimum_target_epoch"], 2)
        self.assertEqual(
            case["capsule"]["target_epoch_floors"],
            {"candidate": 2},
        )
        mutation_evidence = [
            evidence
            for evidence in case["evidence_refs"]
            if str(evidence.get("provenance", "")).startswith(
                ("live_patch_run:", "upgrade_run:")
            )
        ]
        self.assertTrue(mutation_evidence)
        self.assertTrue(
            all(evidence["target_id"] == "candidate" for evidence in mutation_evidence)
        )

    def test_reference_collect_cannot_complete_candidate_patch_verification(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        targets = [
            {
                "ip": "192.0.2.31",
                "target_id": "reference",
                "role": "reference",
            },
            {
                "ip": "192.0.2.32",
                "target_id": "candidate",
                "role": "candidate",
            },
        ]
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "targets": targets,
                    "target_id": "candidate",
                    "local_path": "/tmp/candidate.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="cross-target-verification",
                operation_id="patch-candidate",
            )
            service.call_tool(
                "debug_collect",
                {
                    "case_id": patched.envelope["case_id"],
                    "target_id": "reference",
                    "profile": "freshness",
                },
                task_id="cross-target-verification",
                operation_id="collect-reference",
            )
            after_reference = service.call_tool(
                "case_read",
                {"case_id": patched.envelope["case_id"]},
                task_id="cross-target-verification",
                operation_id="read-after-reference",
            )
            completed = service.call_tool(
                "workflow.advance",
                {"case_id": patched.envelope["case_id"]},
                task_id="cross-target-verification",
                operation_id="advance-candidate-verification",
            )
        finally:
            service.close()

        self.assertFalse(
            after_reference.envelope["continuation"]["workflow_complete"]
        )
        self.assertEqual(
            after_reference.envelope["continuation"]["required_operation"],
            "debug_collect",
        )
        self.assertTrue(completed["completed"])
        collects = [
            arguments
            for name, arguments in backend.calls
            if name == "debug_collect"
        ]
        self.assertEqual(
            [(item["target_id"], item["_minimum_target_epoch"]) for item in collects],
            [("reference", 0), ("candidate", 1)],
        )

    def test_direct_fresh_collect_rejects_a_backend_that_ignores_epoch_floor(self) -> None:
        backend = _StaleCollectBackend()
        service = RuntimeMcpService(backend)
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.27",
                    "local_path": "/tmp/fix.lua",
                    "remote_path": "/opt/bmc/fix.lua",
                },
                task_id="stale-direct-collect",
                operation_id="patch-before-stale-read",
            )
            verified = service.call_tool(
                "debug_collect",
                {
                    "case_id": patched.envelope["case_id"],
                    "profile": "freshness",
                },
                task_id="stale-direct-collect",
                operation_id="stale-read",
            )
        finally:
            service.close()

        self.assertEqual(verified.envelope["status"], "failed")
        self.assertIn(
            "required target epoch",
            verified["canonical_error"]["message"],
        )

    def test_dead_sqlite_task_binding_does_not_prevent_retention_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            first = SQLiteRuntimeRepository(database, clock=lambda: 0.0)
            first.commit(
                "stale-binding-case",
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
                    PendingCaseEvent(
                        "OperationAccepted",
                        {"operation": "debug_run", "idempotency_key": "debug"},
                        "debug",
                    ),
                    PendingCaseEvent("OperationStarted", {}, "debug"),
                    PendingCaseEvent(
                        "OperationTerminal",
                        {
                            "status": "completed",
                            "summary": "done",
                            "case_status": "terminal",
                        },
                        "debug",
                    ),
                ),
            )
            first.bind_task("dead-task", "stale-binding-case")

            second = SQLiteRuntimeRepository(
                database,
                clock=lambda: 10.0,
                owner_is_active=lambda _pid, _started: False,
            )
            service = RuntimeMcpService(
                _DomainBackend(),
                context_repository=second,
                context_retention_seconds=1,
            )
            service.context_runtime.clock = lambda: 10.0
            try:
                service.context_runtime.maintain()
            finally:
                service.close()

            self.assertIsNone(second.load("stale-binding-case"))

    def test_projection_cache_checks_shared_sqlite_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            first_service = RuntimeMcpService(
                _DomainBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=InMemoryBlobRepository(),
            )
            second_service = RuntimeMcpService(
                _DomainBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=InMemoryBlobRepository(),
            )
            try:
                opened = first_service.call_tool(
                    "debug_run",
                    {"case_id": "shared-case", "ip": "192.0.2.17"},
                    task_id="first-runtime",
                    operation_id="first-debug",
                )
                cached = first_service.call_tool(
                    "case_read",
                    {"case_id": "shared-case"},
                    task_id="first-runtime",
                    operation_id="first-read",
                )
                second_service.call_tool(
                    "debug_run",
                    {"case_id": "shared-case", "ip": "192.0.2.18"},
                    task_id="second-runtime",
                    operation_id="second-debug",
                )
                refreshed = first_service.call_tool(
                    "case_read",
                    {"case_id": "shared-case"},
                    task_id="first-runtime",
                    operation_id="refreshed-read",
                )
            finally:
                first_service.close()
                second_service.close()

        self.assertEqual(opened.envelope["case_id"], "shared-case")
        self.assertGreater(refreshed["revision"], cached["revision"])
        self.assertEqual(refreshed["targets"][0]["address"], "192.0.2.18")
        self.assertNotIn(
            "192.0.2.17",
            json.dumps(refreshed["capsule"], sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
