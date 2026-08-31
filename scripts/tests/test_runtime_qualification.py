from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import runtime_stability
from scripts.runtime_stability_contract import (
    verify_legacy_runtime_stability_report,
)


SCRIPT = Path(__file__).resolve().parents[1] / "runtime_qualification.py"
WORKSPACE = SCRIPT.parents[1]
SOURCE_COMMIT = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=WORKSPACE,
    check=True,
    text=True,
    stdout=subprocess.PIPE,
).stdout.strip()
SPEC = importlib.util.spec_from_file_location("runtime_qualification", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qualification)


class RuntimeQualificationTests(unittest.TestCase):
    def assert_test_names_resolve(
        self,
        test_names: tuple[str, ...],
        *,
        cwd: Path,
    ) -> None:
        probe = (
            "import sys, unittest; "
            "loader = unittest.TestLoader(); "
            "suite = loader.loadTestsFromNames(sys.argv[1:]); "
            "errors = '\\n'.join(loader.errors); "
            "expected = len(sys.argv) - 1; "
            "actual = suite.countTestCases(); "
            "print(errors or f'resolved={actual}/{expected}'); "
            "raise SystemExit(1 if errors or actual != expected else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe, *test_names],
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )

    def test_registered_qualification_tests_are_resolvable(self) -> None:
        runtime_test_names = tuple(
            test_name
            for _group_name, test_names in qualification.QUALIFICATIONS
            for test_name in test_names
        ) + qualification.PARTIAL_RESULT_TESTS
        self.assert_test_names_resolve(
            runtime_test_names,
            cwd=WORKSPACE / "openubmc-target-runtime",
        )
        self.assert_test_names_resolve(
            qualification.LIVE_PATCH_CRASH_TESTS,
            cwd=WORKSPACE / "openubmc-live-patch",
        )

    def test_agent_interaction_guidance_is_release_qualified(self) -> None:
        registered = {
            test_name
            for _group_name, test_names in qualification.QUALIFICATIONS
            for test_name in test_names
        }

        self.assertTrue(
            {
                "tests.test_agent_gateway.AgentGatewayTests.test_public_preflight_error_identifies_field_and_canonical_retry",
                "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_gate_target_preserves_runtime_gate_semantics",
                "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_target_preserves_runtime_incident_semantics",
            }.issubset(registered)
        )

    @staticmethod
    def stability_report(source_commit: str) -> str:
        report = {
            "schema": "openubmc-agent-workflow.runtime-stability.v2",
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
                "artifact_capacity_records": 64,
                "artifact_capacity_batch_size": 16,
                "soak_restart_cycles": 4,
                "soak_runs_per_cycle": 16,
                "max_capacity_seconds": 30.0,
                "max_capacity_peak_rss_bytes": 536870912,
                "max_capacity_peak_python_bytes": 134217728,
                "max_capacity_storage_bytes": 67108864,
                "max_artifact_capacity_seconds": 15.0,
                "max_artifact_storage_bytes": 16777216,
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
                    "execute_calls": 9,
                    "failed_calls": 0,
                    "unique_runs": 1,
                    "unique_turns": 1,
                    "turn_states": {"completed": 8},
                    "canonical_turn_state": "completed",
                    "canonical_turn_matches": True,
                    "canonical_reattach_backend_calls": 0,
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
                    "storage_growth_bytes_by_batch": [1000, 1000, 1000, 1000],
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
                    "replay_backend_read_calls": 0,
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
                "artifact_lifecycle": {
                    "status": "passed",
                    "created_raw_records": 64,
                    "created_redacted_records": 1,
                    "created_ephemeral_records": 1,
                    "shared_raw_digests": 1,
                    "redacted_digest_distinct": True,
                    "records_by_batch": [16, 32, 48, 64],
                    "storage_bytes_by_batch": [2000, 3000, 4000, 5000],
                    "restart_record_count": 66,
                    "restart_resolutions": 2,
                    "first_gc_deleted_records": 33,
                    "first_gc_deleted_content": 1,
                    "shared_content_preserved_after_partial_gc": True,
                    "released_run_records": 32,
                    "second_gc_deleted_records": 32,
                    "second_gc_deleted_content": 1,
                    "shared_content_deleted_after_final_reference": True,
                    "expired_resolution_rejected": True,
                    "released_resolution_rejected": True,
                    "final_record_count": 1,
                    "final_managed_record_count": 1,
                    "final_redacted_record_count": 1,
                    "final_audit_record_count": 1,
                    "final_content_files": 1,
                    "storage_bytes": 6000,
                    "elapsed_seconds": 1.0,
                },
                "dual_projection": runtime_stability.qualify_dual_projection(),
            },
            "promotable": True,
        }
        report["environment_fingerprint"] = qualification.evidence_fingerprint(
            report["environment"]
        )
        report["evidence_digest"] = qualification.evidence_fingerprint(report)
        return json.dumps(report)

    def test_legacy_v1_stability_evidence_remains_verifiable(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        report["schema"] = "openubmc-agent-workflow.runtime-stability.v1"
        report["scenarios"].pop("dual_projection")
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "legacy"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
                require_promotable=True,
            )
        verify_legacy_runtime_stability_report(
            report,
            expected_source_commit=SOURCE_COMMIT,
            require_promotable=True,
        )

    def test_dual_projection_verifier_recomputes_reported_bytes(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        report["scenarios"]["dual_projection"]["measurements"]["gate"] = {
            "standard_text_bytes": 1,
            "structured_content_bytes": 1,
            "combined_mcp_result_bytes": 3,
        }
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "measurement"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_requires_long_complete_previews(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        for turn_name in ("gate",):
            result = projection["canonical_results"][turn_name]
            receipt = result["structuredContent"]["diagnostic_receipt"]
            for item in receipt["results"]:
                sentinel = item["value"]["qualification_sentinel"]
                item["value"]["preview"] = sentinel + "::short"
                item["value"]["unrelated_padding"] = "x" * (4 * 1024)
            projection["measurements"][turn_name] = (
                runtime_stability.projection_measurement(result)
            )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "long"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_failed_structured_outcome(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        terminal["structuredContent"]["outcome"]["status"] = "failed"
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|semantics"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_mcp_error_result(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        terminal["isError"] = True
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "MCP|error"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_unavailable_complete_results(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        for turn_name in ("gate",):
            result = projection["canonical_results"][turn_name]
            result["structuredContent"]["diagnostic_receipt"]["results"][0][
                "status"
            ] = "unavailable"
            projection["measurements"][turn_name] = (
                runtime_stability.projection_measurement(result)
            )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|semantics"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_failed_outcome_acceptance(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        terminal["structuredContent"]["outcome"]["acceptance"][0][
            "status"
        ] = "failed"
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|semantics"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_replaced_long_preview_content(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        for turn_name in ("gate",):
            result = projection["canonical_results"][turn_name]
            item = result["structuredContent"]["diagnostic_receipt"]["results"][0]
            sentinel = item["value"]["qualification_sentinel"] + "::"
            original = item["value"]["preview"]
            item["value"]["preview"] = sentinel + "z" * (
                len(original) - len(sentinel)
            )
            projection["measurements"][turn_name] = (
                runtime_stability.projection_measurement(result)
            )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|semantics"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_missing_standard_text_identities(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        for turn_name in ("gate", "terminal"):
            result = projection["canonical_results"][turn_name]
            text = result["content"][0]["text"]
            kept_lines = []
            for line in text.splitlines():
                if line.startswith((
                    "capabilities_shown=",
                    "capabilities:",
                    "evidence_ids_shown=",
                    "evidence_ids:",
                    "results_shown=",
                    "result_ids:",
                )):
                    continue
                if line.startswith("DiagnosticReceipt "):
                    line = (
                        "DiagnosticReceipt status=complete "
                        "agent_acceptance=complete."
                    )
                kept_lines.append(line)
            result["content"][0]["text"] = "\n".join(kept_lines)
            projection["measurements"][turn_name] = (
                runtime_stability.projection_measurement(result)
            )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|semantics|text"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_allows_extra_text_as_efficiency_warning(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        terminal["content"][0]["text"] += "\noperator note: " + "x" * (5 * 1024)
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        projection["efficiency"]["decision"] = "warning"
        projection["efficiency"]["warnings"] = [
            "terminal_standard_text_target_exceeded"
        ]
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        qualification.verify_runtime_stability_report(
            report,
            expected_source_commit=SOURCE_COMMIT,
            require_promotable=True,
        )

    def test_dual_projection_verifier_rejects_conflicting_semantic_text_lines(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        additions = {
            "gate": "GateBinding run_id=wrong gate_id=wrong gate_version=9 schema_digest=sha256:wrong.\n",
            "terminal": "Outcome status=failed summary=conflicting acceptance_shown=0/0.\n",
        }
        for turn_name, addition in additions.items():
            result = projection["canonical_results"][turn_name]
            result["content"][0]["text"] = addition + result["content"][0]["text"]
            projection["measurements"][turn_name] = (
                runtime_stability.projection_measurement(result)
            )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "semantic|text"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_wrapped_semantic_text_lines(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        terminal["content"][0]["text"] += (
            "\noperator note: Outcome status=failed summary=conflicting."
        )
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "semantic|text"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_preview_payload_in_text(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        receipt = projection["canonical_results"]["gate"]["structuredContent"][
            "diagnostic_receipt"
        ]
        value = receipt["results"][0]["value"]
        preview_payload = value["preview"].removeprefix(
            value["qualification_sentinel"] + "::"
        )
        terminal["content"][0]["text"] += "\noperator note: " + preview_payload
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|preview|text"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_a_tampered_receipt_reference(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        terminal["structuredContent"]["diagnostic_receipt_ref"]["digest"] = (
            "sha256:" + "0" * 64
        )
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|semantics|canonical"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_recomputes_reference_savings(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        metrics = terminal["structuredContent"]["projection_metrics"][
            "diagnostic_receipt"
        ]
        metrics["saved_bytes"] += 1
        projection["representative_receipt"]["repeated_projection"] = dict(
            metrics
        )
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|canonical"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_requires_repeated_field_attribution(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        terminal = projection["canonical_results"]["terminal"]
        metrics = terminal["structuredContent"]["projection_metrics"][
            "diagnostic_receipt"
        ]
        metrics.pop("repeated_fields")
        projection["representative_receipt"]["repeated_projection"] = dict(
            metrics
        )
        projection["measurements"]["terminal"] = (
            runtime_stability.projection_measurement(terminal)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|canonical|attribution"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_requires_target_exceeded_cause_attribution(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        gate = projection["canonical_results"]["gate"]
        gate["structuredContent"]["projection_metrics"]["soft_target"][
            "target_exceeded_causes"
        ] = []
        projection["measurements"]["gate"] = (
            runtime_stability.projection_measurement(gate)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "contract|canonical|attribution"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

    def test_dual_projection_verifier_rejects_gate_binding_after_identities(
        self,
    ) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        projection = report["scenarios"]["dual_projection"]
        gate = projection["canonical_results"]["gate"]
        lines = gate["content"][0]["text"].splitlines()
        binding = next(line for line in lines if line.startswith("GateBinding "))
        gate["content"][0]["text"] = "\n".join(
            [line for line in lines if line != binding] + [binding]
        )
        projection["measurements"]["gate"] = (
            runtime_stability.projection_measurement(gate)
        )
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        with self.assertRaisesRegex(ValueError, "semantic|text"):
            qualification.verify_runtime_stability_report(
                report,
                expected_source_commit=SOURCE_COMMIT,
            )

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
            WORKSPACE,
            executor=succeed,
            source_commit=SOURCE_COMMIT,
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
                "semantic_projection_completion": 0,
                "agent_interaction_guidance": 0,
                "task_scoped_mcp_lifecycle": 0,
                "persisted_run_compatibility": 0,
                "real_backend_crash_cuts": 0,
                "runtime_stability": 0,
            },
        )
        self.assertTrue(report["ordinary_partial_result_accepted"])
        self.assertEqual(len(calls), 11)
        self.assertEqual(report["source_commit"], SOURCE_COMMIT)
        self.assertEqual(
            report["environment"],
            {"platform": "test", "python": "3.11.0"},
        )
        self.assertEqual(
            report["environment_fingerprint"],
            qualification.evidence_fingerprint(report["environment"]),
        )
        self.assertEqual(report["parameters"]["stability_profile"], "ci")
        self.assertEqual(
            report["parameters"]["persisted_run_support"],
            {
                "current_run_decision_version": 1,
                "current_run_event_version": 1,
                "accepted_unversioned_event_kinds": [
                    "CaseClosed",
                    "CaseOpened",
                    "CaseUpdated",
                    "CloseoutRecorded",
                    "DeliveryStrategySelected",
                    "EvidenceAttached",
                    "OperationAccepted",
                    "OperationProgressed",
                    "OperationReconciled",
                    "OperationStarted",
                    "OperationTerminal",
                    "RunCancelled",
                    "RunDecisionCommitted",
                    "RunGateOpened",
                    "RunGateSubmitted",
                    "RunIncidentRaised",
                    "RunIncidentResolved",
                    "RunOutcomeRecorded",
                    "RunPhaseRecorded",
                    "RunVerificationDeferred",
                    "WorkflowCycleStarted",
                    "WorkflowStepsInvalidated",
                ],
                "legacy_event_kinds": [
                    "CaseOpened",
                    "CaseUpdated",
                    "DeliveryStrategySelected",
                    "OperationProgressed",
                    "RunCancelled",
                    "RunGateOpened",
                    "RunGateSubmitted",
                    "RunOutcomeRecorded",
                    "RunPhaseRecorded",
                ],
                "legacy_mode": "read-only-upcast",
                "unknown_version_behavior": "reject",
            },
        )
        self.assertEqual(
            report["parameters"]["agent_projection_policy"],
            {
                "budget_mode": "soft-display-target",
                "gate_schema_target_bytes": 4096,
                "manual_narrowing_required_on_target_exceeded": False,
                "observation_receipt_target_bytes": 4096,
                "projection_budget_blocker": False,
                "target_exceeded_behavior": "preserve-runtime-semantics",
                "turn_target_bytes": 8192,
            },
        )
        stability_call = next(
            command
            for command in calls
            if any("runtime_stability.py" in item for item in command)
        )
        self.assertEqual(
            stability_call[stability_call.index("--source-commit") + 1],
            SOURCE_COMMIT,
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

        report = qualification.qualify_runtime(WORKSPACE, executor=fail_second)

        self.assertFalse(report["promotable"])
        self.assertGreater(report["violations"]["false_successes"], 0)
        self.assertEqual(call_count, 11)

    def test_artifact_lifecycle_without_shared_content_safety_blocks_promotion(
        self,
    ) -> None:
        def unsafe_gc(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            artifact = report["scenarios"]["artifact_lifecycle"]
            artifact["shared_content_preserved_after_partial_gc"] = False
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=unsafe_gc,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("Artifact lifecycle", stability_result["verification_error"])

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
            WORKSPACE,
            executor=incomplete,
            source_commit=SOURCE_COMMIT,
        )

        self.assertFalse(report["promotable"])
        self.assertEqual(report["violations"]["runtime_stability"], 1)
        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertIn("schema", stability_result["verification_error"])

    def test_nonterminal_equivalent_gate_turns_block_promotion(self) -> None:
        def nonterminal(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            report["scenarios"]["gate_concurrency"]["turn_states"] = {"running": 8}
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=nonterminal,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("Gate", stability_result["verification_error"])

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
            WORKSPACE,
            executor=over_threshold,
            source_commit=SOURCE_COMMIT,
        )

        self.assertFalse(report["promotable"])
        self.assertEqual(report["violations"]["runtime_stability"], 1)
        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertIn("threshold", stability_result["verification_error"])

    def test_inconsistent_soak_growth_series_blocks_promotion(self) -> None:
        def inconsistent_soak(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            soak = report["scenarios"]["restart_soak"]
            soak["cumulative_events_by_cycle"] = [1, 1, 1, 704]
            soak["storage_bytes_by_cycle"] = [4000, 1000, 3000, 4000]
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=inconsistent_soak,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("soak", stability_result["verification_error"])

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
            WORKSPACE,
            executor=wrong_fingerprint,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("environment fingerprint", stability_result["verification_error"])

    def test_stability_report_rejects_non_string_environment_fields(self) -> None:
        def malformed_environment(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            report["environment"]["platform"] = None
            report["environment_fingerprint"] = qualification.evidence_fingerprint(
                report["environment"]
            )
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=malformed_environment,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("environment is incomplete", stability_result["verification_error"])

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
            WORKSPACE,
            executor=over_rss,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("capacity", stability_result["verification_error"])

    def test_inconsistent_capacity_storage_growth_blocks_promotion(self) -> None:
        def inconsistent_growth(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            capacity = report["scenarios"]["capacity"]
            capacity["storage_bytes_by_batch"] = [4000, 1000, 3000, 4000]
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=inconsistent_growth,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("capacity", stability_result["verification_error"])

    def test_inconsistent_capacity_event_prefixes_block_promotion(self) -> None:
        def inconsistent_prefixes(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            report["scenarios"]["capacity"]["cumulative_events_by_batch"] = [
                352,
                1056,
                1056,
                1408,
            ]
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=inconsistent_prefixes,
            source_commit=SOURCE_COMMIT,
        )

        stability_result = next(
            item
            for item in report["qualifications"]
            if item["name"] == "runtime_stability"
        )
        self.assertFalse(report["promotable"])
        self.assertIn("capacity", stability_result["verification_error"])

    def test_zero_capacity_storage_growth_is_valid(self) -> None:
        def zero_growth(command, *, cwd):
            del cwd
            if not any("runtime_stability.py" in str(item) for item in command):
                return subprocess.CompletedProcess(command, 0, "ok", "")
            source = command[command.index("--source-commit") + 1]
            report = json.loads(self.stability_report(source))
            capacity = report["scenarios"]["capacity"]
            capacity["storage_bytes_by_batch"] = [1000, 2000, 4000, 4000]
            capacity["storage_growth_bytes_by_batch"] = [1000, 1000, 2000, 0]
            report.pop("evidence_digest")
            report["evidence_digest"] = qualification.evidence_fingerprint(report)
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

        report = qualification.qualify_runtime(
            WORKSPACE,
            executor=zero_growth,
            source_commit=SOURCE_COMMIT,
        )

        self.assertTrue(report["promotable"])

    def test_storage_reclamation_between_samples_is_valid(self) -> None:
        report = json.loads(self.stability_report(SOURCE_COMMIT))
        capacity = report["scenarios"]["capacity"]
        capacity["storage_bytes_by_batch"] = [1000, 4000, 3000, 4500]
        capacity["storage_growth_bytes_by_batch"] = [
            1000,
            3000,
            -1000,
            1500,
        ]
        capacity["storage_bytes"] = 4500
        soak = report["scenarios"]["restart_soak"]
        soak["storage_bytes_by_cycle"] = [1000, 3000, 2500, 4000]
        report.pop("evidence_digest")
        report["evidence_digest"] = qualification.evidence_fingerprint(report)

        qualification.verify_runtime_stability_report(
            report,
            expected_source_commit=SOURCE_COMMIT,
            require_promotable=True,
        )

    def test_unrelated_source_commit_is_rejected_before_qualification(self) -> None:
        with self.assertRaisesRegex(ValueError, "workspace HEAD"):
            qualification.qualify_runtime(
                WORKSPACE,
                source_commit="a" * 40,
            )


if __name__ == "__main__":
    unittest.main()
