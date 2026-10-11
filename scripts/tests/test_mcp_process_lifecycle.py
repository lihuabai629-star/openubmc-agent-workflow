from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "mcp_process_lifecycle.py"


class McpProcessLifecycleCliTests(unittest.TestCase):
    def test_cleanup_requires_task_and_session_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "cleanup",
                    "--root",
                    str(Path(raw) / "processes"),
                    "--dry-run",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn(
            "cleanup requires --task-id and --session-id", completed.stderr
        )

    def test_status_reports_empty_lifecycle_root_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "processes"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "status", "--root", str(root)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload["schema"],
            "openubmc-agent-workflow.mcp-process-status.v1",
        )
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["confirmed_orphaned_processes"], [])
        self.assertEqual(payload["cleaned_processes"], [])
        self.assertEqual(
            payload["summary"],
            {
                "record_count": 0,
                "live_processes": 0,
                "active_requests": 0,
                "confirmed_live_orphans": 0,
                "unattributed_live_processes": 0,
                "owned_live_processes": 0,
                "stopped_processes": 0,
            },
        )
        self.assertEqual(
            payload["closeout_checks"],
            {
                "active_requests_zero": True,
                "confirmed_live_orphans_zero": True,
                "owned_live_processes_zero": True,
                "unattributed_live_processes_zero": True,
            },
        )
        self.assertTrue(payload["task_closeout_ready"])

    @unittest.skipUnless(
        Path("/proc/sys/kernel/pid_max").is_file(), "requires Linux procfs"
    )
    def test_cleanup_dry_run_then_retires_one_confirmed_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "processes"
            root.mkdir()
            missing_parent_pid = (
                int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8"))
                + 1
            )
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
            )
            try:
                stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="utf-8")
                command_end = stat.rfind(")")
                identity = stat[command_end + 2 :].split()[19]
                record = {
                    "schema": "openubmc.mcp-process-lifecycle.v1",
                    "component": "test-mcp",
                    "version": "1",
                    "client": "test-client",
                    "task_id": "test-task",
                    "session_id": "test-session",
                    "parent_pid": missing_parent_pid,
                    "parent_identity": "verified-parent-start",
                    "parent_identity_verified": True,
                    "parent_identity_currently_verified": False,
                    "process_id": child.pid,
                    "process_identity": identity,
                    "start_time": "2026-08-28T00:00:00Z",
                    "updated_at": "2026-08-28T00:00:00Z",
                    "state_path": str(Path(raw) / "state"),
                    "lifecycle_state": "orphaned",
                    "active_requests": 0,
                    "idle_seconds": 0,
                    "idle_timeout_seconds": 300,
                    "shutdown_requested": None,
                    "exit_reason": None,
                }
                (root / f"test-mcp-{child.pid}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )

                dry_run = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "cleanup",
                        "--root",
                        str(root),
                        "--dry-run",
                        "--task-id",
                        "test-task",
                        "--session-id",
                        "test-session",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertIsNone(child.poll())
                self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
                self.assertEqual(
                    json.loads(dry_run.stdout)["confirmed_orphaned_processes"],
                    [child.pid],
                )
                self.assertEqual(json.loads(dry_run.stdout)["cleaned_processes"], [])

                cleanup = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "cleanup",
                        "--root",
                        str(root),
                        "--task-id",
                        "test-task",
                        "--session-id",
                        "test-session",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
                self.assertEqual(
                    json.loads(cleanup.stdout)["cleaned_processes"],
                    [child.pid],
                )
                child.wait(timeout=5)
                self.assertEqual(child.returncode, -signal.SIGTERM)
                cleanup_payload = json.loads(cleanup.stdout)
                self.assertEqual(
                    cleanup_payload["summary"]["confirmed_live_orphans"], 0
                )
                self.assertEqual(cleanup_payload["summary"]["live_processes"], 0)
                self.assertEqual(
                    cleanup_payload["summary"]["owned_live_processes"], 0
                )
                self.assertTrue(cleanup_payload["task_closeout_ready"])
                self.assertEqual(
                    cleanup_payload["records_before_cleanup"][0][
                        "lifecycle_state"
                    ],
                    "orphaned",
                )
                self.assertEqual(
                    cleanup_payload["records"][0]["lifecycle_state"],
                    "stopped",
                )
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)

    @unittest.skipUnless(
        Path("/proc/sys/kernel/pid_max").is_file(), "requires Linux procfs"
    )
    def test_cleanup_preserves_unbound_orphan_and_reports_it_unattributed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "processes"
            root.mkdir()
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
            )
            try:
                stat = Path(f"/proc/{child.pid}/stat").read_text(encoding="utf-8")
                command_end = stat.rfind(")")
                identity = stat[command_end + 2 :].split()[19]
                record = {
                    "schema": "openubmc.mcp-process-lifecycle.v1",
                    "component": "test-mcp",
                    "version": "1",
                    "client": "test-client",
                    "task_id": "unknown-task",
                    "session_id": "unknown-session",
                    "parent_pid": 999999999,
                    "parent_identity": "unknown",
                    "parent_identity_verified": False,
                    "process_id": child.pid,
                    "process_identity": identity,
                    "active_requests": 0,
                }
                (root / f"test-mcp-{child.pid}.json").write_text(
                    json.dumps(record), encoding="utf-8"
                )

                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "cleanup",
                        "--root",
                        str(root),
                        "--task-id",
                        "test-task",
                        "--session-id",
                        "test-session",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                payload = json.loads(completed.stdout)

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(payload["confirmed_orphaned_processes"], [])
                self.assertEqual(payload["cleaned_processes"], [])
                self.assertEqual(payload["summary"]["unattributed_live_processes"], 1)
                self.assertFalse(payload["task_closeout_ready"])
                self.assertIsNone(child.poll())
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
