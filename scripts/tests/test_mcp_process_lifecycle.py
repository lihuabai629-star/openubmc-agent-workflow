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
                "stopped_processes": 0,
            },
        )
        self.assertTrue(payload["task_closeout_ready"])

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
                    "parent_identity": "unknown",
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
                    [sys.executable, str(SCRIPT), "cleanup", "--root", str(root)],
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


if __name__ == "__main__":
    unittest.main()
