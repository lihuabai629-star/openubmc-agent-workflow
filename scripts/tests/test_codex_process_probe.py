from __future__ import annotations

import unittest

from scripts.codex_process_probe import (
    _orchestrator_tool_contracts,
    _runtime_tool_contracts,
    _tool_names,
)


class CodexProcessProbeTests(unittest.TestCase):
    def test_captures_orchestrator_tools_from_responses_additional_tools(self) -> None:
        requests = [
            {
                "model": "gpt-5.6-sol",
                "input": [
                    {
                        "type": "additional_tools",
                        "tools": [
                            {
                                "type": "namespace",
                                "name": "functions",
                                "tools": [
                                    {"name": "exec"},
                                    {"name": "wait"},
                                ],
                            },
                            {"type": "namespace", "name": "collaboration"},
                        ],
                    }
                ],
            }
        ]

        self.assertEqual(
            _tool_names(requests),
            ["collaboration", "functions"],
        )
        self.assertEqual(_runtime_tool_contracts(requests), [])
        self.assertEqual(
            _orchestrator_tool_contracts(requests),
            [
                {
                    "name": "functions",
                    "type": "namespace",
                    "tools": [
                        {"name": "exec"},
                        {"name": "wait"},
                    ],
                }
            ],
        )

    def test_preserves_legacy_top_level_runtime_tool_contract(self) -> None:
        requests = [
            {
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__openubmc_target_runtime",
                        "tools": [
                            {"name": "execute"},
                            {"name": "observe"},
                        ],
                    }
                ]
            }
        ]

        self.assertEqual(
            _runtime_tool_contracts(requests),
            [
                {
                    "name": "mcp__openubmc_target_runtime",
                    "type": "namespace",
                    "tools": [
                        {"name": "execute"},
                        {"name": "observe"},
                    ],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
