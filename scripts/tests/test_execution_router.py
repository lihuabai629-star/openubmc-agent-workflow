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
    ShellFallbackBudget,
    probe_protocol,
)


def healthy(host: str = "wsl") -> ProtocolProbe:
    return probe_protocol(
        "diagnose", host=host,
        initialize=lambda: {"protocolVersion": "2025-03-26"},
        list_tools=lambda: {"tools": [{"name": "observe"}, {"name": "execute"}]},
    )


class ExecutionRouterTests(unittest.TestCase):
    def test_windows_client_routes_supported_operations_to_healthy_wsl_runtime(self) -> None:
        for operation in ("diagnose", "build", "upgrade", "evidence", "rollback"):
            with self.subTest(operation=operation):
                router = ExecutionRouter(environment="windows")
                receipt = router.choose(
                    operation, probe=healthy(), requested_scope="BMC 192.0.2.10",
                    evidence_boundary="fresh Runtime request",
                )
                self.assertEqual(receipt["path"], "structured-runtime-mcp")
                self.assertEqual(receipt["execution_host"], "wsl")
                self.assertEqual(router.metrics()["structured_calls"], 0)
                call = router.record_structured_call(
                    record=receipt, tool="observe" if operation in ("diagnose", "evidence") else "execute"
                )
                self.assertEqual(call["execution_host"], "wsl")
                self.assertEqual(router.metrics()["structured_calls"], 1)
                with self.assertRaises(RoutingError):
                    router.admit_shell(["ssh", "fixture.invalid", "uptime"], record=receipt,
                                       observed_host="wsl")

    def test_tool_names_without_initialize_are_not_health(self) -> None:
        probe = probe_protocol(
            "diagnose", host="wsl",
            list_tools=lambda: {"tools": [{"name": "observe"}, {"name": "execute"}]},
        )
        self.assertFalse(probe.ready)
        self.assertEqual(probe.reason, "initialize_missing")
        receipt = ExecutionRouter(environment="windows").choose(
            "diagnose", probe=probe, shell_host="wsl", requested_scope="fixture",
            evidence_boundary="stdout only",
        )
        self.assertEqual(receipt["fallback"]["reason_code"], "initialize_missing")

    def test_probe_performs_initialize_before_tools_list_and_requires_both(self) -> None:
        calls: list[str] = []

        def initialize() -> dict[str, object]:
            calls.append("initialize")
            return {"protocolVersion": "2025-03-26"}

        def list_tools() -> dict[str, object]:
            calls.append("tools/list")
            return {"tools": [{"name": "observe"}, {"name": "execute"}]}

        probe = probe_protocol("diagnose", host="wsl", initialize=initialize, list_tools=list_tools)
        self.assertEqual(calls, ["initialize", "tools/list"])
        self.assertTrue(probe.ready)
        self.assertFalse(probe_protocol("diagnose", host="wsl", initialize=lambda: {},
                                        list_tools=list_tools).ready)
        self.assertFalse(probe_protocol("diagnose", host="wsl", initialize=initialize,
                                        list_tools=lambda: {"tools": [{"name": "observe"}]}).ready)

    def test_probe_errors_are_stable_and_do_not_include_exception_text(self) -> None:
        def broken() -> dict[str, object]:
            raise RuntimeError("token=do-not-report")

        probe = probe_protocol("diagnose", host="wsl", initialize=broken,
                               list_tools=lambda: {"tools": []})
        self.assertEqual(probe.reason, "probe_error:runtimeerror")
        self.assertNotIn("do-not-report", json.dumps(probe.to_public_dict()))
        timed = probe_protocol("diagnose", host="wsl", initialize=lambda: (_ for _ in ()).throw(TimeoutError()),
                               list_tools=lambda: {"tools": []})
        self.assertEqual(timed.reason, "probe_timeout")

    def test_deadline_rejects_slow_adapter_response(self) -> None:
        ticks = iter((0.0, 0.003, 0.003))
        probe = probe_protocol("diagnose", host="wsl",
                               initialize=lambda: {"protocolVersion": "2025-03-26"},
                               list_tools=lambda: {"tools": []}, deadline_ms=2,
                               clock=lambda: next(ticks))
        self.assertEqual(probe.reason, "probe_timeout")
        self.assertFalse(probe.ready)

    def test_windows_native_fallback_requires_exact_declared_host(self) -> None:
        router = ExecutionRouter(environment="windows")
        with self.assertRaisesRegex(RoutingError, "explicit execution host"):
            router.choose("diagnose", requested_scope="fixture", evidence_boundary="stdout only")
        receipt = router.choose("diagnose", shell_host="windows-native",
                                requested_scope="fixture", evidence_boundary="stdout only")
        self.assertEqual(receipt["execution_host"], "windows-native")
        self.assertEqual(receipt["fallback"]["reason_code"], "probe_missing")
        event = router.admit_shell(["powershell.exe", "-Command", "wsl ssh fixture.invalid uptime"],
                                   record=receipt, observed_host="windows-native")
        self.assertEqual(event["host_path"], ["windows-native", "wsl", "target"])
        self.assertEqual(router.metrics()["host_transitions"], 2)

    def test_wsl_launcher_started_inside_wsl_counts_round_trip(self) -> None:
        router = ExecutionRouter(environment="wsl")
        receipt = router.choose("diagnose", requested_scope="fixture",
                                evidence_boundary="stdout only")
        event = router.admit_shell(["wsl.exe", "ssh", "fixture.invalid", "uptime"],
                                   record=receipt, observed_host="wsl")
        self.assertEqual(event["host_path"], ["wsl", "windows-native", "wsl", "target"])
        self.assertEqual(router.metrics()["host_transitions"], 3)

    def test_wsl_and_unavailable_mcp_require_bounded_fallback(self) -> None:
        router = ExecutionRouter(environment="wsl", shell_budget=ShellFallbackBudget(limit=2))
        receipt = router.choose(
            "evidence", probe=ProtocolProbe(False, "wsl", "mcp_unavailable"),
            requested_scope="BMC 192.0.2.10", evidence_boundary="command output only",
        )
        self.assertEqual(receipt["fallback"]["reason_code"], "mcp_unavailable")
        event = router.admit_shell(["ssh", "192.0.2.10", "uptime"], record=receipt,
                                   observed_host="wsl")
        self.assertEqual(event["fallback"]["remaining"], 1)
        self.assertEqual(event["host_path"], ["wsl", "target"])
        self.assertEqual(router.metrics()["unresolved_work"], 1)

    def test_healthy_probe_on_wrong_host_cannot_route_structured(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", probe=healthy("windows-native"),
                                shell_host="wsl", requested_scope="fixture",
                                evidence_boundary="stdout only")
        self.assertEqual(receipt["path"], "shell-fallback")
        self.assertEqual(receipt["fallback"]["reason_code"], "protocol_host_mismatch")
        self.assertEqual(router.metrics()["host_mismatches"], 1)
        self.assertFalse(router.metrics()["host_accuracy"])

    def test_shell_observed_host_must_match_receipt(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", shell_host="wsl", requested_scope="fixture",
                                evidence_boundary="stdout only")
        with self.assertRaisesRegex(RoutingError, "observed shell host differs"):
            router.admit_shell(["uptime"], record=receipt, observed_host="windows-native")
        self.assertEqual(router.metrics()["host_mismatches"], 1)
        self.assertEqual(router.metrics()["fallback_calls"], 0)

    def test_unsupported_operation_is_a_deliberate_fallback(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("custom-read", probe=healthy(), shell_host="wsl",
                                requested_scope="fixture", evidence_boundary="stdout only")
        self.assertEqual(receipt["fallback"]["reason_code"], "operation_unsupported")

    def test_repeated_equivalent_command_stops_with_blocker(self) -> None:
        router = ExecutionRouter(environment="wsl")
        receipt = router.choose("build", requested_scope="component", evidence_boundary="stdout only")
        router.admit_shell(["/usr/bin/ssh", "fixture.invalid", "uptime"], record=receipt,
                           observed_host="wsl")
        with self.assertRaisesRegex(RoutingError, "convergence_blocker: equivalent"):
            router.admit_shell(["ssh", "fixture.invalid", "uptime"], record=receipt,
                               observed_host="wsl")
        self.assertEqual(router.metrics()["fallback_calls"], 1)
        self.assertEqual(router.metrics()["repetitions"], 1)
        self.assertEqual(router.metrics()["blocked_fallback_calls"], 1)

    def test_fallback_budget_exhaustion_is_deterministic(self) -> None:
        router = ExecutionRouter(environment="linux", shell_budget=ShellFallbackBudget(limit=1))
        receipt = router.choose("build", requested_scope="component", evidence_boundary="stdout only")
        router.admit_shell(["bmcgo", "build"], record=receipt, observed_host="linux")
        with self.assertRaisesRegex(RoutingError, "budget exhausted"):
            router.admit_shell(["bmcgo", "test"], record=receipt, observed_host="linux")
        self.assertEqual(router.metrics()["fallback_calls"], 1)
        with self.assertRaisesRegex(RoutingError, "cannot be increased"):
            router.choose("build", requested_scope="component", evidence_boundary="stdout only", shell_budget=2)

    def test_shell_output_cannot_satisfy_any_typed_gate(self) -> None:
        for gate in ("mutation", "deployment", "runtime-verification", "rollback", "future-gate"):
            self.assertFalse(ExecutionRouter.shell_can_satisfy_gate(gate))

    def test_receipts_are_bound_to_router_and_not_mutable(self) -> None:
        router = ExecutionRouter(environment="linux")
        receipt = router.choose("diagnose", requested_scope="fixture", evidence_boundary="stdout only")
        other = ExecutionRouter(environment="linux")
        with self.assertRaises(RoutingError):
            other.admit_shell(["uptime"], record=receipt, observed_host="linux")
        receipt["requested_scope"] = "different"
        with self.assertRaises(RoutingError):
            router.admit_shell(["uptime"], record=receipt, observed_host="linux")

    def test_chained_commands_are_rejected_before_budget_consumption(self) -> None:
        router = ExecutionRouter(environment="windows")
        receipt = router.choose("diagnose", shell_host="windows-native",
                                requested_scope="fixture", evidence_boundary="stdout only")
        with self.assertRaisesRegex(RoutingError, "chained"):
            router.admit_shell(["powershell.exe", "-Command", "wsl ssh fixture uptime; wsl ssh fixture uptime"],
                               record=receipt, observed_host="windows-native")
        self.assertEqual(router.metrics()["fallback_calls"], 0)

    def test_report_contains_no_command_or_inline_credentials(self) -> None:
        router = ExecutionRouter(environment="wsl")
        receipt = router.choose("diagnose", requested_scope="fixture", evidence_boundary="stdout only")
        router.admit_shell(["ssh", "fixture.invalid", "token=private-value"], record=receipt,
                           observed_host="wsl")
        report = json.dumps(router.report())
        self.assertNotIn("private-value", report)
        self.assertNotIn("ssh", report)
        self.assertEqual(len(router.report()["shell_calls"]), 1)
        with self.assertRaisesRegex(RoutingError, "credentials"):
            router.choose("diagnose", requested_scope="token=private-value", evidence_boundary="stdout only")

    def test_unresolved_work_requires_verified_runtime_evidence(self) -> None:
        router = ExecutionRouter(environment="linux")
        receipt = router.choose("diagnose", requested_scope="fixture", evidence_boundary="stdout only")
        with self.assertRaises(RoutingError):
            router.note_runtime_resolution(record=receipt, evidence_ref="shell-stdout",
                                           verify_runtime_evidence=lambda _: False)
        with self.assertRaisesRegex(RoutingError, "verified Runtime evidence"):
            router.note_runtime_resolution(
                record=receipt, evidence_ref="runtime-evidence-id",
                verify_runtime_evidence=lambda _: (_ for _ in ()).throw(RuntimeError("token=private")),
            )
        self.assertEqual(router.metrics()["unresolved_work"], 1)
        router.note_runtime_resolution(record=receipt, evidence_ref="runtime-evidence-id",
                                       verify_runtime_evidence=lambda ref: ref == "runtime-evidence-id")
        self.assertEqual(router.metrics()["unresolved_work"], 0)

    def test_packaged_mirror_has_identical_policy(self) -> None:
        self.assertEqual((ROOT / "scripts" / "execution_router.py").read_bytes(),
                         (ROOT / "openubmc-debug" / "scripts" / "execution_router.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
