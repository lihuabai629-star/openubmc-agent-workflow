from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openubmc_target_runtime import (
    RuntimeMcpService, SQLiteRuntimeRepository, FilesystemBlobRepository,
)
from openubmc_target_runtime.effect_activity import operation_activity
from openubmc_target_runtime.effect_runner import EffectIntent, EffectRunMode, LocalEffectRunner
from openubmc_target_runtime.capability import EffectClass
from openubmc_target_runtime.context_runtime import project_case
from test_agent_gateway import (
    SemanticBackend, BlockingDebugSemanticBackend, BlockingLivePatchSemanticBackend,
    RecoveryAwareLivePatchBackend, accept_diagnosis, artifact_ref, gate_binding,
)


class EffectActivityTests(unittest.TestCase):
    def test_wait_stops_at_durable_deadline_before_caller_timeout(self):
        backend = BlockingDebugSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            started = time.monotonic()
            with patch.object(
                service._test.run_engine.driver, "domain_metadata",
                return_value={"timeout_seconds": 0.05},
            ):
                turn = service.call_exposed_tool("execute", {
                    "kind": "start", "target": "192.0.2.1", "intent": "diagnosis-only",
                    "deadline": 1,
                }, task_id="deadline-wait", operation_id="start")
            self.assertEqual(turn["state"], "incident")
            self.assertEqual(turn["incident"]["code"], "effect_deadline_exceeded")
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(turn["progress"]["owner"], "openubmc-debug")
            self.assertNotIn("reconcile_count", turn["progress"])
            self.assertIsNone(turn["outcome"])
        finally:
            backend.release.set()
            service.close()

    def test_late_deadline_cannot_overwrite_completed_outcome(self):
        service = RuntimeMcpService(SemanticBackend())
        try:
            waiting = service.call_exposed_tool("execute", {
                "kind": "start", "target": "192.0.2.1", "intent": "diagnosis-only",
            }, task_id="late-deadline", operation_id="start")
            final = accept_diagnosis(service, waiting, task_id="late-deadline")
            run_id = final["run_id"]
            projection = service._test.context_runtime.read_case(run_id)
            intent = EffectIntent.from_mapping(projection["effect_intents"][0])
            with patch("openubmc_target_runtime.run_engine.time.time", return_value=time.time() + 10000):
                late = service._test.run_engine._commit_deadline_incident(intent)
            current = service._test.context_runtime.read_case(run_id)
            self.assertEqual(current["revision"], projection["revision"])
            self.assertEqual(late.outcome.status, "completed")
            self.assertIsNone(late.incident)
        finally:
            service.close()

    def test_deadline_incident_survives_restart_then_read_only_effect_recovers(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            backend = BlockingDebugSemanticBackend()
            first = RuntimeMcpService(
                backend, context_repository=SQLiteRuntimeRepository(root / "runs.db"),
                blob_repository=FilesystemBlobRepository(root / "blobs"),
            )
            try:
                running = first.call_exposed_tool("execute", {
                    "kind": "start", "target": "192.0.2.1", "intent": "diagnosis-only",
                    "deadline": 0.02,
                }, task_id="deadline-restart", operation_id="start")
                with patch("openubmc_target_runtime.run_engine.time.time", return_value=time.time() + 10000):
                    incident = first.call_exposed_tool("execute", {
                        "kind": "resume", "run_id": running["run_id"], "deadline": 0.02,
                    }, task_id="deadline-restart", operation_id="expire")
                self.assertEqual(incident["incident"]["code"], "effect_deadline_exceeded")
                effect_id = running["progress"]["effect_id"]
            finally:
                backend.release.set()
                first.close()
            recovered_backend = SemanticBackend()
            second = RuntimeMcpService(
                recovered_backend, context_repository=SQLiteRuntimeRepository(root / "runs.db"),
                blob_repository=FilesystemBlobRepository(root / "blobs"),
            )
            try:
                waiting = second.call_exposed_tool("execute", {
                    "kind": "resume", "run_id": running["run_id"], "deadline": 1,
                }, task_id="deadline-restart", operation_id="recover")
                self.assertEqual(waiting["state"], "waiting_response")
                self.assertIsNone(waiting["incident"])
                projection = second._test.context_runtime.read_case(running["run_id"])
                self.assertEqual([item["operation_id"] for item in projection["operations"]], [effect_id])
                self.assertEqual([name for name, _ in recovered_backend.calls], ["debug_run"])
            finally:
                second.close()

    def test_expired_mutation_restarts_by_reconciling_same_journal_without_apply(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            patch_file = root / "patch.lua"
            patch_file.write_text("return 'patched'\n")
            backend = BlockingLivePatchSemanticBackend()
            first = RuntimeMcpService(
                backend, context_repository=SQLiteRuntimeRepository(root / "runs.db"),
                blob_repository=FilesystemBlobRepository(root / "blobs"),
            )
            try:
                waiting = first.call_exposed_tool("execute", {
                    "kind": "start", "target": "192.0.2.1", "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                }, task_id="mutation-deadline", operation_id="start")
                waiting = accept_diagnosis(first, waiting, task_id="mutation-deadline")
                running = first.call_exposed_tool("execute", {
                    "kind": "respond", "run_id": waiting["run_id"], **gate_binding(waiting),
                    "submission_id": "patch", "deadline": 0.02,
                    "response": {"status": "completed", "summary": "patch ready", "payload": {
                        "source_revision": "source-1", "authored_files": ["patch.lua"],
                        "verification_plan": ["verify checksum"],
                        "artifact_ref": artifact_ref(patch_file, kind="openubmc-live-patch", target="192.0.2.1", run_id=waiting["run_id"]),
                        "remote_path": "/tmp/patch.lua", "restart_scope": "none",
                    }},
                }, task_id="mutation-deadline", operation_id="patch")
                self.assertEqual(running["state"], "running")
                with patch("openubmc_target_runtime.run_engine.time.time", return_value=time.time() + 10000):
                    incident = first.call_exposed_tool("execute", {
                        "kind": "resume", "run_id": running["run_id"], "deadline": 0.02,
                    }, task_id="mutation-deadline", operation_id="expire")
                self.assertEqual(incident["incident"]["code"], "effect_deadline_exceeded")
                effect_id = running["progress"]["effect_id"]
            finally:
                backend.release.set()
                first.close()
            recovery = RecoveryAwareLivePatchBackend()
            second = RuntimeMcpService(
                recovery, context_repository=SQLiteRuntimeRepository(root / "runs.db"),
                blob_repository=FilesystemBlobRepository(root / "blobs"),
            )
            try:
                final = second.call_exposed_tool("execute", {
                    "kind": "resume", "run_id": running["run_id"], "deadline": 1,
                }, task_id="mutation-deadline", operation_id="recover")
                self.assertEqual(final["state"], "completed")
                self.assertEqual(recovery.apply_calls, 0)
                self.assertEqual(recovery.reconcile_calls, 1)
                self.assertEqual(recovery.operation_ids, [effect_id])
                projection = second._test.context_runtime.read_case(running["run_id"])
                operation = next(item for item in projection["operations"] if item["operation_id"] == effect_id)
                self.assertEqual(operation["reconcile_count"], 1)
                self.assertIn("reconcile_requested_at", operation)
                self.assertIn("reconciled_at", operation)
            finally:
                second.close()

    def test_replay_uses_event_time_for_domain_progress_not_payload_heartbeat(self):
        projection = project_case(
            "activity-replay",
            [{
                "kind": "OperationProgressed",
                "operation_id": "effect-1",
                "revision": 1,
                "created_at": 17.5,
                "payload": {
                    "status": "running",
                    "last_progress_at": 999999.0,
                },
            }],
        )
        operation = projection["operations"][0]
        self.assertEqual(operation["last_progress_at"], 17.5)
        self.assertEqual(
            operation_activity(projection)["last_progress_at"], 17.5
        )

    def test_supervisor_heartbeat_does_not_claim_domain_progress(self):
        released = threading.Event()
        started = threading.Event()

        def execute(_intent):
            started.set()
            released.wait(2)
            return {"status": "completed"}

        intent = EffectIntent(
            run_id="activity-run", effect_id="activity-effect", operation="debug_run",
            effect_class=EffectClass.READ_ONLY, request_fingerprint="a" * 64,
            arguments={"password": "private-test-value"},
        )
        runner = LocalEffectRunner(execute, execute)
        try:
            execution = runner.ensure(intent, mode=EffectRunMode.DISPATCH)
            self.assertTrue(started.wait(1))
            activity = runner.activity(intent)
            self.assertEqual(activity["worker_state"], "running")
            self.assertEqual(activity["owner_pid"], os.getpid())
            self.assertNotIn("last_progress_at", activity)
            self.assertNotIn("private-test-value", str(activity))
            released.set()
            execution.future.result(1)
            self.assertTrue(runner.has_settled(intent))
            self.assertEqual(runner.activity(intent)["worker_state"], "settled")
            runner.acknowledge(intent, execution, retain_for_reattach=False)
            self.assertEqual(runner.activity(intent), {})
        finally:
            released.set()
            runner.close()

    def test_projection_excludes_arguments_and_preserves_last_progress(self):
        projection = {"operations": [{
            "operation_id": "effect-1", "operation": "debug_run", "status": "running",
            "started_at": 100, "last_progress_at": 101, "deadline_at": 200,
            "inputs": {"password": "secret"}, "raw_output": "x" * 100000,
        }]}
        first = operation_activity(projection)
        self.assertEqual(first, operation_activity(projection))
        self.assertEqual(first["last_progress_at"], 101)
        self.assertLess(len(str(first)), 1024)
        self.assertNotIn("secret", str(first))
        self.assertEqual(operation_activity({**projection, "run_outcome": {"status": "completed"}}), {})

    def test_expired_effect_is_incident_and_late_result_settles_without_reexecution(self):
        class BlockingBackend(SemanticBackend):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.released = threading.Event()
                self.effects = 0

            def debug_run(self, task, arguments, context):
                self.effects += 1
                self.started.set()
                self.released.wait(3)
                return super().debug_run(task, arguments, context)

        backend = BlockingBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_exposed_tool("execute", {
                "kind": "start", "target": "192.0.2.1", "intent": "diagnosis-only",
                "deadline": 0.02,
            }, task_id="activity-deadline", operation_id="activity-deadline-start")
            self.assertTrue(backend.started.wait(1))
            self.assertEqual(first["state"], "running")
            self.assertIn("deadline_at", first["progress"])
            run_id = first["run_id"]
            future_time = time.time() + 10000
            with patch("openubmc_target_runtime.run_engine.time.time", return_value=future_time):
                incident = service.call_exposed_tool("execute", {
                    "kind": "resume", "run_id": run_id, "deadline": 0.02,
                }, task_id="activity-deadline", operation_id="activity-deadline-expired")
                self.assertEqual(incident["state"], "incident")
                self.assertEqual(incident["incident"]["code"], "effect_deadline_exceeded")
                self.assertIsNone(incident["outcome"])
                replay = service.call_exposed_tool("execute", {
                    "kind": "resume", "run_id": run_id, "deadline": 0.02,
                }, task_id="activity-deadline", operation_id="activity-deadline-expired-again")
                self.assertEqual(replay["incident"]["incident_id"], incident["incident"]["incident_id"])
            backend.released.set()
            final = service.call_exposed_tool("execute", {
                "kind": "resume", "run_id": run_id, "deadline": 1,
            }, task_id="activity-deadline", operation_id="activity-deadline-settle")
            self.assertEqual(final["state"], "waiting_response")
            self.assertEqual(final["gate"]["name"], "diagnosis.acceptance")
            self.assertEqual(backend.effects, 1)
            self.assertIsNone(final["incident"])
        finally:
            backend.released.set()
            service.close()


if __name__ == "__main__":
    unittest.main()
