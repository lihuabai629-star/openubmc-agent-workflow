from __future__ import annotations

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "openubmc-debug" / "scripts"))

from execution_router import (  # noqa: E402
    ExecutionRouter,
    ProtocolProbe,
    RoutingError,
    ShellFallbackBudget,
    probe_protocol,
)


def healthy(host: str = "wsl") -> ProtocolProbe:
    return probe_protocol(
        "diagnose", host=host,
        initialize=lambda: {"protocolVersion": "2025-03-26"},
        list_tools=lambda: {"tools": [{"name": "observe"}, {"name": "execute"}]},
    )


class PackagedExecutionRouterTests(unittest.TestCase):
    def test_packaged_skill_references_routing_receipts(self) -> None:
        skill = (ROOT / "openubmc-debug" / "SKILL.md").read_text(encoding="utf-8")
        reference = (ROOT / "openubmc-debug" / "references" / "windows-routing.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("references/windows-routing.md", skill)
        self.assertIn("execution_router.py", reference)
        self.assertIn("reason_code", reference)

    def test_healthy_wsl_protocol_routes_windows_to_structured_runtime(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", probe=healthy(), requested_scope="BMC fixture.invalid",
                                evidence_boundary="fresh Runtime request")
        self.assertEqual(receipt["path"], "structured-runtime-mcp")
        self.assertEqual(receipt["execution_host"], "wsl")
        router.record_structured_call(record=receipt, tool="observe")
        self.assertEqual(router.metrics()["structured_calls"], 1)

    def test_tool_listing_without_initialize_is_not_healthy(self) -> None:
        probe = probe_protocol("diagnose", host="wsl",
                               list_tools=lambda: {"tools": [{"name": "observe"}, {"name": "execute"}]})
        self.assertFalse(probe.ready)
        self.assertEqual(probe.reason, "initialize_missing")

    def test_unhealthy_mcp_requires_bounded_shell_receipt(self) -> None:
        router = ExecutionRouter(environment="wsl")
        receipt = router.choose("evidence", probe=ProtocolProbe(False, "wsl", "mcp_unhealthy"),
                                requested_scope="BMC fixture.invalid", evidence_boundary="stdout only")
        self.assertEqual(receipt["fallback"]["reason_code"], "mcp_unhealthy")
        event = router.admit_shell(["ssh", "fixture.invalid", "uptime"], record=receipt,
                                   observed_host="wsl")
        self.assertEqual(event["fallback"]["calls"], 1)
        self.assertEqual(event["host_path"], ["wsl", "target"])
        self.assertFalse(router.shell_can_satisfy_gate("runtime-verification"))

    def test_healthy_mcp_on_wrong_host_is_not_selected(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", probe=healthy("windows-native"), shell_host="wsl",
                                requested_scope="BMC fixture.invalid", evidence_boundary="stdout only")
        self.assertEqual(receipt["path"], "shell-fallback")
        self.assertEqual(receipt["fallback"]["reason_code"], "protocol_host_mismatch")
        self.assertEqual(router.metrics()["host_mismatches"], 1)

    def test_unsupported_operation_falls_back_with_reason(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("custom-read", probe=healthy(), shell_host="wsl",
                                requested_scope="BMC fixture.invalid", evidence_boundary="stdout only")
        self.assertEqual(receipt["fallback"]["reason_code"], "operation_unsupported")

    def test_windows_native_command_counts_nested_transitions(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", shell_host="windows-native",
                                requested_scope="BMC fixture.invalid", evidence_boundary="stdout only")
        event = router.admit_shell(["powershell.exe", "-Command", "wsl ssh fixture.invalid uptime"],
                                   record=receipt, observed_host="windows-native")
        self.assertEqual(event["host_path"], ["windows-native", "wsl", "target"])
        self.assertEqual(router.metrics()["host_transitions"], 2)

    def test_repeated_equivalent_command_blocks_convergence(self) -> None:
        router = ExecutionRouter(environment="linux")
        receipt = router.choose("build", requested_scope="component", evidence_boundary="stdout only")
        router.admit_shell(["bmcgo", "build"], record=receipt, observed_host="linux")
        with self.assertRaisesRegex(RoutingError, "convergence_blocker"):
            router.admit_shell(["bmcgo", "build"], record=receipt, observed_host="linux")
        self.assertEqual(router.metrics()["repetitions"], 1)

    def test_budget_exhaustion_blocks_after_one_call(self) -> None:
        router = ExecutionRouter(environment="wsl", shell_budget=ShellFallbackBudget(limit=1))
        receipt = router.choose("evidence", requested_scope="fixture", evidence_boundary="stdout only")
        router.admit_shell(["uptime"], record=receipt, observed_host="wsl")
        with self.assertRaisesRegex(RoutingError, "budget exhausted"):
            router.admit_shell(["date"], record=receipt, observed_host="wsl")

    def test_report_excludes_raw_commands_and_secrets(self) -> None:
        router = ExecutionRouter(environment="wsl")
        receipt = router.choose("diagnose", requested_scope="fixture", evidence_boundary="stdout only")
        router.admit_shell(["ssh", "fixture.invalid", "password=private-value"], record=receipt,
                           observed_host="wsl")
        report = json.dumps(router.report())
        self.assertNotIn("private-value", report)
        self.assertNotIn("password=", report)
        self.assertEqual(len(router.report()["shell_calls"]), 1)

    def test_invalid_windows_host_and_budget_are_rejected(self) -> None:
        router = ExecutionRouter(environment="windows")
        with self.assertRaisesRegex(RoutingError, "explicit execution host"):
            router.choose("diagnose", requested_scope="fixture", evidence_boundary="stdout only")
        with self.assertRaisesRegex(RoutingError, "between 1 and 32"):
            router.choose("diagnose", shell_host="wsl", requested_scope="fixture",
                          evidence_boundary="stdout only", shell_budget=33)


if __name__ == "__main__":
    unittest.main()
