from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    EVIDENCE_QUERY_MAX_BYTES,
    InMemoryBlobRepository,
    JsonRpcMcpEndpoint,
    LocalArtifactStore,
    PendingCaseEvent,
    RuntimeMcpService,
    SQLiteArtifactRepository,
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
        return {
            "ok": True,
            "schema": "test/debug",
            "summary": "captured",
            "root_cause": "bounded test diagnosis",
            "observed_at": "2026-08-30T00:00:00Z",
            "freshness": {"status": "fresh"},
        }


def _create_public_run(
    repository: SQLiteRuntimeRepository,
    *,
    target: str,
    terminal: bool = False,
) -> str:
    service = RuntimeMcpService(_Backend(), context_repository=repository)
    try:
        turn = service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": target,
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "entry_operation": "debug_run",
            },
            task_id=f"public-run-{target}",
            operation_id=f"public-run-{target}-start",
        )
        run_id = str(turn["run_id"])
        if terminal:
            gate = turn["gate"]
            assert isinstance(gate, dict)
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": run_id,
                    "gate_id": gate["gate_id"],
                    "gate_version": gate["gate_version"],
                    "schema_digest": gate["schema_digest"],
                    "response": {
                        "status": "completed",
                        "summary": "source-only test completed",
                        "payload": {
                            "source_revision": "test-source-revision",
                            "authored_files": ["src/test.lua"],
                            "verification_plan": ["local validation"],
                        },
                    },
                },
                task_id=f"public-run-{target}",
                operation_id=f"public-run-{target}-developer",
            )
            if turn["state"] != "completed":
                raise AssertionError("public terminal Run did not complete")
        return run_id
    finally:
        service.close()


class EvidenceIndexTests(unittest.TestCase):
    def test_operator_can_attach_digest_bound_file_evidence_to_an_open_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "runtime.sqlite3"
            evidence_path = root / "official-ut.log"
            evidence_path.write_bytes(b"3/3 passed\n")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            repository = SQLiteRuntimeRepository(database)
            run_id = _create_public_run(repository, target="192.0.2.80")
            service = RuntimeMcpService(
                _Backend(),
                context_repository=repository,
                interface_profile="operator",
            )
            try:
                attached = service.call_exposed_tool(
                    "evidence_attach",
                    {
                        "run_id": run_id,
                        "target": "192.0.2.80",
                        "path": str(evidence_path),
                        "sha256": digest,
                        "evidence_type": "workflow-official-ut-record",
                    },
                    task_id="operator-evidence",
                    operation_id="attach-official-ut",
                )
                loaded = service.call_exposed_tool(
                    "evidence_read",
                    {
                        "case_id": run_id,
                        "evidence_id": attached["evidence"]["evidence_id"],
                    },
                    task_id="operator-evidence",
                    operation_id="read-official-ut",
                )
            finally:
                service.close()

        self.assertTrue(attached["attached"])
        self.assertFalse(attached["idempotent_replay"])
        self.assertEqual(attached["evidence"]["blob_id"], digest)
        self.assertEqual(attached["evidence"]["target_id"], "target-1")
        self.assertEqual(loaded["body"], "3/3 passed\n")

    def test_recovery_package_is_managed_by_artifact_store_not_evidence_blob(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            recovery = root / "recovery.hpm"
            recovery_body = b"firmware recovery package" * 4096
            recovery.write_bytes(recovery_body)
            digest = hashlib.sha256(recovery_body).hexdigest()
            repository = SQLiteRuntimeRepository(root / "runtime.sqlite3")
            blobs = InMemoryBlobRepository()
            artifacts = LocalArtifactStore(
                content_root=root / "artifacts",
                repository=SQLiteArtifactRepository(root / "artifacts.sqlite3"),
            )
            run_id = _create_public_run(repository, target="192.0.2.84")
            service = RuntimeMcpService(
                _Backend(),
                context_repository=repository,
                blob_repository=blobs,
                artifact_store=artifacts,
                interface_profile="operator",
            )
            try:
                attached = service.call_exposed_tool(
                    "evidence_attach",
                    {
                        "run_id": run_id,
                        "target": "192.0.2.84",
                        "path": str(recovery),
                        "sha256": digest,
                        "evidence_type": "firmware-recovery-artifact",
                    },
                    task_id="operator-recovery-artifact",
                    operation_id="attach-recovery-artifact",
                )
                loaded = service.call_exposed_tool(
                    "evidence_read",
                    {
                        "case_id": run_id,
                        "evidence_id": attached["evidence"]["evidence_id"],
                    },
                    task_id="operator-recovery-artifact",
                    operation_id="read-recovery-artifact",
                )
            finally:
                service.close()
            artifact_ref = attached["evidence"]["artifact_ref"]
            managed_body = artifacts.resolve(
                artifacts.reference(artifact_ref)
            ).read_bytes()

        self.assertNotEqual(attached["evidence"]["blob_id"], digest)
        self.assertLess(blobs.size_bytes(), len(recovery_body))
        self.assertEqual(artifact_ref["handle"], f"artifact://sha256/{digest}")
        self.assertEqual(artifact_ref["digest"], f"sha256:{digest}")
        self.assertEqual(artifact_ref["kind"], "openubmc-hpm")
        self.assertEqual(artifact_ref["run_id"], run_id)
        self.assertEqual(artifact_ref["target"], "192.0.2.84")
        self.assertEqual(managed_body, recovery_body)
        self.assertEqual(json.loads(loaded["body"])["artifact_ref"], artifact_ref)

    def test_operator_file_evidence_attach_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_path = root / "build.log"
            evidence_path.write_bytes(b"build completed\n")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            repository = SQLiteRuntimeRepository(root / "runtime.sqlite3")
            run_id = _create_public_run(repository, target="192.0.2.81")
            service = RuntimeMcpService(
                _Backend(),
                context_repository=repository,
                interface_profile="operator",
            )
            arguments = {
                "run_id": run_id,
                "target": "target-1",
                "path": str(evidence_path),
                "sha256": digest,
                "evidence_type": "component-build-log",
            }
            try:
                first = service.call_exposed_tool(
                    "evidence_attach",
                    arguments,
                    task_id="operator-evidence",
                    operation_id="attach-build-first",
                )
                first_revision = repository.current_revision(run_id)
                attachment_events = [
                    event["kind"]
                    for event in repository.events(run_id)
                    if event["operation_id"] == "attach-build-first"
                ]
                second = service.call_exposed_tool(
                    "evidence_attach",
                    arguments,
                    task_id="operator-evidence",
                    operation_id="attach-build-second",
                )
                second_revision = repository.current_revision(run_id)
            finally:
                service.close()

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["evidence"], second["evidence"])
        self.assertEqual(
            attachment_events,
            ["EvidenceAttached", "RunDecisionCommitted"],
        )
        self.assertEqual(first_revision, second_revision)

    def test_operator_file_evidence_attach_rejects_invalid_run_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_path = root / "diagnosis.md"
            evidence_path.write_bytes(b"root cause and fix\n")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            repository = SQLiteRuntimeRepository(root / "runtime.sqlite3")
            run_id = _create_public_run(
                repository,
                target="192.0.2.82",
                terminal=True,
            )
            service = RuntimeMcpService(
                _Backend(),
                context_repository=repository,
                interface_profile="operator",
            )
            try:
                with self.assertRaisesRegex(Exception, "no longer accepts evidence"):
                    service.call_exposed_tool(
                        "evidence_attach",
                        {
                            "run_id": run_id,
                            "target": "target-1",
                            "path": str(evidence_path),
                            "sha256": digest,
                            "evidence_type": "workflow-diagnosis-record",
                        },
                        task_id="operator-evidence",
                        operation_id="attach-terminal",
                    )
            finally:
                service.close()

    def test_operator_file_evidence_attach_rejects_digest_and_target_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            evidence_path = root / "diagnosis.md"
            evidence_path.write_bytes(b"root cause and fix\n")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            repository = SQLiteRuntimeRepository(root / "runtime.sqlite3")
            run_id = _create_public_run(repository, target="192.0.2.83")
            service = RuntimeMcpService(
                _Backend(),
                context_repository=repository,
                interface_profile="operator",
            )
            base = {
                "run_id": run_id,
                "target": "target-1",
                "path": str(evidence_path),
                "sha256": digest,
                "evidence_type": "workflow-diagnosis-record",
            }
            try:
                with self.assertRaisesRegex(ValueError, "exactly one Runtime Run target"):
                    service.call_exposed_tool(
                        "evidence_attach",
                        {**base, "target": "192.0.2.200"},
                        task_id="operator-evidence",
                        operation_id="attach-wrong-target",
                    )
                with self.assertRaisesRegex(ValueError, "digest mismatch"):
                    service.call_exposed_tool(
                        "evidence_attach",
                        {**base, "sha256": "0" * 64},
                        task_id="operator-evidence",
                        operation_id="attach-wrong-digest",
                    )
            finally:
                service.close()

    def test_operator_query_rejects_non_finite_observation_times(self) -> None:
        service = RuntimeMcpService(_Backend(), interface_profile="operator")
        try:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "finite"
                ):
                    service.call_tool(
                        "evidence_query",
                        {"observed_after": value},
                        task_id="evidence-operator",
                        operation_id="query-non-finite-time",
                    )
        finally:
            service.close()

    def test_operator_mcp_query_returns_structured_evidence_results(self) -> None:
        service = RuntimeMcpService(_Backend(), interface_profile="operator")
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="operator-session")
        try:
            service.call_tool(
                "debug_run",
                {"ip": "192.0.2.69"},
                task_id="evidence-mcp-source",
                operation_id="debug-mcp-source",
            )
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "evidence_query", "arguments": {}},
                }
            )
        finally:
            service.close()

        structured = response["result"]["structuredContent"]
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(structured["returned_item_count"], 1)
        self.assertEqual(len(structured["items"]), 1)

    def test_operator_query_response_stays_bounded_for_oversized_filters(self) -> None:
        service = RuntimeMcpService(_Backend(), interface_profile="operator")
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="operator-session")
        try:
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "evidence_query",
                        "arguments": {
                            "producer": "x" * (EVIDENCE_QUERY_MAX_BYTES * 2)
                        },
                    },
                }
            )
        finally:
            service.close()

        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertLessEqual(
            len(json.dumps(result, separators=(",", ":")).encode("utf-8")),
            EVIDENCE_QUERY_MAX_BYTES,
        )

    def test_operator_query_deduplicates_content_and_returns_a_readable_reference(self) -> None:
        service = RuntimeMcpService(_Backend(), interface_profile="operator")
        try:
            first = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.70"},
                task_id="evidence-query-one",
                operation_id="debug-query-one",
            )
            second = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.70"},
                task_id="evidence-query-two",
                operation_id="debug-query-two",
            )

            result = service.call_tool(
                "evidence_query",
                {"target_id": "target-1", "limit": 10},
                task_id="evidence-operator",
                operation_id="query-duplicate-content",
            )
            item = result["items"][0]
            loaded = service.call_tool(
                "evidence_read",
                {
                    "case_id": item["case_id"],
                    "evidence_id": item["evidence_id"],
                },
                task_id="evidence-operator",
                operation_id="read-query-result",
            )
        finally:
            service.close()

        self.assertEqual(result["matched_reference_count"], 2)
        self.assertEqual(result["unique_content_count"], 1)
        self.assertEqual(result["returned_item_count"], 1)
        self.assertEqual(item["reference_count"], 2)
        self.assertEqual(item["case_count"], 2)
        self.assertEqual(item["target_count"], 1)
        self.assertEqual(item["generation_count"], 1)
        self.assertIn(
            item["case_id"],
            {first.envelope["case_id"], second.envelope["case_id"]},
        )
        self.assertEqual(json.loads(loaded["body"])["schema"], "test/debug")

    def test_operator_query_filters_orders_and_bounds_exact_references(self) -> None:
        service = RuntimeMcpService(_Backend(), interface_profile="operator")
        try:
            first = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.73"},
                task_id="evidence-filter-one",
                operation_id="debug-filter-one",
            )
            second = service.call_tool(
                "debug_run",
                {"ip": "192.0.2.73"},
                task_id="evidence-filter-two",
                operation_id="debug-filter-two",
            )

            exact = service.call_tool(
                "evidence_query",
                {
                    "producer": "debug_run",
                    "deduplicate": False,
                    "limit": 1,
                },
                task_id="evidence-operator",
                operation_id="query-exact-references",
            )
            scoped = service.call_tool(
                "evidence_query",
                {"case_id": first.envelope["case_id"]},
                task_id="evidence-operator",
                operation_id="query-one-case",
            )
        finally:
            service.close()

        self.assertEqual(exact["matched_reference_count"], 2)
        self.assertEqual(exact["unique_content_count"], 1)
        self.assertEqual(exact["returned_item_count"], 1)
        self.assertTrue(exact["truncated"])
        self.assertEqual(exact["items"][0]["case_id"], second.envelope["case_id"])
        self.assertEqual(exact["items"][0]["reference_count"], 1)
        self.assertEqual(scoped["matched_reference_count"], 1)
        self.assertEqual(scoped["items"][0]["case_id"], first.envelope["case_id"])

    def test_content_folding_reports_cross_target_scope_counts(self) -> None:
        service = RuntimeMcpService(_Backend(), interface_profile="operator")
        try:
            service.call_tool(
                "debug_run",
                {"ip": "192.0.2.75", "target_id": "bmc-a"},
                task_id="evidence-target-one",
                operation_id="debug-target-one",
            )
            service.call_tool(
                "debug_run",
                {"ip": "192.0.2.76", "target_id": "bmc-b"},
                task_id="evidence-target-two",
                operation_id="debug-target-two",
            )
            result = service.call_tool(
                "evidence_query",
                {},
                task_id="evidence-operator",
                operation_id="query-cross-target-content",
            )
        finally:
            service.close()

        self.assertEqual(result["matched_reference_count"], 2)
        self.assertEqual(result["unique_content_count"], 1)
        self.assertEqual(result["returned_item_count"], 1)
        self.assertEqual(result["items"][0]["target_count"], 2)

    def test_sqlite_operator_query_matches_in_memory_shape(self) -> None:
        def collect(service: RuntimeMcpService) -> dict[str, object]:
            service.call_tool(
                "debug_run",
                {"ip": "192.0.2.74"},
                task_id="evidence-parity-one",
                operation_id="debug-parity-one",
            )
            service.call_tool(
                "debug_run",
                {"ip": "192.0.2.74"},
                task_id="evidence-parity-two",
                operation_id="debug-parity-two",
            )
            result = service.call_tool(
                "evidence_query",
                {"target_id": "target-1"},
                task_id="evidence-operator",
                operation_id="query-parity",
            )
            return {
                "matched_reference_count": result["matched_reference_count"],
                "unique_content_count": result["unique_content_count"],
                "returned_item_count": result["returned_item_count"],
                "reference_count": result["items"][0]["reference_count"],
                "case_count": result["items"][0]["case_count"],
            }

        memory = RuntimeMcpService(_Backend(), interface_profile="operator")
        try:
            memory_shape = collect(memory)
        finally:
            memory.close()
        with tempfile.TemporaryDirectory() as raw:
            sqlite = RuntimeMcpService(
                _Backend(),
                interface_profile="operator",
                context_repository=SQLiteRuntimeRepository(
                    Path(raw) / "runtime.sqlite3"
                ),
            )
            try:
                sqlite_shape = collect(sqlite)
            finally:
                sqlite.close()

        self.assertEqual(sqlite_shape, memory_shape)

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
