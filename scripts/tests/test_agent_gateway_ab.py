from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "agent_gateway_ab.py"
SPEC = importlib.util.spec_from_file_location("agent_gateway_ab", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


EXPECTED_QUERIES = [
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Name",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Protocol",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive ResourceId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive SlotNumber",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive Presence",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive TemperatureCelsius",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo Type",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.AddrInfo SocketId",
    "getprop Drive_1_010102 bmc.kepler.Systems.Storage.Drive.DriveStatus Health",
]


def candidate_observe_event(*, queries=None, complete: bool = True):
    selected_queries = EXPECTED_QUERIES if queries is None else queries
    receipt_id = "observation-test"
    return {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "openubmc-target-runtime",
            "tool": "observe",
            "arguments": {
                "target": "10.121.136.200",
                "assurance": "auto",
                "freshness": {"mode": "live", "max_age_seconds": 0},
                "selectors": [
                    {
                        "id": "capabilities",
                        "kind": "capability",
                        "names": ["SSH", "Telnet", "MDBCTL", "BUSCTL"],
                    },
                    {"id": "drive", "kind": "mdb", "queries": selected_queries},
                ],
            },
            "result": {
                "structured_content": {
                    "receipt_id": receipt_id,
                    "status": "complete" if complete else "incomplete",
                    "coverage": {
                        "requested": 13,
                        "available": 13 if complete else 12,
                        "unavailable": 0,
                        "not_checked": 0 if complete else 1,
                        "complete": complete,
                    },
                    "results": {
                        "capabilities": {
                            "kind": "capability",
                            "values": [
                                {"name": name, "status": "available"}
                                for name in ("ssh", "telnet", "mdbctl", "busctl")
                            ],
                        },
                        "drive": {
                            "kind": "mdb",
                            "values": [
                                {"query_index": index, "status": "available", "value": index}
                                for index in range(9)
                            ],
                        },
                    },
                    "claims": [
                        {
                            "selector_id": selector_id,
                            "status": "grounded",
                            "receipt_id": receipt_id,
                        }
                        for selector_id in ("capabilities", "drive")
                    ],
                }
            },
        },
    }


class AgentGatewayAbTests(unittest.TestCase):
    def test_prepare_arm_home_links_selected_skill_for_supported_clients(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_root = root / "source"
            skill_source = source_root / "openubmc-debug"
            skill_source.mkdir(parents=True)
            home = root / "home"
            home.mkdir()

            module.prepare_arm_home(home, source_root)

            for client_root in (".agents", ".codex"):
                link = home / client_root / "skills" / "openubmc-debug"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), skill_source.resolve())

    def test_schedule_is_balanced_and_deterministic(self) -> None:
        first = module.balanced_schedule(10, seed=7)
        second = module.balanced_schedule(10, seed=7)
        self.assertEqual(first, second)
        orders = [(a, b) for _pair, a, b in first]
        self.assertEqual(orders.count(("A", "B")), 5)
        self.assertEqual(orders.count(("B", "A")), 5)

    def test_semantic_acceptance_requires_fields_and_cautious_conclusion(self) -> None:
        text = (
            "SSH Telnet MDBCTL BUSCTL；Name Disk0，Protocol 3，ResourceId 0，"
            "SlotNumber 0，Presence 1，TemperatureCelsius 29，Type SATA/SAS，"
            "SocketId 0，Health 0。不能单独证明 ResourceId=0 异常。"
        )
        self.assertTrue(module.semantic_acceptance(text)["passed"])
        self.assertFalse(module.semantic_acceptance(text.replace("Health", ""))["passed"])

    def test_metric_parser_requires_candidate_to_use_one_observe(self) -> None:
        events = [
            candidate_observe_event(),
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                },
            },
        ]
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            events_path = root / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_path = root / "final.md"
            final_path.write_text(final, encoding="utf-8")
            metric = module.metric_from_run(
                arm="B",
                pair=1,
                order=1,
                events_path=events_path,
                final_path=final_path,
                exit_code=0,
                duration_seconds=2,
            )
        self.assertTrue(metric["valid"])
        self.assertEqual(metric["noncached_input_plus_output"], 90)

    def test_metric_parser_rejects_wrong_scope_or_incomplete_receipt(self) -> None:
        final = (
            "SSH Telnet MDBCTL BUSCTL Name Protocol ResourceId SlotNumber Presence "
            "TemperatureCelsius Type SocketId Health，不能证明 ResourceId 异常。"
        )
        cases = (
            candidate_observe_event(queries=EXPECTED_QUERIES[:-1]),
            candidate_observe_event(complete=False),
        )
        for index, event in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                events_path = root / "events.jsonl"
                events_path.write_text(
                    "\n".join(
                        json.dumps(item)
                        for item in (
                            event,
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 20,
                                    "output_tokens": 10,
                                },
                            },
                        )
                    )
                    + "\n",
                    encoding="utf-8",
                )
                final_path = root / "final.md"
                final_path.write_text(final, encoding="utf-8")
                metric = module.metric_from_run(
                    arm="B",
                    pair=1,
                    order=1,
                    events_path=events_path,
                    final_path=final_path,
                    exit_code=0,
                    duration_seconds=2,
                )
            self.assertFalse(metric["valid"])
            self.assertFalse(metric["scope_acceptance"])

    def test_analyzer_passes_ten_good_pairs_and_expands_uncertain_result(self) -> None:
        passing = []
        for pair in range(1, 11):
            passing.extend(
                (
                    {
                        "arm": "A",
                        "pair": pair,
                        "valid": True,
                        "total_tokens": 100,
                        "noncached_input_plus_output": 100,
                        "duration_seconds": 100,
                    },
                    {
                        "arm": "B",
                        "pair": pair,
                        "valid": True,
                        "total_tokens": 105,
                        "noncached_input_plus_output": 105,
                        "duration_seconds": 105,
                    },
                )
            )
        self.assertEqual(module.analyze(passing)["decision"], "passed")

        uncertain = [dict(item) for item in passing]
        for item in uncertain:
            if item["arm"] == "B":
                item["duration_seconds"] = 130
        result = module.analyze(uncertain)
        self.assertEqual(result["decision"], "collect_more")
        self.assertEqual(result["next_pair_target"], 20)


if __name__ == "__main__":
    unittest.main()
