from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openubmc_target_runtime import (
    DomainAction,
    DomainReceipt,
    RuntimeMcpService,
    RuntimeSDKContext,
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


class BatchWorkflowTests(unittest.TestCase):
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
                            {
                                "kind": "start",
                                "intent": "upgrade-and-verify",
                                "targets": [
                                    {"target_id": "bmc-a", "ip": "192.0.2.81", "role": "reference"},
                                    {"target_id": "bmc-b", "ip": "192.0.2.82", "role": "candidate"},
                                ],
                                "entry_operation": entry,
                                "entry_arguments": {
                                    "artifact_path": str(artifact),
                                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                    "product_version": "1.2.3",
                                },
                            },
                            task_id=f"batch-workflow-{entry}",
                            operation_id=f"batch-start-{entry}",
                        )
                        projection = service._test.context_runtime.read_case(final["run_id"])
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


if __name__ == "__main__":
    unittest.main()
