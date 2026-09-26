from __future__ import annotations

from pathlib import Path
import copy
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.sanitized_replay import ReplayError, evaluate_case, evaluate_directory  # noqa: E402


class SanitizedReplayTests(unittest.TestCase):
    @staticmethod
    def _positive_fixture():
        path = ROOT / "evaluation" / "sanitized-replays" / "offline-package-analysis-positive.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_live_probe_cannot_be_overridden_by_a_fixture_observation(self):
        path = ROOT / "evaluation" / "sanitized-replays" / "skill-routing-negative.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        case["observed"]["skill_routing"] = dict(case["expected"]["skill_routing"])
        result = evaluate_case(case)
        self.assertEqual(result["observed_status"], "failed")
        self.assertEqual(result["dimensions"]["skill_routing"]["observed_behavior"]["owner"],
                         "openubmc-bingo-build")

    def test_execution_host_is_taken_from_the_live_router(self):
        path = ROOT / "evaluation" / "sanitized-replays" / "execution-host-negative.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        case["observed"]["execution_host"] = dict(case["expected"]["execution_host"])
        result = evaluate_case(case)
        self.assertEqual(result["observed_status"], "failed")
        self.assertEqual(result["dimensions"]["execution_host"]["observed_behavior"]["host"],
                         "wsl")
        self.assertEqual(result["dimensions"]["execution_host"]["observed_behavior"]["route"],
                         "shell-fallback")

    def test_live_fallback_exposes_required_receipt_fields(self):
        path = ROOT / "evaluation" / "sanitized-replays" / "execution-host-negative.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        case["probe"]["host"] = "wsl"
        case["probe"]["initialized"] = False
        result = evaluate_case(case)
        observed = result["dimensions"]["execution_host"]["observed_behavior"]
        self.assertEqual(observed["reason_code"], "initialize_invalid")
        self.assertEqual(observed["host"], "wsl")
        self.assertEqual(observed["requested_scope"], "sanitized fixture")
        self.assertEqual(observed["evidence_boundary"], "router receipt")
        self.assertEqual(observed["call_budget"], 8)

    def test_final_presence_is_taken_from_the_live_delivery_gate(self):
        path = ROOT / "evaluation" / "sanitized-replays" / "final-answer-negative.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        case["observed"]["final_answer"] = dict(case["expected"]["final_answer"])
        result = evaluate_case(case)
        self.assertEqual(result["observed_status"], "failed")
        self.assertFalse(result["dimensions"]["final_answer"]["observed_behavior"]["present"])

    def test_live_probe_requires_the_exact_known_behavior(self):
        path = ROOT / "evaluation" / "sanitized-replays" / "skill-routing-negative.json"
        case = json.loads(path.read_text(encoding="utf-8"))
        case["probe"]["expected_observation"]["owner"] = "other-owner"
        result = evaluate_case(case)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["probe"]["status"], "failed")

    def test_known_good_offline_package_analysis_is_positive(self):
        report = evaluate_directory(ROOT / "evaluation" / "sanitized-replays")
        self.assertEqual(report["status"], "passed")
        negatives = {
            item["case_id"].removesuffix("-negative"): item
            for item in report["cases"]
            if item["case_id"].endswith("-negative")
        }
        self.assertEqual(set(negatives), {
            "skill-routing", "execution-host", "evidence-lineage",
            "completion-calibration", "release-gates",
            "credential-containment", "convergence-cost",
            "recovery-rollback", "version-consistency", "final-answer",
        })
        for dimension in (
            "skill_routing", "execution_host", "evidence_lineage",
            "completion_calibration", "release_gates",
            "credential_containment", "convergence_cost",
            "recovery_rollback", "version_consistency", "final_answer",
        ):
            negative = negatives[dimension.replace("_", "-")]
            self.assertEqual(negative["status"], "passed")
            self.assertEqual(negative["observed_status"], "failed")
            finding = negative["dimensions"][dimension]
            self.assertEqual(finding["status"], "failed")
            self.assertTrue(finding["expected_behavior"])
            self.assertTrue(finding["observed_behavior"])
            self.assertTrue(finding["bounded_evidence"])
        good = next(item for item in report["cases"] if item["case_id"] == "offline-package-analysis-positive")
        self.assertEqual(good["status"], "passed")
        self.assertEqual(report["sanitization"]["status"], "passed")

    def test_replay_reports_dimension_and_identity_failures(self):
        case = {
            "case_id": "wrong-target",
            "evidence_boundary": {
                name: {"source": "synthetic", "record_ids": [name + "-1"]}
                for name in (
                    "skill_routing", "execution_host", "evidence_lineage",
                    "completion_calibration", "release_gates",
                    "credential_containment", "convergence_cost",
                    "recovery_rollback", "version_consistency", "final_answer",
                )
            },
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
        self.assertEqual(
            result["dimensions"]["evidence_lineage"]["bounded_evidence"]["source"],
            "synthetic",
        )

    def test_shell_fallback_and_required_release_gates_fail_closed(self):
        expected = {
            "skill_routing": {"owner": "openubmc-debug"},
            "execution_host": {"host": "wsl", "route": "shell-fallback"},
            "evidence_lineage": {"identity": {"target": "fixture"}},
            "completion_calibration": {"stage": "diagnosed", "status": "partial"},
            "release_gates": {"required": True},
            "credential_containment": {"persisted": False},
            "convergence_cost": {"budget": 1},
            "recovery_rollback": {"status": "not_applicable"},
            "version_consistency": {"installed": "1", "source": "1", "artifact": "1"},
            "final_answer": {"present": True, "task_id": "fallback"},
        }
        observed = {key: dict(value) for key, value in expected.items()}
        observed["evidence_lineage"]["evidence"] = [{"target": "fixture"}]
        observed["execution_host"].update({"call_budget": 2})
        observed["release_gates"]["gates"] = []
        observed["convergence_cost"]["actions"] = []
        result = evaluate_case({
            "case_id": "fallback", "expected": expected, "observed": observed,
            "evidence_boundary": {
                name: {"source": "synthetic", "record_ids": [name]}
                for name in expected
            },
        })
        self.assertEqual(result["dimensions"]["execution_host"]["status"], "failed")
        self.assertEqual(result["dimensions"]["release_gates"]["status"], "failed")

    def test_evidence_from_another_observation_interval_cannot_satisfy_fixture(self):
        case = self._positive_fixture()
        identity = copy.deepcopy(case["expected"]["evidence_lineage"]["identity"])
        identity.pop("observed_at")
        identity["observation_interval"] = {
            "start": "2026-09-20T00:00:00Z", "end": "2026-09-20T00:00:10Z",
        }
        case["expected"]["evidence_lineage"]["identity"] = identity
        case["observed"]["evidence_lineage"]["identity"] = copy.deepcopy(identity)
        case["observed"]["evidence_lineage"]["evidence"][0]["observed_at"] = "2026-09-21T00:00:00Z"
        result = evaluate_case(case)
        self.assertEqual(result["dimensions"]["evidence_lineage"]["status"], "failed")
        self.assertTrue(any("observation interval" in failure for failure in
                            result["dimensions"]["evidence_lineage"]["failures"]))

    def test_same_evidence_cannot_be_justified_as_a_retry(self):
        case = self._positive_fixture()
        actions = case["observed"]["convergence_cost"]["actions"]
        actions.append({**actions[0], "justified_retry": True})
        result = evaluate_case(case)
        self.assertEqual(result["dimensions"]["convergence_cost"]["status"], "failed")
        self.assertTrue(any("without changed evidence" in failure for failure in
                            result["dimensions"]["convergence_cost"]["failures"]))
        actions[-1]["evidence_digest"] = "sha256:new-evidence"
        self.assertEqual(evaluate_case(case)["dimensions"]["convergence_cost"]["status"], "passed")

    def test_unsanitized_fixture_is_rejected_without_echoing_secret(self):
        case = self._positive_fixture()
        secret = "synthetic-report-secret"
        case["observed"]["credential_containment"]["password"] = secret
        with self.assertRaises(ReplayError) as failure:
            evaluate_case(case)
        self.assertNotIn(secret, str(failure.exception))
        case = self._positive_fixture()
        case["observed"]["credential_containment"]["password=" + secret] = "value"
        with self.assertRaises(ReplayError) as failure:
            evaluate_case(case)
        self.assertNotIn(secret, str(failure.exception))


if __name__ == "__main__":
    unittest.main()
