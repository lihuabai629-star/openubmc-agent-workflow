from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime_stability.py"
SPEC = importlib.util.spec_from_file_location("runtime_stability", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
stability = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stability)


class RuntimeStabilityTests(unittest.TestCase):
    def test_public_semantic_seams_survive_storm_capacity_and_restart_soak(
        self,
    ) -> None:
        source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--workspace",
                str(ROOT),
                "--source-commit",
                source_commit,
            ],
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        report = json.loads(completed.stdout)

        self.assertTrue(report["promotable"])
        self.assertEqual(
            report["schema"],
            "openubmc-agent-workflow.runtime-stability.v2",
        )
        self.assertTrue(report["evidence_digest"].startswith("sha256:"))
        self.assertTrue(report["environment_fingerprint"].startswith("sha256:"))
        self.assertEqual(
            report["source_commit"],
            source_commit,
        )
        storm = report["scenarios"]["duplicate_storm"]
        self.assertEqual(storm["status"], "passed")
        self.assertEqual(storm["unique_runs"], 1)
        self.assertEqual(storm["command_decisions"], 1)
        self.assertEqual(storm["outcome_events"], 1)
        self.assertTrue(storm["same_key_different_hash_rejected"])
        gate = report["scenarios"]["gate_concurrency"]
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["gate_submissions"], 1)
        self.assertEqual(gate["outcome_events"], 1)
        self.assertEqual(gate["unique_turns"], 1)
        self.assertEqual(gate["turn_states"], {"completed": 8})
        self.assertTrue(gate["canonical_turn_matches"])
        self.assertEqual(gate["canonical_reattach_backend_calls"], 0)
        capacity = report["scenarios"]["capacity"]
        self.assertEqual(capacity["status"], "passed")
        self.assertEqual(capacity["completed_runs"], 128)
        self.assertEqual(capacity["failed_calls"], 0)
        self.assertEqual(capacity["incomplete_operations"], 0)
        self.assertEqual(
            sum(capacity["events_per_batch"]),
            capacity["total_events"],
        )
        self.assertEqual(
            sum(capacity["storage_growth_bytes_by_batch"]),
            capacity["storage_bytes"],
        )
        self.assertGreater(capacity["peak_rss_bytes"], 0)
        soak = report["scenarios"]["restart_soak"]
        self.assertEqual(soak["status"], "passed")
        self.assertEqual(soak["invalid_runs"], 0)
        self.assertEqual(soak["open_incidents"], 0)
        self.assertEqual(soak["incomplete_operations"], 0)
        self.assertEqual(soak["failed_calls"], 0)
        self.assertEqual(soak["replay_mismatches"], 0)
        self.assertEqual(soak["replay_backend_read_calls"], 0)
        self.assertEqual(
            sum(soak["events_per_cycle"]),
            soak["total_events"],
        )
        self.assertEqual(
            soak["cumulative_events_by_cycle"][-1],
            soak["total_events"],
        )
        self.assertEqual(
            soak["completed_runs"],
            report["parameters"]["soak_restart_cycles"]
            * report["parameters"]["soak_runs_per_cycle"],
        )
        projection = report["scenarios"]["dual_projection"]
        self.assertEqual(projection["status"], "passed")
        self.assertTrue(projection["correctness"]["passed"])
        self.assertTrue(projection["correctness"]["mcp_results_successful"])
        self.assertFalse(projection["efficiency"]["blocks_promotability"])

    def test_dual_projection_qualification_measures_gate_and_terminal_seams(
        self,
    ) -> None:
        report = stability.qualify_dual_projection()

        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["correctness"]["passed"])
        self.assertEqual(
            report["representative_receipt"]["result_kinds"],
            [
                "active-alarms",
                "bounded-logs",
                "mdb",
                "service-tree",
                "target-clock",
                "version-file",
            ],
        )
        self.assertTrue(
            report["representative_receipt"]["structured_semantics_complete"]
        )
        self.assertTrue(
            report["representative_receipt"]
            ["gate_structured_semantics_complete"]
        )
        self.assertTrue(
            report["representative_receipt"]
            ["terminal_structured_semantics_complete"]
        )
        self.assertFalse(
            report["representative_receipt"]["preview_values_duplicated"]
        )
        expected_receipt = stability._representative_diagnostic_receipt()
        self.assertEqual(
            report["canonical_results"]["gate"]["structuredContent"]["schema"],
            "openubmc.target-runtime.v1/agent-gateway-v1/turn",
        )
        stability.Gate.from_public_dict(
            report["canonical_results"]["gate"]["structuredContent"]["gate"]
        )
        gate_receipt = report["canonical_results"]["gate"]["structuredContent"][
            "diagnostic_receipt"
        ]
        self.assertEqual(
            gate_receipt["schema"],
            "openubmc.target-runtime.v1/diagnostic-receipt-v1",
        )
        self.assertEqual(gate_receipt["results"], expected_receipt["results"])
        self.assertFalse(gate_receipt.get("content_compacted", False))
        self.assertTrue(
            all(
                not item.get("projection_truncated", False)
                for item in gate_receipt["results"]
            )
        )
        preview_bytes = report["representative_receipt"]["preview_bytes"]
        self.assertEqual(
            set(preview_bytes),
            {item["result_id"] for item in gate_receipt["results"]},
        )
        self.assertTrue(
            all(
                preview_bytes[item["result_id"]]
                == len(item["value"]["preview"].encode("utf-8"))
                for item in gate_receipt["results"]
            )
        )
        terminal_turn = report["canonical_results"]["terminal"][
            "structuredContent"
        ]
        self.assertNotIn("diagnostic_receipt", terminal_turn)
        reference = terminal_turn["diagnostic_receipt_ref"]
        self.assertEqual(
            reference["schema"],
            "openubmc.target-runtime.v1/agent-gateway-v1/diagnostic-receipt-ref-v1",
        )
        self.assertEqual(reference["receipt_id"], expected_receipt["receipt_id"])
        self.assertEqual(
            reference["result_ids"],
            [item["result_id"] for item in expected_receipt["results"]],
        )
        self.assertEqual(
            reference["evidence_ids"],
            [item["evidence_id"] for item in expected_receipt["evidence"]],
        )
        repeated = report["representative_receipt"]["repeated_projection"]
        self.assertTrue(repeated["repeated_reference"])
        self.assertEqual(repeated["repeated_fields"], ["diagnostic_receipt"])
        self.assertEqual(
            repeated["target_exceeded_causes"],
            [{"field": "diagnostic_receipt", "bytes": repeated["full_bytes"]}],
        )
        self.assertGreater(repeated["saved_bytes"], 0)
        self.assertEqual(
            repeated["saved_bytes"],
            repeated["full_bytes"] - repeated["reference_bytes"],
        )
        for turn_name in ("gate", "terminal"):
            measurements = report["measurements"][turn_name]
            self.assertGreater(measurements["standard_text_bytes"], 0)
            self.assertGreater(measurements["structured_content_bytes"], 0)
            self.assertGreater(measurements["combined_mcp_result_bytes"], 0)
            self.assertGreater(
                measurements["combined_mcp_result_bytes"],
                measurements["standard_text_bytes"]
                + measurements["structured_content_bytes"],
            )
            self.assertEqual(
                measurements,
                stability.projection_measurement(
                    report["canonical_results"][turn_name]
                ),
            )
        gate_target = report["canonical_results"]["gate"]["structuredContent"][
            "projection_metrics"
        ]["soft_target"]
        self.assertGreater(gate_target["full_bytes"], gate_target["target_bytes"])
        self.assertTrue(gate_target["target_exceeded_causes"])

    def test_dual_projection_efficiency_warning_never_blocks_correctness(
        self,
    ) -> None:
        report = stability.qualify_dual_projection(text_target_bytes=1)

        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["correctness"]["passed"])
        self.assertEqual(report["efficiency"]["decision"], "warning")
        self.assertTrue(report["efficiency"]["warnings"])
        self.assertFalse(report["efficiency"]["blocks_promotability"])

    def test_dual_projection_detects_a_compacted_preview_prefix(self) -> None:
        receipt = stability._representative_diagnostic_receipt()
        sentinel = receipt["results"][0]["value"]["qualification_sentinel"]

        self.assertTrue(
            stability._preview_value_duplicated(
                f"fallback leaked {sentinel}truncated...",
                receipt,
            )
        )

    def test_dual_projection_detects_preview_payload_without_sentinel(
        self,
    ) -> None:
        receipt = stability._representative_diagnostic_receipt()
        value = receipt["results"][0]["value"]
        payload = value["preview"].removeprefix(
            value["qualification_sentinel"] + "::"
        )

        self.assertTrue(
            stability._preview_value_duplicated(
                f"fallback leaked {payload}",
                receipt,
            )
        )


if __name__ == "__main__":
    unittest.main()
