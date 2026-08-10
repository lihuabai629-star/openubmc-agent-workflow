from __future__ import annotations

import importlib.util
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
        f"openubmc_debug_multi_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def result_for(ip: str, value: object, *, ok: bool = True) -> dict[str, object]:
    return {
        "schema_version": "openubmc-debug.v1",
        "tool": "workflow_remote",
        "ip": ip,
        "observed_at": "2026-08-01T10:00:00+00:00",
        "ok": ok,
        "code": "ok" if ok else "target_failed",
        "normalized_code": "ok" if ok else "target_failed",
        "returncode": 0 if ok else 1,
        "warnings": [],
        "error": "" if ok else "target failure",
        "request": {},
        "result": {
            "capabilities": {"object_alarm": True},
            "sample": {"state": value},
        },
    }


class MultiTargetComparisonTests(unittest.TestCase):
    def test_reference_is_compared_to_each_candidate_without_candidate_pairs(self) -> None:
        comparison = load_script("_comparison")
        values = {
            "reference": "healthy",
            "candidate-a": "healthy",
            "candidate-b": "degraded",
            "candidate-c": "offline",
        }

        payload = comparison.run_multi_target_comparison(
            targets=[
                {"ip": "reference", "role": "reference", "target_id": "ref"},
                {"ip": "candidate-a", "role": "candidate", "target_id": "a"},
                {"ip": "candidate-b", "role": "candidate", "target_id": "b"},
                {"ip": "candidate-c", "role": "candidate", "target_id": "c"},
            ],
            run_target=lambda target, _context: result_for(
                str(target["ip"]), values[str(target["ip"])]
            ),
            context=None,
            concurrency=2,
        )

        candidate_comparisons = payload["comparison"]["candidate_comparisons"]
        self.assertEqual(
            [item["candidate_target_id"] for item in candidate_comparisons],
            ["a", "b", "c"],
        )
        self.assertTrue(
            all(item["reference_target_id"] == "ref" for item in candidate_comparisons)
        )
        self.assertEqual(payload["comparison"]["value_groups"], [])
        self.assertEqual(payload["scheduler"]["actual_concurrency_budget"], 2)
        self.assertEqual(payload["completeness"]["completed"], 4)

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(payload)

    def test_symmetric_targets_are_grouped_by_value_instead_of_all_pair_diff(self) -> None:
        comparison = load_script("_comparison")
        states = ["same", "same", "different", "same"]
        payload = comparison.run_multi_target_comparison(
            targets=[
                {"ip": f"target-{index}", "target_id": f"t{index}"}
                for index in range(4)
            ],
            run_target=lambda target, _context: result_for(
                str(target["ip"]), states[int(str(target["ip"]).rsplit("-", 1)[1])]
            ),
            context=None,
            concurrency="auto",
        )

        self.assertEqual(payload["mode"], "symmetric")
        self.assertEqual(payload["comparison"]["candidate_comparisons"], [])
        state_group = next(
            group
            for group in payload["comparison"]["value_groups"]
            if group["path"] == "$.result.sample.state"
        )
        self.assertEqual(len(state_group["groups"]), 2)
        self.assertEqual(
            sorted(len(group["target_ids"]) for group in state_group["groups"]),
            [1, 3],
        )

    def test_failure_timeout_and_queue_metadata_are_partial_not_global_abort(self) -> None:
        comparison = load_script("_comparison")

        def run_target(target, _context):
            ip = str(target["ip"])
            if ip == "bad":
                raise TimeoutError("bounded target timeout")
            time.sleep(0.01)
            return result_for(ip, "ok")

        payload = comparison.run_multi_target_comparison(
            targets=[
                {"ip": "good-a", "target_id": "a"},
                {"ip": "bad", "target_id": "bad"},
                {"ip": "good-b", "target_id": "b"},
            ],
            run_target=run_target,
            context=None,
            concurrency=1,
        )

        self.assertEqual(payload["code"], "partial_comparison")
        self.assertEqual(payload["completeness"], {"requested": 3, "completed": 2, "failed": 1})
        self.assertEqual(
            payload["comparison"]["target_local_failures"][0]["target_id"],
            "bad",
        )
        self.assertTrue(
            all("queue_delay_ms" in target for target in payload["targets"])
        )
        self.assertIn("target_duration_p75_ms", payload["scheduler"])

    def test_invalid_target_schema_is_reported_as_a_partial_comparison(self) -> None:
        comparison = load_script("_comparison")

        def run_target(target, _context):
            result = result_for(str(target["ip"]), "ok")
            if target["ip"] == "bad-schema":
                result["schema_version"] = "unexpected-debug-schema"
            return result

        payload = comparison.run_multi_target_comparison(
            targets=[
                {"ip": "good-a", "target_id": "a"},
                {"ip": "bad-schema", "target_id": "bad"},
                {"ip": "good-b", "target_id": "b"},
            ],
            run_target=run_target,
            context=None,
            concurrency=2,
        )

        self.assertEqual(payload["code"], "partial_comparison")
        self.assertEqual(
            payload["completeness"],
            {"requested": 3, "completed": 2, "failed": 1},
        )
        self.assertEqual(
            payload["comparison"]["target_local_failures"][0]["target_id"],
            "bad",
        )
        self.assertEqual(
            payload["comparison"]["target_local_failures"][0]["code"],
            "ValueError",
        )

    def test_mcp_accepts_more_than_two_targets_and_reports_actual_budget(self) -> None:
        module = load_script("target_runtime_mcp")
        backend = module.DebugMcpBackend()
        expected = {
            "schema_version": "openubmc-debug.compare.v1",
            "scheduler": {"actual_concurrency_budget": 3},
        }
        with mock.patch.object(
            module,
            "run_multi_target_comparison",
            return_value=expected,
        ) as run_multi:
            payload = backend.debug_run(
                mock.sentinel.task,
                {
                    "targets": [
                        {"ip": f"target-{index}"} for index in range(5)
                    ],
                    "concurrency": 3,
                    "deadline": 10,
                },
                mock.Mock(
                    remaining=mock.Mock(return_value=10),
                    raise_if_stopped=mock.Mock(),
                ),
            )

        self.assertEqual(payload, expected)
        self.assertEqual(run_multi.call_args.kwargs["concurrency"], 3)
        self.assertEqual(len(run_multi.call_args.kwargs["targets"]), 5)


if __name__ == "__main__":
    unittest.main()
