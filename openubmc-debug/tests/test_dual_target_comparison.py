from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import jsonschema


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
SCHEMA_PATH = SKILL_ROOT / "references" / "openubmc-debug-compare-v1.schema.json"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"openubmc_debug_compare_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def single_result(ip: str, *, ok: bool = True) -> dict[str, object]:
    return {
        "schema_version": "openubmc-debug.v1",
        "tool": "workflow_remote",
        "ip": ip,
        "observed_at": "2026-08-01T10:00:00+00:00",
        "ok": ok,
        "code": "ok" if ok else "workflow_partial_failure",
        "normalized_code": "ok" if ok else "workflow_partial_failure",
        "returncode": 0 if ok else 1,
        "warnings": [],
        "error": "" if ok else "target-local failure",
        "request": {},
        "result": {
            "started_at": "2026-08-01T10:00:00+00:00",
            "completed_at": "2026-08-01T10:00:01+00:00",
            "capabilities": {
                "object_alarm": True,
                "remote_log_file": ip.endswith("a"),
                "optional_probe": None,
            },
            "identity": {"firmware": "v1" if ip.endswith("a") else "v2"},
            "only_a": "present" if ip.endswith("a") else None,
        },
    }


class ComparisonContractTests(unittest.TestCase):
    def test_reference_candidate_result_preserves_raw_evidence_and_normalizes_diff_only(self) -> None:
        comparison = load_script("_comparison")
        reference = single_result("target-a")
        candidate = single_result("target-b")
        candidate["observed_at"] = "2026-08-01T10:05:00+00:00"
        reference["result"]["runtime"] = {"ssh_authentications": 1}
        candidate["result"]["runtime"] = {"ssh_authentications": 2}
        reference["result"]["command"] = ["probe", "target-a"]
        candidate["result"]["command"] = ["probe", "target-b"]
        original_reference = copy.deepcopy(reference)
        original_candidate = copy.deepcopy(candidate)

        payload = comparison.build_dual_comparison(
            observations=[
                comparison.TargetObservation.success(
                    role="reference",
                    target_id="reference",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=reference,
                ),
                comparison.TargetObservation.success(
                    role="candidate",
                    target_id="candidate",
                    started_at="2026-08-01T10:00:00.020000+00:00",
                    completed_at="2026-08-01T10:00:01.100000+00:00",
                    result=candidate,
                ),
            ]
        )

        self.assertEqual(payload["schema_version"], "openubmc-debug.compare.v1")
        self.assertEqual(payload["mode"], "reference-candidate")
        self.assertEqual(payload["targets"][0]["result"], original_reference)
        self.assertEqual(payload["targets"][1]["result"], original_candidate)
        self.assertEqual(reference, original_reference)
        self.assertEqual(candidate, original_candidate)
        difference_paths = {
            item["path"] for item in payload["comparison"]["differences"]
        }
        self.assertNotIn("$.observed_at", difference_paths)
        self.assertNotIn("$.result.command", difference_paths)
        self.assertNotIn("$.result.runtime.ssh_authentications", difference_paths)
        self.assertIn("$.result.identity.firmware", difference_paths)
        self.assertIn("$.result.only_a", difference_paths)
        self.assertIn(
            "$.result.capabilities.remote_log_file",
            payload["comparison"]["capability_asymmetry"],
        )
        self.assertIn(
            "$.result.capabilities.optional_probe",
            payload["comparison"]["unsupported"]["reference"],
        )
        self.assertLess(payload["observation"]["start_skew_ms"], 25)
        self.assertEqual(
            payload["scheduler"]["target_duration_p75_ms"],
            1060.0,
        )

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)

    def test_symmetric_mode_does_not_invent_reference_role(self) -> None:
        comparison = load_script("_comparison")
        payload = comparison.build_dual_comparison(
            observations=[
                comparison.TargetObservation.success(
                    role="target-a",
                    target_id="a",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=single_result("target-a"),
                ),
                comparison.TargetObservation.success(
                    role="target-b",
                    target_id="b",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=single_result("target-b"),
                ),
            ]
        )

        self.assertEqual(payload["mode"], "symmetric")
        self.assertEqual(
            [target["role"] for target in payload["targets"]],
            ["target-a", "target-b"],
        )

    def test_semantic_diff_matches_identified_lists_by_identity_not_position(self) -> None:
        comparison = load_script("_comparison")
        reference = single_result("target-a")
        candidate = single_result("target-b")
        reference["result"]["objects"] = [
            {"object_path": "/a", "state": "ok"},
            {"object_path": "/b", "state": "failed"},
        ]
        candidate["result"]["objects"] = [
            {"object_path": "/b", "state": "failed"},
            {"object_path": "/a", "state": "ok"},
        ]

        payload = comparison.build_dual_comparison(
            observations=[
                comparison.TargetObservation.success(
                    role="reference",
                    target_id="reference",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=reference,
                ),
                comparison.TargetObservation.success(
                    role="candidate",
                    target_id="candidate",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=candidate,
                ),
            ]
        )

        paths = {
            item["path"] for item in payload["comparison"]["differences"]
        }
        self.assertFalse(any("objects" in path for path in paths))
        self.assertNotIn(
            "$.result.objects",
            payload["comparison"]["diff_card"]["incomparable_paths"],
        )

    def test_unidentified_or_stale_list_is_inconclusive_not_same_or_missing(self) -> None:
        comparison = load_script("_comparison")
        reference = single_result("target-a")
        candidate = single_result("target-b")
        reference["result"]["samples"] = [{"value": 1}, {"value": 2}]
        candidate["result"]["samples"] = [{"value": 1}, {"value": 3}]
        candidate["warnings"] = ["stale_evidence"]

        payload = comparison.build_dual_comparison(
            observations=[
                comparison.TargetObservation.success(
                    role="reference",
                    target_id="reference",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=reference,
                ),
                comparison.TargetObservation.success(
                    role="candidate",
                    target_id="candidate",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=candidate,
                ),
            ]
        )

        card = payload["comparison"]["diff_card"]
        self.assertEqual(card["status"], "partial")
        self.assertEqual(card["conclusion"], "inconclusive")
        self.assertIn("stale", card["quality_statuses"])
        self.assertEqual(card["comparability"], "inconclusive")
        self.assertIn("$.result.samples", card["incomparable_paths"])
        self.assertIn("stale", card["quality_flags"]["candidate"])
        self.assertFalse(
            any(
                item["kind"].startswith("only-on")
                and "samples" in item["path"]
                for item in payload["comparison"]["differences"]
            )
        )

    def test_one_target_failure_returns_partial_comparison_and_other_evidence(self) -> None:
        comparison = load_script("_comparison")
        payload = comparison.build_dual_comparison(
            observations=[
                comparison.TargetObservation.success(
                    role="reference",
                    target_id="reference",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=single_result("target-a"),
                ),
                comparison.TargetObservation.failure(
                    role="candidate",
                    target_id="candidate",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    error_code="target_timeout",
                    error="bounded target timeout",
                ),
            ]
        )

        self.assertEqual(payload["code"], "partial_comparison")
        self.assertEqual(payload["comparison"]["status"], "partial")
        self.assertEqual(payload["targets"][0]["result"]["schema_version"], "openubmc-debug.v1")
        self.assertIsNone(payload["targets"][1]["result"])
        self.assertEqual(
            payload["comparison"]["target_local_failures"][0]["code"],
            "target_timeout",
        )

    def test_compact_comparison_hashes_large_values_without_dropping_targets(
        self,
    ) -> None:
        comparison = load_script("_comparison")
        reference = single_result("target-a")
        candidate = single_result("target-b")
        reference["result"]["large"] = "a" * 1024
        candidate["result"]["large"] = "b" * 1024
        payload = comparison.build_dual_comparison(
            observations=[
                comparison.TargetObservation.success(
                    role="target-a",
                    target_id="a",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=reference,
                ),
                comparison.TargetObservation.success(
                    role="target-b",
                    target_id="b",
                    started_at="2026-08-01T10:00:00+00:00",
                    completed_at="2026-08-01T10:00:01+00:00",
                    result=candidate,
                ),
            ]
        )

        compacted = comparison.compact_comparison_values(payload)

        self.assertEqual(
            compacted["targets"][0]["result"]["result"]["large"],
            "a" * 1024,
        )
        large_diff = next(
            item
            for item in compacted["comparison"]["differences"]
            if item["path"] == "$.result.large"
        )
        self.assertTrue(large_diff["left"]["compacted"])
        self.assertEqual(large_diff["left"]["size_bytes"], 1026)
        self.assertEqual(len(large_diff["left"]["sha256"]), 64)
        self.assertIn("comparison_values_compacted", compacted["warnings"])
        original_large_diff = next(
            item
            for item in payload["comparison"]["differences"]
            if item["path"] == "$.result.large"
        )
        self.assertEqual(original_large_diff["left"], "a" * 1024)

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(compacted)


class DualRunnerTests(unittest.TestCase):
    def test_two_targets_start_together_and_slow_target_does_not_hide_fast_result(self) -> None:
        comparison = load_script("_comparison")

        def run_target(request, _context):
            if request["ip"] == "target-a":
                time.sleep(0.15)
            else:
                time.sleep(0.01)
            return single_result(str(request["ip"]))

        payload = comparison.run_dual_target_comparison(
            targets=[
                {"ip": "target-a", "role": "reference"},
                {"ip": "target-b", "role": "candidate"},
            ],
            run_target=run_target,
            context=None,
        )

        self.assertEqual(payload["comparison"]["status"], "complete")
        self.assertEqual(payload["mode"], "reference-candidate")
        self.assertEqual(
            [target["role"] for target in payload["targets"]],
            ["reference", "candidate"],
        )
        self.assertLess(payload["observation"]["start_skew_ms"], 50)
        self.assertEqual(
            {target["status"] for target in payload["targets"]}, {"ok"}
        )

    def test_dual_comparison_rejects_mixed_or_unknown_roles(self) -> None:
        comparison = load_script("_comparison")
        invalid_roles = [
            (None, "symmetric"),
            ("reference", "symmetric"),
            ("left", "right"),
        ]

        for left_role, right_role in invalid_roles:
            with self.subTest(roles=(left_role, right_role)):
                targets = [{"ip": "target-a"}, {"ip": "target-b"}]
                if left_role is not None:
                    targets[0]["role"] = left_role
                if right_role is not None:
                    targets[1]["role"] = right_role
                with self.assertRaisesRegex(
                    ValueError,
                    "target roles must both be omitted, both be symmetric, or be reference and candidate",
                ):
                    comparison.run_dual_target_comparison(
                        targets=targets,
                        run_target=mock.Mock(),
                        context=None,
                    )

    def test_dual_comparison_uses_general_scheduler_concurrency_and_queue_metadata(
        self,
    ) -> None:
        comparison = load_script("_comparison")

        def run_target(request, _context):
            time.sleep(0.02)
            return single_result(str(request["ip"]))

        expected_budgets = {
            "auto": 2,
            "unbounded": 2,
            1: 1,
        }
        for concurrency, expected_budget in expected_budgets.items():
            with self.subTest(concurrency=concurrency):
                payload = comparison.run_dual_target_comparison(
                    targets=[
                        {"ip": "target-a", "role": "reference"},
                        {"ip": "target-b", "role": "candidate"},
                    ],
                    run_target=run_target,
                    context=None,
                    concurrency=concurrency,
                )

                self.assertEqual(
                    payload["scheduler"]["actual_concurrency_budget"],
                    expected_budget,
                )
                self.assertEqual(
                    payload["scheduler"]["requested_policy"],
                    str(concurrency),
                )
                self.assertEqual(
                    [target["role"] for target in payload["targets"]],
                    ["reference", "candidate"],
                )
                self.assertTrue(
                    all(
                        "queue_delay_ms" in target
                        for target in payload["targets"]
                    )
                )
                if concurrency == 1:
                    self.assertGreater(
                        payload["targets"][1]["queue_delay_ms"],
                        payload["targets"][0]["queue_delay_ms"],
                    )

    def test_dual_completion_order_uses_sorted_output_target_indexes(self) -> None:
        comparison = load_script("_comparison")

        def run_target(request, _context):
            if request["ip"] == "reference":
                time.sleep(0.04)
            else:
                time.sleep(0.005)
            return single_result(str(request["ip"]))

        payload = comparison.run_dual_target_comparison(
            targets=[
                {
                    "ip": "candidate",
                    "role": "candidate",
                    "target_id": "candidate-id",
                },
                {
                    "ip": "reference",
                    "role": "reference",
                    "target_id": "reference-id",
                },
            ],
            run_target=run_target,
            context=None,
            concurrency="unbounded",
        )

        self.assertEqual(
            [target["target_id"] for target in payload["targets"]],
            ["reference-id", "candidate-id"],
        )
        self.assertEqual(payload["scheduler"]["completion_order"], [1, 0])

    def test_invalid_target_schema_is_reported_as_a_partial_comparison(self) -> None:
        comparison = load_script("_comparison")

        def run_target(request, _context):
            result = single_result(str(request["ip"]))
            if request["ip"] == "candidate":
                result["schema_version"] = "unexpected-debug-schema"
            return result

        payload = comparison.run_dual_target_comparison(
            targets=[
                {"ip": "reference", "role": "reference"},
                {"ip": "candidate", "role": "candidate"},
            ],
            run_target=run_target,
            context=None,
        )

        self.assertEqual(payload["code"], "partial_comparison")
        self.assertEqual(
            payload["completeness"],
            {"requested": 2, "completed": 1, "failed": 1},
        )
        self.assertEqual(
            payload["comparison"]["target_local_failures"][0]["target_id"],
            "candidate",
        )
        self.assertEqual(
            payload["comparison"]["target_local_failures"][0]["code"],
            "ValueError",
        )


class ConcurrentCaptureTests(unittest.TestCase):
    def test_in_process_child_json_capture_is_thread_isolated(self) -> None:
        runtime = load_script("_workflow_runtime")

        def invoke(ip: str):
            command = [sys.executable, "/tmp/probe.py", "--ip", ip]

            def child(_executed):
                time.sleep(0.01 if ip.endswith("a") else 0.02)
                print(
                    json.dumps(
                        {
                            "schema_version": "openubmc-debug.v1",
                            "tool": "probe",
                            "ip": ip,
                            "observed_at": "2026-08-01T10:00:00+00:00",
                            "ok": True,
                            "code": "ok",
                            "normalized_code": "ok",
                            "returncode": 0,
                            "warnings": [],
                            "error": "",
                            "request": {},
                            "result": {},
                        }
                    )
                )
                return 0

            return runtime.run_python_json_tool(
                "probe", command, child, 1
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(invoke, ["target-a", "target-b"]))

        self.assertEqual(
            [result["payload"]["ip"] for result in results],
            ["target-a", "target-b"],
        )
        self.assertTrue(all(result["ok"] for result in results), results)


class CompareCliAndMcpTests(unittest.TestCase):
    def test_cli_delegates_explicit_roles_to_context_runtime_adapter(self) -> None:
        cli = load_script("compare_remote")
        delegated = mock.Mock(return_value=0)
        argv = [
            "--reference-ip",
            "target-a",
            "--candidate-ip",
            "target-b",
            "--case-id",
            "case-existing",
            "--json",
        ]
        with mock.patch.dict(
            sys.modules,
            {
                "target_runtime_cli": mock.Mock(
                    run_compare_legacy=delegated,
                )
            },
        ):
            returncode = cli.main(
                argv
            )

        self.assertEqual(returncode, 0)
        delegated.assert_called_once_with(argv)

    def test_cli_forwards_workflow_bounds_and_compact_output(self) -> None:
        cli = load_script("compare_remote")
        args = cli.parse_args(
            [
                "--target",
                "target-a",
                "--target",
                "target-b",
                "--mdb-query",
                "lsobj BusinessConnector",
                "--mdb-query",
                "lsobj PcieAddrInfo",
                "--mdb-expand-class",
                "PCIeDevice",
                "--mdb-concurrency",
                "3",
                "--mdb-only",
                "--include-rotated",
                "--rotated-limit",
                "5",
                "--log-max-bytes",
                "131072",
                "--tree-service",
                "bmc.example",
                "--tree-head",
                "12",
                "--alarm-service",
                "bmc.kepler.event",
                "--alarm-path",
                "/bmc/kepler/Systems/1/Events",
                "--alarm-limit",
                "25",
                "--source-max-matches",
                "9",
                "--correlate-alarm-limit",
                "7",
                "--correlation-time-window",
                "60",
                "--compact-json",
            ]
        )

        request = cli.build_request(args)

        self.assertTrue(request["include_rotated"])
        self.assertEqual(
            request["mdb_queries"],
            ["lsobj BusinessConnector", "lsobj PcieAddrInfo"],
        )
        self.assertEqual(request["mdb_expand_classes"], ["PCIeDevice"])
        self.assertEqual(request["mdb_concurrency"], "3")
        self.assertTrue(request["mdb_only"])
        self.assertEqual(request["rotated_limit"], 5)
        self.assertEqual(request["log_max_bytes"], 131072)
        self.assertEqual(request["tree_service"], "bmc.example")
        self.assertEqual(request["tree_head"], 12)
        self.assertEqual(request["alarm_service"], "bmc.kepler.event")
        self.assertEqual(
            request["alarm_path"],
            "/bmc/kepler/Systems/1/Events",
        )
        self.assertEqual(request["alarm_limit"], 25)
        self.assertEqual(request["source_max_matches"], 9)
        self.assertEqual(request["correlate_alarm_limit"], 7)
        self.assertEqual(request["correlation_time_window"], 60)
        self.assertTrue(request["compact_json"])

    def test_mcp_debug_run_accepts_two_targets_without_new_generic_tool(self) -> None:
        module = load_script("target_runtime_mcp")
        backend = module.DebugMcpBackend()
        comparison_payload = {
            "schema_version": "openubmc-debug.compare.v1",
            "ok": True,
            "code": "ok",
            "returncode": 0,
        }
        with mock.patch.object(
            module,
            "run_dual_target_comparison",
            return_value=comparison_payload,
        ) as compare:
            result = backend.debug_run(
                mock.sentinel.task,
                {
                    "targets": [
                        {"ip": "target-a", "role": "reference"},
                        {"ip": "target-b", "role": "candidate"},
                    ],
                    "deadline": 10,
                    "concurrency": 1,
                },
                mock.Mock(
                    remaining=mock.Mock(return_value=10),
                    raise_if_stopped=mock.Mock(),
                ),
            )

        self.assertEqual(result["schema_version"], "openubmc-debug.compare.v1")
        self.assertEqual(len(compare.call_args.kwargs["targets"]), 2)
        self.assertEqual(compare.call_args.kwargs["concurrency"], 1)

    def test_mcp_compacts_comparison_values_when_child_json_is_compact(self) -> None:
        module = load_script("target_runtime_mcp")
        backend = module.DebugMcpBackend()
        comparison_payload = {
            "schema_version": "openubmc-debug.compare.v1",
            "ok": True,
            "code": "ok",
            "returncode": 0,
        }
        compact_payload = {**comparison_payload, "warnings": ["compacted"]}
        with (
            mock.patch.object(
                module,
                "run_dual_target_comparison",
                return_value=comparison_payload,
            ),
            mock.patch.object(
                module,
                "compact_comparison_values",
                return_value=compact_payload,
            ) as compact,
        ):
            result = backend.debug_run(
                mock.sentinel.task,
                {
                    "targets": [
                        {"ip": "target-a"},
                        {"ip": "target-b"},
                    ],
                    "deadline": 10,
                    "compact_json": True,
                },
                mock.Mock(
                    remaining=mock.Mock(return_value=10),
                    raise_if_stopped=mock.Mock(),
                ),
            )

        self.assertEqual(result, compact_payload)
        compact.assert_called_once_with(comparison_payload)


if __name__ == "__main__":
    unittest.main()
