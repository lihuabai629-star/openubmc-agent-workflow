from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from scripts import continuous_closeout_qualification as qualification


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "continuous_closeout_qualification.py"


class ContinuousCloseoutQualificationTests(unittest.TestCase):
    def test_qualification_integrates_product_client_isolation_lifecycle_and_projection(
        self,
    ) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(
            report["schema"],
            "openubmc-agent-workflow.continuous-closeout-qualification.v1",
        )
        self.assertTrue(report["qualified"])
        self.assertEqual(
            report["client_matrix"]["product_clients"],
            ["claude", "codex", "openclaw"],
        )
        self.assertEqual(
            report["client_matrix"]["evaluation_harnesses"], ["dsh"]
        )
        self.assertEqual(report["client_matrix"]["overlap"], [])
        self.assertEqual(
            sorted(report["client_matrix"]["runs"]),
            ["claude", "codex", "openclaw"],
        )
        self.assertTrue(
            all(
                run["status"] == "passed"
                for run in report["client_matrix"]["runs"].values()
            )
        )
        self.assertTrue(report["evaluation_isolation"]["global_state_blocked"])
        self.assertTrue(report["evaluation_isolation"]["task_owned"])
        self.assertTrue(report["mcp_lifecycle"]["parent_loss_covered"])
        self.assertTrue(report["mcp_lifecycle"]["active_request_drain_covered"])
        self.assertTrue(report["mcp_lifecycle"]["cleanup_covered"])
        self.assertTrue(report["mcp_lifecycle"]["zero_live_orphans_covered"])
        closeout = report["mcp_lifecycle"]["closeout"]
        self.assertTrue(closeout["task_closeout_ready"])
        self.assertEqual(closeout["summary"]["live_processes"], 0)
        self.assertEqual(closeout["summary"]["active_requests"], 0)
        self.assertEqual(closeout["task_ids"], ["continuous-closeout-qualification"])
        self.assertEqual(closeout["session_ids"], ["continuous-closeout-session"])
        projection = report["execute_projection"]
        self.assertTrue(projection["correctness_primary"])
        self.assertTrue(projection["repeated_reference"])
        self.assertGreater(projection["saved_bytes"], 0)
        self.assertFalse(projection["blocks_promotability"])
        self.assertIn(
            "fresh_runtime_product_evidence_required",
            report["external_blockers"],
        )
        self.assertTrue(report["qualification_digest"].startswith("sha256:"))

    def test_qualification_ignores_target_and_credential_environment(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                "OPENUBMC_TARGET": "198.51.100.99",
                "OPENUBMC_SSH_PASSWORD": "must-not-be-used",
                "OPENUBMC_REDFISH_PASSWORD": "must-not-be-used",
                },
            ),
            mock.patch.object(
                qualification,
                "_workflow_metadata",
                return_value={
                    "clients": {
                        name: {"role": "supported-product-client"}
                        for name in ("claude", "codex", "openclaw")
                    },
                    "evaluation_harnesses": {
                        "dsh": {"role": "evaluation-harness"}
                    },
                },
            ),
            mock.patch.object(
                qualification,
                "_run_tests",
                return_value={"status": "passed", "tests": [], "returncode": 0},
            ),
            mock.patch.object(
                qualification,
                "_mcp_closeout_snapshot",
                return_value={
                    "status": "passed",
                    "task_closeout_ready": True,
                    "summary": {"live_processes": 0, "active_requests": 0},
                },
            ),
            mock.patch.object(
                qualification,
                "qualify_dual_projection",
                return_value={
                    "status": "passed",
                    "correctness": {"passed": True},
                    "representative_receipt": {
                        "repeated_projection": {
                            "repeated_reference": True,
                            "full_bytes": 2,
                            "reference_bytes": 1,
                            "saved_bytes": 1,
                        }
                    },
                },
            ),
            mock.patch.object(qualification, "_source_clean", return_value=True),
            mock.patch.object(
                qualification,
                "resolve_source_commit",
                return_value="a" * 40,
            ),
        ):
            report = qualification.qualify()

        self.assertTrue(report["qualified"])
        self.assertTrue(report["task_matrix"]["correctness_primary"])
        self.assertEqual(
            sorted(report["task_matrix"]["groups"]),
            [
                "build_upgrade",
                "dependency_blocked",
                "hardware_blocked",
                "live_patch",
                "restart_crash",
                "source_only",
                "wide_observe",
            ],
        )
        self.assertTrue(
            all(
                group["status"] == "passed"
                for group in report["task_matrix"]["groups"].values()
            )
        )
        self.assertIn(
            "test_source_only_keeps_dependency_and_nvme_coverage_gaps_visible",
            " ".join(report["task_matrix"]["groups"]["hardware_blocked"]["tests"]),
        )
        self.assertNotIn(
            "test_completed_failed_build_does_not_advance_to_upgrade",
            " ".join(report["task_matrix"]["groups"]["hardware_blocked"]["tests"]),
        )
        self.assertNotIn("198.51.100.99", json.dumps(report))
        self.assertNotIn("must-not-be-used", json.dumps(report))

    def test_qualification_accepts_trusted_product_ingestion_input(self) -> None:
        descriptor = Path("fresh-product-ingestion.json")
        runtime_repository = Path("runtime.sqlite3")
        with (
            mock.patch.object(
                qualification,
                "_workflow_metadata",
                return_value={
                    "clients": {
                        name: {"role": "supported-product-client"}
                        for name in ("claude", "codex", "openclaw")
                    },
                    "evaluation_harnesses": {
                        "dsh": {"role": "evaluation-harness"}
                    },
                },
            ),
            mock.patch.object(
                qualification,
                "_run_tests",
                return_value={"status": "passed", "tests": [], "returncode": 0},
            ),
            mock.patch.object(
                qualification,
                "_mcp_closeout_snapshot",
                return_value={
                    "status": "passed",
                    "task_closeout_ready": True,
                    "summary": {"live_processes": 0, "active_requests": 0},
                },
            ),
            mock.patch.object(
                qualification,
                "qualify_dual_projection",
                return_value={
                    "status": "passed",
                    "correctness": {"passed": True},
                    "representative_receipt": {
                        "repeated_projection": {
                            "repeated_reference": True,
                            "full_bytes": 2,
                            "reference_bytes": 1,
                            "saved_bytes": 1,
                        }
                    },
                },
            ),
            mock.patch.object(qualification, "_source_clean", return_value=True),
            mock.patch.object(
                qualification,
                "resolve_source_commit",
                return_value="a" * 40,
            ),
            mock.patch.object(
                qualification,
                "load_product_ingestion",
                return_value={"schema": "ingestion"},
            ) as load_ingestion,
            mock.patch.object(
                qualification,
                "assemble_product_manifest",
                return_value={"schema": "assembled-manifest"},
            ) as assemble,
            mock.patch.object(
                qualification,
                "qualify_product_closeout",
                return_value={
                    "qualified": True,
                    "promotable": True,
                    "claim_level": "fresh-runtime-product-closed",
                    "manifest_digest": "sha256:" + "b" * 64,
                    "evidence_digest": "sha256:" + "c" * 64,
                    "gaps": [],
                    "violations": [],
                },
            ),
        ):
            report = qualification.qualify(
                product_ingestion=descriptor,
                runtime_repository=runtime_repository,
            )

        load_ingestion.assert_called_once_with(descriptor)
        assemble.assert_called_once_with(
            {"schema": "ingestion"},
            runtime_repository=runtime_repository,
        )
        self.assertEqual(
            report["product_evidence"]["status"], "verified-ingestion"
        )
        self.assertTrue(report["fresh_product_promotable"])
        self.assertEqual(report["external_blockers"], [])


if __name__ == "__main__":
    unittest.main()
