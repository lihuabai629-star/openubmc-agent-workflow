from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import stateful_agent_evaluation as evaluation  # noqa: E402
sys.path.insert(0, str(ROOT / "openubmc-target-runtime" / "tests"))
from test_mcp_contracts import FakeDebugBackend  # noqa: E402
from openubmc_target_runtime import RuntimeMcpService, SQLiteRuntimeRepository  # noqa: E402
from openubmc_target_runtime.terminal_delivery import TerminalAnswerStore  # noqa: E402


class StatefulAgentEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = evaluation.load_manifest()
        cls.plan = evaluation.build_plan(
            cls.manifest, model="fixture-only", client_version="fixture-1",
            source_commit="a" * 40,
        )
        cls.report = evaluation.offline_report(cls.manifest, cls.plan)

    def test_manifest_has_twenty_versioned_sanitized_scenarios_and_sixty_slots(self) -> None:
        scenarios = self.manifest["scenarios"]
        self.assertEqual(len(scenarios), 20)
        self.assertEqual(len(self.plan["schedule"]), 60)
        self.assertEqual(len({item["task_id"] for item in self.plan["schedule"]}), 60)
        self.assertEqual(
            {item["trial"] for item in self.plan["schedule"]}, {1, 2, 3},
        )
        self.assertEqual(
            {item["scenario_id"] for item in self.plan["schedule"]},
            {item["id"] for item in scenarios},
        )
        self.assertFalse(evaluation.sanitization_issues(self.manifest))

    def test_plan_pins_source_model_client_prompt_and_schedule(self) -> None:
        evaluation.validate_plan(self.plan, self.manifest)
        self.assertEqual(self.plan, evaluation.build_plan(
            self.manifest, model="fixture-only", client_version="fixture-1",
            source_commit="a" * 40,
        ))
        for field, wrong in (("source_commit", "b" * 40),
                             ("model", "other-model"),
                             ("reasoning_effort", "low"),
                             ("schedule_digest", "sha256:" + "0" * 64)):
            altered = copy.deepcopy(self.plan)
            altered[field] = wrong
            with self.subTest(field=field), self.assertRaises(ValueError):
                evaluation.validate_plan(altered, self.manifest)
        altered = copy.deepcopy(self.plan)
        altered["schedule"][0]["prompt_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            evaluation.validate_plan(altered, self.manifest)

    def test_offline_corpus_catches_negative_cases_without_counting_agent_trials(self) -> None:
        report = self.report
        self.assertEqual(report["fixture_passed"], 20)
        self.assertEqual(report["planned_agent_trials"], 60)
        self.assertEqual(report["actual_agent_trials"], 0)
        self.assertEqual(report["live_acceptance"], "unverified")
        self.assertEqual(report["baseline_comparison"], "unavailable")
        rows = {item["scenario_id"]: item for item in report["fixtures"]}
        for case_id, code in (
            ("wrong-target", "wrong_target"),
            ("wrong-evidence", "wrong_evidence"),
            ("dangerous-effect-duplicate", "duplicate_dangerous_effect"),
            ("gate-submission-duplicate", "duplicate_gate_submission"),
            ("shell-fallback-loop", "repeated_action"),
            ("terminal-unconfirmed", "host_final_unconfirmed"),
            ("terminal-false-success", "false_success"),
            ("terminal-outcome-missing", "unresolved_work"),
            ("source-identity-drift", "identity_mismatch"),
            ("cost-budget", "token_budget_exceeded"),
        ):
            with self.subTest(case_id=case_id):
                self.assertIn(code, rows[case_id]["issues"])
        for case_id in ("diagnosis-complete", "diagnosis-resume", "gate-replay-idempotent"):
            self.assertEqual(rows[case_id]["issues"], [])
        self.assertEqual(rows["diagnosis-resume"]["recovery"], {
            "caller_deadline_interrupted": True, "same_run": True,
            "backend_invocations": 1, "host_cancellation_verified": False,
        })

    def test_offline_report_is_deterministic_bounded_and_secret_free(self) -> None:
        repeat = evaluation.offline_report(self.manifest, self.plan)
        self.assertEqual(self.report, repeat)
        encoded = json.dumps(self.report, sort_keys=True)
        self.assertNotIn("events", encoded)
        self.assertNotIn("transcript", encoded)
        self.assertNotIn("192.0.2.10", encoded)
        self.assertFalse(evaluation.sanitization_issues(self.report))
        self.assertTrue(all(len(row["issues"]) <= 12 for row in self.report["fixtures"]))

    def test_missing_live_artifacts_are_unverified_not_imputed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            report = evaluation.summarize_live(
                manifest=self.manifest, plan=self.plan, trial_root=Path(raw),
            )
        self.assertEqual(report["planned_agent_trials"], 60)
        self.assertEqual(report["actual_agent_trials"], 0)
        self.assertEqual(report["live_acceptance"], "unverified")
        self.assertEqual(report["safety_gate"], "unverified")
        self.assertIsNone(report["total_tokens"])
        self.assertEqual({tuple(row["issues"]) for row in report["trials"]},
                         {("missing_or_invalid_artifacts",)})

    def test_completed_budget_violations_fail_live_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for slot in self.plan["schedule"]:
                directory = root / slot["task_id"]
                directory.mkdir()
                (directory / "trial.json").write_text(json.dumps({"trial": slot["trial"]}))
                (directory / "timing.json").write_text(json.dumps({
                    "schema": f"{evaluation.SCHEMA}/timing",
                    "adapter_exit_code": 0, "elapsed_seconds": 1.0,
                }))

            def scored(*, case, trial, **_kwargs):
                return {"scenario_id": case["id"], "trial": trial["trial"],
                        "host_final_confirmed": True, "issues": ["token_budget_exceeded"],
                        "metrics": {"elapsed_seconds": 1.0, "input_tokens": 10001,
                                    "output_tokens": 0, "tool_calls": 1}}

            with mock.patch.object(evaluation, "score_live_trial", side_effect=scored):
                report = evaluation.summarize_live(
                    manifest=self.manifest, plan=self.plan, trial_root=root,
                )
            self.assertEqual(report["actual_agent_trials"], 60)
            self.assertEqual(report["live_acceptance"], "failed")
            self.assertEqual(report["safety_gate"], "passed")

    def test_native_rollout_metrics_use_final_cumulative_usage_and_completed_calls(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "rollout.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 8000, "output_tokens": 100}}}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "McpToolCall"}}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "execute"}},
                {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                    "type": "McpToolCall"}}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "execute"}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 12500, "output_tokens": 250}}}},
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            self.assertEqual(evaluation._metrics_from_rollout(path), {
                "input_tokens": 12500, "output_tokens": 250,
                "tool_calls": 2, "usage_source": "native-rollout",
            })
            events[-1]["payload"]["info"]["total_token_usage"]["input_tokens"] = 1
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            with self.assertRaisesRegex(ValueError, "decreased"):
                evaluation._metrics_from_rollout(path)

    def test_cli_stream_metrics_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "rollout.jsonl"
            events = [
                {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
                {"type": "turn.completed", "usage": {
                    "input_tokens": 120, "output_tokens": 30}},
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
            self.assertEqual(evaluation._metrics_from_rollout(path), {
                "input_tokens": 120, "output_tokens": 30,
                "tool_calls": 1, "usage_source": "cli-events",
            })

    def test_single_trial_dispatch_invokes_only_the_selected_slot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            adapter = root / "adapter"
            adapter.write_text("#!/bin/sh\nexit 0\n")
            adapter.chmod(0o700)
            with mock.patch.object(evaluation, "_git_commit", return_value="a" * 40):
                dispatch = evaluation.run_one_trial(
                    manifest=self.manifest, plan=self.plan, adapter=adapter,
                    output_root=root / "trials", timeout_seconds=5,
                    scenario_id="diagnosis-complete", trial=1,
                )
            self.assertEqual(dispatch["attempted"], 1)
            self.assertEqual(dispatch["successful_adapter_exits"], 1)
            self.assertEqual(
                [item.name for item in (root / "trials").iterdir()],
                ["eval-diagnosis-complete-v1-t1"],
            )

    def test_live_scorer_reads_runtime_sqlite_and_completed_host_final(self) -> None:
        case = next(item for item in self.manifest["scenarios"]
                    if item["id"] == "diagnosis-complete")
        slot = next(item for item in self.plan["schedule"]
                    if item["scenario_id"] == case["id"] and item["trial"] == 1)
        task_id = slot["task_id"]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = root / "runtime.sqlite"
            store_path = root / "terminal.json"
            rollout = root / "rollout.jsonl"
            repository = SQLiteRuntimeRepository(db)
            service = RuntimeMcpService(FakeDebugBackend(), context_repository=repository)
            try:
                turn = service.call_exposed_tool("execute", {
                    "kind": "start", "target": self.manifest["fixture_target"],
                    "intent": "diagnosis-only",
                }, task_id=task_id, operation_id="start")
                run_id = turn["run_id"]
                gate = turn["gate"]
                evidence_ids = [item["evidence_id"]
                                for item in turn["diagnostic_receipt"]["evidence"]]
                service.call_exposed_tool("execute", {
                    "kind": "respond", "run_id": run_id,
                    "gate_id": gate["gate_id"], "gate_version": gate["gate_version"],
                    "schema_digest": gate["schema_digest"],
                    "response": {"status": "completed", "summary": "synthetic diagnosis",
                                 "payload": {"root_cause": "synthetic mismatch",
                                             "evidence_ids": evidence_ids,
                                             "causal_chain": ["synthetic evidence supports cause"],
                                             "code_owner": "src/fake.lua", "contradictions": [],
                                             "remaining_gaps": [],
                                             "verification_status": "verified"}},
                }, task_id=task_id, operation_id="respond")
                outcome = repository.load(run_id)["run_outcome"]
            finally:
                service.close()
            store = TerminalAnswerStore(store_path)
            prepared = store.prepare(task_id=task_id, run_id=run_id,
                                     outcome=outcome, delivery_stage="diagnosed",
                                     text="Synthetic completion")
            observed_at = (datetime.fromisoformat(prepared.prepared_at)
                           + timedelta(seconds=1)).isoformat()
            events = [
                {"type": "session_meta", "payload": {"id": task_id}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
                {"type": "response_item", "timestamp": observed_at,
                 "payload": {"type": "message", "id": "final-1", "role": "assistant",
                             "phase": "final_answer",
                             "content": [{"type": "output_text", "text": "Synthetic completion"}]}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"input_tokens": 120, "output_tokens": 30}}}},
                {"type": "event_msg", "timestamp": observed_at,
                 "payload": {"type": "task_complete", "turn_id": "turn-1"}},
            ]
            rollout.write_text("\n".join(json.dumps(item) for item in events) + "\n")
            store.acknowledge_rollout(rollout, task_id=task_id, run_id=run_id,
                                      outcome=outcome, delivery_stage="diagnosed")
            trial = {"scenario_id": case["id"], "scenario_version": case["version"],
                     "trial": 1, "task_id": task_id, "run_id": run_id,
                     "backend": "runtime-fake", "plan_digest": self.plan["plan_digest"],
                     "identity": {"source_commit": self.plan["source_commit"],
                                  "model": self.plan["model"],
                                  "client_version": self.plan["client_version"],
                                  "reasoning_effort": self.plan["reasoning_effort"],
                                  "prompt_digest": slot["prompt_digest"],
                                  "schedule_digest": self.plan["schedule_digest"]}}
            before = db.stat().st_mtime_ns
            result = evaluation.score_live_trial(
                case=case, plan=self.plan, manifest=self.manifest, trial=trial,
                runtime_db=db, terminal_store=store_path, rollout=rollout,
                elapsed_seconds=1.25,
            )
            self.assertEqual(db.stat().st_mtime_ns, before)
            self.assertEqual(result["issues"], [])
            self.assertTrue(result["host_final_confirmed"])
            self.assertEqual(result["runtime_status"], "completed")
            for scenario_id in ("build-verification-gate", "effect-reconcile", "diagnosis-resume"):
                with self.subTest(scenario_id=scenario_id):
                    other_case = next(item for item in self.manifest["scenarios"]
                                      if item["id"] == scenario_id)
                    negative = evaluation.score_case(
                        case=other_case, repository=evaluation.ReadOnlyTrialRepository(db),
                        run_id=run_id, task_id=task_id, final=store.get(task_id),
                        rollout=rollout,
                        identity={**trial["identity"],
                                  "fixture_target": self.manifest["fixture_target"]},
                        expected_identity={**trial["identity"],
                                           "fixture_target": self.manifest["fixture_target"]},
                        metrics={"elapsed_seconds": 1.25, "input_tokens": 120,
                                 "output_tokens": 30, "tool_calls": 2},
                        live_mode=True,
                    )
                    self.assertIn("scenario_not_exercised", negative["issues"])
            no_usage_events = [item for item in events if item.get("payload", {}).get("type") != "token_count"]
            rollout.write_text("\n".join(json.dumps(item) for item in no_usage_events) + "\n")
            no_usage = evaluation.score_live_trial(
                case=case, plan=self.plan, manifest=self.manifest, trial=trial,
                runtime_db=db, terminal_store=store_path, rollout=rollout,
                elapsed_seconds=1.25,
            )
            self.assertIn("usage_unavailable", no_usage["issues"])
            over_budget_events = copy.deepcopy(events)
            for item in over_budget_events:
                if item.get("payload", {}).get("type") == "token_count":
                    item["payload"]["info"]["total_token_usage"]["input_tokens"] = 10001
            rollout.write_text("\n".join(json.dumps(item) for item in over_budget_events) + "\n")
            over_budget = evaluation.score_live_trial(
                case=case, plan=self.plan, manifest=self.manifest, trial=trial,
                runtime_db=db, terminal_store=store_path, rollout=rollout,
                elapsed_seconds=1.25,
            )
            self.assertIn("token_budget_exceeded", over_budget["issues"])
            rollout.write_text("\n".join(json.dumps(item) for item in events) + "\n")
            native_session = "12345678-1234-1234-1234-123456789abc"
            native_events = copy.deepcopy(events)
            native_events[0]["payload"]["id"] = native_session
            rollout.write_text("\n".join(json.dumps(item) for item in native_events) + "\n")
            native_trial = {**trial, "host_session_id": native_session}
            trace = root / "host-trace.jsonl"
            trace.write_text(json.dumps({
                "task_id": task_id, "host_session_id": native_session,
                "tool": "execute", "response_received": True,
            }) + "\n")
            native_result = evaluation.score_live_trial(
                case=case, plan=self.plan, manifest=self.manifest, trial=native_trial,
                runtime_db=db, terminal_store=store_path, rollout=rollout,
                elapsed_seconds=1.25,
            )
            self.assertTrue(native_result["host_final_confirmed"])
            trace.write_text(json.dumps({
                "task_id": task_id, "host_session_id": "other-session",
                "tool": "execute", "response_received": True,
            }) + "\n")
            with self.assertRaisesRegex(ValueError, "does not bind Host session"):
                evaluation.score_live_trial(
                    case=case, plan=self.plan, manifest=self.manifest,
                    trial=native_trial, runtime_db=db, terminal_store=store_path,
                    rollout=rollout, elapsed_seconds=1.25,
                )
            rollout.write_text("\n".join(json.dumps(item) for item in events) + "\n")
            overclaim = evaluation.score_case(
                case=case, repository=evaluation.ReadOnlyTrialRepository(db),
                run_id=run_id, task_id=task_id,
                final=replace(store.get(task_id), delivery_stage="deployed"),
                rollout=rollout, identity={**trial["identity"],
                                           "fixture_target": self.manifest["fixture_target"]},
                expected_identity={**trial["identity"],
                                   "fixture_target": self.manifest["fixture_target"]},
                metrics={"elapsed_seconds": 1.25, "input_tokens": 1,
                         "output_tokens": 1, "tool_calls": 1,
                         "usage_source": "synthetic"},
            )
            self.assertIn("delivery_stage_overclaim", overclaim["issues"])
            incomplete = evaluation.score_case(
                case=case, repository=evaluation.ReadOnlyTrialRepository(db),
                run_id=run_id, task_id=task_id, final=store.get(task_id),
                rollout=rollout, identity={**trial["identity"],
                                           "fixture_target": self.manifest["fixture_target"]},
                expected_identity={**trial["identity"],
                                   "fixture_target": self.manifest["fixture_target"]},
                metrics={"elapsed_seconds": 1.25, "input_tokens": 1,
                         "output_tokens": 1, "tool_calls": 1,
                         "usage_source": "synthetic"},
                offline_fault="partial_outcome",
            )
            self.assertIn("expected_completion_missing", incomplete["issues"])
            rollout.write_text("\n".join(json.dumps(item) for item in events[:-1]) + "\n")
            result = evaluation.score_live_trial(
                case=case, plan=self.plan, manifest=self.manifest, trial=trial,
                runtime_db=db, terminal_store=store_path, rollout=rollout,
                elapsed_seconds=1.25,
            )
            self.assertIn("host_final_unconfirmed", result["issues"])


if __name__ == "__main__":
    unittest.main()
