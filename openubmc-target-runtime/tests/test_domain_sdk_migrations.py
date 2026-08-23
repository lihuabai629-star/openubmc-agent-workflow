from __future__ import annotations

from pathlib import Path
import sys
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    OrchestratedMcpBackend,
    RuntimeMcpService,
)


class _Task:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _DomainBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.remaining: dict[str, float] = {}

    @staticmethod
    def open_task(task_id: str) -> _Task:
        return _Task(task_id)

    @staticmethod
    def close_task(_task: _Task) -> None:
        return None

    @staticmethod
    def maintain_task(_task: _Task) -> int:
        return 0

    @staticmethod
    def task_status(task: _Task) -> dict[str, object]:
        return {"task_id": task.task_id}

    def _capture(self, name: str, arguments, context) -> dict[str, object]:
        captured = dict(arguments)
        self.calls.append((name, captured))
        self.remaining[name] = context.remaining()
        return {
            "ok": True,
            "schema": f"test/{name}",
            "evidence_ids": [f"evidence-{name}"],
        }

    def debug_run(self, _task, arguments, context) -> dict[str, object]:
        return self._capture("debug_run", arguments, context)

    def debug_collect(self, _task, arguments, context) -> dict[str, object]:
        value = self._capture("debug_collect", arguments, context)
        value["target_epoch"] = int(arguments.get("_minimum_target_epoch", 0))
        return value

    def log_bundle_collect(self, _task, arguments, context) -> dict[str, object]:
        return self._capture("log_bundle_collect", arguments, context)

    def live_patch_run(self, _task, arguments, context) -> dict[str, object]:
        value = self._capture("live_patch_run", arguments, context)
        minimum = int(arguments.get("_minimum_target_epoch", 0))
        value.update(
            {
                "journal": {
                    "stage": "verified",
                    "action": "live_patch",
                    "operation_id": context.operation_id,
                },
                "target_epoch": minimum + 1,
                "deployment_integrity": "passed",
                "metadata_status": "passed",
                "suggested_events": [{"kind": "CaseClosed"}],
            }
        )
        return value

    def upgrade_run(self, _task, arguments, context) -> dict[str, object]:
        value = self._capture("upgrade_run", arguments, context)
        minimum = int(arguments.get("_minimum_target_epoch", 0))
        value.update(
            {
                "journal": {
                    "stage": "verified",
                    "action": "upgrade",
                    "operation_id": context.operation_id,
                },
                "target_epoch": minimum + 1,
                "artifact_sha256": arguments["artifact_sha256"],
                "product_version": arguments["product_version"],
                "verification": {
                    "installed_version": arguments["product_version"]
                },
            }
        )
        return value


class DomainSdkMigrationTests(unittest.TestCase):
    def test_log_analyzer_uses_registry_deadline_evidence_and_public_workflow(self) -> None:
        backend = _DomainBackend()
        domain_backends = {
            "log_bundle_collect": backend,
            "debug_run": backend,
        }
        service = RuntimeMcpService(
            OrchestratedMcpBackend(domain_backends),
        )
        operator = RuntimeMcpService(
            OrchestratedMcpBackend(domain_backends),
            interface_profile="operator",
        )
        try:
            collected = service.call_tool(
                "log_bundle_collect",
                {
                    "ip": "192.0.2.81",
                    "problem": "inspect bundle",
                    "deadline": 900,
                },
                task_id="sdk-log",
                operation_id="sdk-log-collect",
            )
            completed = service.call_tool(
                "workflow.next",
                {"case_id": collected.envelope["case_id"]},
                task_id="sdk-log",
                operation_id="sdk-log-next",
            )
            status = operator.call_exposed_tool(
                "runtime_status",
                {},
                task_id="sdk-log",
                operation_id="sdk-log-status",
            )
        finally:
            service.close()
            operator.close()

        descriptor = next(
            item
            for item in status["capability_registry"]["capabilities"]
            if item["operation"] == "log_bundle_collect"
        )
        self.assertEqual(descriptor["owner_skill"], "openubmc-log-analyzer")
        self.assertLessEqual(backend.remaining["log_bundle_collect"], 600)
        self.assertEqual(collected["evidence_ids"], ["evidence-log_bundle_collect"])
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["log_bundle_collect", "debug_run"],
        )
        self.assertTrue(completed["completed"])

    def test_unavailable_log_capability_returns_explicit_workflow_outcome(self) -> None:
        backend = _DomainBackend()
        backend.log_bundle_collect = None
        service = RuntimeMcpService(backend)
        try:
            opened = service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.82",
                    "intent": "bundle-and-diagnose",
                },
                task_id="sdk-log-unavailable",
                operation_id="sdk-log-open",
            )
            waiting = service.call_tool(
                "workflow.next",
                {"case_id": opened.envelope["case_id"]},
                task_id="sdk-log-unavailable",
                operation_id="sdk-log-next",
            )
        finally:
            service.close()

        self.assertEqual(waiting["status"], "waiting_external")
        self.assertEqual(waiting["outcome"]["status"], "unavailable")
        self.assertEqual(waiting["outcome"]["operation"], "log_bundle_collect")

    def test_live_patch_suggestions_cannot_advance_or_close_the_case(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            patched = service.call_tool(
                "live_patch_run",
                {
                    "ip": "192.0.2.83",
                    "intent": "live-patch",
                    "local_path": "/tmp/fix.lua",
                    "remote_path": "/opt/bmc/apps/fix.lua",
                    "deadline": 900,
                },
                task_id="sdk-live-patch",
                operation_id="sdk-live-patch-apply",
            )
            case = service.call_tool(
                "case_read",
                {"case_id": patched.envelope["case_id"]},
                task_id="sdk-live-patch",
                operation_id="sdk-live-patch-read",
            )
        finally:
            service.close()

        self.assertLessEqual(backend.remaining["live_patch_run"], 600)
        self.assertFalse(case["closed"])
        self.assertFalse(patched.envelope["continuation"]["workflow_complete"])
        self.assertEqual(
            patched.envelope["continuation"]["required_operation"],
            "debug_collect",
        )
        self.assertEqual(
            set(case["workflow_step_states"]),
            {"step-01-live_patch_run"},
        )

    def test_upgrade_requires_artifact_identity_target_and_advances_epoch(self) -> None:
        backend = _DomainBackend()
        service = RuntimeMcpService(backend)
        try:
            with self.assertRaisesRegex(ValueError, "artifact_sha256 is required"):
                service.call_tool(
                    "upgrade_run",
                    {
                        "ip": "192.0.2.84",
                        "artifact_path": "/tmp/openubmc.hpm",
                        "product_version": "2.0.0",
                    },
                    task_id="sdk-upgrade-invalid",
                    operation_id="sdk-upgrade-invalid",
                )
            with self.assertRaisesRegex(
                ValueError, "first mutation domain call must provide ip or targets"
            ):
                service.call_tool(
                    "upgrade_run",
                    {
                        "artifact_path": "/tmp/openubmc.hpm",
                        "artifact_sha256": "a" * 64,
                        "product_version": "2.0.0",
                    },
                    task_id="sdk-upgrade-unbound",
                    operation_id="sdk-upgrade-unbound",
                )
            upgraded = service.call_tool(
                "upgrade_run",
                {
                    "ip": "192.0.2.84",
                    "artifact_path": "/tmp/openubmc.hpm",
                    "artifact_sha256": "a" * 64,
                    "product_version": "2.0.0",
                    "deadline": 3600,
                },
                task_id="sdk-upgrade",
                operation_id="sdk-upgrade-apply",
            )
        finally:
            service.close()

        self.assertLessEqual(backend.remaining["upgrade_run"], 1800)
        self.assertEqual(upgraded["target_epoch"], 1)
        self.assertEqual(upgraded.envelope["continuation"]["target_epoch_floor"], 1)
        self.assertEqual(upgraded["journal"]["operation_id"], "sdk-upgrade-apply")
        self.assertNotIn("backend_operation_id", upgraded["journal"])


if __name__ == "__main__":
    unittest.main()
