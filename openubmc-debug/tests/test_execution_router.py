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
    probe_protocol,
)


class ExecutionRouterTests(unittest.TestCase):
    def test_packaged_debug_entry_routes_windows_through_runtime_first(self) -> None:
        skill = (ROOT / "openubmc-debug" / "SKILL.md").read_text(encoding="utf-8")
        reference = (ROOT / "openubmc-debug" / "references" / "windows-routing.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("references/windows-routing.md", skill)
        self.assertIn("execution_router.py", reference)
        self.assertIn("reason_code", reference)

    def test_healthy_windows_protocol_uses_structured_path(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose(
            "diagnose",
            probe=probe_protocol("diagnose", host="wsl", list_tools=lambda: ["observe", "execute"]),
            requested_scope="BMC 192.0.2.10",
            evidence_boundary="fresh request",
        )
        self.assertEqual(receipt["path"], "structured-runtime-mcp")
        self.assertEqual(router.metrics()["structured_calls"], 1)

    def test_unhealthy_wsl_protocol_requires_bounded_shell_receipt(self) -> None:
        router = ExecutionRouter(environment="wsl")
        receipt = router.choose(
            "evidence",
            probe=ProtocolProbe(False, "wsl", "mcp_unhealthy"),
            requested_scope="BMC 192.0.2.10",
            evidence_boundary="command output only",
        )
        self.assertEqual(receipt["path"], "shell-fallback")
        executed = router.admit_shell(["ssh", "192.0.2.10", "uptime"], record=receipt)
        self.assertEqual(executed["fallback"]["calls"], 1)
        self.assertFalse(router.shell_can_satisfy_gate("runtime-verification"))

    def test_repeated_shell_action_stops_convergence(self) -> None:
        router = ExecutionRouter(environment="linux")
        receipt = router.choose(
            "build",
            probe=ProtocolProbe(False, "linux", "mcp_unavailable"),
            requested_scope="component",
            evidence_boundary="stdout and exit code",
        )
        router.admit_shell(["bmcgo", "build"], record=receipt)
        with self.assertRaisesRegex(RoutingError, "convergence_blocker"):
            router.admit_shell(["bmcgo", "build"], record=receipt)

    def test_shell_receipts_redact_secret_fields_and_count_host_mismatch(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose(
            "upgrade",
            probe=ProtocolProbe(False, "linux", "mcp_unavailable"),
            requested_scope="BMC 192.0.2.10",
            evidence_boundary="upload response",
        )
        receipt["password"] = "private"
        rendered = json.dumps(router.report())
        self.assertNotIn("private", rendered)
        self.assertEqual(router.metrics()["host_mismatches"], 1)

    def test_fallback_requires_reason_scope_and_boundary(self) -> None:
        router = ExecutionRouter()
        with self.assertRaises(RoutingError):
            router.choose("diagnose", probe=ProtocolProbe(False, "linux"), requested_scope="", evidence_boundary="x")

    def test_healthy_protocol_on_wrong_host_cannot_select_structured_path(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose(
            "diagnose", probe=ProtocolProbe(True, "windows-native", structured_tools=("observe", "execute")),
            requested_scope="BMC fixture.invalid", evidence_boundary="fresh observation",
        )
        self.assertEqual(receipt["path"], "shell-fallback")
        self.assertEqual(receipt["fallback"]["reason_code"], "protocol_host_mismatch")

    def test_unsupported_operation_has_explicit_bounded_fallback(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose(
            "custom-read", probe=ProtocolProbe(True, "wsl", structured_tools=("observe", "execute")),
            requested_scope="BMC fixture.invalid", evidence_boundary="command output only",
        )
        self.assertEqual(receipt["path"], "shell-fallback")
        self.assertEqual(receipt["fallback"]["reason_code"], "operation_unsupported")

    def test_nested_windows_wsl_ssh_fallback_counts_host_transitions(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", probe=ProtocolProbe(False, "windows-native", "mcp_unavailable"),
                                requested_scope="BMC fixture.invalid", evidence_boundary="stdout only")
        router.admit_shell(["powershell.exe", "-Command", "wsl ssh fixture.invalid uptime"], record=receipt)
        self.assertEqual(router.metrics()["host_transitions"], 2)

    def test_fallback_budget_above_the_hard_limit_is_rejected(self) -> None:
        router = ExecutionRouter(environment="wsl")
        with self.assertRaisesRegex(RoutingError, "between 1 and 32"):
            router.choose("diagnose", probe=ProtocolProbe(False, "wsl", "mcp_unavailable"),
                          requested_scope="BMC fixture.invalid", evidence_boundary="stdout",
                          shell_budget=33)


if __name__ == "__main__":
    unittest.main()
