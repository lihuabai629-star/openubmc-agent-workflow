from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "diagnosis_chain_qualification.py"
FIXTURE = (
    ROOT
    / "scripts"
    / "fixtures"
    / "observation-ba1a3b5277447c57cf972ee2.json"
)
sys.path.insert(0, str(ROOT))

from scripts.diagnosis_chain_qualification import (  # noqa: E402
    _events_have_single_outcome,
    _events_include_development,
    qualification_violations,
)


class DiagnosisChainQualificationTests(unittest.TestCase):
    def test_current_runtime_completes_the_public_chain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "qualification.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--fixture",
                    str(FIXTURE),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr or completed.stdout,
            )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertTrue(report["promotable"])
        self.assertEqual(report["fixture"]["source_receipt_id"], FIXTURE.stem)
        self.assertEqual(report["blocked_path"]["gate"], "diagnosis.acceptance")
        self.assertEqual(report["blocked_path"]["diagnostic_status"], "blocked")
        self.assertTrue(report["blocked_path"]["gap_present"])
        self.assertTrue(report["blocked_path"]["gate_binding_complete"])
        self.assertTrue(report["blocked_path"]["evidence_present"])
        self.assertTrue(report["blocked_path"]["same_gate_after_resume"])
        self.assertTrue(report["blocked_path"]["same_gate_after_restart"])
        self.assertTrue(report["blocked_path"]["same_evidence_after_resume"])
        self.assertTrue(report["blocked_path"]["same_evidence_after_restart"])
        self.assertEqual(report["recovery_path"]["gate"], "developer.change")
        self.assertTrue(report["recovery_path"]["gate_binding_complete"])
        self.assertTrue(report["recovery_path"]["accepted_receipt_present"])
        self.assertTrue(report["recovery_path"]["typed_diagnosis_verified"])
        self.assertEqual(report["recovery_path"]["outcome"], "completed")
        self.assertEqual(
            report["recovery_path"]["acceptance"],
            {"stage.development": "passed", "stage.diagnosis": "passed"},
        )
        self.assertEqual(
            report["validation_readiness"]["dependency_readiness"],
            {
                "status": "blocked",
                "resolution": "blocked_external",
                "attempt_count": 1,
                "reused_by": ["official_ut", "build"],
            },
        )
        self.assertEqual(
            report["validation_readiness"]["official_ut"],
            {
                "status": "dependency_blocked_before_start",
                "acceptance": "blocked",
            },
        )
        self.assertEqual(
            report["validation_readiness"]["build"],
            {
                "status": "dependency_graph_blocked",
                "acceptance": "blocked",
            },
        )
        self.assertEqual(
            report["validation_readiness"]["supplementary"],
            {
                "status": "passed",
                "count": 1,
                "counts_as_official_ut": False,
            },
        )
        self.assertEqual(
            report["validation_readiness"]["hardware_coverage"],
            {
                "status": "blocked",
                "required_protocols": ["NVMe"],
                "observed_protocols": ["SAS", "SATA"],
                "proves_required_protocols": False,
            },
        )
        self.assertEqual(
            report["validation_readiness"]["claim_level"],
            "source_changed",
        )
        self.assertTrue(report["validation_readiness"]["gaps_visible"])
        self.assertEqual(report["terminal_paths"], {"cancelled": True, "failed": True})
        self.assertEqual(report["metrics"]["target_calls_after_observe"], 0)
        self.assertEqual(report["metrics"]["duplicate_diagnosis_gates"], 0)
        self.assertEqual(report["metrics"]["duplicate_development_gates"], 0)
        self.assertEqual(report["metrics"]["duplicate_development_work"], 0)
        self.assertEqual(report["metrics"]["durable_outcome_records"], 1)
        self.assertTrue(report["metrics"]["durable_outcome_completed"])
        self.assertEqual(len(report["metrics"]["restart_worker_pids"]), 2)
        self.assertNotIn(os.getpid(), report["metrics"]["restart_worker_pids"])

    def test_v201_runtime_reproduces_the_regression_and_fails(self) -> None:
        archive = subprocess.run(
            ["git", "archive", "v2.0.1", "openubmc-target-runtime"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        with tempfile.TemporaryDirectory() as raw:
            extracted = Path(raw) / "source"
            extracted.mkdir()
            with tarfile.open(fileobj=BytesIO(archive), mode="r:") as bundle:
                bundle.extractall(extracted, filter="data")
            output = Path(raw) / "v201.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--runtime-root",
                    str(extracted / "openubmc-target-runtime"),
                    "--fixture",
                    str(FIXTURE),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(report["promotable"])
        self.assertIn(
            "blocked diagnosis exposed developer.change",
            report["violations"],
        )

    def test_terminal_outcome_is_the_primary_success_gate(self) -> None:
        report = {
            "fixture": {"semantic_match": True},
            "blocked_path": {
                "gate": "diagnosis.acceptance",
                "diagnostic_status": "blocked",
                "gap_present": True,
                "gate_binding_complete": True,
                "evidence_present": True,
                "same_gate_after_resume": True,
                "same_gate_after_restart": True,
                "same_evidence_after_resume": True,
                "same_evidence_after_restart": True,
            },
            "recovery_path": {
                "gate": "developer.change",
                "gate_binding_complete": True,
                "accepted_receipt_present": True,
                "outcome": "failed",
                "acceptance": {
                    "stage.diagnosis": "passed",
                    "stage.development": "passed",
                },
                "accepted_diagnosis_survived_restart": True,
            },
            "terminal_paths": {"failed": True, "cancelled": True},
            "metrics": {
                "observe_calls": 1,
                "target_calls_after_observe": 0,
                "duplicate_diagnosis_gates": 0,
                "duplicate_development_gates": 0,
                "duplicate_development_work": 0,
                "durable_outcome_records": 1,
                "durable_outcome_completed": True,
            },
        }

        self.assertIn(
            "terminal Outcome is not completed",
            qualification_violations(report),
        )

    def test_false_official_validation_claim_is_not_promotable(self) -> None:
        report = {
            "fixture": {"semantic_match": True},
            "blocked_path": {
                "gate": "diagnosis.acceptance",
                "diagnostic_status": "blocked",
                "gap_present": True,
                "gate_binding_complete": True,
                "evidence_present": True,
                "same_gate_after_resume": True,
                "same_gate_after_restart": True,
                "same_evidence_after_resume": True,
                "same_evidence_after_restart": True,
            },
            "recovery_path": {
                "gate": "developer.change",
                "gate_binding_complete": True,
                "accepted_receipt_present": True,
                "outcome": "completed",
                "acceptance": {
                    "stage.diagnosis": "passed",
                    "stage.development": "passed",
                },
                "accepted_diagnosis_survived_restart": True,
            },
            "validation_readiness": {
                "dependency_readiness": {
                    "status": "blocked",
                    "resolution": "blocked_external",
                    "attempt_count": 1,
                    "reused_by": ["official_ut", "build"],
                },
                "official_ut": {"status": "passed", "acceptance": "passed"},
                "build": {"status": "compiled", "acceptance": "passed"},
                "supplementary": {
                    "status": "passed",
                    "count": 1,
                    "counts_as_official_ut": True,
                },
                "hardware_coverage": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "observed_protocols": ["SAS", "SATA"],
                    "proves_required_protocols": True,
                },
                "claim_level": "firmware_validated",
                "gaps_visible": False,
            },
            "terminal_paths": {"failed": True, "cancelled": True},
            "metrics": {
                "observe_calls": 1,
                "target_calls_after_observe": 0,
                "duplicate_diagnosis_gates": 0,
                "duplicate_development_gates": 0,
                "duplicate_development_work": 0,
                "durable_outcome_records": 1,
                "durable_outcome_completed": True,
            },
        }

        violations = qualification_violations(report)

        self.assertIn(
            "blocked dependency readiness was promoted to official UT success",
            violations,
        )
        self.assertIn(
            "blocked dependency readiness was promoted to compile success",
            violations,
        )
        self.assertIn(
            "supplementary validation was counted as official UT",
            violations,
        )
        self.assertIn(
            "SATA/SAS-only evidence was promoted to NVMe hardware coverage",
            violations,
        )
        self.assertIn(
            "source-only qualification exceeded the source_changed claim level",
            violations,
        )
        self.assertIn(
            "validation readiness gaps are not visible",
            violations,
        )

    def test_representative_nvme_coverage_is_an_allowed_checkpoint(self) -> None:
        report = {
            "fixture": {"semantic_match": True},
            "blocked_path": {
                "gate": "diagnosis.acceptance",
                "diagnostic_status": "blocked",
                "gap_present": True,
                "gate_binding_complete": True,
                "evidence_present": True,
                "same_gate_after_resume": True,
                "same_gate_after_restart": True,
                "same_evidence_after_resume": True,
                "same_evidence_after_restart": True,
            },
            "recovery_path": {
                "gate": "developer.change",
                "gate_binding_complete": True,
                "accepted_receipt_present": True,
                "outcome": "completed",
                "acceptance": {
                    "stage.diagnosis": "passed",
                    "stage.development": "passed",
                },
                "accepted_diagnosis_survived_restart": True,
            },
            "validation_readiness": {
                "dependency_readiness": {
                    "status": "blocked",
                    "resolution": "blocked_external",
                    "attempt_count": 1,
                    "reused_by": ["official_ut", "build"],
                },
                "official_ut": {
                    "status": "dependency_blocked_before_start",
                    "acceptance": "blocked",
                },
                "build": {
                    "status": "dependency_graph_blocked",
                    "acceptance": "blocked",
                },
                "supplementary": {
                    "status": "passed",
                    "count": 1,
                    "counts_as_official_ut": False,
                },
                "hardware_coverage": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "observed_protocols": ["NVMe"],
                    "proves_required_protocols": True,
                },
                "claim_level": "source_changed",
                "gaps_visible": True,
            },
            "terminal_paths": {"failed": True, "cancelled": True},
            "metrics": {
                "observe_calls": 1,
                "target_calls_after_observe": 0,
                "duplicate_diagnosis_gates": 0,
                "duplicate_development_gates": 0,
                "duplicate_development_work": 0,
                "durable_outcome_records": 1,
                "durable_outcome_completed": True,
            },
        }

        violations = qualification_violations(report)

        self.assertNotIn(
            "representative NVMe hardware gap was not preserved",
            violations,
        )

    def test_changed_resume_evidence_is_not_promotable(self) -> None:
        report = {
            "fixture": {"semantic_match": True},
            "blocked_path": {
                "gate": "diagnosis.acceptance",
                "diagnostic_status": "blocked",
                "gap_present": True,
                "gate_binding_complete": True,
                "evidence_present": True,
                "same_gate_after_resume": True,
                "same_gate_after_restart": True,
                "same_evidence_after_resume": False,
                "same_evidence_after_restart": True,
            },
            "recovery_path": {
                "gate": "developer.change",
                "gate_binding_complete": True,
                "accepted_receipt_present": True,
                "outcome": "completed",
                "acceptance": {
                    "stage.diagnosis": "passed",
                    "stage.development": "passed",
                },
                "accepted_diagnosis_survived_restart": True,
            },
            "terminal_paths": {"failed": True, "cancelled": True},
            "metrics": {
                "observe_calls": 1,
                "target_calls_after_observe": 0,
                "duplicate_diagnosis_gates": 0,
                "duplicate_development_gates": 0,
                "duplicate_development_work": 0,
                "durable_outcome_records": 1,
                "durable_outcome_completed": True,
            },
        }

        self.assertIn(
            "resume changed diagnostic evidence identities",
            qualification_violations(report),
        )

    def test_empty_runtime_identities_are_not_promotable(self) -> None:
        report = {
            "fixture": {"semantic_match": True},
            "blocked_path": {
                "gate": "diagnosis.acceptance",
                "diagnostic_status": "blocked",
                "gap_present": True,
                "gate_binding_complete": False,
                "evidence_present": False,
                "same_gate_after_resume": True,
                "same_gate_after_restart": True,
                "same_evidence_after_resume": True,
                "same_evidence_after_restart": True,
            },
            "recovery_path": {
                "gate": "developer.change",
                "gate_binding_complete": False,
                "accepted_receipt_present": False,
                "outcome": "completed",
                "acceptance": {
                    "stage.diagnosis": "passed",
                    "stage.development": "passed",
                },
                "accepted_diagnosis_survived_restart": True,
            },
            "terminal_paths": {"failed": True, "cancelled": True},
            "metrics": {
                "observe_calls": 1,
                "target_calls_after_observe": 0,
                "duplicate_diagnosis_gates": 0,
                "duplicate_development_gates": 0,
                "duplicate_development_work": 0,
                "durable_outcome_records": 1,
                "durable_outcome_completed": True,
            },
        }

        violations = qualification_violations(report)
        self.assertIn("diagnosis Gate binding is incomplete", violations)
        self.assertIn("diagnostic evidence identities are missing", violations)
        self.assertIn("developer Gate binding is incomplete", violations)
        self.assertIn("accepted diagnosis receipt identity is missing", violations)

    def test_terminal_event_ledger_detects_any_development_progression(self) -> None:
        for event in (
            {
                "kind": "RunGateOpened",
                "payload": {"gate": {"name": "developer.change"}},
            },
            {
                "kind": "RunGateSubmitted",
                "payload": {
                    "phase": {"phase_type": "developer.change"},
                },
            },
        ):
            with self.subTest(event=event):
                self.assertTrue(_events_include_development([event]))

    def test_terminal_event_ledger_requires_one_matching_outcome(self) -> None:
        completed = {
            "kind": "RunOutcomeRecorded",
            "payload": {"outcome": {"status": "failed"}},
        }

        self.assertTrue(_events_have_single_outcome([completed], "failed"))
        self.assertFalse(_events_have_single_outcome([], "failed"))
        self.assertFalse(
            _events_have_single_outcome([completed, completed], "failed")
        )
        self.assertFalse(_events_have_single_outcome([completed], "cancelled"))


if __name__ == "__main__":
    unittest.main()
