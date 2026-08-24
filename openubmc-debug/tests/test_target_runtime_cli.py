from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"openubmc_debug_cli_{name}", SCRIPTS / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.passwords: list[str] = []

    @staticmethod
    def open_task(task_id: str) -> dict[str, str]:
        return {"task_id": task_id}

    @staticmethod
    def close_task(task: dict[str, str]) -> None:
        del task

    @staticmethod
    def maintain_task(task: dict[str, str]) -> int:
        del task
        return 0

    @staticmethod
    def task_status(task: dict[str, str]) -> dict[str, object]:
        return {"task_id": task["task_id"]}

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        del task, context
        captured = dict(arguments)
        self.calls.append(captured)
        password_env = str(captured.get("ssh_password_env", ""))
        self.passwords.append(os.environ.get(password_env, ""))
        return {
            "ok": True,
            "returncode": 0,
            "summary": "fake debug completed",
            "request": captured,
        }

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        return self.debug_run(task, arguments, context)


class TargetRuntimeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script("target_runtime_cli")
        self.runtime = self.module.target_runtime_mcp._load_runtime_module()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backend = FakeBackend()

        def create_service():
            return self.runtime.RuntimeMcpService(
                self.backend,
                context_repository=self.runtime.SQLiteRuntimeRepository(
                    self.root / "context-runtime.sqlite3"
                ),
                blob_repository=self.runtime.FilesystemBlobRepository(
                    self.root / "evidence-blobs"
                ),
            )

        self.create_service = create_service
        self.factory_patch = mock.patch.object(
            self.module.target_runtime_mcp,
            "create_service",
            side_effect=create_service,
        )
        self.factory_patch.start()

    def tearDown(self) -> None:
        self.factory_patch.stop()
        self.temporary.cleanup()

    def invoke(self, argv: list[str]) -> dict[str, object]:
        output = io.StringIO()
        with redirect_stdout(output):
            returncode = self.module.run_legacy(argv)
        self.assertEqual(returncode, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_legacy_cli_reuses_one_persistent_case_and_suppresses_replay(self) -> None:
        first = self.invoke(
            [
                "--ip",
                "target.example",
                "--ssh-password",
                "direct-secret",
                "--task-id",
                "cli-task-a",
                "--operation-id",
                "cli-operation-a",
                "--idempotency-key",
                "cli-idempotency-a",
                "--json",
            ]
        )
        case_id = str(first["case_id"])
        second_argv = [
            "--ip",
            "target.example",
            "--ssh-password",
            "direct-secret",
            "--case-id",
            case_id,
            "--task-id",
            "cli-task-b",
            "--operation-id",
            "cli-operation-b",
            "--idempotency-key",
            "cli-idempotency-b",
            "--json",
        ]
        second = self.invoke(second_argv)
        replay = self.invoke(second_argv)

        self.assertEqual(second["case_id"], case_id)
        self.assertEqual(replay["case_id"], case_id)
        self.assertEqual(replay["revision"], second["revision"])
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.backend.passwords, ["direct-secret", "direct-secret"])
        self.assertNotIn(
            self.module._CLI_SSH_PASSWORD_ENV,
            os.environ,
        )

        inspector = self.create_service()
        try:
            status = inspector.call_tool(
                "runtime_status",
                {},
                task_id="inspector-status",
                operation_id="inspect-status",
            )
            case = inspector.call_tool(
                "case_read",
                {"case_id": case_id},
                task_id="inspector-task",
                operation_id="inspect-case",
            )
        finally:
            inspector.close()
        self.assertEqual(
            status["compatibility_telemetry"]["operation_counts"],
            {},
        )
        self.assertEqual(len(case["operations"]), 2)
        self.assertEqual(
            [item["operation_id"] for item in case["operations"]],
            ["cli-operation-a", "cli-operation-b"],
        )

    def test_catalog_cli_lists_the_same_operation_names_as_the_service(self) -> None:
        service = self.create_service()
        try:
            expected = list(service.interface_catalog.names())
        finally:
            service.close()
        output = io.StringIO()
        with redirect_stdout(output):
            returncode = self.module._generic_main(["--list-operations"])
        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)

    def test_agent_cli_maps_legacy_case_id_to_execute_run_id_only(self) -> None:
        parser = self.module._generic_parser(("observe", "execute"))
        args = parser.parse_args(
            [
                "--operation",
                "execute",
                "--case-id",
                "run-123",
                "--expected-revision",
                "9",
                "--idempotency-key",
                "transport-operation",
            ]
        )

        arguments = self.module._generic_arguments(
            {"kind": "resume"},
            args,
            operation="execute",
            interface_profile="agent",
        )

        self.assertEqual(arguments, {"kind": "resume", "run_id": "run-123"})

    def test_legacy_cli_emits_json_for_a_validation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_state:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "target_runtime_cli.py"),
                    "--ip",
                    "target.example",
                    "--mdb-query",
                    "setprop Bad Value",
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "CODEX_TASK_ID": "invalid-cli-task",
                    "OPENUBMC_TARGET_RUNTIME_STATE_DIR": raw_state,
                },
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        envelope = json.loads(completed.stdout)
        self.assertEqual(envelope["status"], "failed")
        self.assertEqual(envelope["canonical_error"]["code"], "ValueError")


if __name__ == "__main__":
    unittest.main()
