from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.sanitized_replay import evaluate_case, evaluate_directory  # noqa: E402


class SanitizedReplayTests(unittest.TestCase):
    def test_known_good_offline_package_analysis_is_positive(self):
        report = evaluate_directory(ROOT / "evaluation" / "sanitized-replays")
        self.assertEqual(report["status"], "passed")
        negative = next(item for item in report["cases"] if item["case_id"] == "credential-containment-negative")
        self.assertEqual(negative["status"], "passed")
        self.assertEqual(negative["observed_status"], "failed")
        good = next(item for item in report["cases"] if item["case_id"] == "offline-package-analysis-positive")
        self.assertEqual(good["status"], "passed")

    def test_replay_reports_dimension_and_identity_failures(self):
        case = {
            "case_id": "wrong-target",
            "expected": {
                "skill_routing": {"owner": "openubmc-debug"},
                "execution_host": {"host": "wsl"},
                "evidence_lineage": {"identity": {"target": "a"}},
                "completion_calibration": {"stage": "diagnosed", "status": "partial"},
                "release_gates": {"required": True},
                "credential_containment": {"persisted": False},
                "convergence_cost": {"budget": 1},
                "recovery_rollback": {"status": "not_applicable"},
                "version_consistency": {"installed": "1", "source": "1", "artifact": "1"},
                "final_answer": {"present": True, "task_id": "wrong-target"},
            },
            "observed": {
                "skill_routing": {"owner": "openubmc-debug"}, "execution_host": {"host": "wsl"},
                "evidence_lineage": {"identity": {}, "evidence": [{"target": "b"}]},
                "completion_calibration": {"stage": "diagnosed", "status": "partial"},
                "release_gates": {"required": True, "gates": [{"status": "pass"}]},
                "credential_containment": {"persisted": False}, "convergence_cost": {"actions": []},
                "recovery_rollback": {"status": "not_applicable"}, "version_consistency": {"installed": "1", "source": "1", "artifact": "1"},
                "final_answer": {"present": True, "task_id": "wrong-target"},
            },
        }
        result = evaluate_case(case)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["dimensions"]["evidence_lineage"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
