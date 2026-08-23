from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
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
        with tempfile.TemporaryDirectory() as raw:
            report = stability.qualify_runtime_stability(
                Path(raw),
                source_commit="b" * 40,
            )

        self.assertTrue(report["promotable"])
        self.assertEqual(
            report["schema"],
            "openubmc-agent-workflow.runtime-stability.v1",
        )
        self.assertTrue(report["evidence_digest"].startswith("sha256:"))
        self.assertEqual(report["source_commit"], "b" * 40)
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
        soak = report["scenarios"]["restart_soak"]
        self.assertEqual(soak["status"], "passed")
        self.assertEqual(soak["invalid_runs"], 0)
        self.assertEqual(soak["open_incidents"], 0)
        self.assertEqual(soak["failed_calls"], 0)
        self.assertEqual(soak["replay_mismatches"], 0)
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


if __name__ == "__main__":
    unittest.main()
