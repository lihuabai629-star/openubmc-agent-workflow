from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("collect_logs_window", SCRIPTS / "collect_logs.py")
assert SPEC is not None and SPEC.loader is not None
collect_logs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collect_logs)


class CollectLogsWindowTests(unittest.TestCase):
    def test_requested_restart_window_excludes_stale_startup_errors(self) -> None:
        lines = [
            "2026-09-22 21:29:59 StartupCheck failed: stale retry",
            "2026-09-22 21:30:00 check startup status completely, total components count: 4, normal count: 4",
        ]
        filtered = collect_logs.filter_lines(
            lines,
            "2026-09-22 21:30:00",
            ["startupcheck failed", "check startup status completely"],
        )
        self.assertEqual(filtered, lines[1:])

    def test_receipt_binds_effective_window_and_boot_identity(self) -> None:
        args = argparse.Namespace(
            ip="bmc.example",
            since_time="2026-09-22 21:30:00",
            since_boot=True,
            lines=260,
            max_bytes=1024,
            include_rotated=False,
            rotated_limit=3,
            command_timeout=30,
            output_dir="",
            compact_json=True,
        )
        payload = collect_logs.build_json_payload(
            args,
            ok=True,
            code="ok",
            returncode=0,
            logs=["app.log"],
            keywords=[],
            boot_time="2026-09-22 20:00:00",
            boot_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            target_clock="2026-09-22 21:31:00",
            target_clock_epoch="1790083860",
            target_uptime_seconds="3660.00",
            utc_offset_minutes=480,
            warnings=[],
            entries=[],
        )
        self.assertEqual(payload["result"]["effective_since_time"], args.since_time)
        self.assertEqual(
            payload["result"]["boot_id"],
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        self.assertEqual(
            payload["result"]["target_clock"],
            "2026-09-22 21:31:00",
        )
        self.assertEqual(payload["result"]["target_clock_epoch"], "1790083860")
        self.assertEqual(payload["result"]["target_uptime_seconds"], "3660.00")


if __name__ == "__main__":
    unittest.main()
