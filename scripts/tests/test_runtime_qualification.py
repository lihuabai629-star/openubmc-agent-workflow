from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runtime_qualification.py"
SPEC = importlib.util.spec_from_file_location("runtime_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class RuntimeQualificationTests(unittest.TestCase):
    @staticmethod
    def stability_report(source_commit: str) -> str:
        report = {
            "schema": "openubmc-agent-workflow.runtime-stability.v1",
            "source_commit": source_commit,
            "environment": {
                "python": "3.11.0",
                "python_implementation": "CPython",
                "platform": "test",
            },
            "environment_fingerprint": "",
            "parameters": {
                "storm_workers": 16,
                "gate_workers": 8,
                "capacity_runs": 128,
                "capacity_batch_size": 32,
                "soak_restart_cycles": 4,
                "soak_runs_per_cycle": 16,
                "max_capacity_seconds": 30.0,
                "max_capacity_peak_rss_bytes": 536870912,
                "max_capacity_peak_python_bytes": 134217728,
                "max_capacity_storage_bytes": 67108864,
                "max_soak_seconds": 30.0,
                "max_soak_peak_rss_bytes": 536870912,
                "max_soak_peak_bytes": 134217728,
                "max_soak_storage_bytes": 33554432,
                "max_events_per_run": 16,
            },
            "scenarios": {
                "duplicate_storm": {
                    "status": "passed",
                    "execute_calls": 18,
                    "failed_calls": 0,
                    "unique_runs": 1,
                    "operation_count": 1,
                    "command_decisions": 1,
                    "outcome_events": 1,
                    "open_incidents": 0,
                    "same_key_different_hash_rejected": True,
                },
                "gate_concurrency": {
                    "status": "passed",
                    "execute_calls": 8,
                    "failed_calls": 0,
                    "unique_runs": 1,
                    "gate_submissions": 1,
                    "outcome_events": 1,
                    "open_incidents": 0,
                },
                "capacity": {
                    "status": "passed",
                    "execute_calls": 128,
                    "failed_calls": 0,
                    "completed_turns": 128,
                    "completed_runs": 128,
                    "invalid_runs": 0,
                    "outcome_events": 128,
                    "open_incidents": 0,
                    "incomplete_operations": 0,
                    "total_events": 1408,
                    "events_per_batch": [352, 352, 352, 352],
                    "cumulative_events_by_batch": [352, 704, 1056, 1408],
                    "storage_bytes_by_batch": [1000, 2000, 3000, 4000],
                    "max_events_per_run": 11,
                    "storage_bytes": 4000,
                    "peak_rss_bytes": 100000000,
                    "peak_python_allocation_bytes": 1000000,
                    "elapsed_seconds": 5.0,
                },
                "restart_soak": {
                    "status": "passed",
                    "execute_calls": 128,
                    "failed_calls": 0,
                    "completed_turns": 128,
                    "completed_runs": 64,
                    "replay_mismatches": 0,
                    "invalid_runs": 0,
                    "outcome_events": 64,
                    "open_incidents": 0,
                    "incomplete_operations": 0,
                    "total_events": 704,
                    "events_per_cycle": [176, 176, 176, 176],
                    "cumulative_events_by_cycle": [176, 352, 528, 704],
                    "storage_bytes_by_cycle": [1000, 2000, 3000, 4000],
                    "max_events_per_run": 11,
                    "storage_bytes": 4000,
                    "peak_rss_bytes": 100000000,
                    "peak_traced_memory_bytes": 1000000,
                    "elapsed_seconds": 5.0,
                },
            },
            "promotable": True,
        }
        report["environment_fingerprint"] = qualification.evidence_fingerprint(
            report["environment"]
        )
        report["evidence_digest"] = qualification.evidence_fingerprint(report)
        return json.dumps(report)

    def test_all_safety_qualifications_must_pass_with_zero_violations(self) -> None:
        calls: list[tuple[str, ...]] = []

        def succeed(command, *, cwd):
            self.assertTrue(cwd.is_dir())
            calls.append(tuple(command))
            stdout = "ok"
            if any("runtime_stability.py" in str(item) for item in command):
                source = command[command.index("--source-commit") + 1]
                stdout = self.stability_report(source)
            return subprocess.CompletedProcess(command, 0, stdout, "")

        report = qualification.qualify_runtime(
            Path.cwd(),
            executor=succeed,
            source_commit="a" * 40,
            environment={"python": "3.11.0", "platform": "test"},
        )

        self.assertTrue(report["promotable"])
        self.assertEqual(
            report["violations"],
            {
                "duplicate_dangerous_effects": 0,
                "false_successes": 0,
                "wrong_target_or_artifact_mutations": 0,
                "unknown_new_identity_retries": 0,
                "real_backend_crash_cuts": 0,
                "runtime_stability": 0,
            },
        )
        self.assertTrue(report["ordinary_partial_result_accepted"])
        self.assertEqual(len(calls), 7)
        self.assertEqual(report["source_commit"], "a" * 40)
        self.assertEqual(
            report["environment"],
            {"platform": "test", "python": "3.11.0"},
        )
        self.assertEqual(
            report["environment_fingerprint"],
            qualification.evidence_fingerprint(report["environment"]),
        )
        self.assertEqual(report["parameters"]["stability_profile"], "ci")
        stability_call = next(
            command
            for command in calls
            if any("runtime_stability.py" in item for item in command)
        )
        self.assertEqual(
            stability_call[stability_call.index("--source-commit") + 1],
            "a" * 40,
        )

    def test_failed_safety_qualification_blocks_promotion(self) -> None:
        call_count = 0

        def fail_second(command, *, cwd):
            nonlocal call_count
            call_count += 1
            stdout = ""
            if any("runtime_stability.py" in str(item) for item in command):
                source = command[command.index("--source-commit") + 1]
                stdout = self.stability_report(source)
            return subprocess.CompletedProcess(
                command,
                7 if call_count == 2 else 0,
                stdout,
                "qualified invariant failed" if call_count == 2 else "",
            )

        report = qualification.qualify_runtime(Path.cwd(), executor=fail_second)

        self.assertFalse(report["promotable"])
        self.assertGreater(report["violations"]["false_successes"], 0)
        self.assertEqual(call_count, 7)

    def test_incomplete_stability_report_blocks_promotion(self) -> None:
        def incomplete(command, *, cwd):
            del cwd
            stdout = (
                '{"promotable":true}'
                if any("runtime_stability.py" in str(item) for item in command)
                else "ok"
            )
            return subprocess.CompletedProcess(command, 0, stdout, "")

        report = qualification.qualify_runtime(
            Path.cwd(),
            executor=incomplete,
            source_commit="a" * 40,
        )

        self.assertFalse(report["promotable"])
        self.assertEqual(report["violations"]["runtime_stability"], 1)
        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertIn("schema", stability_result["verification_error"])

    def test_stability_report_over_a_hard_threshold_blocks_promotion(self) -> None:
        def over_threshold(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            soak = report["scenarios"]["restart_soak"]
            soak["total_events"] = 2048
            soak["cumulative_events_by_cycle"][-1] = 2048
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            Path.cwd(),
            executor=over_threshold,
            source_commit="a" * 40,
        )

        self.assertFalse(report["promotable"])
        self.assertEqual(report["violations"]["runtime_stability"], 1)
        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertIn("threshold", stability_result["verification_error"])

    def test_stability_report_with_wrong_environment_fingerprint_blocks_promotion(
        self,
    ) -> None:
        def wrong_fingerprint(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            report["environment_fingerprint"] = "sha256:" + "0" * 64
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            Path.cwd(),
            executor=wrong_fingerprint,
            source_commit="a" * 40,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("environment fingerprint", stability_result["verification_error"])

    def test_capacity_rss_over_the_hard_threshold_blocks_promotion(self) -> None:
        def over_rss(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            report["scenarios"]["capacity"]["peak_rss_bytes"] = 536870913
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            Path.cwd(),
            executor=over_rss,
            source_commit="a" * 40,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("capacity", stability_result["verification_error"])


if __name__ == "__main__":
    unittest.main()
