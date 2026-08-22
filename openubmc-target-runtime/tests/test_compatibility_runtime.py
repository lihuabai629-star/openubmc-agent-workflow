from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from test_agent_gateway import SemanticBackend  # noqa: E402


class CompatibilityRuntimeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )

    def tearDown(self) -> None:
        self.service.close()

    def _start_run(self):
        return self.service.compatibility_runtime.translate(
            "workflow.advance",
            {
                "ip": "192.0.2.90",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "final_purpose": "verify compatibility Module seam",
            },
            task_id="compatibility-module",
            operation_id="compatibility-module-start",
        )

    def test_adapter_starts_a_typed_run_for_legacy_workflow_advance(self) -> None:
        result = self._start_run()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.envelope["status"], "waiting_response")
        self.assertTrue(result["case_id"].startswith("run-"))

    def test_adapter_submits_a_legacy_phase_record_to_the_typed_gate(self) -> None:
        started = self._start_run()
        assert started is not None
        contract = started["handoff_arguments"]["phase_record_contract"]

        result = self.service.compatibility_runtime.translate(
            "phase_record",
            {
                key: contract[key]
                for key in (
                    "case_id",
                    "expected_revision",
                    "idempotency_key",
                    "phase_type",
                    "producer_identity",
                )
            }
            | {
                "status": "completed",
                "summary": "implemented through the compatibility Module",
                "source_revision": "abc123",
                "authored_files": ["runtime.py"],
                "verification_plan": ["run Runtime tests"],
            },
            task_id="compatibility-module",
            operation_id="compatibility-module-phase",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.envelope["status"], "completed")
        self.assertEqual(result["phase_type"], "developer.change")
        self.assertEqual(
            result["summary"],
            "implemented through the compatibility Module",
        )

    def test_adapter_reattaches_legacy_workflow_next_to_the_typed_run(self) -> None:
        started = self._start_run()
        assert started is not None

        result = self.service.compatibility_runtime.translate(
            "workflow.next",
            {"case_id": started["case_id"]},
            task_id="compatibility-module",
            operation_id="compatibility-module-next",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["run_id"], started["case_id"])
        self.assertEqual(result.envelope["status"], "waiting_response")


if __name__ == "__main__":
    unittest.main()
