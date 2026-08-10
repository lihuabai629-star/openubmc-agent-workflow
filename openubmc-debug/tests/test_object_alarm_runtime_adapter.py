from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from _target_runtime_adapter import open_object_alarm_lease  # noqa: E402
import _target_runtime_adapter as target_runtime_adapter  # noqa: E402
import active_alarms  # noqa: E402
import busctl_remote  # noqa: E402


DBUS_ENV_OUTPUT = (
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus\n"
    "XDG_RUNTIME_DIR=/run/user/0\n"
)
ALARM_XML = """<node>
  <interface name="org.example.Events">
    <method name="GetAlarmList">
      <arg type="a{ss}" direction="in"/>
      <arg type="q" direction="in"/>
      <arg type="q" direction="in"/>
      <arg type="a(ss)" direction="in"/>
      <arg type="qa(a(ss))" direction="out"/>
    </method>
  </interface>
</node>
"""
ALARM_JSON = json.dumps(
    {
        "type": "qa(a(ss))",
        "data": [
            1,
            [
                [["EventName", "FixtureAlarm"]],
                [["EventCode", "fixture-code"]],
                [["State", "Asserted"]],
            ],
        ],
    }
)


@dataclass
class FakeMaster:
    identifier: int
    alive: bool = True


class FakeControlMasterTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        self.commands: list[str] = []

    def open_master(self, *, target, credentials):
        self.opens += 1
        self.target = target
        self.credentials = credentials
        return FakeMaster(self.opens)

    def check_master(self, master: FakeMaster) -> bool:
        return master.alive

    def run_channel(self, master: FakeMaster, remote_command: str, **_kwargs):
        self.commands.append(remote_command)
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=f"{remote_command}\n",
            stderr="",
        )

    def channel_lost_master(self, _master, _result) -> bool:
        return False

    def close_master(self, master: FakeMaster) -> None:
        self.closes += 1
        master.alive = False


class RoutingControlMasterTransport(FakeControlMasterTransport):
    def run_channel(self, master: FakeMaster, remote_command: str, **_kwargs):
        self.commands.append(remote_command)
        if "printenv" in remote_command:
            stdout = DBUS_ENV_OUTPUT
        elif "--xml-interface introspect" in remote_command:
            stdout = ALARM_XML
        elif "--json=short call" in remote_command:
            stdout = ALARM_JSON
        elif "busctl --user --no-pager list" in remote_command:
            stdout = "NAME PID PROCESS\norg.example.events 3 eventsd\n"
        else:
            raise AssertionError(f"unexpected SSH channel: {remote_command}")
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=stdout,
            stderr="",
        )


class FallbackRoutingControlMasterTransport(RoutingControlMasterTransport):
    def run_channel(self, master: FakeMaster, remote_command: str, **_kwargs):
        self.commands.append(remote_command)
        if "printenv" in remote_command:
            stdout = DBUS_ENV_OUTPUT
        elif (
            "--xml-interface introspect" in remote_command
            and active_alarms.STANDARD_ALARM_SERVICE in remote_command
            and active_alarms.STANDARD_ALARM_PATH in remote_command
        ):
            stdout = "<node><interface name=\"org.example.Empty\"/></node>"
        elif "busctl --user --no-pager list" in remote_command:
            stdout = "NAME PID PROCESS\norg.example.events 3 eventsd\n"
        elif "busctl --user --no-pager tree" in remote_command:
            stdout = "/org/example/Events\n"
        elif "--xml-interface introspect" in remote_command:
            stdout = ALARM_XML
        elif "--json=short call" in remote_command:
            stdout = ALARM_JSON
        else:
            raise AssertionError(f"unexpected SSH channel: {remote_command}")
        return subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=stdout,
            stderr="",
        )


class StaleEndpointControlMasterTransport(RoutingControlMasterTransport):
    def __init__(self) -> None:
        super().__init__()
        self.introspection_calls = 0
        self.alarm_calls = 0

    def run_channel(self, master: FakeMaster, remote_command: str, **_kwargs):
        self.commands.append(remote_command)
        if "printenv" in remote_command:
            return subprocess.CompletedProcess(
                args=["ssh"], returncode=0, stdout=DBUS_ENV_OUTPUT, stderr=""
            )
        if "--xml-interface introspect" in remote_command:
            self.introspection_calls += 1
            xml = ALARM_XML.replace(
                "org.example.Events",
                f"org.example.EventsV{self.introspection_calls}",
            )
            return subprocess.CompletedProcess(
                args=["ssh"], returncode=0, stdout=xml, stderr=""
            )
        if "--json=short call" in remote_command:
            self.alarm_calls += 1
            if self.alarm_calls == 2:
                return subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=1,
                    stdout="",
                    stderr="Call failed: Unknown interface",
                )
            return subprocess.CompletedProcess(
                args=["ssh"], returncode=0, stdout=ALARM_JSON, stderr=""
            )
        raise AssertionError(f"unexpected SSH channel: {remote_command}")


class ObjectAlarmRuntimeAdapterTests(unittest.TestCase):
    def test_production_lease_reuses_transport_credentials_and_derived_state(self) -> None:
        credential_calls = 0
        env_calls = 0
        endpoint_calls = 0
        transport = FakeControlMasterTransport()
        args = argparse.Namespace(
            ip="bmc.example",
            ssh_user="debug-user",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
        )

        def load_credentials():
            nonlocal credential_calls
            credential_calls += 1
            return {
                "user": "debug-user",
                "password": "",
                "port": 22,
                "identity_file": "",
            }

        lease = open_object_alarm_lease(
            args=args,
            credential_loader=load_credentials,
            task_id="adapter-task",
            transport_factory=lambda **_kwargs: transport,
        )
        try:
            first = lease.run_read(
                request_id="mdb-request",
                collector_name="mdbctl",
                operation={"command": ["lsclass"]},
                collect=lambda ssh, context: context.ssh_runner(
                    args.ip,
                    str(ssh["user"]),
                    str(ssh["password"]),
                    "mdbctl lsclass",
                    2,
                    port=int(ssh["port"]),
                    identity_file=str(ssh["identity_file"]),
                ).stdout,
            )

            def load_env():
                nonlocal env_calls
                env_calls += 1
                return {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/0/bus"}

            def load_endpoint():
                nonlocal endpoint_calls
                endpoint_calls += 1
                return {"service": "alarm.service", "path": "/alarm"}

            env_first = lease.get_dbus_environment(load_env)
            env_second = lease.get_dbus_environment(load_env)
            endpoint_first = lease.get_alarm_endpoint(load_endpoint)
            endpoint_second = lease.get_alarm_endpoint(load_endpoint)
            second = lease.run_read(
                request_id="alarm-request",
                collector_name="active-alarms",
                operation={"method": "GetAlarmList"},
                collect=lambda ssh, context: context.ssh_runner(
                    args.ip,
                    str(ssh["user"]),
                    str(ssh["password"]),
                    "busctl call GetAlarmList",
                    2,
                    port=int(ssh["port"]),
                    identity_file=str(ssh["identity_file"]),
                ).stdout,
            )

            self.assertEqual(first, "mdbctl lsclass\n")
            self.assertEqual(second, "busctl call GetAlarmList\n")
            self.assertIs(env_first, env_second)
            self.assertIs(endpoint_first, endpoint_second)
            self.assertEqual(credential_calls, 1)
            self.assertEqual(transport.opens, 1)
            self.assertEqual(env_calls, 1)
            self.assertEqual(endpoint_calls, 1)
            self.assertEqual(
                lease.runtime_status()["metrics"]["ssh_channels"],
                2,
            )
        finally:
            lease.close()
        self.assertEqual(transport.closes, 1)

    def _invoke_public_collector(
        self,
        module,
        argv: list[str],
        *,
        transport: RoutingControlMasterTransport | None = None,
    ) -> tuple[int, dict[str, object], RoutingControlMasterTransport, int]:
        transport = transport or RoutingControlMasterTransport()
        credential_mapping = {
            "user": "debug-user",
            "password": "",
            "port": 22,
            "identity_file": "",
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                module,
                "resolve_ssh_credentials",
                return_value=credential_mapping,
            ) as credentials,
            mock.patch.object(
                target_runtime_adapter,
                "OpenSshControlMasterTransport",
                return_value=transport,
            ),
            mock.patch.object(
                module,
                "run_ssh",
                side_effect=lambda *args, **kwargs: transport.run_channel(
                    FakeMaster(0),
                    args[3],
                    **kwargs,
                ),
            ),
            mock.patch.object(module, "build_debug_dumper", return_value=None),
            contextlib.redirect_stdout(stdout),
        ):
            returncode = module.main()
        payload = json.loads(stdout.getvalue())
        payload.pop("observed_at", None)
        return returncode, payload, transport, credentials.call_count

    def test_busctl_public_v1_reuses_one_authentication_for_env_and_query(self) -> None:
        argv = [
            "busctl_remote.py",
            "--ip",
            "bmc.example",
            "--json",
            "--compact-json",
        ]
        result = self._invoke_public_collector(
            busctl_remote,
            argv,
        )

        self.assertEqual(result[2].opens, 1)
        self.assertEqual(len(result[2].commands), 2)
        self.assertEqual(result[3], 1)
        self.assertFalse(any("telnet" in command for command in result[2].commands))

    def test_active_alarm_public_v1_reuses_one_authentication_for_all_channels(self) -> None:
        argv = [
            "active_alarms.py",
            "--ip",
            "bmc.example",
            "--service",
            "org.example.events",
            "--path",
            "/org/example/Events",
            "--json",
            "--compact-json",
        ]
        result = self._invoke_public_collector(
            active_alarms,
            argv,
        )

        self.assertEqual(result[2].opens, 1)
        self.assertEqual(len(result[2].commands), 3)
        self.assertEqual(result[3], 1)
        self.assertEqual(result[1]["result"]["record_count"], 1)
        self.assertFalse(any("telnet" in command for command in result[2].commands))

    def test_active_alarm_public_v1_tries_the_standard_endpoint_first(self) -> None:
        argv = [
            "active_alarms.py",
            "--ip",
            "bmc.example",
            "--json",
            "--compact-json",
        ]
        result = self._invoke_public_collector(
            active_alarms,
            argv,
        )

        self.assertEqual(result[2].opens, 1)
        self.assertEqual(len(result[2].commands), 3)
        self.assertFalse(
            any("busctl --user --no-pager list" in command for command in result[2].commands)
        )
        self.assertFalse(
            any("busctl --user --no-pager tree" in command for command in result[2].commands)
        )
        self.assertEqual(
            result[1]["result"]["discovery"]["mode"],
            "standard-fast-path",
        )

    def test_active_alarm_falls_back_when_standard_endpoint_lacks_method(self) -> None:
        argv = [
            "active_alarms.py",
            "--ip",
            "bmc.example",
            "--json",
            "--compact-json",
        ]
        transport = FallbackRoutingControlMasterTransport()
        result = self._invoke_public_collector(
            active_alarms,
            argv,
            transport=transport,
        )

        self.assertEqual(result[2].opens, 1)
        self.assertEqual(len(result[2].commands), 6)
        self.assertTrue(
            any("busctl --user --no-pager list" in command for command in result[2].commands)
        )
        self.assertTrue(
            any("busctl --user --no-pager tree" in command for command in result[2].commands)
        )
        discovery = result[1]["result"]["discovery"]
        self.assertEqual(discovery["mode"], "automatic")
        self.assertFalse(discovery["standard_fast_path"]["ok"])
        self.assertEqual(
            discovery["standard_fast_path"]["code"],
            "get_alarm_list_missing",
        )
        self.assertEqual(result[1]["result"]["record_count"], 1)

    def test_active_alarm_runtime_lease_caches_discovery_but_reads_fresh_records(self) -> None:
        args = active_alarms.parse_args(
            ["--ip", "bmc.example", "--json", "--compact-json"]
        )
        credentials = {
            "user": "debug-user",
            "password": "",
            "port": 22,
            "identity_file": "",
        }
        transport = RoutingControlMasterTransport()
        lease = open_object_alarm_lease(
            args=args,
            credential_loader=lambda: credentials,
            task_id="alarm-cache-task",
            transport_factory=lambda **_kwargs: transport,
        )
        payloads = []
        try:
            for _index in range(2):
                stdout = io.StringIO()
                with (
                    mock.patch.object(
                        active_alarms, "build_debug_dumper", return_value=None
                    ),
                    contextlib.redirect_stdout(stdout),
                ):
                    returncode = active_alarms.main(
                        ssh_runner=lease.ssh_runner,
                        runtime_lease=lease,
                        _args=args,
                        _ssh=credentials,
                    )
                self.assertEqual(returncode, 0)
                payloads.append(json.loads(stdout.getvalue()))
        finally:
            lease.close()

        introspections = [
            command for command in transport.commands if "introspect" in command
        ]
        calls = [command for command in transport.commands if "GetAlarmList" in command]
        self.assertEqual(len(introspections), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item["result"]["record_count"] for item in payloads], [1, 1])

    def test_stale_cached_alarm_endpoint_is_rediscovered_once(self) -> None:
        args = active_alarms.parse_args(
            ["--ip", "bmc.example", "--json", "--compact-json"]
        )
        credentials = {
            "user": "debug-user",
            "password": "",
            "port": 22,
            "identity_file": "",
        }
        transport = StaleEndpointControlMasterTransport()
        lease = open_object_alarm_lease(
            args=args,
            credential_loader=lambda: credentials,
            task_id="alarm-stale-endpoint-task",
            transport_factory=lambda **_kwargs: transport,
        )
        try:
            for _index in range(2):
                stdout = io.StringIO()
                with (
                    mock.patch.object(
                        active_alarms, "build_debug_dumper", return_value=None
                    ),
                    contextlib.redirect_stdout(stdout),
                ):
                    returncode = active_alarms.main(
                        ssh_runner=lease.ssh_runner,
                        runtime_lease=lease,
                        _args=args,
                        _ssh=credentials,
                    )
                self.assertEqual(returncode, 0)
                self.assertEqual(json.loads(stdout.getvalue())["code"], "ok")
        finally:
            lease.close()

        self.assertEqual(transport.introspection_calls, 2)
        self.assertEqual(transport.alarm_calls, 3)
        self.assertEqual(transport.opens, 1)


if __name__ == "__main__":
    unittest.main()
