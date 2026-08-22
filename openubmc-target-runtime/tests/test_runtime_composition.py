from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from openubmc_target_runtime.semantic_runtime import (  # noqa: E402
    RunTurn,
    StartRun,
)
from test_agent_gateway import SemanticBackend  # noqa: E402


class RuntimeCompositionTests(unittest.TestCase):
    def test_composed_runtime_executes_through_typed_semantic_port(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            turn = service.semantic_runtime.execute(
                StartRun(
                    target="192.0.2.91",
                    intent="diagnose-and-fix",
                    purpose="verify Runtime composition",
                    delivery_strategy="source-only",
                    command_id="runtime-composition-start",
                    input_digest="",
                ),
                task_id="runtime-composition",
                operation_id="runtime-composition-start",
            )

            self.assertIsInstance(turn, RunTurn)
            self.assertEqual(turn.state, "waiting_response")
            self.assertTrue(turn.run_id.startswith("run-"))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
