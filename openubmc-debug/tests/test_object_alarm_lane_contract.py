from __future__ import annotations

import argparse
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
RUNTIME_DIR = REPO_ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(RUNTIME_DIR))

import _remote_common as remote_common  # noqa: E402
import active_alarms  # noqa: E402
import mdbctl_remote  # noqa: E402
from openubmc_target_runtime.diagnostic_receipt import build_diagnostic_receipt  # noqa: E402
from openubmc_target_runtime.observation import selected_scope_complete  # noqa: E402
from openubmc_target_runtime.semantic_runtime import ObservationQuery  # noqa: E402


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
    def test_object_read_absence_is_a_successful_typed_fact_without_fallback(self) -> None:
        runner = RecordingRunner(
            [completed(returncode=1, stdout="Failed: Object does not exist.\n")]
        )
        args = argparse.Namespace(
            ip="bmc.example",
            debug_dump="",
            mode="auto",
            timeout=2,
        )

        result = mdbctl_remote.execute_mdb_query(
            args,
            ["lsprop", "Drive_1_010102"],
            {
                "user": "debug-user",
                "password": "",
                "port": 22,
                "identity_file": "",
            },
            ssh_runner=runner,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "observed_absent")
        self.assertEqual(
            result.fact,
            {
                "status": "observed_absent",
                "query": "lsprop Drive_1_010102",
                "object": "Drive_1_010102",
                "command_parts": ["lsprop", "Drive_1_010102"],
            },
        )
        self.assertEqual(len(runner.commands), 1)

    def test_absence_classification_does_not_generalize_to_class_or_failed_transport(
        self,
    ) -> None:
        cases = (
            (
                ["lsclass"],
                completed(returncode=1, stdout="Failed: Object does not exist.\n"),
                "object-not-found",
            ),
            (
                ["lsprop", "Drive_1_010102"],
                completed(returncode=255, stderr="ssh: connect failed"),
                "remote-command-failed",
            ),
            (
                ["getprop", "Drive_1_010102", "Iface", "Value"],
                completed(returncode=1, stderr="Permission denied"),
                "remote-command-failed",
            ),
            (
                ["lsmethod", "Drive_1_010102"],
                completed(
                    returncode=1,
                    stdout="Failed: org.freedesktop.DBus.Error.ServiceUnknown\n",
                ),
                "service-unknown",
            ),
        )
        for command, response, expected_code in cases:
            with self.subTest(command=command):
                runner = RecordingRunner([response])
                args = argparse.Namespace(
                    ip="bmc.example",
                    debug_dump="",
                    mode="login-shell",
                    timeout=2,
                )
                result = mdbctl_remote.execute_mdb_query(
                    args,
                    command,
                    {
                        "user": "debug-user",
                        "password": "",
                        "port": 22,
                        "identity_file": "",
                    },
                    ssh_runner=runner,
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.code, expected_code)
                self.assertIsNone(result.fact)

    def test_observed_absent_fact_remains_available_and_scope_complete(self) -> None:
        command = ["lsprop", "Drive_1_010102"]
        args = argparse.Namespace(
            ip="bmc.example", debug_dump="", mode="auto", timeout=2, compact_json=True
        )
        result = mdbctl_remote.execute_mdb_query(
            args,
            command,
            {"user": "debug-user", "password": "", "port": 22, "identity_file": ""},
            ssh_runner=RecordingRunner([completed(stdout="Object not found.\n")]),
        )
        child = mdbctl_remote.build_json_payload(
            args,
            command,
            ok=result.ok,
            code=result.code,
            returncode=result.returncode,
            selected_mode=result.selected_mode,
            stdout=result.stdout,
            stderr=result.stderr,
            attempts=list(result.attempts),
            hint=result.hint,
            transport=result.transport,
            fact=result.fact,
        )
        observation = {
            "ok": True,
            "observed_at": child["observed_at"],
            "freshness": {"status": "fresh"},
            "result": {"lanes": {"ssh": {"mdbctl": child}}},
        }
        receipt = build_diagnostic_receipt(
            "debug_run",
            observation,
            {"mdb_queries": ["lsprop Drive_1_010102"], "mdb_only": True},
            [],
            closeout_stage="diagnosis",
        )
        self.assertIsNotNone(receipt)
        public = receipt.to_public_dict()
        item = public["results"][0]
        self.assertEqual(item["status"], "available")
        self.assertEqual(item["value"]["fact"], result.fact)
        self.assertEqual(public["coverage"]["evaluable"], 1)
        self.assertEqual(public["coverage"]["not_checked"], 0)
        self.assertEqual(public["coverage"]["unavailable"], 0)
        query = ObservationQuery.from_query(
            {
                "target": "bmc.example",
                "selectors": [
                    {
                        "id": "drive",
                        "kind": "mdb",
                        "queries": ["lsprop Drive_1_010102"],
                    }
                ],
            }
        )
        self.assertTrue(selected_scope_complete(observation, query))

    def test_partial_or_mixed_absence_output_is_not_a_fact(self) -> None:
        for attribute in ("timed_out", "output_limit_exceeded", "capture_failed"):
            with self.subTest(attribute=attribute):
                response = completed(stdout="Object not found.\n")
                setattr(response, attribute, True)
                self.assertIsNone(
                    mdbctl_remote.observed_absent_fact(
                        ["lsprop", "Drive0"],
                        classification="object-not-found",
                        completed=response,
                        stdout=response.stdout,
                        stderr=response.stderr,
                    )
                )
        for stdout, stderr in (
            ("Object not found.", "Permission denied"),
            ("Object not found.\nFailed: Permission denied", ""),
            ("note: Object not found.", ""),
            ("", "Object not found."),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                self.assertIsNone(
                    mdbctl_remote.observed_absent_fact(
                        ["lsprop", "Drive0"],
                        classification="object-not-found",
                        completed=completed(stdout=stdout, stderr=stderr),
                        stdout=stdout,
                        stderr=stderr,
                    )
                )

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
