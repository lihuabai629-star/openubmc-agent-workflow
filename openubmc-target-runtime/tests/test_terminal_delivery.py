from __future__ import annotations

from pathlib import Path
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
            record = store.deliver(task_id="task", run_id="run", outcome=outcome, delivery_stage="runtime-verified", text="done")
            self.assertEqual(qualify_terminal_answer(task_id="task", run_id="run", outcome=outcome, record=record)["status"], "passed")

    def test_restart_recovery_and_duplicate_delivery_are_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "answers.json"
            outcome = {"status": "partial", "summary": "partial"}
            first = TerminalAnswerStore(path).deliver(task_id="task", run_id="run", outcome=outcome, delivery_stage="patched", text="partial")
            second = TerminalAnswerStore(path).deliver(task_id="task", run_id="run", outcome=outcome, delivery_stage="patched", text="ignored")
            self.assertEqual(first.delivery_id, second.delivery_id)
            self.assertEqual(TerminalAnswerStore(path).get("task").text, "partial")

    def test_missing_or_mismatched_answer_fails_closed(self):
        outcome = {"status": "blocked", "summary": "blocked"}
        missing = qualify_terminal_answer(task_id="task", run_id="run", outcome=outcome, record=None)
        self.assertIn("final_answer_missing", missing["failures"])
        with self.assertRaisesRegex(TerminalAnswerError, "another Run"):
            with tempfile.TemporaryDirectory() as raw:
                store = TerminalAnswerStore(Path(raw) / "answers.json")
                store.deliver(task_id="task", run_id="run-a", outcome=outcome, delivery_stage="diagnosed", text="blocked")
                store.deliver(task_id="task", run_id="run-b", outcome=outcome, delivery_stage="diagnosed", text="wrong")

    def test_final_text_distinguishes_status_and_next_action(self):
        text = render_final_answer(status="failed", summary="gate failed", delivery_stage="packaged", next_action="修复门禁")
        self.assertIn("失败", text)
        self.assertIn("packaged", text)
        self.assertIn("修复门禁", text)


if __name__ == "__main__":
    unittest.main()
