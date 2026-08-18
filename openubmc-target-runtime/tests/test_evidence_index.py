from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    InMemoryBlobRepository,
    PendingCaseEvent,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _Backend:
    def open_task(self, task_id: str) -> _Task:
        return _Task(task_id)

    @staticmethod
    def close_task(_task: _Task) -> None:
        return None

    @staticmethod
    def maintain_task(_task: _Task) -> int:
        return 0

    @staticmethod
    def task_status(task: _Task) -> dict[str, object]:
        return {"task_id": task.task_id}

    @staticmethod
    def debug_run(_task, _arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {"ok": True, "schema": "test/debug", "summary": "captured"}


class EvidenceIndexTests(unittest.TestCase):
    def test_earliest_evidence_remains_readable_after_projection_truncation(self) -> None:
        service = RuntimeMcpService(_Backend())
        try:
            first = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.71"},
                task_id="evidence-index-case",
                operation_id="debug-000",
            )
            case_id = first.envelope["case_id"]
            earliest = first.envelope["evidence_refs"][0]
            for index in range(1, 260):
                service.call_tool(
                    "debug_run",
                    {"case_id": case_id},
                    task_id="evidence-index-case",
                    operation_id=f"debug-{index:03d}",
                )
            case = service.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="evidence-index-case",
                operation_id="read-truncated-case",
            )
            loaded = service.call_tool(
                "evidence_read",
                {
                    "case_id": case_id,
                    "evidence_id": earliest["evidence_id"],
                },
                task_id="evidence-index-case",
                operation_id="read-earliest-evidence",
            )
        finally:
            service.close()

        self.assertTrue(case["projection_truncated"])
        self.assertNotIn(
            earliest["evidence_id"],
            {item["evidence_id"] for item in case["evidence_refs"]},
        )
        self.assertEqual(json.loads(loaded["body"])["schema"], "test/debug")
        self.assertEqual(loaded["evidence"]["producer"], "debug_run")
        self.assertEqual(loaded["evidence"]["workflow_attempt"], 1)

    def test_sqlite_initialization_idempotently_backfills_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "context.sqlite3"
            repository = SQLiteRuntimeRepository(database)
            reference = {
                "evidence_id": "evidence-legacy",
                "blob_id": "a" * 64,
                "media_type": "application/json",
                "byte_count": 2,
                "target_id": "target-1",
                "generation": "1",
                "provenance": "debug_run:legacy",
                "observed_at": 1.0,
            }
            repository.commit(
                "legacy-index-case",
                expected_revision=0,
                events=(
                    PendingCaseEvent(
                        "CaseOpened",
                        {
                            "intent": "diagnosis-only",
                            "acceptance_plan": {},
                            "workflow_inputs": {},
                        },
                    ),
                    PendingCaseEvent(
                        "EvidenceAttached",
                        {"evidence": reference},
                        "legacy-operation",
                    ),
                ),
            )
            with sqlite3.connect(database) as connection:
                connection.execute("DELETE FROM evidence_index")
            reopened = SQLiteRuntimeRepository(database)

            first = reopened.evidence_reference(
                "legacy-index-case", "evidence-legacy"
            )
            reopened_again = SQLiteRuntimeRepository(database)
            second = reopened_again.evidence_reference(
                "legacy-index-case", "evidence-legacy"
            )

        self.assertEqual(first["blob_id"], "a" * 64)
        self.assertEqual(first, second)

    def test_reference_aware_gc_retains_then_deletes_a_shared_blob(self) -> None:
        blobs = InMemoryBlobRepository()
        service = RuntimeMcpService(_Backend(), blob_repository=blobs)
        try:
            first = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.72"},
                task_id="shared-blob-one",
                operation_id="shared-debug-one",
            )
            second = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.72"},
                task_id="shared-blob-two",
                operation_id="shared-debug-two",
            )
            first_ref = first.envelope["evidence_refs"][0]
            second_ref = second.envelope["evidence_refs"][0]
            self.assertEqual(first_ref["blob_id"], second_ref["blob_id"])

            first_forget = service.call_tool(
                "case_forget",
                {"case_id": first.envelope["case_id"]},
                task_id="shared-blob-one",
                operation_id="forget-shared-one",
            )
            still_readable = service.call_tool(
                "evidence_read",
                {
                    "case_id": second.envelope["case_id"],
                    "evidence_id": second_ref["evidence_id"],
                },
                task_id="shared-blob-two",
                operation_id="read-shared-two",
            )
            second_forget = service.call_tool(
                "case_forget",
                {"case_id": second.envelope["case_id"]},
                task_id="shared-blob-two",
                operation_id="forget-shared-two",
            )
        finally:
            service.close()

        self.assertEqual(first_forget["evidence_gc"]["retained"], 1)
        self.assertIn("test/debug", still_readable["body"])
        self.assertEqual(second_forget["evidence_gc"]["deleted"], 1)
        self.assertEqual(blobs.blob_ids(), ())


if __name__ == "__main__":
    unittest.main()
