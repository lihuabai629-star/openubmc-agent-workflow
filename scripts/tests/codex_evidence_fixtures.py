from __future__ import annotations


def direct_runtime_route_evidence() -> dict[str, object]:
    return {
        "captured_model_tools": ["mcp__openubmc_target_runtime"],
        "captured_runtime_tool_contracts": [
            {
                "name": "mcp__openubmc_target_runtime",
                "type": "namespace",
                "tools": [
                    {"name": "execute"},
                    {"name": "observe"},
                ],
            }
        ],
        "captured_orchestrator_tool_contracts": [],
    }
