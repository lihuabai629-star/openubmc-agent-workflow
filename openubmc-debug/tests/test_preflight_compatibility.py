from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _preflight_checks  # noqa: E402
import _remote_common  # noqa: E402
import preflight_remote  # noqa: E402
import workflow_remote  # noqa: E402


class PreflightCompatibilityTests(unittest.TestCase):
    def test_public_preflight_modules_remain_importable(self) -> None:
        expected = {
            "preflight_checks": "check_ssh",
            "preflight_recommendations": "build_check_result",
        }
        for name, public_function in expected.items():
            path = SCRIPTS / f"{name}.py"
            spec = importlib.util.spec_from_file_location(
                f"openubmc_debug_public_{name}", path
            )
            self.assertIsNotNone(spec)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertTrue(callable(getattr(module, public_function)))

    def test_public_preflight_entrypoints_remain_executable(self) -> None:
        for name in ("preflight_checks", "preflight_recommendations"):
            path = SCRIPTS / f"{name}.py"
            self.assertNotEqual(
                stat.S_IMODE(path.stat().st_mode) & 0o111,
                0,
                f"{path} lost its executable entrypoint",
            )
            completed = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


class PreflightRuntimeFailureTests(unittest.TestCase):
    def _run_typed_json_preflight(
        self,
        requested: list[str],
    ) -> dict[str, object]:
        command = [
            sys.executable,
            str(SCRIPTS / "preflight_remote.py"),
            "--ip",
            "192.0.2.130",
            *(item for name in requested for item in ("--check", name)),
            "--json",
            "--compact-json",
        ]

        def invoke(executed_command: list[str]) -> int:
            args = preflight_remote.parse_args(
                workflow_remote._command_argv(executed_command)
            )
            return preflight_remote.execute_preflight(
                args,
                {
                    "user": "debug-user",
                    "password": "",
                    "port": 22,
                    "identity_file": "",
                },
                {"user": "", "password": "", "port": 23},
            )

        return workflow_remote.run_python_json_tool(
            "preflight_start",
            command,
            invoke,
            30,
        )

    def test_explicit_ssh_preflight_has_one_success_contract(self) -> None:
        with mock.patch.object(
            preflight_remote,
            "check_ssh",
            return_value=(True, ["ssh ready"]),
        ):
            result = self._run_typed_json_preflight(["SSH"])

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "ok")
        self.assertEqual(result["returncode"], 0)
        payload = result["payload"]
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["returncode"], 0)
        self.assertTrue(payload["result"]["overall_ok"])
        self.assertEqual(payload["result"]["overall_code"], "ok")

    def test_explicit_preflight_partial_result_keeps_a_valid_contract(self) -> None:
        with (
            mock.patch.object(
                preflight_remote,
                "check_ssh",
                return_value=(True, ["ssh ready"]),
            ),
            mock.patch.object(
                preflight_remote,
                "check_mdbctl",
                return_value=(False, ["mdb unavailable"]),
            ),
        ):
            result = self._run_typed_json_preflight(["MDBCTL"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "partial")
        self.assertEqual(result["returncode"], 1)
        self.assertNotEqual(result["code"], "invalid_tool_contract")
        payload = result["payload"]
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["returncode"], 1)
        self.assertFalse(payload["result"]["overall_ok"])
        self.assertEqual(payload["result"]["overall_code"], "partial")

    def test_explicit_preflight_checks_only_probe_requested_surfaces(self) -> None:
        credentials = {
            "user": "debug-user",
            "password": "",
            "port": 22,
            "identity_file": "",
        }
        telnet = {"user": "", "password": "", "port": 23}
        cases = (
            (["SSH"], {"SSH"}),
            (["TELNET"], {"TELNET", "LOG_FILES"}),
            (["MDBCTL"], {"SSH", "MDBCTL"}),
            (["DBUS_ENV"], {"SSH", "DBUS_ENV"}),
            (["BUSCTL"], {"SSH", "DBUS_ENV", "BUSCTL"}),
        )
        for requested, expected_results in cases:
            with self.subTest(requested=requested):
                args = preflight_remote.parse_args(
                    [
                        "--ip",
                        "192.0.2.130",
                        *(item for name in requested for item in ("--check", name)),
                    ]
                )
                with (
                    mock.patch.object(
                        preflight_remote,
                        "check_ssh",
                        return_value=(True, ["ssh ready"]),
                    ) as ssh_check,
                    mock.patch.object(
                        preflight_remote,
                        "check_mdbctl",
                        return_value=(True, ["mdb ready"]),
                    ) as mdb_check,
                    mock.patch.object(
                        preflight_remote,
                        "check_dbus_env",
                        return_value=(True, ["dbus ready"], {"DBUS": "fixture"}),
                    ) as dbus_check,
                    mock.patch.object(
                        preflight_remote,
                        "check_busctl",
                        return_value=(True, ["bus ready"]),
                    ) as bus_check,
                    mock.patch.object(
                        preflight_remote,
                        "check_telnet",
                        return_value=(True, ["telnet ready"], True),
                    ) as telnet_check,
                ):
                    raw = preflight_remote.run_preflight_checks(
                        args,
                        credentials,
                        telnet,
                    )

                self.assertEqual(set(raw), expected_results)
                expected_calls = preflight_remote._selected_preflight_checks(args)
                self.assertEqual(ssh_check.call_count, int("SSH" in expected_calls))
                self.assertEqual(
                    mdb_check.call_count, int("MDBCTL" in expected_calls)
                )
                self.assertEqual(
                    dbus_check.call_count, int("DBUS_ENV" in expected_calls)
                )
                self.assertEqual(
                    bus_check.call_count, int("BUSCTL" in expected_calls)
                )
                self.assertEqual(
                    telnet_check.call_count, int("TELNET" in expected_calls)
                )

    def test_telnet_only_assurance_refresh_does_not_probe_ssh(self) -> None:
        args = preflight_remote.parse_args(
            ["--ip", "192.0.2.130", "--check", "TELNET"]
        )
        base_checks = {
            "SSH": {"ok": True},
            "MDBCTL": {"ok": True},
            "TELNET": {"ok": True},
            "LOG_FILES": {"ok": True},
        }
        with (
            mock.patch.object(preflight_remote, "check_ssh") as ssh_check,
            mock.patch.object(
                preflight_remote,
                "check_telnet_time",
                return_value=(True, ["telnet refreshed"]),
            ) as telnet_check,
        ):
            refreshed = preflight_remote.refresh_preflight_checks(
                args,
                {"user": "", "password": "", "port": 22},
                {"user": "", "password": "", "port": 23},
                base_checks,
            )

        ssh_check.assert_not_called()
        telnet_check.assert_called_once()
        self.assertEqual(set(refreshed), {"TELNET", "LOG_FILES"})

    def test_preflight_observer_reports_mdb_before_slow_dbus_finishes(self) -> None:
        args = preflight_remote.parse_args(
            ["--ip", "192.0.2.130", "--skip-telnet"]
        )
        release_dbus = threading.Event()
        mdb_ready = threading.Event()
        observations: list[str] = []
        completed: list[dict[str, tuple]] = []

        def slow_dbus(*_args, **_kwargs):
            release_dbus.wait(timeout=2)
            return True, ["dbus ready"], {"DBUS_SESSION_BUS_ADDRESS": "fixture"}

        def observe(name: str, _result) -> None:
            observations.append(name)
            if name == "MDBCTL":
                mdb_ready.set()

        def run() -> None:
            completed.append(
                preflight_remote.run_preflight_checks(
                    args,
                    {
                        "user": "debug-user",
                        "password": "",
                        "port": 22,
                        "identity_file": "",
                    },
                    {"user": "", "password": "", "port": 23},
                    check_observer=observe,
                )
            )

        with (
            mock.patch.object(
                preflight_remote,
                "check_ssh",
                return_value=(True, ["ssh ready"]),
            ),
            mock.patch.object(
                preflight_remote,
                "check_mdbctl",
                return_value=(True, ["mdb ready"]),
            ),
            mock.patch.object(
                preflight_remote,
                "check_dbus_env",
                side_effect=slow_dbus,
            ),
            mock.patch.object(
                preflight_remote,
                "check_busctl",
                return_value=(True, ["bus ready"]),
            ),
        ):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(mdb_ready.wait(timeout=1))
            self.assertTrue(worker.is_alive())
            release_dbus.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(completed), 1)
        self.assertIn("MDBCTL", observations)
        self.assertIn("BUSCTL", observations)

    def test_mdb_only_preflight_skips_dbus_busctl_and_telnet(self) -> None:
        args = preflight_remote.parse_args(
            ["--ip", "192.0.2.130", "--mdb-only"]
        )
        args.skip_telnet = True
        with (
            mock.patch.object(
                preflight_remote,
                "check_ssh",
                return_value=(True, ["2026-08-03 00:00:00 +0000"]),
            ) as ssh_check,
            mock.patch.object(
                preflight_remote,
                "check_mdbctl",
                return_value=(True, ["PowerStrategy"]),
            ) as mdb_check,
            mock.patch.object(preflight_remote, "check_dbus_env") as dbus_check,
            mock.patch.object(preflight_remote, "check_busctl") as bus_check,
            mock.patch.object(preflight_remote, "check_telnet") as telnet_check,
        ):
            raw = preflight_remote.run_preflight_checks(
                args,
                {
                    "user": "debug-user",
                    "password": "",
                    "port": 22,
                    "identity_file": "",
                },
                {"user": "", "password": "", "port": 23},
            )

        self.assertEqual(set(raw), {"SSH", "MDBCTL"})
        ssh_check.assert_called_once()
        mdb_check.assert_called_once()
        dbus_check.assert_not_called()
        bus_check.assert_not_called()
        telnet_check.assert_not_called()
        checks = preflight_remote.build_checks(args, raw)
        summary = preflight_remote.summarize_capabilities(checks)
        self.assertTrue(summary["capabilities"]["remote_object"])
        self.assertTrue(summary["capabilities"]["mdbctl"])
        self.assertFalse(summary["capabilities"]["busctl"])

    def test_public_preflight_reuses_one_typed_runtime_lease(self) -> None:
        args = preflight_remote.parse_args(
            [
                "--ip",
                "192.0.2.130",
                "--ssh-timeout",
                "9",
                "--telnet-connect-timeout",
                "7",
                "--telnet-prompt-timeout",
                "5",
            ]
        )
        credentials = {
            "ssh": {
                "user": "debug-user",
                "password": "debug-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "debug-user",
                "password": "debug-password",
                "port": 23,
            },
        }

        class FakeLease:
            object_alarm_lease = mock.sentinel.object_alarm_lease
            telnet_session = mock.sentinel.telnet_session

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            @staticmethod
            def ssh_credentials_mapping():
                return credentials["ssh"]

            @staticmethod
            def telnet_credentials_mapping():
                return credentials["telnet"]

        lease = FakeLease()
        with (
            mock.patch.object(
                preflight_remote,
                "resolve_debug_credentials",
                return_value=credentials,
            ) as resolve,
            mock.patch.object(
                preflight_remote,
                "open_debug_runtime_lease",
                return_value=lease,
            ) as open_lease,
            mock.patch.object(
                preflight_remote,
                "execute_preflight",
                return_value=0,
            ) as execute,
        ):
            returncode = preflight_remote.run_typed_preflight_one_shot(args)

        self.assertEqual(returncode, 0)
        resolve.assert_called_once_with(args, include_telnet=True)
        self.assertEqual(args.timeout, 9)
        self.assertEqual(
            open_lease.call_args.kwargs["credential_bundle"],
            credentials,
        )
        execute.assert_called_once_with(
            args,
            credentials["ssh"],
            credentials["telnet"],
            object_alarm_lease=mock.sentinel.object_alarm_lease,
            telnet_session=mock.sentinel.telnet_session,
        )

    def test_cli_main_selects_typed_preflight_without_injected_resources(self) -> None:
        args = preflight_remote.parse_args(["--ip", "192.0.2.130"])
        with (
            mock.patch.object(preflight_remote, "parse_args", return_value=args),
            mock.patch.object(
                preflight_remote,
                "run_typed_preflight_one_shot",
                return_value=0,
            ) as run_typed,
        ):
            returncode = preflight_remote.main()

        self.assertEqual(returncode, 0)
        run_typed.assert_called_once_with(args)

    def test_explicit_ssh_check_does_not_open_a_telnet_lease(self) -> None:
        args = preflight_remote.parse_args(
            ["--ip", "192.0.2.130", "--check", "SSH"]
        )
        credentials = {
            "ssh": {
                "user": "debug-user",
                "password": "debug-password",
                "port": 22,
                "identity_file": "",
            },
            "telnet": {
                "user": "",
                "password": "",
                "port": 23,
            },
        }

        class FakeLease:
            object_alarm_lease = mock.sentinel.object_alarm_lease
            telnet_session = None

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            @staticmethod
            def ssh_credentials_mapping():
                return credentials["ssh"]

            @staticmethod
            def telnet_credentials_mapping():
                return credentials["telnet"]

        with (
            mock.patch.object(
                preflight_remote,
                "resolve_debug_credentials",
                return_value=credentials,
            ) as resolve,
            mock.patch.object(
                preflight_remote,
                "open_debug_runtime_lease",
                return_value=FakeLease(),
            ) as open_lease,
            mock.patch.object(
                preflight_remote,
                "execute_preflight",
                return_value=0,
            ),
        ):
            returncode = preflight_remote.run_typed_preflight_one_shot(args)

        self.assertEqual(returncode, 0)
        self.assertTrue(args.mdb_only)
        self.assertTrue(args.skip_telnet)
        resolve.assert_called_once_with(args, include_telnet=False)
        self.assertTrue(open_lease.call_args.kwargs["args"].skip_telnet)

    def test_management_cli_rejection_disables_object_capability(self) -> None:
        with mock.patch.object(
            _preflight_checks,
            "run_ssh",
            return_value=subprocess.CompletedProcess(
                args=["ssh"],
                returncode=0,
                stdout="        COMMAND NOT SUPPORTED\n",
                stderr="",
            ),
        ):
            ok, lines = _preflight_checks.check_mdbctl(
                SimpleNamespace(ip="192.0.2.130", ssh_timeout=5),
                {
                    "user": "debug-user",
                    "password": "debug-password",
                    "port": 22,
                    "identity_file": "",
                },
            )

        self.assertFalse(ok)
        self.assertEqual(lines, ["COMMAND NOT SUPPORTED"])
        self.assertEqual(lines.failure_code, "remote-command-unsupported")
        args = preflight_remote.parse_args(["--ip", "192.0.2.130"])
        checks = preflight_remote.build_checks(
            args,
            {
                "SSH": (True, ["COMMAND NOT SUPPORTED", "iBMC:/->"]),
                "DBUS_ENV": (False, ["missing env"], {}),
                "MDBCTL": (ok, lines),
                "BUSCTL": (False, ["DBUS/XDG env not detected"]),
                "TELNET": (False, ["connection refused"]),
            },
        )
        summary = preflight_remote.summarize_capabilities(checks)
        self.assertFalse(summary["capabilities"]["mdbctl"])
        self.assertFalse(summary["capabilities"]["remote_object"])
        self.assertEqual(summary["overall_code"], "unavailable")
        self.assertEqual(checks["MDBCTL"]["code"], "remote-command-unsupported")
        self.assertEqual(checks["MDBCTL"]["recommended_command"], "")
        self.assertEqual(
            checks["DBUS_ENV"]["blocked_by"], "remote-command-unsupported"
        )
        self.assertEqual(checks["DBUS_ENV"]["recommended_command"], "")
        self.assertEqual(
            checks["BUSCTL"]["blocked_by"], "remote-command-unsupported"
        )
        self.assertEqual(checks["BUSCTL"]["recommended_command"], "")
        self.assertEqual(checks["SSH"]["recommended_command"], "")
        payload = preflight_remote.build_json_report_payload(args, checks)
        self.assertEqual(payload["result"]["recommended_command"], "")
        self.assertIn(
            "do not retry mdbctl or busctl",
            payload["result"]["recommended_next_step"],
        )

    def test_dbus_cache_converts_control_master_failure_to_audited_result(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=255,
            stdout="",
            stderr="Host key verification failed.",
        )
        completed.host_key_verification_failed = True
        completed.ssh_host_key_policy = "strict"
        completed.ssh_host_key_policy_source = "default"
        completed.ssh_known_hosts_source = "ssh_default"
        completed.ssh_transport_warnings = []

        def failed_cache(_loader):
            raise _remote_common.SshControlMasterOpenError(completed)

        ok, lines, env = _preflight_checks.check_dbus_env(
            SimpleNamespace(ip="192.0.2.90", ssh_timeout=5),
            {
                "user": "debug-user",
                "password": "debug-password",
                "port": 22,
                "identity_file": "",
            },
            environment_cache=failed_cache,
        )

        self.assertFalse(ok)
        self.assertEqual(env, {})
        self.assertEqual(lines, ["ssh_host_key_verification_failed"])
        self.assertEqual(
            lines.transport["failure_code"],
            "ssh_host_key_verification_failed",
        )
        self.assertTrue(lines.transport["host_key_verification_failed"])

    def test_dbus_cache_does_not_hide_unknown_failures(self) -> None:
        def failed_cache(_loader):
            raise RuntimeError("unexpected cache failure")

        with self.assertRaisesRegex(RuntimeError, "unexpected cache failure"):
            _preflight_checks.check_dbus_env(
                SimpleNamespace(ip="192.0.2.90", ssh_timeout=5),
                {
                    "user": "debug-user",
                    "password": "debug-password",
                    "port": 22,
                    "identity_file": "",
                },
                environment_cache=failed_cache,
            )


if __name__ == "__main__":
    unittest.main()
