from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openubmc_target_runtime.host_continuity import (
    HostContinuity, META_KEY, read_runtime_projection,
)
from openubmc_target_runtime import JsonRpcMcpEndpoint, RuntimeMcpService, SQLiteRuntimeRepository
from test_mcp_contracts import FakeDebugBackend


class HostContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = HostContinuity(self.root / "host")
        self.projection = {"status": "open", "intent": "diagnosis-only", "targets": []}

    def read(self, _run_id):
        return self.projection

    def capture(self, run_id="run", task_id="task"):
        return self.store.capture(task_id, {"run_id": run_id}, read_run=self.read)

    def terminal(self, status="completed"):
        self.projection.update(status="terminal", run_outcome={
            "status": status, "summary": "verified result", "outcome_id": "outcome-1",
        })

    def test_restart_reads_current_runtime_not_cached_status(self):
        self.capture()
        self.terminal()
        recovered = HostContinuity(self.root / "host").handoff("task", read_run=self.read)
        run = recovered["runs"][0]
        self.assertEqual(run["turn"]["state"], "completed")
        self.assertNotIn("resume_action", run)
        self.assertFalse(run["terminal_answer"]["delivery_confirmed"])

    def test_notes_are_not_authority_and_cannot_fake_a_completed_run(self):
        self.capture()
        self.store.save_notes("task", {"goal": "already completed", "hypotheses": ["done"]})
        recovered = self.store.handoff("task", read_run=self.read)
        self.assertFalse(recovered["notes_authoritative"])
        self.assertEqual(recovered["runs"][0]["turn"]["state"], "running")
        self.assertNotIn("terminal_answer", recovered["runs"][0])
        with self.assertRaises(ValueError):
            self.store.save_notes("task", {"run_outcome": {"status": "completed"}})

    def test_missing_ledger_never_recovers_an_old_success(self):
        self.terminal()
        self.capture()
        recovered = self.store.handoff("task", read_run=lambda _: None)
        self.assertEqual(recovered["runs"][0]["status"], "runtime_unavailable")
        self.assertNotIn("terminal_answer", recovered["runs"][0])
        self.assertNotIn("resume_action", recovered["runs"][0])

    def test_multiple_runs_and_tasks_do_not_overwrite_each_other(self):
        self.terminal()
        for task, run in (("task", "one"), ("task", "two"), ("other", "one")):
            self.capture(run, task)
        first = self.store.handoff("task", read_run=self.read)
        second = self.store.handoff("other", read_run=self.read)
        self.assertEqual(len(first["runs"]), 2)
        self.assertEqual(len(second["runs"]), 1)
        ids = [run["terminal_answer"]["delivery_id"] for run in first["runs"] + second["runs"]]
        self.assertEqual(len(set(ids)), 3)

    def test_repeated_capture_keeps_prepared_answer_identity(self):
        self.terminal("partial")
        self.projection["run_outcome"]["remaining_work"] = [{"summary": "build not verified"}]
        first = self.capture()
        second = self.capture()
        self.assertEqual(first["terminal_answer"], second["terminal_answer"])
        self.assertIn("build not verified", second["terminal_answer"]["text"])
        self.assertIn("部分完成", second["terminal_answer"]["text"])
        self.assertIn("unverified", second["terminal_answer"]["text"])

    def test_changed_outcome_is_not_acknowledged_as_old_result(self):
        self.terminal()
        self.capture()
        self.projection["run_outcome"]["summary"] = "different"
        run = self.store.handoff("task", read_run=self.read)["runs"][0]
        self.assertEqual(run["status"], "runtime_unavailable")
        self.assertNotIn("terminal_answer", run)

    def test_real_final_evidence_is_required_for_acknowledgement(self):
        self.terminal()
        answer = self.capture()["terminal_answer"]
        rollout = self.root / "rollout.jsonl"
        timestamp = (datetime.fromisoformat(answer["prepared_at"]) + timedelta(seconds=1)).isoformat()
        events = [
            {"type": "session_meta", "payload": {"id": "task"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {"type": "response_item", "timestamp": timestamp, "payload": {
                "type": "message", "role": "assistant", "phase": "commentary", "id": "final",
                "content": [{"type": "output_text", "text": answer["text"]}],
            }},
            {"type": "event_msg", "timestamp": timestamp,
             "payload": {"type": "task_complete", "turn_id": "turn"}},
        ]
        def write():
            rollout.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        write()
        with self.assertRaises(ValueError):
            self.store.acknowledge_rollout("task", "run", rollout, read_run=self.read)
        events[2]["payload"]["phase"] = "final_answer"
        write()
        self.store.acknowledge_rollout("task", "run", rollout, read_run=self.read)
        self.assertTrue(self.capture()["terminal_answer"]["delivery_confirmed"])
        with self.assertRaises(ValueError):
            self.store.acknowledge_rollout("other", "run", rollout, read_run=self.read)

    def test_parallel_store_instances_do_not_lose_bookmarks(self):
        def capture(index):
            HostContinuity(self.root / "host").capture(
                "task", {"run_id": f"run-{index}"}, read_run=self.read,
            )
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(capture, range(12)))
        self.assertEqual(len(self.store.handoff("task", read_run=self.read)["runs"]), 12)

    def test_failed_readback_still_leaves_a_recoverable_bookmark(self):
        with self.assertRaises(OSError):
            self.store.capture("task", {"run_id": "run"}, read_run=lambda _: (_ for _ in ()).throw(OSError()))
        self.assertEqual(len(self.store.handoff("task", read_run=self.read)["runs"]), 1)

    def test_input_bounds_and_task_identity(self):
        for notes in ({"goal": "x" * 30000}, {"hypotheses": list(range(33))}, {"goal": 42}):
            with self.assertRaises(ValueError):
                self.store.save_notes("task", notes)
        with self.assertRaises(ValueError):
            self.capture(task_id="../../escape")

    def test_readonly_ledger_does_not_create_missing_database(self):
        path = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            read_runtime_projection(path, "run")
        self.assertFalse(path.exists())

    def test_unknown_mutation_incident_does_not_offer_blind_resume(self):
        self.projection["current_incident"] = {
            "incident_id": "incident-1", "code": "mutation_outcome_unknown",
            "message": "unknown Effect", "status": "open",
        }
        self.capture()
        run = self.store.handoff("task", read_run=self.read)["runs"][0]
        self.assertNotIn("resume_action", run)
        self.assertEqual(run["turn"]["state"], "incident")

    def test_mcp_real_execute_wires_bookmark_without_changing_tool_set(self):
        repo = SQLiteRuntimeRepository(self.root / "runtime.sqlite3")
        service = RuntimeMcpService(FakeDebugBackend(), context_repository=repo, host_continuity=self.store)
        self.addCleanup(service.close)
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="task")
        response = endpoint.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "execute", "arguments": {"kind": "start", "target": "192.0.2.1", "intent": "diagnosis-only"},
        }})
        result = response["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["_meta"][META_KEY]["status"], "saved")
        self.assertNotIn("host_metadata", result["structuredContent"])
        self.assertEqual({t["name"] for t in service.tool_definitions()}, {"observe", "execute"})
        handoff = self.store.handoff("task", read_run=lambda rid: read_runtime_projection(repo.path, rid))
        self.assertEqual(handoff["runs"][0]["run_id"], result["structuredContent"]["run_id"])

    def test_native_thread_metadata_binds_hooks_to_same_real_terminal_run(self):
        repo = SQLiteRuntimeRepository(self.root / "runtime.sqlite3")
        backend = FakeDebugBackend()
        service = RuntimeMcpService(backend, context_repository=repo, host_continuity=self.store)
        self.addCleanup(service.close)
        endpoint = JsonRpcMcpEndpoint(service)
        def call(action):
            return endpoint.handle({"jsonrpc": "2.0", "id": action["kind"], "method": "tools/call",
                                    "params": {"name": "execute", "arguments": action,
                                               "_meta": {"threadId": "native-thread"}}})["result"]
        start = call({"kind": "start", "target": "192.0.2.1", "intent": "diagnosis-only"})
        turn = start["structuredContent"]
        gate = turn["gate"]
        final = call({"kind": "control", "run_id": turn["run_id"], "command": "cancel",
                      **{key: gate[key] for key in ("gate_id", "gate_version", "schema_digest")}})
        self.assertFalse(final["isError"], final)
        self.assertEqual(final["structuredContent"]["state"], "cancelled")
        recovered = HostContinuity(self.root / "host")
        read = lambda rid: read_runtime_projection(repo.path, rid)
        answer = recovered.handoff("native-thread", read_run=read)["runs"][0]["terminal_answer"]
        recovered.handle_hook({"hook_event_name": "Stop", "session_id": "native-thread",
                               "turn_id": "native-turn", "last_assistant_message": answer["text"]}, read_run=read)
        self.assertFalse(recovered.handoff("native-thread", read_run=read)["runs"][0]["terminal_answer"]["delivery_confirmed"])
        rollout = self.root / "native-rollout.jsonl"
        timestamp = (datetime.fromisoformat(answer["prepared_at"]) + timedelta(seconds=1)).isoformat()
        rollout.write_text("\n".join(json.dumps(item) for item in (
            {"type": "session_meta", "payload": {"id": "native-thread"}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "native-turn"}},
            {"type": "response_item", "timestamp": timestamp, "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer", "id": "final",
                "content": [{"type": "output_text", "text": answer["text"]}],
            }},
            {"type": "event_msg", "timestamp": timestamp,
             "payload": {"type": "task_complete", "turn_id": "native-turn"}},
        )) + "\n", encoding="utf-8")
        recovered.acknowledge_rollout("native-thread", turn["run_id"], rollout, read_run=read)
        self.assertTrue(recovered.handoff("native-thread", read_run=read)["runs"][0]["terminal_answer"]["delivery_confirmed"])
        self.assertEqual(sum(task.calls.count("debug_run") for task in backend.created), 1)
        self.assertEqual(recovered.handoff("different-thread", read_run=read)["runs"], [])

    def test_storage_failure_does_not_fail_or_repeat_successful_execute(self):
        store = unittest.mock.Mock()
        store.capture.side_effect = OSError("synthetic private marker")
        service = RuntimeMcpService(FakeDebugBackend(), host_continuity=store)
        self.addCleanup(service.close)
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="task")
        with patch.object(service._runtime.agent, "execute", return_value={"run_id": "run", "state": "running"}) as execute:
            response = endpoint.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "execute", "arguments": {"kind": "resume", "run_id": "run"},
            }})
        self.assertEqual(execute.call_count, 1)
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["_meta"][META_KEY]["status"], "unavailable")
        self.assertNotIn("synthetic private marker", json.dumps(response))

    def test_stop_hook_requests_only_one_text_delivery_and_leaves_exact_candidate_pending(self):
        self.terminal()
        answer = self.capture()["terminal_answer"]
        event = {"session_id": "task", "hook_event_name": "Stop", "turn_id": "turn-1",
                 "stop_hook_active": False, "last_assistant_message": None}
        response = self.store.handle_hook(event, read_run=self.read)
        self.assertEqual(response["decision"], "block")
        self.assertIn("Do not call execute", response["reason"])
        event["stop_hook_active"] = True
        self.assertNotIn("decision", self.store.handle_hook(event, read_run=self.read))
        event["last_assistant_message"] = answer["text"]
        self.assertEqual(self.store.handle_hook(event, read_run=self.read), {})
        confirmed = self.capture()["terminal_answer"]
        self.assertFalse(confirmed["delivery_confirmed"])
        self.assertEqual(self.store.handle_hook(event, read_run=self.read), {})

    def test_noncanonical_model_answer_is_not_replaced_or_falsely_confirmed(self):
        self.terminal()
        self.capture()
        result = self.store.handle_hook({"session_id": "task", "hook_event_name": "Stop",
                                        "turn_id": "turn", "last_assistant_message": "A rich explanation"},
                                       read_run=self.read)
        self.assertEqual(result, {})
        self.assertFalse(self.capture()["terminal_answer"]["delivery_confirmed"])

    def test_multiple_run_stop_hook_accepts_only_exact_composite_as_a_candidate(self):
        self.terminal()
        first = self.capture("one")["terminal_answer"]["text"]
        second = self.capture("two")["terminal_answer"]["text"]
        event = {"session_id": "task", "hook_event_name": "Stop", "turn_id": "turn",
                 "last_assistant_message": first + "\n\n" + second}
        self.store.handle_hook(event, read_run=self.read)
        runs = self.store.handoff("task", read_run=self.read)["runs"]
        self.assertFalse(any(run["terminal_answer"]["delivery_confirmed"] for run in runs))
        event["last_assistant_message"] = first + "\n\n" + second + " extra"
        self.assertEqual(self.store.handle_hook(event, read_run=self.read), {})

    def test_final_answer_exposes_runtime_stage_and_next_evidence(self):
        self.terminal("partial")
        self.projection["closeout"] = {"delivery_stage": {
            "highest": "component-built", "next": "product-built", "stages": {
                "product-built": {"verified": False, "required": "product artifact identity and build evidence"},
            },
        }}
        answer = self.capture()["terminal_answer"]["text"]
        self.assertIn("交付阶段：component-built", answer)
        self.assertIn("下一未验证阶段：product-built", answer)
        self.assertIn("所需证据：product artifact identity and build evidence", answer)

    def test_session_start_restores_notes_without_execution_or_secret_authority(self):
        self.capture()
        self.store.save_notes("task", {"goal": "find NIC", "open_questions": ["MDB created?"]})
        response = self.store.handle_hook({"session_id": "task", "hook_event_name": "SessionStart"}, read_run=self.read)
        context = response["hookSpecificOutput"]["additionalContext"]
        self.assertIn("find NIC", context)
        self.assertIn("untrusted reasoning", context)
        self.assertIn("running", context)
        self.assertEqual(self.store.handle_hook({"session_id": "other", "hook_event_name": "SessionStart"}, read_run=self.read), {})


if __name__ == "__main__":
    unittest.main()
