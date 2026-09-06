import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "runtime_measurement.py"


class RuntimeMeasurementTests(unittest.TestCase):
    def test_deterministic_report_separates_public_stages_and_identity(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repetitions", "1", "--warm-repetitions", "1"],
            cwd=ROOT.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema"], "openubmc.runtime-measurement.v1")
        self.assertEqual(report["condition"]["network"], "disabled")
        self.assertEqual(
            {item["stage"] for item in report["records"]},
            {"start", "diagnosis.acceptance", "developer.change", "mcp_tools_list"},
        )
        for item in report["records"]:
            self.assertIn("runtime_wall_seconds", item)
            self.assertIn("response_bytes", item)
            self.assertIn("storage_bytes_after", item)
            self.assertEqual(item["failure_class"], None)
        self.assertEqual(report["attempted"], 8)
        self.assertEqual(report["valid"], 8)


if __name__ == "__main__":
    unittest.main()
