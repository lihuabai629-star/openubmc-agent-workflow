from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openubmc_target_runtime import GateConflict, RuntimeMcpService
from openubmc_target_runtime.diagnosis_record import (
    DiagnosisRecord,
    accepted_diagnosis_record,
)

from test_agent_gateway import (
    CompleteBoundedDiagnosticBackend,
    SemanticBackend,
    accept_diagnosis,
    accepted_diagnosis_payload,
    gate_binding,
)


class DiagnosisRecordTests(unittest.TestCase):
    def test_complete_collection_still_requests_a_diagnosis(self) -> None:
        for backend in (SemanticBackend(), CompleteBoundedDiagnosticBackend()):
            with self.subTest(backend=type(backend).__name__):
                service = RuntimeMcpService(backend)
                try:
                    turn = service.call_exposed_tool(
                        "execute",
                        {"kind": "start", "target": "192.0.2.10", "intent": "diagnosis-only"},
                        task_id="diagnosis-record-test", operation_id="collect",
                    )
                    self.assertEqual(turn["state"], "waiting_response")
                    self.assertEqual(turn["gate"]["name"], "diagnosis.acceptance")
                    self.assertIsNone(turn["outcome"])
                    self.assertNotIn("diagnosis_record", turn)
                    resumed = service.call_exposed_tool(
                        "execute", {"kind": "resume", "run_id": turn["run_id"]},
                        task_id="diagnosis-record-test", operation_id="resume",
                    )
                    self.assertEqual(gate_binding(resumed), gate_binding(turn))
                    self.assertEqual(resumed["progress"]["status"], "no_progress")
                finally:
                    service.close()

    def test_verified_record_is_bound_and_required_for_outcome(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {"kind": "start", "target": "192.0.2.10", "intent": "diagnosis-only"},
                task_id="diagnosis-record-accepted", operation_id="collect",
            )
            receipt = waiting["diagnostic_receipt"]
            final = accept_diagnosis(service, waiting, task_id="diagnosis-record-accepted")
            self.assertEqual(final["state"], "completed")
            self.assertEqual(final["outcome"]["status"], "completed")
            self.assertEqual(final["diagnosis_record"]["verification_status"], "verified")
            projection = service._test.context_runtime.read_case(waiting["run_id"])
            record = projection["phase_records"][-1]["diagnosis_record"]
            self.assertEqual(record["run_id"], waiting["run_id"])
            self.assertEqual(record["workflow_cycle_id"], projection["workflow_cycle_id"])
            self.assertEqual(record["source_receipt_id"], receipt["receipt_id"])
            self.assertNotIn("diagnostic_receipt", projection["phase_records"][-1])
            self.assertIsNotNone(accepted_diagnosis_record(projection))
            self.assertIsNone(accepted_diagnosis_record({**projection, "target_version": 2}))
            tampered = dict(projection)
            tampered["phase_records"] = [dict(projection["phase_records"][-1])]
            tampered["phase_records"][0]["diagnosis_record"] = {
                **tampered["phase_records"][0]["diagnosis_record"],
                "source_receipt_id": "receipt-not-in-lineage",
            }
            self.assertIsNone(accepted_diagnosis_record(tampered))
        finally:
            service.close()

    def test_invalid_or_unverified_writer_keeps_the_gate_open(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {"kind": "start", "target": "192.0.2.10", "intent": "diagnosis-only"},
                task_id="diagnosis-record-invalid", operation_id="collect",
            )
            evidence = [item["evidence_id"] for item in waiting["diagnostic_receipt"]["evidence"]]
            valid = accepted_diagnosis_payload(evidence)
            cases = [
                {"root_cause": "legacy root cause", "evidence_ids": evidence, "known_gaps": []},
                {**valid, "evidence_ids": ["evidence-unrelated"]},
                {**valid, "causal_chain": []},
                {**valid, "code_owner": " "},
                {**valid, "contradictions": ["the source disproves the claim"]},
                {**valid, "verification_status": "unverified"},
            ]
            for index, payload in enumerate(cases):
                with self.subTest(index=index), self.assertRaises(GateConflict):
                    service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "respond", "run_id": waiting["run_id"],
                            **gate_binding(waiting),
                            "response": {"status": "completed", "summary": "fixture claim", "payload": payload},
                        },
                        task_id="diagnosis-record-invalid", operation_id=f"reject-{index}",
                    )
            projection = service._test.context_runtime.read_case(waiting["run_id"])
            self.assertFalse(projection["phase_records"])
            self.assertFalse(projection.get("run_outcome"))
        finally:
            service.close()

    def test_typed_record_rejects_invalid_field_types(self) -> None:
        payload = accepted_diagnosis_payload(["evidence-1"])
        for field, value in (
            ("root_cause", 1), ("evidence_ids", "evidence-1"),
            ("causal_chain", [True]), ("code_owner", {}),
            ("contradictions", {}), ("remaining_gaps", [None]),
            ("verification_status", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                DiagnosisRecord.from_mapping({**payload, field: value})

    def test_historical_completed_receipt_is_not_new_diagnosis_acceptance(self) -> None:
        projection = {
            "case_id": "legacy", "workflow_cycle_id": "cycle-1", "target_version": 1,
            "phase_records": [{
                "phase_type": "diagnosis.acceptance", "status": "completed",
                "root_cause": "legacy conclusion", "evidence_ids": ["legacy-evidence"],
                "known_gaps": [], "native_run_fact": True,
            }],
        }
        self.assertIsNone(accepted_diagnosis_record(projection))


if __name__ == "__main__":
    unittest.main()
