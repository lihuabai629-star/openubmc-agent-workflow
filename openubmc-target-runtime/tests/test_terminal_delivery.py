from __future__ import annotations

from pathlib import Path
import json
from datetime import datetime, timedelta
import tempfile
import unittest

from openubmc_target_runtime.terminal_delivery import (
    TerminalAnswerError,
    TerminalAnswerStore,
    qualify_terminal_answer,
    render_final_answer,
)


class TerminalDeliveryTests(unittest.TestCase):
    def test_normal_delivery_is_bound_to_terminal_outcome(self):
        with tempfile.TemporaryDirectory() as raw:
            store = TerminalAnswerStore(Path(raw) / "answers.json")
            outcome = {"status": "completed", "summary": "done"}
            pending = store.prepare(task_id="task", run_id="run", outcome=outcome, delivery_stage="runtime-verified", text="done")
            self.assertIn("final_answer_unconfirmed", qualify_terminal_answer(
                task_id="task", run_id="run", outcome=outcome,
                delivery_stage="runtime-verified", record=pending,
            )["failures"])
            event_time = (datetime.fromisoformat(pending.prepared_at) + timedelta(seconds=1)).isoformat()
            rollout = Path(raw) / "rollout.jsonl"
            rollout.write_text("\n".join(json.dumps(item) for item in (
                {"type": "session_meta", "payload": {"id": "task"}},
                {"type": "response_item", "timestamp": event_time,
                 "payload": {"type": "message", "id": "rollout-final-1", "role": "assistant",
                             "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]}},
            )) + "\n", encoding="utf-8")
            record = store.acknowledge_rollout(
                rollout, task_id="task", run_id="run", outcome=outcome,
                delivery_stage="runtime-verified",
            )
            self.assertEqual(qualify_terminal_answer(
                task_id="task", run_id="run", outcome=outcome,
                delivery_stage="runtime-verified", record=record,
            )["status"], "passed")

    def test_restart_recovery_and_duplicate_delivery_are_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "answers.json"
            outcome = {"status": "partial", "summary": "partial"}
            first = TerminalAnswerStore(path).prepare(task_id="task", run_id="run", outcome=outcome, delivery_stage="patched", text="partial")
            second = TerminalAnswerStore(path).prepare(task_id="task", run_id="run", outcome=outcome, delivery_stage="patched", text="ignored")
            self.assertEqual(first.delivery_id, second.delivery_id)
            self.assertEqual(TerminalAnswerStore(path).get("task").text, "partial")

    def test_missing_or_mismatched_answer_fails_closed(self):
        outcome = {"status": "blocked", "summary": "blocked"}
        missing = qualify_terminal_answer(
            task_id="task", run_id="run", outcome=outcome,
            delivery_stage="diagnosed", record=None,
        )
        self.assertIn("final_answer_missing", missing["failures"])
        with self.assertRaisesRegex(TerminalAnswerError, "another Run"):
            with tempfile.TemporaryDirectory() as raw:
                store = TerminalAnswerStore(Path(raw) / "answers.json")
                store.prepare(task_id="task", run_id="run-a", outcome=outcome, delivery_stage="diagnosed", text="blocked")
                store.prepare(task_id="task", run_id="run-b", outcome=outcome, delivery_stage="diagnosed", text="wrong")

    def test_final_text_distinguishes_status_and_next_action(self):
        text = render_final_answer(status="failed", summary="gate failed", delivery_stage="packaged", next_action="修复门禁")
        self.assertIn("失败", text)
        self.assertIn("packaged", text)
        self.assertIn("修复门禁", text)

    def test_delivery_stage_is_part_of_terminal_identity(self):
        outcome = {"status": "partial", "summary": "source verified"}
        with tempfile.TemporaryDirectory() as raw:
            store = TerminalAnswerStore(Path(raw) / "answers.json")
            record = store.prepare(
                task_id="task", run_id="run", outcome=outcome,
                delivery_stage="patched", text="partial",
            )
            mismatch = qualify_terminal_answer(
                task_id="task", run_id="run", outcome=outcome,
                delivery_stage="component-built", record=record,
            )
        self.assertIn("final_answer_stage_mismatch", mismatch["failures"])
        self.assertIn("final_answer_outcome_mismatch", mismatch["failures"])

    def test_interrupted_delivery_can_be_recovered_without_rerunning_work(self):
        outcome = {"status": "blocked", "summary": "authentication required"}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "answers.json"
            store = TerminalAnswerStore(path)
            self.assertIsNone(store.get("task"))
            text = render_final_answer(
                status="blocked", summary="authentication required",
                delivery_stage="diagnosed", next_action="本地重新登录",
            )
            recovered = TerminalAnswerStore(path).prepare(
                task_id="task", run_id="run", outcome=outcome,
                delivery_stage="diagnosed", text=text,
            )
        self.assertIn("受阻", recovered.text)
        self.assertIn("本地重新登录", recovered.text)

    def test_acknowledgement_rejects_a_different_final_text(self):
        outcome = {"status": "completed", "summary": "done"}
        with tempfile.TemporaryDirectory() as raw:
            store = TerminalAnswerStore(Path(raw) / "answers.json")
            store.prepare(task_id="task", run_id="run", outcome=outcome,
                          delivery_stage="runtime-verified", text="done")
            with self.assertRaisesRegex(TerminalAnswerError, "final text mismatch"):
                store.acknowledge(task_id="task", run_id="run", outcome=outcome,
                                  delivery_stage="runtime-verified", text="other",
                                  host_event_id="rollout-final-1")

    def test_rollout_audit_acknowledges_only_matching_host_final_event(self):
        outcome = {"status": "completed", "summary": "done"}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = TerminalAnswerStore(root / "answers.json")
            store.prepare(task_id="task", run_id="run", outcome=outcome,
                          delivery_stage="runtime-verified", text="done")
            prepared = store.get("task")
            event_time = (datetime.fromisoformat(prepared.prepared_at) + timedelta(seconds=1)).isoformat()
            rollout = root / "rollout.jsonl"
            rollout.write_text("\n".join(json.dumps(item) for item in (
                {"type": "session_meta", "payload": {"id": "task"}},
                {"type": "response_item", "timestamp": event_time,
                 "payload": {"type": "message", "id": "final-1",
                    "role": "assistant", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "done"}]}},
            )) + "\n", encoding="utf-8")
            record = store.acknowledge_rollout(
                rollout, task_id="task", run_id="run", outcome=outcome,
                delivery_stage="runtime-verified",
            )
            self.assertEqual(record.host_event_id, "final-1")
            self.assertEqual(qualify_terminal_answer(task_id="task", run_id="run", outcome=outcome,
                             delivery_stage="runtime-verified", record=record)["status"], "passed")

    def test_rollout_audit_rejects_missing_final_and_task_mismatch(self):
        outcome = {"status": "blocked", "summary": "blocked"}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = TerminalAnswerStore(root / "answers.json")
            store.prepare(task_id="task", run_id="run", outcome=outcome,
                          delivery_stage="diagnosed", text="blocked")
            rollout = root / "rollout.jsonl"
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other"}}) + "\n",
                               encoding="utf-8")
            with self.assertRaisesRegex(TerminalAnswerError, "another task"):
                store.acknowledge_rollout(rollout, task_id="task", run_id="run", outcome=outcome,
                                          delivery_stage="diagnosed")
            rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "task"}}) + "\n",
                               encoding="utf-8")
            with self.assertRaisesRegex(TerminalAnswerError, "final event is missing"):
                store.acknowledge_rollout(rollout, task_id="task", run_id="run", outcome=outcome,
                                          delivery_stage="diagnosed")

    def test_rollout_audit_rejects_an_old_final_from_the_same_task(self):
        outcome = {"status": "completed", "summary": "done"}
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            rollout = root / "rollout.jsonl"
            rollout.write_text("\n".join(json.dumps(item) for item in (
                {"type": "session_meta", "payload": {"id": "task"}},
                {"type": "response_item", "timestamp": "2020-01-01T00:00:00+00:00",
                 "payload": {"type": "message", "id": "old-final", "role": "assistant",
                             "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]}},
            )) + "\n", encoding="utf-8")
            store = TerminalAnswerStore(root / "answers.json")
            store.prepare(task_id="task", run_id="new-run", outcome=outcome,
                          delivery_stage="runtime-verified", text="done")
            with self.assertRaisesRegex(TerminalAnswerError, "final event is missing"):
                store.acknowledge_rollout(rollout, task_id="task", run_id="new-run",
                                          outcome=outcome, delivery_stage="runtime-verified")


if __name__ == "__main__":
    unittest.main()
