from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openubmc_target_runtime import (
    aggregate_case_closeout,
    DomainAction,
    DomainReceipt,
    InMemoryRuntimeRepository,
    RuntimeMcpService,
    RuntimeSDKContext,
    SQLiteRuntimeRepository,
    mutation_receipt_verifier,
)
from openubmc_target_runtime.domain_packs import _batch_conformance_example
from test_agent_gateway import SemanticBackend


class BatchBackend(SemanticBackend):
    def upgrade_batch(self, task, arguments, context):
        context.raise_if_stopped()
        self.calls.append(("upgrade_batch", dict(arguments)))
        targets = []
        epochs = {}
        for index, target in enumerate(arguments["targets"], start=1):
            target_id, host = target["target_id"], target["ip"]
            port = arguments.get("redfish_port", 443)
            suffix = hashlib.sha256(
                f"{target_id}\0{host}\0{port}".encode()
            ).hexdigest()[:20]
            child_id = f"{context.operation_id}:target-{suffix}"
            journal = {
                "schema": "openubmc.target-runtime.v1/mutation-journal",
                "task_id": task.task_id,
                "operation_id": child_id,
                "operation_fingerprint": "a" * 64,
                "target_fingerprint": hashlib.sha256(host.encode()).hexdigest(),
                "action": "upgrade",
                "stage": "verified",
                "effects_started": True,
                "expected_checksum": arguments["artifact_sha256"],
            }
            epochs[target_id] = index + 1
            result = {
                "operation_id": child_id,
                "target_fingerprint": journal["target_fingerprint"],
                "journal": journal,
                "epoch_after": epochs[target_id],
                "verification": {"installed_version": arguments["product_version"]},
            }
            targets.append({
                **target,
                "redfish_port": port,
                "operation_id": child_id,
                "requested_operation_id": child_id,
                "target_fingerprint": journal["target_fingerprint"],
                "status": "completed",
                "journal": journal,
                "result": result,
            })
        return {
            "batch_operation_id": context.operation_id,
            "ok": True,
            "status": "completed",
            "outcome_status": "succeeded",
            "total": len(targets),
            "succeeded": len(targets),
            "failed": 0,
            "unknown": 0,
            "skipped": 0,
            "targets": targets,
            "target_epochs": epochs,
            "epoch_after": max(epochs.values()),
        }


class TruncatedBatchProjectionRepository(InMemoryRuntimeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.hidden_evidence_id = ""
        self.compacted_evidence_id = ""
        self.replacement_target_address = ""
        self.reference_overrides = {}
        self.reference_fields_to_remove = set()

    def load(self, case_id):
        projection = super().load(case_id)
        if projection is None or not (
            self.hidden_evidence_id or self.compacted_evidence_id
        ):
            return projection
        projected_references = []
        for reference in projection["evidence_refs"]:
            if reference["evidence_id"] == self.hidden_evidence_id:
                continue
            if reference["evidence_id"] == self.compacted_evidence_id:
                projected_references.append(
                    {
                        "evidence_id": reference["evidence_id"],
                        "blob_id": reference["blob_id"],
                    }
                )
            else:
                projected_references.append(reference)
        projection["evidence_refs"] = projected_references
        if self.replacement_target_address:
            projection["targets"][0]["address"] = self.replacement_target_address
        return projection

    def evidence_reference(self, case_id, evidence_id):
        reference = super().evidence_reference(case_id, evidence_id)
        if reference is None or evidence_id != self.hidden_evidence_id:
            return reference
        reference.update(self.reference_overrides)
        for field in self.reference_fields_to_remove:
            reference.pop(field, None)
        return reference


class BatchReceiptTests(unittest.TestCase):
    def setUp(self):
        example = _batch_conformance_example()
        self.arguments = copy.deepcopy(dict(example.arguments))
        self.value = copy.deepcopy(dict(example.receipt.value))
        self.action = DomainAction(
            operation="upgrade_batch",
            pack="upgrade",
            pack_version="1",
            context=RuntimeSDKContext(
                task_id="conformance-upgrade_batch",
                operation_id="effect-conformance-upgrade_batch",
                timeout_seconds=10,
            ),
            arguments=self.arguments,
        )

    def valid(self, value=None):
        return mutation_receipt_verifier(
            self.action,
            DomainReceipt("upgrade_batch", "succeeded", value or self.value),
            journal_action="upgrade",
        )

    def test_request_bound_receipt_is_valid(self):
        self.assertTrue(self.valid())

    def test_old_child_journal_cannot_authenticate_current_effect(self):
        target = self.value["targets"][0]
        old_id = target["operation_id"].replace("effect-conformance", "old-rollout")
        target["operation_id"] = old_id
        target["journal"]["operation_id"] = old_id
        target["reconciled_existing_operation"] = True
        self.assertFalse(self.valid())

    def test_other_target_artifact_task_or_count_is_rejected(self):
        for scenario in ("target", "artifact", "task", "count", "journal", "port"):
            with self.subTest(scenario=scenario):
                value = copy.deepcopy(self.value)
                target = value["targets"][0]
                if scenario == "target":
                    target["ip"] = "another-target"
                elif scenario == "artifact":
                    target["journal"]["expected_checksum"] = "d" * 64
                elif scenario == "task":
                    target["journal"]["task_id"] = "another-task"
                elif scenario == "count":
                    value["succeeded"] = 2
                elif scenario == "journal":
                    target["journal"]["operation_id"] = "another-operation"
                else:
                    target["redfish_port"] = 8443
                self.assertFalse(self.valid(value))

    def test_child_result_cannot_change_artifact_identity(self):
        for field, replacement in (("artifact_sha256", "d" * 64), ("product_version", "2.0.0")):
            with self.subTest(field=field):
                value = copy.deepcopy(self.value)
                target = value["targets"][0]
                target["result"] = {
                    "operation_id": target["operation_id"],
                    "target_fingerprint": target["target_fingerprint"],
                    "journal": target["journal"],
                    "epoch_after": 1,
                }
                self.assertTrue(self.valid(value))
                target["result"][field] = replacement
                self.assertFalse(self.valid(value))

    def test_duplicate_missing_or_reordered_target_receipts_are_rejected(self):
        value = copy.deepcopy(self.value)
        value["targets"].append(copy.deepcopy(value["targets"][0]))
        self.assertFalse(self.valid(value))
        value["targets"] = []
        self.assertFalse(self.valid(value))

    def test_aggregate_epoch_and_status_must_match_child_receipts(self):
        for field, replacement in (
            ("target_epochs", {"target-1": 2}),
            ("target_epochs", {"another-target": 1}),
            ("target_epochs", {"target-1": True}),
            ("epoch_after", 2),
            ("status", "partial"),
            ("outcome_status", "failed"),
            ("ok", False),
            ("total", True),
        ):
            with self.subTest(field=field, replacement=replacement):
                value = copy.deepcopy(self.value)
                value[field] = replacement
                self.assertFalse(self.valid(value))


def batch_start_arguments(
    artifact: Path,
    *,
    entry_operation: str = "upgrade_batch",
) -> dict[str, object]:
    return {
        "kind": "start",
        "intent": "upgrade-and-verify",
        "targets": [
            {
                "target_id": "bmc-a",
                "ip": "192.0.2.81",
                "role": "reference",
            },
            {
                "target_id": "bmc-b",
                "ip": "192.0.2.82",
                "role": "candidate",
            },
        ],
        "entry_operation": entry_operation,
        "entry_arguments": {
            "artifact_path": str(artifact),
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "product_version": "1.2.3",
        },
    }


class BatchWorkflowTests(unittest.TestCase):
    def test_evidence_query_finds_a_batch_reference_by_child_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "product.hpm"
            artifact.write_bytes(b"batch firmware")
            repositories = (
                ("memory", InMemoryRuntimeRepository()),
                ("sqlite", SQLiteRuntimeRepository(root / "runtime.sqlite3")),
            )
            for adapter, repository in repositories:
                with self.subTest(adapter=adapter):
                    service = RuntimeMcpService(
                        BatchBackend(), context_repository=repository
                    )
                    try:
                        final = service.call_exposed_tool(
                            "execute",
                            batch_start_arguments(artifact),
                            task_id=f"batch-evidence-query-{adapter}",
                            operation_id=f"batch-evidence-query-start-{adapter}",
                        )
                    finally:
                        service.close()
                    operator = RuntimeMcpService(
                        BatchBackend(),
                        context_repository=repository,
                        interface_profile="operator",
                    )
                    try:
                        result = operator.call_tool(
                            "evidence_query",
                            {
                                "case_id": final["run_id"],
                                "target_id": "bmc-a",
                                "producer": "upgrade_batch",
                            },
                            task_id=f"batch-evidence-query-operator-{adapter}",
                            operation_id=f"batch-evidence-query-read-{adapter}",
                        )
                    finally:
                        operator.close()

                    self.assertEqual(result["matched_reference_count"], 1)
                    self.assertEqual(result["returned_item_count"], 1)
                    self.assertEqual(result["items"][0]["target_count"], 2)
                    self.assertEqual(
                        {
                            binding["target_id"]
                            for binding in result["items"][0]["target_bindings"]
                        },
                        {"bmc-a", "bmc-b"},
                    )

    def test_closeout_validates_durable_batch_identity_after_projection_truncation(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product.hpm"
            artifact.write_bytes(b"batch firmware")
            repository = TruncatedBatchProjectionRepository()
            service = RuntimeMcpService(
                BatchBackend(),
                context_repository=repository,
            )
            try:
                final = service.call_exposed_tool(
                    "execute",
                    batch_start_arguments(artifact),
                    task_id="batch-truncated-reference",
                    operation_id="batch-truncated-reference-start",
                )
                projection = repository.load(final["run_id"])
                batch_operation = next(
                    operation
                    for operation in projection["operations"]
                    if operation["operation"] == "upgrade_batch"
                )
                repository.hidden_evidence_id = batch_operation["evidence_ids"][-1]
                repository.replacement_target_address = "192.0.2.99"

                derived = service._test.context_runtime.derive_run_closeout(
                    final["run_id"], terminal_status="completed"
                )
            finally:
                service.close()

        upgrade_receipts = [
            receipt
            for receipt in derived["closeout"]["receipts"]
            if receipt["stage"] == "upgrade"
        ]
        self.assertEqual(
            [receipt["status"] for receipt in upgrade_receipts], ["partial"]
        )
        self.assertEqual(derived["closeout"]["identity_status"], "incomplete")
        self.assertEqual(derived["closeout"]["closure_status"], "failed")

    def test_closeout_resolves_durable_identity_after_projection_compaction(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product.hpm"
            artifact.write_bytes(b"batch firmware")
            repository = TruncatedBatchProjectionRepository()
            service = RuntimeMcpService(
                BatchBackend(),
                context_repository=repository,
            )
            try:
                final = service.call_exposed_tool(
                    "execute",
                    batch_start_arguments(artifact),
                    task_id="batch-compacted-reference",
                    operation_id="batch-compacted-reference-start",
                )
                projection = repository.load(final["run_id"])
                batch_operation = next(
                    operation
                    for operation in projection["operations"]
                    if operation["operation"] == "upgrade_batch"
                )
                repository.compacted_evidence_id = batch_operation[
                    "evidence_ids"
                ][-1]
                repository.replacement_target_address = "192.0.2.99"

                derived = service._test.context_runtime.derive_run_closeout(
                    final["run_id"], terminal_status="completed"
                )
            finally:
                service.close()

        upgrade_receipts = [
            receipt
            for receipt in derived["closeout"]["receipts"]
            if receipt["stage"] == "upgrade"
        ]
        self.assertEqual(
            [receipt["status"] for receipt in upgrade_receipts], ["partial"]
        )
        self.assertEqual(derived["closeout"]["identity_status"], "incomplete")
        self.assertEqual(derived["closeout"]["closure_status"], "failed")

    def test_closeout_validates_durable_batch_producer_after_projection_truncation(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product.hpm"
            artifact.write_bytes(b"batch firmware")
            repository = TruncatedBatchProjectionRepository()
            service = RuntimeMcpService(
                BatchBackend(),
                context_repository=repository,
            )
            try:
                final = service.call_exposed_tool(
                    "execute",
                    batch_start_arguments(artifact),
                    task_id="batch-durable-producer",
                    operation_id="batch-durable-producer-start",
                )
                projection = repository.load(final["run_id"])
                batch_operation = next(
                    operation
                    for operation in projection["operations"]
                    if operation["operation"] == "upgrade_batch"
                )
                repository.hidden_evidence_id = batch_operation["evidence_ids"][-1]

                for field, invalid_value in (
                    ("operation", "debug_collect"),
                    ("operation_id", "another-operation"),
                    ("observed_at", 0.0),
                ):
                    with self.subTest(field=field):
                        repository.reference_overrides = {field: invalid_value}
                        derived = service._test.context_runtime.derive_run_closeout(
                            final["run_id"], terminal_status="completed"
                        )
                        upgrade_receipts = [
                            receipt
                            for receipt in derived["closeout"]["receipts"]
                            if receipt["stage"] == "upgrade"
                        ]
                        self.assertEqual(
                            [
                                receipt["status"]
                                for receipt in upgrade_receipts
                            ],
                            ["partial"],
                        )
                        self.assertEqual(
                            derived["closeout"]["closure_status"], "failed"
                        )
            finally:
                service.close()

    def test_closeout_keeps_legacy_batch_evidence_readable_after_projection_truncation(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product.hpm"
            artifact.write_bytes(b"batch firmware")
            repository = TruncatedBatchProjectionRepository()
            service = RuntimeMcpService(
                BatchBackend(),
                context_repository=repository,
            )
            try:
                final = service.call_exposed_tool(
                    "execute",
                    batch_start_arguments(artifact),
                    task_id="batch-legacy-reference",
                    operation_id="batch-legacy-reference-start",
                )
                projection = repository.load(final["run_id"])
                batch_operation = next(
                    operation
                    for operation in projection["operations"]
                    if operation["operation"] == "upgrade_batch"
                )
                repository.hidden_evidence_id = batch_operation["evidence_ids"][-1]
                repository.reference_fields_to_remove = {
                    "target_bindings",
                    "operation",
                    "operation_id",
                    "observed_at",
                }

                derived = service._test.context_runtime.derive_run_closeout(
                    final["run_id"], terminal_status="completed"
                )
            finally:
                service.close()

        upgrade_receipts = [
            receipt
            for receipt in derived["closeout"]["receipts"]
            if receipt["stage"] == "upgrade"
        ]
        self.assertEqual(
            [receipt["status"] for receipt in upgrade_receipts],
            ["completed", "completed"],
        )
        self.assertEqual(derived["closeout"]["closure_status"], "verified")

    def test_agent_upgrades_every_target_and_collects_each_new_epoch(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product.hpm"
            artifact.write_bytes(b"batch firmware")
            for entry in ("upgrade_run", "upgrade_batch"):
                with self.subTest(entry=entry):
                    backend = BatchBackend()
                    service = RuntimeMcpService(backend)
                    try:
                        final = service.call_exposed_tool(
                            "execute",
                            batch_start_arguments(
                                artifact, entry_operation=entry
                            ),
                            task_id=f"batch-workflow-{entry}",
                            operation_id=f"batch-start-{entry}",
                        )
                        projection = service._test.context_runtime.read_case(final["run_id"])
                        batch_operation = next(
                            item
                            for item in projection["operations"]
                            if item["operation"] == "upgrade_batch"
                        )
                        batch_reference = next(
                            item
                            for item in projection["evidence_refs"]
                            if item["evidence_id"]
                            == batch_operation["evidence_ids"][-1]
                        )
                        first_binding = batch_reference["target_bindings"][0]
                        with self.assertRaisesRegex(Exception, "operation id"):
                            service.call_tool(
                                "evidence_read",
                                {
                                    "case_id": final["run_id"],
                                    "evidence_id": batch_reference["evidence_id"],
                                    "target_id": first_binding["target_id"],
                                    "operation_id": batch_operation["operation_id"],
                                },
                                task_id=f"batch-workflow-{entry}",
                                operation_id=f"batch-parent-binding-read-{entry}",
                            )
                        selected_batch_evidence = service.call_tool(
                            "evidence_read",
                            {
                                "case_id": final["run_id"],
                                "evidence_id": batch_reference["evidence_id"],
                                "target_id": first_binding["target_id"],
                                "target_address": first_binding["target_address"],
                                "expected_product_version": first_binding[
                                    "expected_product_version"
                                ],
                                "observed_product_version": first_binding[
                                    "observed_product_version"
                                ],
                                "operation": "upgrade_batch",
                                "operation_id": first_binding["operation_id"],
                            },
                            task_id=f"batch-workflow-{entry}",
                            operation_id=f"batch-binding-read-{entry}",
                        )
                        evidence_bodies = {
                            reference["evidence_id"]: json.loads(
                                service.call_tool(
                                    "evidence_read",
                                    {
                                        "case_id": final["run_id"],
                                        "evidence_id": reference["evidence_id"],
                                    },
                                    task_id=f"batch-workflow-{entry}",
                                    operation_id=(
                                        f"batch-evidence-read-{entry}-"
                                        f"{index}"
                                    ),
                                )["body"]
                            )
                            for index, reference in enumerate(
                                projection["evidence_refs"], start=1
                            )
                        }
                    finally:
                        service.close()
                    self.assertEqual(
                        [name for name, _ in backend.calls],
                        ["upgrade_batch", "debug_collect", "debug_collect"],
                        final,
                    )
                    self.assertEqual(
                        [args["target_id"] for name, args in backend.calls if name == "debug_collect"],
                        ["bmc-a", "bmc-b"],
                    )
                    self.assertEqual(final["state"], "completed", final)
                    self.assertEqual(final["outcome"]["status"], "completed")
                    self.assertEqual(projection["closeout"]["freshness_status"], "fresh")
                    self.assertEqual(projection["closeout"]["identity_status"], "matched")
                    self.assertEqual(
                        [args["_minimum_target_epoch"] for name, args in backend.calls if name == "debug_collect"],
                        [2, 3],
                    )
                    self.assertEqual(
                        [target["ip"] for target in backend.calls[0][1]["targets"]],
                        ["192.0.2.81", "192.0.2.82"],
                    )
                    self.assertEqual(len(projection["workflow_definition"]["steps"]), 3)
                    self.assertEqual(
                        json.loads(selected_batch_evidence["body"])["total"],
                        2,
                    )
                    self.assertEqual(
                        [
                            {
                                "target_id": binding["target_id"],
                                "target_address": binding["target_address"],
                                "expected_product_version": binding[
                                    "expected_product_version"
                                ],
                                "observed_product_version": binding[
                                    "observed_product_version"
                                ],
                            }
                            for binding in batch_reference["target_bindings"]
                        ],
                        [
                            {
                                "target_id": "bmc-a",
                                "target_address": "192.0.2.81",
                                "expected_product_version": "1.2.3",
                                "observed_product_version": "1.2.3",
                            },
                            {
                                "target_id": "bmc-b",
                                "target_address": "192.0.2.82",
                                "expected_product_version": "1.2.3",
                                "observed_product_version": "1.2.3",
                            },
                        ],
                    )

                    replaced_target = copy.deepcopy(projection)
                    replaced_target["targets"][0]["address"] = "192.0.2.99"
                    closeout = aggregate_case_closeout(
                        replaced_target,
                        lambda reference: evidence_bodies[
                            str(reference["evidence_id"])
                        ],
                    )
                    upgrade_receipts = {
                        receipt.facts.get("target_id"): receipt
                        for receipt in closeout.receipts
                        if receipt.stage == "upgrade"
                    }
                    self.assertEqual(upgrade_receipts["bmc-a"].status, "partial")
                    self.assertEqual(upgrade_receipts["bmc-b"].status, "completed")


if __name__ == "__main__":
    unittest.main()
