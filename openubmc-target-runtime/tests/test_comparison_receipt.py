from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "openubmc-target-runtime"))
spec = importlib.util.spec_from_file_location(
    "receipt_debug_comparison", ROOT / "openubmc-debug/scripts/_comparison.py"
)
comparison = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = comparison
spec.loader.exec_module(comparison)

from openubmc_target_runtime import (
    COMPARISON_RECEIPT_SCHEMA, RuntimeMcpService, build_comparison_receipt,
    comparison_target_identities,
)
from openubmc_target_runtime.diagnostic_receipt import build_diagnostic_receipt

TIME = "2026-09-05T01:00:00+00:00"


def arguments(count=2, *, symmetric=False):
    return {
        "targets": [
            {
                "ip": f"192.0.2.{20 + index}",
                **({} if symmetric else {
                    "role": "reference" if index == 0 else "candidate"
                }),
            }
            for index in range(count)
        ],
        "files": ["/etc/version.json"],
        "mdb_only": True,
    }


def target_result(ip, version="1"):
    return {
        "schema_version": "openubmc-debug.v1",
        "ok": True,
        "ip": ip,
        "observed_at": TIME,
        "request": {"files": ["/etc/version.json"]},
        "result": {
            "completed_at": TIME,
            "freshness": {"status": "fresh"},
            "capabilities": {"remote_log_file": True},
            "lanes": {"telnet": {"files": {"/etc/version.json": {
                "ok": True,
                "observed_at": TIME,
                "result": {"value": version, "content_complete": True},
            }}}},
        },
    }


def compare(args, versions=None):
    observations = [
        comparison.TargetObservation.success(
            role=role, target_id=identity, started_at=TIME, completed_at=TIME,
            result=target_result(target["ip"], versions[index] if versions else "1"),
        )
        for index, (target, (role, identity)) in enumerate(zip(
            args["targets"], comparison_target_identities(args["targets"]), strict=True
        ))
    ]
    if len(observations) == 2:
        return comparison.build_dual_comparison(observations=observations)
    return comparison.build_multi_comparison(observations=observations, scheduler_metrics={})


class ComparisonBackend:
    def open_task(self, task_id):
        return SimpleNamespace(task_id=task_id)

    def close_task(self, _task):
        pass

    def maintain_task(self, _task):
        return 0

    def task_status(self, task):
        return {"task_id": task.task_id}

    def debug_run(self, task, arguments, context):
        context.raise_if_stopped()
        return compare(arguments)


class ComparisonReceiptTests(unittest.TestCase):
    def receipt(self, value, args, complete=True):
        return build_comparison_receipt(
            value, args, ["evidence-comparison"], source_results_complete=complete,
        )

    def test_dual_and_multi_algorithms_bind_same_and_different_facts(self):
        for count, symmetric in ((2, False), (2, True), (3, False), (3, True)):
            args = arguments(count, symmetric=symmetric)
            with self.subTest(count=count, symmetric=symmetric):
                same = self.receipt(compare(args), args)
                self.assertEqual(same.status, "complete")
                self.assertEqual(same.conclusion, "same", same.incomparable_reasons)
                self.assertEqual(len(same.sources), count)
                different = self.receipt(compare(args, ["1"] * (count - 1) + ["2"]), args)
                self.assertEqual(different.conclusion, "different", different.incomparable_reasons)
                self.assertTrue(different.differences)
                for source, target in zip(different.sources, args["targets"]):
                    self.assertEqual(source.address, target["ip"])
                    self.assertEqual(source.evidence_ids, ("evidence-comparison",))
                    self.assertRegex(source.source_digest, r"^sha256:[a-f0-9]{64}$")
                    self.assertRegex(source.scope_digest, r"^sha256:[a-f0-9]{64}$")

    def test_missing_duplicate_unexpected_and_misidentified_sources_are_inconclusive(self):
        args = arguments()
        changes = {
            "candidate:missing": lambda value: value["targets"].pop(),
            "candidate:duplicate": lambda value: value["targets"].append(copy.deepcopy(value["targets"][1])),
            "unexpected_target": lambda value: value["targets"].append({"target_id": "extra"}),
            "candidate:identity_mismatch": lambda value: value["targets"][1]["result"].update(ip="192.0.2.99"),
        }
        for reason, change in changes.items():
            value = compare(args)
            change(value)
            with self.subTest(reason=reason):
                receipt = self.receipt(value, args)
                self.assertEqual(receipt.conclusion, "inconclusive")
                self.assertIn(reason, receipt.incomparable_reasons)
        value = compare(args)
        value["targets"][1]["role"] = "reference"
        self.assertIn("candidate:identity_mismatch", self.receipt(value, args).incomparable_reasons)

    def test_partial_scope_unknown_freshness_and_truncation_never_become_same(self):
        args = arguments()
        changes = (
            lambda result: result.update(ok=False),
            lambda result: result.update(content_complete=False),
            lambda result: result["result"].update(stdout_truncated=True),
            lambda result: result["result"].update(status="partial"),
            lambda result: result.pop("request"),
            lambda result: result["request"].update(files=["/etc/other.json"]),
            lambda result: result["result"].pop("freshness"),
            lambda result: result["result"]["freshness"].update(complete=False),
            lambda result: result["result"]["freshness"].update(status="stale"),
            lambda result: result.update(observed_at="2026-09-05"),
            lambda result: result.update(freshness_boundary={"epoch": 2}),
        )
        for index, change in enumerate(changes):
            value = compare(args)
            change(value["targets"][1]["result"])
            with self.subTest(index=index):
                receipt = self.receipt(value, args)
                self.assertEqual(receipt.conclusion, "inconclusive")
                self.assertTrue(receipt.incomparable_reasons)

    def test_unidentifiable_lists_keep_the_existing_algorithm_incomparability(self):
        args = arguments()
        value = compare(args)
        value["comparison"]["diff_card"]["incomparable_paths"] = ["$.records"]
        receipt = self.receipt(value, args)
        self.assertEqual(receipt.conclusion, "inconclusive")
        self.assertIn("incomparable_path:$.records", receipt.incomparable_reasons)

    def test_malformed_comparison_and_freshness_shapes_fail_closed(self):
        args = arguments()
        changes = (
            lambda value: value["comparison"].update(differences=[None]),
            lambda value: value["comparison"].update(differences="[]"),
            lambda value: value["comparison"].update(candidate_comparisons={}),
            lambda value: value["comparison"].update(value_groups="unknown"),
            lambda value: value["comparison"].update(status=[]),
            lambda value: value["comparison"].update(diff_card="complete"),
            lambda value: value["comparison"]["diff_card"].update(conclusion=[]),
            lambda value: value["targets"][1].update(status=[]),
            lambda value: value["targets"][1]["result"].update(ok=1),
            lambda value: value["targets"][1]["result"].update(content_complete="true"),
            lambda value: value["targets"][1]["result"]["result"]["freshness"].update(complete="true"),
        )
        for index, change in enumerate(changes):
            value = compare(args)
            change(value)
            with self.subTest(index=index):
                receipt = self.receipt(value, args)
                self.assertEqual(receipt.conclusion, "inconclusive")
                self.assertTrue(receipt.incomparable_reasons)

    def test_receipt_identity_tracks_source_bytes_and_bound_evidence(self):
        args = arguments()
        value = compare(args)
        receipt = self.receipt(value, args)
        self.assertEqual(receipt.receipt_id, self.receipt(copy.deepcopy(value), args).receipt_id)
        value["targets"][0]["result"]["observed_at"] = "2026-09-05T01:00:01+00:00"
        changed = self.receipt(value, args)
        self.assertNotEqual(receipt.receipt_id, changed.receipt_id)
        self.assertNotEqual(receipt.sources[0].source_digest, changed.sources[0].source_digest)
        missing = build_comparison_receipt(value, args, [], source_results_complete=True)
        self.assertEqual(missing.conclusion, "inconclusive")
        self.assertIn("source_evidence_missing", missing.incomparable_reasons)

    def test_public_diagnostic_receipt_carries_typed_comparison_and_missing_fact_gap(self):
        args = arguments()
        for missing in (False, True):
            value = compare(args)
            if missing:
                value["targets"][1]["result"]["result"]["lanes"] = {}
            with self.subTest(missing=missing):
                receipt = build_diagnostic_receipt(
                    "debug_run", value, args, [{"evidence_id": "evidence-comparison"}],
                    closeout_stage="diagnosis",
                ).to_public_dict()
                result = next(item for item in receipt["results"] if item["result_id"] == "comparison")
                self.assertEqual(result["value"]["schema"], COMPARISON_RECEIPT_SCHEMA)
                self.assertEqual(result["value"]["conclusion"], "inconclusive" if missing else "same")
                self.assertEqual(result["status"], "unavailable" if missing else "available")
                self.assertEqual(receipt["status"], "partial" if missing else "complete")

    def test_execute_projection_retains_comparison_target_and_evidence_binding(self):
        service = RuntimeMcpService(ComparisonBackend())
        try:
            args = arguments()
            turn = service.call_exposed_tool(
                "execute", {
                    "kind": "start", "targets": args["targets"],
                    "intent": "diagnosis-only", "entry_operation": "debug_run",
                    "entry_arguments": {"files": args["files"], "mdb_only": True},
                }, task_id="comparison-receipt", operation_id="compare",
            )
        finally:
            service.close()
        receipt = turn["diagnostic_receipt"]
        result = next(item for item in receipt["results"] if item["result_id"] == "comparison")
        typed = result["value"]
        self.assertEqual(typed["schema"], COMPARISON_RECEIPT_SCHEMA)
        self.assertEqual(typed["conclusion"], "same")
        self.assertEqual(typed["sources"][0]["address"], "192.0.2.20")
        self.assertTrue(typed["sources"][0]["evidence_ids"])
        self.assertEqual(receipt["coverage"]["evaluable"], 3)
        self.assertLess(len(json.dumps(turn).encode()), 32 * 1024)


if __name__ == "__main__":
    unittest.main()
