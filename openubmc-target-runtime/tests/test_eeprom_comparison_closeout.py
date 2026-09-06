from __future__ import annotations

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openubmc_target_runtime import (
    COMPARISON_RECEIPT_SCHEMA,
    FilesystemBlobRepository,
    GateConflict,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
    comparison_target_identities,
)
from test_comparison_receipt import ComparisonBackend, TIME, comparison


EEPROM_QUERY = "lsprop Eeprom0"
REFERENCE_HEX = "00112233445566778899aabbccddeeff"
CANDIDATE_HEX = "00112233445566778899aabbccddeefe"
TARGETS = [
    {"ip": "192.0.2.20", "role": "reference", "target_id": "reference"},
    {"ip": "192.0.2.21", "role": "candidate", "target_id": "candidate"},
]


def eeprom_result(ip, content, *, truncated=False):
    return {
        "schema_version": "openubmc-debug.v1",
        "ok": True,
        "ip": ip,
        "observed_at": TIME,
        "request": {"mdb_only": True, "mdb_queries": [EEPROM_QUERY]},
        "result": {
            "completed_at": TIME,
            "freshness": {"status": "fresh"},
            "capabilities": {"mdbctl": True},
            "lanes": {"ssh": {"mdbctl": {
                "ok": True,
                "observed_at": TIME,
                "result": {
                    "properties": {"Eeprom0": {
                        "Offset": 0,
                        "Length": 16,
                        "Hex": content[:16] if truncated else content,
                    }},
                    "content_complete": not truncated,
                    "source_truncated": truncated,
                },
            }}},
        },
    }


class EepromComparisonBackend(ComparisonBackend):
    def __init__(self, *, missing=False, truncated=False):
        self.calls = []
        self.missing = missing
        self.truncated = truncated

    def debug_run(self, task, arguments, context):
        context.raise_if_stopped()
        self.calls.append(copy.deepcopy(arguments))
        observations = [
            comparison.TargetObservation.success(
                role=role,
                target_id=identity,
                started_at=TIME,
                completed_at=TIME,
                result=eeprom_result(
                    target["ip"],
                    REFERENCE_HEX if index == 0 else CANDIDATE_HEX,
                    truncated=self.truncated and index == 1,
                ),
            )
            for index, (target, (role, identity)) in enumerate(zip(
                arguments["targets"],
                comparison_target_identities(arguments["targets"]),
                strict=True,
            ))
        ]
        value = comparison.build_dual_comparison(observations=observations)
        if self.missing:
            # An incomplete transport envelope must not retain a conclusive diff.
            value["targets"].pop()
        return value


def start_arguments():
    return {
        "kind": "start",
        "targets": copy.deepcopy(TARGETS),
        "intent": "diagnosis-only",
        "purpose": "compare Eeprom0 bytes at offset 0 for length 16",
        "entry_operation": "debug_run",
        "entry_arguments": {"mdb_only": True, "mdb_queries": [EEPROM_QUERY]},
    }


def diagnosis_arguments(waiting):
    gate = waiting["gate"]
    return {
        "kind": "respond",
        "run_id": waiting["run_id"],
        "gate_id": gate["gate_id"],
        "gate_version": gate["gate_version"],
        "schema_digest": gate["schema_digest"],
        "submission_id": "eeprom-diagnosis",
        "response": {
            "status": "completed",
            "summary": "Eeprom0 differs at offset 15 in the compared 16-byte range",
            "payload": {
                "root_cause": "candidate Eeprom0 byte at offset 15 is fe; reference is ff",
                "evidence_ids": [
                    item["evidence_id"]
                    for item in waiting["diagnostic_receipt"]["evidence"]
                ],
                "causal_chain": [
                    "both MDB reads cover Eeprom0 offset 0 and length 16",
                    "the final EEPROM byte differs, producing the reported content mismatch",
                ],
                "code_owner": "fixture/eeprom_mgmt/eeprom_reader.lua",
                "contradictions": [],
                "remaining_gaps": [],
                "verification_status": "verified",
            },
        },
    }


class EepromComparisonCloseoutTests(unittest.TestCase):
    def comparison_result(self, waiting):
        return next(
            result for result in waiting["diagnostic_receipt"]["results"]
            if result["result_id"] == "comparison"
        )

    def assert_waiting_for_diagnosis(self, waiting):
        self.assertEqual(waiting["gate"]["name"], "diagnosis.acceptance")
        self.assertIsNone(waiting["outcome"])
        self.assertNotEqual(waiting["state"], "completed")

    def test_eeprom_difference_has_one_closeout_and_survives_restart_replay(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def open_service(backend):
                repository = SQLiteRuntimeRepository(root / "runtime.sqlite3")
                service = RuntimeMcpService(
                    backend,
                    context_repository=repository,
                    blob_repository=FilesystemBlobRepository(root / "blobs"),
                )
                return service, repository

            backend = EepromComparisonBackend()
            service, repository = open_service(backend)
            try:
                waiting = service.call_exposed_tool(
                    "execute", start_arguments(),
                    task_id="eeprom-closeout", operation_id="compare-eeprom",
                )
                self.assert_waiting_for_diagnosis(waiting)
                receipt = waiting["diagnostic_receipt"]
                self.assertEqual(receipt["status"], "complete", receipt)
                typed = self.comparison_result(waiting)["value"]
                self.assertEqual(typed["schema"], COMPARISON_RECEIPT_SCHEMA)
                self.assertEqual(typed["conclusion"], "different", typed)
                self.assertTrue(typed["differences"])
                self.assertEqual(len(typed["sources"]), 2)
                self.assertEqual(len({source["scope_digest"] for source in typed["sources"]}), 1)
                evidence_ids = {item["evidence_id"] for item in receipt["evidence"]}
                self.assertTrue(evidence_ids)
                for source, target in zip(typed["sources"], TARGETS, strict=True):
                    self.assertEqual(source["target_id"], target["target_id"])
                    self.assertEqual(source["role"], target["role"])
                    self.assertEqual(source["address"], target["ip"])
                    self.assertTrue(source["evidence_ids"])
                    self.assertLessEqual(set(source["evidence_ids"]), evidence_ids)
                    self.assertRegex(source["source_digest"], r"^sha256:[a-f0-9]{64}$")
                    self.assertRegex(source["scope_digest"], r"^sha256:[a-f0-9]{64}$")
                    self.assertEqual(source["observed_at"], TIME)
                eeprom_facts = [result for result in receipt["results"] if result["kind"] == "mdb"]
                self.assertEqual(len(eeprom_facts), 2)
                self.assertEqual(
                    {item["request"] for item in eeprom_facts},
                    {"target-1: " + EEPROM_QUERY, "target-2: " + EEPROM_QUERY},
                )
                self.assertEqual({
                    item["value"]["properties"]["Eeprom0"]["Hex"]
                    for item in eeprom_facts
                }, {REFERENCE_HEX, CANDIDATE_HEX})
                events = repository.events(waiting["run_id"])
                self.assertFalse(any(event["kind"] in {"RunOutcomeRecorded", "CloseoutRecorded"} for event in events))
                self.assertEqual(len(backend.calls), 1)
            finally:
                service.close()

            restarted_backend = EepromComparisonBackend()
            service, repository = open_service(restarted_backend)
            try:
                resumed = service.call_exposed_tool(
                    "execute", {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id="eeprom-closeout", operation_id="resume-eeprom-gate",
                )
                self.assert_waiting_for_diagnosis(resumed)
                self.assertEqual(resumed["gate"]["gate_id"], waiting["gate"]["gate_id"])
                self.assertEqual(self.comparison_result(resumed)["value"], typed)
                accepted = diagnosis_arguments(waiting)
                final = service.call_exposed_tool(
                    "execute", accepted,
                    task_id="eeprom-closeout", operation_id="accept-eeprom",
                )
                self.assertEqual(final["state"], "completed", final)
                self.assertTrue(final["outcome_recorded"])
                self.assertEqual(final["run_id"], waiting["run_id"])
                projection = service._test.context_runtime.read_case(waiting["run_id"])
                self.assertIn(projection["closeout"]["closure_status"], {"verified", "completed_in_scope"})
                self.assertTrue(projection["closeout_markdown"])
                self.assertIn("closeout_bundle", projection)
                diagnoses = [
                    phase["diagnosis_record"] for phase in projection["phase_records"]
                    if phase.get("phase_type") == "diagnosis.acceptance"
                ]
                self.assertEqual(len(diagnoses), 1)
                for name, expected in accepted["response"]["payload"].items():
                    self.assertEqual(diagnoses[0][name], expected)
                self.assertEqual(diagnoses[0]["run_id"], waiting["run_id"])
                self.assertEqual(diagnoses[0]["source_receipt_id"], receipt["receipt_id"])
                self.assertEqual(diagnoses[0]["workflow_cycle_id"], projection["workflow_cycle_id"])
                self.assertEqual(diagnoses[0]["target_version"], projection["target_version"])
                self.assertEqual(
                    projection["run_outcome"]["closeout_fingerprint"],
                    projection["closeout"]["fingerprint"],
                )
                self.assertEqual(restarted_backend.calls, [])
            finally:
                service.close()

            replay_backend = EepromComparisonBackend()
            service, repository = open_service(replay_backend)
            try:
                for arguments, operation_id in (
                    ({"kind": "resume", "run_id": waiting["run_id"]}, "resume-terminal"),
                    (accepted, "accept-eeprom"),
                    (start_arguments(), "compare-eeprom"),
                ):
                    replay = service.call_exposed_tool(
                        "execute", arguments,
                        task_id="eeprom-closeout", operation_id=operation_id,
                    )
                    self.assertEqual(replay["run_id"], waiting["run_id"])
                    self.assertEqual(replay["outcome"], final["outcome"])
                recovered = service._test.context_runtime.read_case(waiting["run_id"])
                for field in ("closeout", "closeout_markdown", "closeout_bundle", "run_outcome"):
                    self.assertEqual(recovered[field], projection[field])
                kinds = [event["kind"] for event in repository.events(waiting["run_id"])]
                self.assertEqual(kinds.count("CaseOpened"), 1)
                self.assertEqual(kinds.count("RunGateSubmitted"), 1)
                self.assertEqual(kinds.count("CloseoutRecorded"), 1)
                self.assertEqual(kinds.count("RunOutcomeRecorded"), 1)
                self.assertEqual(replay_backend.calls, [])
            finally:
                service.close()

    def test_missing_or_truncated_eeprom_target_cannot_complete_diagnosis(self):
        for mode, remaining_gaps in (
            ("missing", []),
            ("truncated", []),
            ("truncated", ["candidate EEPROM bytes 8 through 15 were not collected"]),
        ):
            with self.subTest(mode=mode, remaining_gaps=remaining_gaps):
                backend = EepromComparisonBackend(**{mode: True})
                service = RuntimeMcpService(backend)
                try:
                    task = f"eeprom-{mode}"
                    waiting = service.call_exposed_tool(
                        "execute", start_arguments(), task_id=task,
                        operation_id="compare-eeprom",
                    )
                    self.assert_waiting_for_diagnosis(waiting)
                    result = self.comparison_result(waiting)
                    self.assertEqual(result["value"]["conclusion"], "inconclusive")
                    self.assertEqual(result["status"], "unavailable")
                    self.assertTrue(result["value"]["incomparable_reasons"])
                    self.assertFalse(waiting["diagnostic_receipt"]["content_complete"])
                    rejected = diagnosis_arguments(waiting)
                    rejected["response"]["payload"]["remaining_gaps"] = remaining_gaps
                    with self.assertRaisesRegex(GateConflict, "evaluable comparison"):
                        service.call_exposed_tool(
                            "execute", rejected,
                            task_id=task, operation_id="accept-incomplete-eeprom",
                        )
                    resumed = service.call_exposed_tool(
                        "execute", {"kind": "resume", "run_id": waiting["run_id"]},
                        task_id=task, operation_id="resume-incomplete-eeprom",
                    )
                    self.assert_waiting_for_diagnosis(resumed)
                    kinds = [event["kind"] for event in service._test.context_runtime.repository.events(waiting["run_id"])]
                    self.assertNotIn("RunOutcomeRecorded", kinds)
                    self.assertNotIn("CloseoutRecorded", kinds)
                    self.assertEqual(len(backend.calls), 1)
                finally:
                    service.close()

    def test_incomplete_eeprom_comparison_can_be_failed_or_cancelled(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                backend = EepromComparisonBackend(truncated=True)
                service = RuntimeMcpService(backend)
                try:
                    waiting = service.call_exposed_tool(
                        "execute", start_arguments(),
                        task_id=status, operation_id="compare-eeprom",
                    )
                    arguments = diagnosis_arguments(waiting)
                    arguments["response"] = {
                        "status": status,
                        "summary": "EEPROM comparison stopped after a truncated candidate read",
                        "payload": {"remaining_gaps": ["candidate EEPROM read is incomplete"]},
                    }
                    final = service.call_exposed_tool(
                        "execute", arguments,
                        task_id=status, operation_id="stop-eeprom-comparison",
                    )
                    self.assertEqual(final["state"], status)
                    self.assertEqual(final["outcome"]["status"], status)
                    projection = service._test.context_runtime.read_case(waiting["run_id"])
                    self.assertNotIn(projection["closeout"]["closure_status"], {"verified", "completed_in_scope"})
                    kinds = [event["kind"] for event in service._test.context_runtime.repository.events(waiting["run_id"])]
                    self.assertEqual(kinds.count("CloseoutRecorded"), 1)
                    self.assertEqual(kinds.count("RunOutcomeRecorded"), 1)
                    self.assertEqual(len(backend.calls), 1)
                finally:
                    service.close()


if __name__ == "__main__":
    unittest.main()
