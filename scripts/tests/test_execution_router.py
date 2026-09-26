from __future__ import annotations

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.execution_router import (  # noqa: E402
    ExecutionRouter,
    ProtocolProbe,
    RoutingError,
    probe_protocol,
)


class ExecutionRouterTests(unittest.TestCase):
    def test_windows_client_uses_healthy_wsl_runtime(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose(
            "diagnose",
            probe=probe_protocol("diagnose", host="wsl", list_tools=lambda: ["observe", "execute"]),
            requested_scope="BMC 192.0.2.10",
            evidence_boundary="fresh request",
        )
        self.assertEqual(receipt["path"], "structured-runtime-mcp")
        self.assertEqual(receipt["client_environment"], "windows")
        self.assertEqual(receipt["execution_host"], "wsl")
        self.assertEqual(router.metrics()["host_mismatches"], 0)
        self.assertEqual(router.metrics()["structured_calls"], 1)

    def test_windows_fallback_remains_on_wsl_when_protocol_is_unavailable(self) -> None:
        receipt = ExecutionRouter(environment="windows").choose(
            "diagnose", requested_scope="BMC 192.0.2.10", evidence_boundary="fresh request",
        )
        self.assertEqual(receipt["path"], "shell-fallback")
        self.assertEqual(receipt["execution_host"], "wsl")
        self.assertEqual(receipt["fallback"]["reason_code"], "probe_missing")

    def test_windows_native_probe_does_not_impersonate_wsl_backend(self) -> None:
        receipt = ExecutionRouter(environment="windows").choose(
            "diagnose", probe=ProtocolProbe(True, "windows-native"),
            requested_scope="BMC 192.0.2.10", evidence_boundary="fresh request",
        )
        self.assertEqual(receipt["path"], "shell-fallback")
        self.assertEqual(receipt["fallback"]["reason_code"], "protocol_host_mismatch")

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


if __name__ == "__main__":
    unittest.main()
