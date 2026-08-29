from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


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
        self.assertTrue(report["evaluation_isolation"]["global_state_blocked"])
        self.assertTrue(report["evaluation_isolation"]["task_owned"])
        self.assertTrue(report["mcp_lifecycle"]["parent_loss_covered"])
        self.assertTrue(report["mcp_lifecycle"]["active_request_drain_covered"])
        self.assertTrue(report["mcp_lifecycle"]["cleanup_covered"])
        self.assertTrue(report["mcp_lifecycle"]["zero_live_orphans_covered"])
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

    def test_qualification_does_not_require_a_target_or_credentials(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn("10.121.", source)
        self.assertNotIn("ssh_password", source)
        self.assertNotIn("redfish_password", source)


if __name__ == "__main__":
    unittest.main()
