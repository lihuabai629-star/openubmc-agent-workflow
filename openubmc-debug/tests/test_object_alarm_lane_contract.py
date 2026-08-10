from __future__ import annotations

import argparse
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _remote_common as remote_common  # noqa: E402
import active_alarms  # noqa: E402
import mdbctl_remote  # noqa: E402


def completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    result = subprocess.CompletedProcess(
        args=["ssh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    return remote_common._attach_capture_metadata(
        result,
        stdout_limit_bytes=None,
        stderr_limit_bytes=None,
        stdout_bytes_read=len(stdout.encode("utf-8")),
        stderr_bytes_read=len(stderr.encode("utf-8")),
        stdout_bytes_captured=len(stdout.encode("utf-8")),
        stderr_bytes_captured=len(stderr.encode("utf-8")),
    )


class RecordingRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self.results = list(results)
        self.commands: list[str] = []

    def __call__(
        self,
        _ip: str,
        _user: str,
        _password: str,
        remote_command: str,
        _timeout: float,
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(remote_command)
        return self.results.pop(0)


class ObjectAlarmCollectorLaneTests(unittest.TestCase):
    def test_mdb_auto_fallback_uses_the_injected_lane_runner(self) -> None:
        runner = RecordingRunner(
            [
                completed(returncode=127, stderr="mdbctl: command not found"),
                completed(stdout="ClassA\n"),
            ]
        )
        args = argparse.Namespace(
            ip="bmc.example",
            debug_dump="",
            mode="auto",
            timeout=2,
        )

        result = mdbctl_remote.execute_mdb_query(
            args,
            ["lsclass"],
            {
                "user": "debug-user",
                "password": "",
                "port": 22,
                "identity_file": "",
            },
            ssh_runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.selected_mode, "direct-skynet")
        self.assertEqual(len(runner.commands), 2)

    def test_dbus_environment_detection_uses_the_injected_lane_runner(self) -> None:
        runner = RecordingRunner(
            [
                completed(
                    stdout=(
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus\n"
                        "XDG_RUNTIME_DIR=/run/user/0\n"
                    )
                )
            ]
        )

        env = remote_common.detect_dbus_env(
            "bmc.example",
            "debug-user",
            "",
            2,
            ssh_runner=runner,
        )

        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/0/bus")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/0")
        self.assertEqual(len(runner.commands), 1)

    def test_alarm_discovery_channel_uses_the_injected_lane_runner(self) -> None:
        runner = RecordingRunner(
            [completed(stdout="alarm.service 1 alarm-user\n")]
        )
        args = argparse.Namespace(
            ip="bmc.example",
            timeout=2,
        )
        discovery: dict[str, object] = {}

        result, stdout, stderr = active_alarms.run_remote_busctl(
            args,
            {
                "user": "debug-user",
                "password": "",
                "port": 22,
                "identity_file": "",
            },
            None,
            "unix:path=/run/user/0/bus",
            "/run/user/0",
            ["busctl", "--user", "--no-pager", "list"],
            "active_alarms_list",
            active_alarms.WorkflowDeadline(5),
            discovery,
            ssh_runner=runner,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(stdout, "alarm.service 1 alarm-user")
        self.assertEqual(stderr, "")
        self.assertEqual(len(runner.commands), 1)
        self.assertIn("busctl --user --no-pager list", runner.commands[0])


if __name__ == "__main__":
    unittest.main()
