from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "agent_gateway_ab.py"
SPEC = importlib.util.spec_from_file_location("agent_gateway_ab", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


EXPECTED_QUERIES = [
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Name",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Protocol",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive ResourceId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive SlotNumber",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Presence",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive TemperatureCelsius",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo Type",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo SocketId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.DriveStatus Health",
]


def candidate_observe_event(*, queries=None, complete: bool = True):
    selected_queries = EXPECTED_QUERIES if queries is None else queries
    receipt_id = "observation-test"
    return {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": "observe",
            "arguments": {
                "target": "10.121.136.200",
                "assurance": "auto",
                "freshness": {"mode": "live", "max_age_seconds": 0},
                "selectors": [
                    {
                        "id": "capabilities",
                        "kind": "capability",
                        "names": ["SSH", "Telnet", "MDBCTL", "BUSCTL"],
                    },
                    {"id": "drive", "kind": "mdb", "queries": selected_queries},
                ],
            },
            "result": {
                "structured_content": {
                    "receipt_id": receipt_id,
                    "status": "complete" if complete else "incomplete",
                    "coverage": {
                        "requested": 13,
                        "available": 13 if complete else 12,
                        "unavailable": 0,
                        "not_checked": 0 if complete else 1,
                        "complete": complete,
                    },
                    "results": {
                        "capabilities": {
                            "kind": "capability",
                            "values": [
                                {"name": name, "status": "available"}
                                for name in ("ssh", "telnet", "mdbctl", "busctl")
                            ],
                        },
                        "drive": {
                            "kind": "mdb",
                            "values": [
                                {"query_index": index, "status": "available", "value": index}
                                for index in range(9)
                            ],
                        },
                    },
                    "claims": [
                        {
                            "selector_id": selector_id,
                            "status": "grounded",
                            "receipt_id": receipt_id,
                        }
                        for selector_id in ("capabilities", "drive")
                    ],
                }
            },
        },
    }


def candidate_execute_event(kind: str, state: str, *, elapsed: float):
    structured = {
        "run_id": "run-qualified",
        "state": state,
        "gate": None,
        "outcome": None,
    }
    arguments = {"kind": kind}
    if kind == "start":
        arguments.update(
            {
                "target": "10.121.136.200",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            }
        )
        structured["gate"] = {
            "gate_id": "gate-developer",
            "version": 1,
            "schema_digest": "a" * 64,
            "owner": "openubmc-developer",
        }
    else:
        arguments.update(
            {
                "run_id": "run-qualified",
                "gate_id": "gate-developer",
                "gate_version": 1,
                "schema_digest": "a" * 64,
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "qualified-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run tests"],
                    },
                },
            }
        )
        structured["outcome"] = {"status": "completed"}
    return {
        "type": "item.completed",
        "observed_elapsed_seconds": elapsed,
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": "execute",
            "arguments": arguments,
            "result": {"structured_content": structured},
        },
    }


def baseline_execute_event(tool: str, arguments, structured, *, elapsed: float):
    return {
        "type": "item.completed",
        "observed_elapsed_seconds": elapsed,
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": tool,
            "arguments": arguments,
            "result": {"structured_content": structured},
        },
    }


class AgentGatewayAbTests(unittest.TestCase):
    def test_run_benchmark_rejects_a_dirty_candidate_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            module.subprocess.run(["git", "init"], cwd=root, check=True)
            module.subprocess.run(
                ["git", "config", "user.email", "benchmark@example.invalid"],
                cwd=root,
                check=True,
            )
            module.subprocess.run(
                ["git", "config", "user.name", "Benchmark Test"],
                cwd=root,
                check=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("clean\n", encoding="utf-8")
            module.subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            module.subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True)
            (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            args = module.argparse.Namespace(repo=root)

            with patch.object(module, "_prepare_worktree") as prepare, self.assertRaisesRegex(
                RuntimeError, "clean candidate repository"
            ):
                module.run_benchmark(args)

        prepare.assert_not_called()

    def test_prepare_arm_home_links_selected_skill_for_supported_clients(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            skill_sources = {
                name: source_root / name
                for name in ("openubmc-debug", "openubmc-developer")
            }
            for skill_source in skill_sources.values():
                skill_source.mkdir(parents=True)
            home = root / "home"
            home.mkdir()

            module.prepare_arm_home(home, source_root)

            for client_root in (".agents", ".codex"):
                for name, skill_source in skill_sources.items():
                    link = home / client_root / "skills" / name
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), skill_source.resolve())

    def test_schedule_is_balanced_and_deterministic(self) -> None:
        first = module.balanced_schedule(10, seed=7)
        second = module.balanced_schedule(10, seed=7)
        self.assertEqual(first, second)
        orders = [(a, b) for _pair, a, b in first]
        self.assertEqual(orders.count(("A", "B")), 5)
        self.assertEqual(orders.count(("B", "A")), 5)

    def test_semantic_acceptance_requires_fields_and_cautious_conclusion(self) -> None:
        text = (
            "SSH Telnet MDBCTL BUSCTL；Name Disk0，Protocol 3，ResourceId 0，"
            "SlotNumber 0，Presence 1，TemperatureCelsius 29，Type SATA/SAS，"
            "SocketId 0，Health 0。不能单独证明 ResourceId=0 异常。"
        )
        self.assertTrue(module.semantic_acceptance(text)["passed"])
        self.assertFalse(module.semantic_acceptance(text.replace("Health", ""))["passed"])

    def test_metric_parser_requires_candidate_to_use_one_observe(self) -> None:
        events = [
            candidate_observe_event(),
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                },
            },
        ]
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text(final, encoding="utf-8")
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=2,
                scenario="observation",
            )
        self.assertTrue(metric["valid"], metric)
        self.assertEqual(metric["noncached_input_plus_output"], 90)
        self.assertEqual(metric["model_turns"], 1)
        self.assertEqual(metric["time_to_next_actionable_turn_seconds"], 2)

    def test_metric_parser_rejects_wrong_scope_or_incomplete_receipt(self) -> None:
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )
        cases = (
            candidate_observe_event(queries=EXPECTED_QUERIES[:-1]),
            candidate_observe_event(complete=False),
        )
        for index, event in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                events_path = root / "events.jsonl"
                events_path.write_text(
                    "\n".join(
                        json.dumps(item)
                        for item in (
                            event,
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 10,
                                },
                            },
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                final_path = root / "final.md"
                final_path.write_text(final, encoding="utf-8")
                metric = module.metric_from_run(
                    arm="B",
                    pair=1,
                    order=1,
                    events_path=events_path,
                    final_path=final_path,
                    exit_code=0,
                    duration_seconds=2,
                    scenario="observation",
                )
            self.assertFalse(metric["valid"])
            self.assertFalse(metric["scope_acceptance"])

    def test_execute_metric_requires_one_gate_roundtrip_without_polling(self) -> None:
        events = [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "start"}},
            candidate_execute_event("start", "waiting_response", elapsed=1.25),
            {"type": "item.completed", "item": {"type": "agent_message", "text": "respond"}},
            candidate_execute_event("respond", "completed", elapsed=2.5),
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                },
            },
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text(
                "source-only Runtime Outcome completed，未修改目标。",
                encoding="utf-8",
            )
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=3,
                scenario="execute-source-only",
            )

        self.assertTrue(metric["valid"], metric)
        self.assertEqual(metric["model_turns"], 2)
        self.assertEqual(metric["gate_roundtrips"], 1)
        self.assertEqual(metric["resume_calls"], 0)
        self.assertEqual(metric["time_to_next_actionable_turn_seconds"], 1.25)

    def test_execute_metric_rejects_polling_or_duplicate_gate_response(self) -> None:
        events = [
            candidate_execute_event("start", "waiting_response", elapsed=1),
            candidate_execute_event("respond", "completed", elapsed=2),
            candidate_execute_event("respond", "completed", elapsed=3),
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text("source-only Runtime Outcome completed", encoding="utf-8")
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=3,
                scenario="execute-source-only",
            )

        self.assertFalse(metric["valid"])
        self.assertFalse(metric["scope_acceptance"])

    def test_baseline_execute_metric_requires_compatibility_terminal_workflow(self) -> None:
        phase_contract = {
            "case_id": "case-qualified",
            "expected_revision": 2,
            "idempotency_key": "qualification-development",
            "phase_type": "developer.change",
            "producer_identity": "openubmc-developer",
        }
        events = [
            baseline_execute_event(
                "workflow.advance",
                {
                    "ip": "10.121.136.200",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "qualify Runtime source-only execution",
                },
                {
                    "status": "waiting_phase_record",
                    "required_skill": "openubmc-developer",
                    "handoff_arguments": {
                        "phase_record_contract": phase_contract,
                    },
                    "agent_envelope": {
                        "case_id": "case-qualified",
                        "revision": 5,
                    },
                },
                elapsed=10,
            ),
            baseline_execute_event(
                "phase_record",
                {
                    **phase_contract,
                    "expected_revision": 5,
                    "status": "completed",
                    "source_revision": "qualification-source",
                    "summary": "qualification source-only receipt completed",
                    "authored_files": ["src/qualification.lua"],
                    "verification_plan": ["run qualification tests"],
                },
                {"status": "completed", "case_id": "case-qualified"},
                elapsed=20,
            ),
            baseline_execute_event(
                "workflow.next",
                {"case_id": "case-qualified"},
                {"status": "completed", "completed": True, "case_id": "case-qualified"},
                elapsed=30,
            ),
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text(
                "source-only Runtime Outcome completed", encoding="utf-8"
            )
            metric = module.metric_from_run(
                arm="A",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=31,
                scenario="execute-source-only",
            )

        self.assertTrue(metric["valid"])
        self.assertEqual(metric["gate_roundtrips"], 1)
        self.assertEqual(metric["time_to_next_actionable_turn_seconds"], 10)

    def test_analyzer_passes_ten_good_pairs_and_expands_uncertain_result(self) -> None:
        passing = []
        for pair in range(1, 11):
            passing.extend(
                (
                    {
                        "arm": "A",
                        "pair": pair,
                        "valid": True,
                        "total_tokens": 100,
                        "noncached_input_plus_output": 100,
                        "duration_seconds": 100,
                        "tool_output_bytes": 100,
                        "model_turns": 4,
                        "time_to_next_actionable_turn_seconds": 100,
                    },
                    {
                        "arm": "B",
                        "pair": pair,
                        "valid": True,
                        "total_tokens": 105,
                        "noncached_input_plus_output": 105,
                        "duration_seconds": 105,
                        "tool_output_bytes": 105,
                        "model_turns": 4,
                        "time_to_next_actionable_turn_seconds": 105,
                    },
                )
            )
        self.assertEqual(module.analyze(passing)["decision"], "passed")

        uncertain = [dict(item) for item in passing]
        for item in uncertain:
            if item["arm"] == "B":
                item["duration_seconds"] = 130
        result = module.analyze(uncertain)
        self.assertEqual(result["decision"], "collect_more")
        self.assertEqual(result["next_pair_target"], 20)

    def test_analyzer_marks_legacy_metrics_incomplete_instead_of_crashing(self) -> None:
        legacy = []
        for pair in range(1, 11):
            for arm in ("A", "B"):
                legacy.append(
                    {
                        "arm": arm,
                        "pair": pair,
                        "valid": True,
                        "total_tokens": 100,
                        "noncached_input_plus_output": 100,
                        "duration_seconds": 100,
                        "tool_output_bytes": 100,
                    }
                )

        result = module.analyze(legacy)

        self.assertEqual(result["decision"], "collect_more")
        self.assertEqual(result["valid_pairs"], 0)
        self.assertIn(
            "model_turns",
            result["invalid_pairs"][0]["missing_or_nonpositive_metrics"]["A"],
        )

    def test_release_evidence_records_source_environment_thresholds_and_digests(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics = root / "all_metrics.json"
            schedule = root / "schedule.json"
            metrics.write_text("[]\n", encoding="utf-8")
            schedule.write_text("[]\n", encoding="utf-8")
            evidence = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit="b" * 40,
                model="gpt-qualified",
                metrics_path=metrics,
                schedule_path=schedule,
                analysis={"valid_pairs": 10, "invalid_pairs": []},
                environment={"python": "3.12", "node": "v22"},
            )

        self.assertEqual(evidence["samples"]["valid_pairs"], 10)
        self.assertEqual(evidence["source"]["candidate_commit"], "a" * 40)
        self.assertIn("geometric_mean_ratio_max", evidence["thresholds"])
        self.assertRegex(evidence["environment_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(evidence["evidence_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_verify_summary_accepts_current_untampered_release_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text("[]\n", encoding="utf-8")
            schedule_path.write_text("[]\n", encoding="utf-8")
            analysis = {
                "schema": module.SCHEMA,
                "valid_pairs": 10,
                "invalid_pairs": [],
                "decision": "passed",
                "thresholds": dict(module.THRESHOLDS),
                "metrics": {
                    metric: {"passed": True} for metric in module.METRICS
                },
            }
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit="b" * 40,
                model="gpt-qualified",
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                analysis=analysis,
                environment={"python": "3.12", "node": "v22"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(
                json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            verified = module.verify_summary(
                summary_path, expected_source_commit="a" * 40
            )

        self.assertTrue(verified["promotable"], verified)
        self.assertRegex(verified["summary_sha256"], r"^[0-9a-f]{64}$")

    def test_verify_summary_rejects_source_mismatch_and_artifact_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            metrics_path = root / "all_metrics.json"
            schedule_path = root / "schedule.json"
            metrics_path.write_text("[]\n", encoding="utf-8")
            schedule_path.write_text("[]\n", encoding="utf-8")
            analysis = {
                "schema": module.SCHEMA,
                "valid_pairs": 10,
                "invalid_pairs": [],
                "decision": "passed",
                "thresholds": dict(module.THRESHOLDS),
                "metrics": {
                    metric: {"passed": True} for metric in module.METRICS
                },
            }
            analysis["release_evidence"] = module.release_evidence(
                scenario="execute-source-only",
                requested_pairs=10,
                candidate_source_commit="a" * 40,
                baseline_source_commit="b" * 40,
                model="gpt-qualified",
                metrics_path=metrics_path,
                schedule_path=schedule_path,
                analysis=analysis,
                environment={"python": "3.12"},
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(analysis), encoding="utf-8")
            metrics_path.write_text("tampered\n", encoding="utf-8")

            verified = module.verify_summary(
                summary_path, expected_source_commit="c" * 40
            )

        self.assertFalse(verified["promotable"])
        self.assertTrue(any("candidate source commit" in error for error in verified["errors"]))
        self.assertTrue(any("all_metrics digest" in error for error in verified["errors"]))


if __name__ == "__main__":
    unittest.main()
