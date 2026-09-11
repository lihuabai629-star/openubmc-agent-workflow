from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(ROOT / "openubmc-debug/scripts"))

from openubmc_target_runtime import RuntimeMcpService
from openubmc_target_runtime.diagnostic_receipt import DiagnosticReceipt, DIAGNOSTIC_RECEIPT_MAX_BYTES
import json
from test_agent_gateway import CompleteBoundedDiagnosticBackend

spec = importlib.util.spec_from_file_location("debug_advice", ROOT / "openubmc-debug/scripts/diagnostic_advice.py")
advice_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(advice_helper)

TARGET = "192.0.2.10"
DEVICE = {"Name": "Drive0", "Protocol": "NVMe"}


class AdviceBackend(CompleteBoundedDiagnosticBackend):
    def __init__(self, *, include_advice=True, tamper=False, historical_source=False):
        super().__init__()
        self.include_advice = include_advice
        self.tamper = tamper
        self.historical_source = historical_source

    def debug_run(self, task, arguments, context):
        value = super().debug_run(task, arguments, context)
        value.update(ip=TARGET, observed_at=datetime.now(timezone.utc).isoformat())
        value["result"]["runtime"] = {"status": {"targets": [{
            "target": {"host": TARGET}, "epochs": {"target_epoch": 0},
        }]}}
        value["result"]["drive"] = dict(DEVICE, hardware_discovery=True, mdb=False, northbound=False)
        if self.historical_source:
            value["observed_at"] = "2000-01-01T00:00:00+00:00"
        if not self.include_advice:
            return value
        request = {
            "schema": "openubmc-debug.diagnostic-advice-request.v1", "target": TARGET,
            "device": DEVICE, "target_epochs": {TARGET: 0}, "sources": {"capture": value},
            "facts": [{"stage": stage, "source_id": "capture", "device_pointer": "/result/drive",
                       "value_pointer": "/result/drive/" + stage}
                      for stage in ("hardware_discovery", "mdb", "northbound")],
        }
        if self.historical_source:
            request["snapshot_at"] = value["observed_at"]
        result = advice_helper.attach_diagnostic_advice(value, request)
        if self.historical_source:
            result["diagnostic_advice"]["snapshot"]["at"] = datetime.now(timezone.utc).isoformat()
        if self.tamper:
            result["result"]["drive"]["mdb"] = True
        return result


class DiagnosticAdviceProjectionTests(unittest.TestCase):
    def start(self, backend):
        service = RuntimeMcpService(backend)
        self.addCleanup(service.close)
        return service, service.call_exposed_tool(
            "execute", {"kind": "start", "target": TARGET, "intent": "diagnosis-only"},
            task_id="diagnostic-advice", operation_id="collect",
        )

    def test_helper_advice_is_visible_with_current_evidence_without_accepting_diagnosis(self):
        _, baseline = self.start(AdviceBackend(include_advice=False))
        service, turn = self.start(AdviceBackend())
        self.assertEqual(turn["state"], "waiting_response")
        self.assertEqual(turn["gate"]["name"], "diagnosis.acceptance")
        self.assertIsNone(turn["outcome"])
        self.assertNotIn("diagnosis_record", turn)
        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["coverage"], baseline["diagnostic_receipt"]["coverage"])
        self.assertEqual(receipt["status"], baseline["diagnostic_receipt"]["status"])
        advice = receipt["diagnostic_advice"]
        self.assertEqual(advice["status"], "advisory")
        self.assertEqual(next(item for item in advice["hypotheses"] if item["id"] == "mdb_not_created")["status"], "fulfilled")
        evidence_ids = {item["evidence_id"] for item in receipt["evidence"]}
        for fact in advice["facts"]:
            self.assertEqual(set(fact["evidence_ref"]["evidence_ids"]), evidence_ids)
        resumed = service.call_exposed_tool(
            "execute", {"kind": "resume", "run_id": turn["run_id"]},
            task_id="diagnostic-advice", operation_id="resume",
        )
        self.assertEqual(resumed["gate"]["name"], "diagnosis.acceptance")

    def test_tampered_attachment_is_omitted_without_changing_factual_coverage(self):
        _, baseline = self.start(AdviceBackend(include_advice=False))
        _, turn = self.start(AdviceBackend(tamper=True))
        receipt = turn["diagnostic_receipt"]
        self.assertNotIn("diagnostic_advice", receipt)
        self.assertEqual(receipt["coverage"], baseline["diagnostic_receipt"]["coverage"])

    def test_optional_advice_yields_to_receipt_budgets_without_compacting_facts(self):
        _, turn = self.start(AdviceBackend())
        receipt = DiagnosticReceipt.from_public_dict(turn["diagnostic_receipt"])
        projected = receipt.compacted_for_agent().to_public_dict()
        self.assertNotIn("diagnostic_advice", projected)
        self.assertEqual(projected["diagnostic_advice_omitted"], "projection_budget")
        value = receipt.to_public_dict()
        without = copy.deepcopy(value)
        without.pop("diagnostic_advice")
        padding = DIAGNOSTIC_RECEIPT_MAX_BYTES - len(json.dumps(without).encode()) - 1024
        value["results"][0]["value"]["details"] = "x" * padding
        stored = DiagnosticReceipt.from_public_dict(value).bounded_for_persistence().to_public_dict()
        self.assertNotIn("diagnostic_advice", stored)
        self.assertEqual(stored["diagnostic_advice_omitted"], "persistence_budget")
        self.assertEqual(stored["coverage"], value["coverage"])
        self.assertEqual(stored["results"], value["results"])

    def test_changed_advice_timestamp_cannot_make_historical_source_evidence_current(self):
        _, turn = self.start(AdviceBackend(historical_source=True))
        self.assertEqual(turn["gate"]["name"], "diagnosis.acceptance")
        self.assertIsNone(turn["outcome"])
        self.assertNotIn("diagnostic_advice", turn["diagnostic_receipt"])


if __name__ == "__main__":
    unittest.main()
