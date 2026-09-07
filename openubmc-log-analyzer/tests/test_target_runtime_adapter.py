from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import dataclass
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pull_bundle  # noqa: E402
import target_runtime_adapter  # noqa: E402

runtime_pull_bundle = target_runtime_adapter.pull_bundle


def args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "ip": "bmc.example",
        "transport": "auto",
        "ssh_port": 22,
        "ssh_user": "Administrator",
        "ssh_user_env": "",
        "ssh_password": "ssh-secret",
        "ssh_password_env": "",
        "ssh_identity_file": "",
        "redfish_port": 443,
        "redfish_user": "Administrator",
        "redfish_user_env": "",
        "redfish_password": "redfish-secret",
        "redfish_password_env": "",
        "redfish_proxy": "auto",
        "redfish_timeout": 10,
        "redfish_manager_id": "1",
        "redfish_action": "dump",
        "redfish_task_timeout": 60,
        "redfish_poll_interval": 1,
        "remote_path": "",
        "remote_command": "",
        "search_timeout": 10,
        "generate_timeout": 20,
        "download_timeout": 30,
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@dataclass
class FakeRedfishSession:
    number: int
    closed: bool = False


class CallbackRedfishTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.operations: list[str] = []
        self.credentials: list[object] = []

    def open_session(self, *, target, credentials) -> FakeRedfishSession:
        del target
        self.opens += 1
        self.credentials.append(credentials)
        return FakeRedfishSession(self.opens)

    def request(self, session, operation: str, **kwargs):
        self.operations.append(operation)
        return kwargs["callback"](session)

    @staticmethod
    def is_authentication_failure(error: BaseException) -> bool:
        return (
            isinstance(error, runtime_pull_bundle.BundlePullError)
            and error.code == "redfish_auth_failed"
        )

    @staticmethod
    def close_session(session: FakeRedfishSession) -> None:
        session.closed = True


@dataclass
class FakeSshMaster:
    number: int
    closed: bool = False


class ScriptedSshTransport:
    def __init__(self, channel_results: list[subprocess.CompletedProcess[str]]) -> None:
        self.channel_results = list(channel_results)
        self.opens = 0
        self.commands: list[str] = []
        self.downloads: list[tuple[str, str]] = []

    def open_master(self, *, target, credentials) -> FakeSshMaster:
        self.opens += 1
        return FakeSshMaster(self.opens)

    @staticmethod
    def check_master(master: FakeSshMaster) -> bool:
        return not master.closed

    def run_channel(self, master, remote_command: str, **_kwargs):
        self.commands.append(remote_command)
        return self.channel_results.pop(0)

    @staticmethod
    def channel_lost_master(_master, _result) -> bool:
        return False

    def download_file(self, master, remote_path: str, local_path: str, **_kwargs):
        del master
        self.downloads.append((remote_path, local_path))
        Path(local_path).write_bytes(b"bundle")
        return subprocess.CompletedProcess(["scp"], 0, "", "")

    @staticmethod
    def close_master(master: FakeSshMaster) -> None:
        master.closed = True


class LogAnalyzerTargetRuntimeTests(unittest.TestCase):
    def test_orchestrated_bundle_accepts_selected_key_without_a_password(self) -> None:
        class KeyTransport(ScriptedSshTransport):
            def open_master(self, *, target, credentials):
                self.selected = credentials
                return super().open_master(target=target, credentials=credentials)

        ssh = KeyTransport([])
        runtime = target_runtime_adapter._load_runtime_module()
        backend = target_runtime_adapter.LogBundleMcpBackend(ssh_transport_factory=lambda _args: ssh)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'credentials.json'
            path.write_text(json.dumps({'schema_version': 1, 'credentials': {
                'key': {'user': 'fixture-user', 'identity_file': '/fixture/local-key'}},
                'defaults': {'bmc': {'ssh': 'key'}}}))
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {'OPENUBMC_CREDENTIALS_CONFIG': str(path)}, clear=True):
                service = runtime.RuntimeMcpService(runtime.OrchestratedMcpBackend({'log_bundle_collect': backend}))
                try:
                    result = service.call_tool('log_bundle_collect', {
                        'ip': '192.0.2.10', 'transport': 'ssh', 'extract': False,
                        'remote_path': '/tmp/fixture.tar.gz', 'deadline': 10,
                    }, task_id='key-only-bundle', operation_id='collect')
                finally:
                    service.close()
        self.assertTrue(result['ok'], result)
        self.assertEqual(ssh.opens, 1)
        self.assertEqual(ssh.selected.identity_file, '/fixture/local-key')
        self.assertEqual(ssh.selected.password, '')

    def test_orchestrated_bundle_uses_credentials_file_without_manual_exports(self) -> None:
        redfish = CallbackRedfishTransport()
        manager = {"UUID": "machine-a", "FirmwareVersion": "1.0"}
        runtime = target_runtime_adapter._load_runtime_module()
        backend = target_runtime_adapter.LogBundleMcpBackend(
            redfish_transport_factory=lambda _args: redfish,
            ssh_transport_factory=lambda _args: ScriptedSshTransport([]),
        )
        with tempfile.TemporaryDirectory() as raw:
            bundle_path = Path(raw) / "dump.tar.gz"
            bundle_path.write_bytes(b"bundle")
            stage = runtime_pull_bundle.BundleStageResult(
                remote_bundle_path="/tmp/dump.tar.gz",
                local_bundle_path=bundle_path,
                generation_ran=True,
                transport="redfish",
            )
            credentials_path = Path(raw) / "credentials.env"
            credentials_path.write_text(
                "REDFISH_USERNAME=file-user\n"
                "REDFISH_PASSWORD=file-password\n"
                "OPENUBMC_SSH_USER=ssh-file-user\n"
                "OPENUBMC_SSH_PASSWORD=ssh-file-password\n",
                encoding="utf-8",
            )
            credentials_path.chmod(0o600)
            with (
                mock.patch.dict(
                    os.environ,
                    {"OPENUBMC_CREDENTIALS_FILE": str(credentials_path)},
                    clear=False,
                ),
                mock.patch.object(
                    runtime_pull_bundle,
                    "redfish_request_json",
                    return_value=manager,
                ),
                mock.patch.object(
                    runtime_pull_bundle,
                    "run_redfish_bundle_flow_with_session",
                    return_value=stage,
                ),
            ):
                service = runtime.RuntimeMcpService(
                    runtime.OrchestratedMcpBackend(
                        {"log_bundle_collect": backend}
                    )
                )
                try:
                    result = service.call_tool(
                        "log_bundle_collect",
                        {
                            "ip": "bmc.example",
                            "transport": "redfish",
                            "extract": False,
                            "deadline": 10,
                        },
                        task_id="credential-file-bundle-task",
                        operation_id="credential-file-bundle-operation",
                    )
                finally:
                    service.close()

        self.assertTrue(result["ok"])
        self.assertEqual(redfish.opens, 1)
        self.assertEqual(redfish.credentials[0].user, "file-user")
        self.assertEqual(redfish.credentials[0].password, "file-password")

    def test_redfish_only_collection_ignores_unused_ssh_record_and_rejects_alias_conflicts(self):
        runtime = target_runtime_adapter._load_runtime_module()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'credentials.json'
            path.write_text(json.dumps({'schema_version': 1, 'credentials': {
                'web': {'user': 'fixture-user', 'password': 'fixture-password'},
                'unused': {'password': 'incomplete-unused'}},
                'defaults': {'bmc': {'redfish': 'web', 'ssh': 'unused'}}}))
            path.chmod(0o600)
            bundle = Path(raw) / 'bundle.tar.gz'; bundle.write_bytes(b'bundle')
            cases = [({'OPENUBMC_CREDENTIALS_CONFIG': str(path)}, True),
                     ({'OPENUBMC_REDFISH_USER': 'fixture-user', 'OPENUBMC_REDFISH_PASSWORD': 'one-fixture', 'REDFISH_PASSWORD': 'other-fixture'}, False)]
            for environment, success in cases:
                with self.subTest(success=success):
                    redfish = CallbackRedfishTransport()
                    backend = target_runtime_adapter.LogBundleMcpBackend(redfish_transport_factory=lambda _args: redfish)
                    with (mock.patch.dict(os.environ, {**environment, 'XDG_CONFIG_HOME': raw, 'HOME': raw}, clear=True),
                          mock.patch.object(runtime_pull_bundle, 'redfish_request_json', return_value={'UUID': 'fixture', 'FirmwareVersion': '1'}),
                          mock.patch.object(runtime_pull_bundle, 'run_redfish_bundle_flow_with_session', return_value=runtime_pull_bundle.BundleStageResult(remote_bundle_path='/tmp/bundle.tar.gz', local_bundle_path=bundle, generation_ran=True, transport='redfish'))):
                        service = runtime.RuntimeMcpService(runtime.OrchestratedMcpBackend({'log_bundle_collect': backend}))
                        try:
                            result = service.call_tool('log_bundle_collect', {'ip': '192.0.2.10', 'transport': 'redfish', 'extract': False, 'deadline': 10}, task_id='redfish-only', operation_id='collect')
                        finally:
                            service.close()
                    self.assertEqual(result['ok'], success, result)
                    self.assertEqual(redfish.opens, int(success))
                    if not success:
                        self.assertEqual(result['canonical_error']['code'], 'credentials_conflict')

    def test_task_reuses_one_lease_under_concurrent_same_target_calls(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        created: list[object] = []

        def open_lease(**_kwargs):
            lease = mock.MagicMock()
            created.append(lease)
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return lease

        task = target_runtime_adapter.LogBundleMcpTask(
            "parallel-log-task",
            redfish_transport_factory=None,
            ssh_transport_factory=None,
        )
        same_target = args()
        with (
            mock.patch.object(
                target_runtime_adapter,
                "open_log_bundle_runtime_lease",
                side_effect=open_lease,
            ),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            futures = [pool.submit(task.lease_for, same_target) for _ in range(4)]
            self.assertTrue(entered.wait(timeout=2))
            release.set()
            leases = [future.result(timeout=2) for future in futures]

        try:
            self.assertEqual(len(created), 1)
            self.assertTrue(all(lease is leases[0] for lease in leases))
        finally:
            task.close()

    def test_task_lru_eviction_closes_only_the_evicted_lease(self) -> None:
        first = mock.MagicMock()
        second = mock.MagicMock()
        task = target_runtime_adapter.LogBundleMcpTask(
            "log-lru-task",
            redfish_transport_factory=None,
            ssh_transport_factory=None,
            max_cached_leases=1,
        )
        with mock.patch.object(
            target_runtime_adapter,
            "open_log_bundle_runtime_lease",
            side_effect=[first, second],
        ):
            self.assertIs(task.lease_for(args(ip="bmc-a.example")), first)
            self.assertIs(task.lease_for(args(ip="bmc-b.example")), second)

        self.assertEqual(task.status()["lease_evictions"], 1)
        first.close.assert_called_once_with()
        second.close.assert_not_called()
        task.close()
        second.close.assert_called_once_with()

    def test_lease_identity_changes_when_direct_password_changes(self) -> None:
        first = target_runtime_adapter._mcp_parse_args(
            {
                "ip": "bmc.example",
                "ssh_password": "password-a",
                "redfish_password": "redfish-a",
            }
        )
        second = target_runtime_adapter._mcp_parse_args(
            {
                "ip": "bmc.example",
                "ssh_password": "password-b",
                "redfish_password": "redfish-a",
            }
        )
        self.assertNotEqual(
            target_runtime_adapter._lease_key(first),
            target_runtime_adapter._lease_key(second),
        )

    def test_mcp_log_bundle_tool_reuses_task_owned_session(self) -> None:
        redfish = CallbackRedfishTransport()
        manager = {"UUID": "machine-a", "FirmwareVersion": "1.0"}
        runtime = target_runtime_adapter._load_runtime_module()
        backend = target_runtime_adapter.LogBundleMcpBackend(
            redfish_transport_factory=lambda _args: redfish,
            ssh_transport_factory=lambda _args: ScriptedSshTransport([]),
        )
        with tempfile.TemporaryDirectory() as raw:
            bundle_path = Path(raw) / "dump.tar.gz"
            bundle_path.write_bytes(b"bundle")
            stage = runtime_pull_bundle.BundleStageResult(
                remote_bundle_path="/tmp/dump.tar.gz",
                local_bundle_path=bundle_path,
                generation_ran=True,
                transport="redfish",
            )
            with (
                mock.patch.object(
                    runtime_pull_bundle,
                    "redfish_request_json",
                    return_value=manager,
                ),
                mock.patch.object(
                    runtime_pull_bundle,
                    "run_redfish_bundle_flow_with_session",
                    return_value=stage,
                ),
            ):
                service = runtime.RuntimeMcpService(backend)
                try:
                    names = [tool["name"] for tool in service.tool_definitions()]
                    first = service.call_tool(
                        "log_bundle_collect",
                        {
                            "ip": "bmc.example",
                            "transport": "redfish",
                            "redfish_password": "redfish-secret",
                            "extract": False,
                            "deadline": 10,
                        },
                        task_id="codex-task",
                        operation_id="bundle-1",
                    )
                    second = service.call_tool(
                        "log_bundle_collect",
                        {
                            "ip": "bmc.example",
                            "transport": "redfish",
                            "redfish_password": "redfish-secret",
                            "extract": False,
                            "deadline": 10,
                        },
                        task_id="codex-task",
                        operation_id="bundle-2",
                    )
                finally:
                    service.close()

        self.assertEqual(names, ["observe", "execute"])
        self.assertEqual(first["result"]["transport"], "redfish")
        self.assertEqual(second["result"]["transport"], "redfish")
        self.assertEqual(redfish.opens, 1)

    def test_public_cli_is_v1_only_even_if_legacy_env_remains(self) -> None:
        stage = runtime_pull_bundle.BundleStageResult(
            remote_bundle_path="/tmp/dump.tar.gz",
            local_bundle_path=Path("/tmp/dump.tar.gz"),
            generation_ran=True,
            transport="redfish",
        )
        lease = mock.MagicMock()
        lease.__enter__.return_value = lease
        lease.collect.return_value = stage
        output = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"OPENUBMC_TARGET_RUNTIME_ENGINE": "legacy"},
                clear=False,
            ),
            mock.patch.object(
                target_runtime_adapter,
                "open_log_bundle_runtime_lease",
                return_value=lease,
            ) as open_lease,
            mock.patch.object(pull_bundle, "run_redfish_bundle_flow") as legacy_redfish,
            mock.patch.object(pull_bundle, "run_ssh_bundle_flow") as legacy_ssh,
            redirect_stdout(output),
        ):
            returncode = pull_bundle.main(
                [
                    "--ip",
                    "bmc.example",
                    "--transport",
                    "redfish",
                    "--no-extract",
                    "--json",
                ]
            )

        self.assertEqual(returncode, 0)
        self.assertEqual(json.loads(output.getvalue())["result"]["transport"], "redfish")
        open_lease.assert_called_once()
        legacy_redfish.assert_not_called()
        legacy_ssh.assert_not_called()

    def test_redfish_primary_reuses_one_domain_session_across_followups(self) -> None:
        redfish = CallbackRedfishTransport()
        seen_sessions: list[int] = []
        stage = pull_bundle.BundleStageResult(
            remote_bundle_path="/tmp/dump.tar.gz",
            local_bundle_path=Path("/tmp/dump.tar.gz"),
            generation_ran=True,
            transport="redfish",
        )
        manager = {
            "Id": "1",
            "UUID": "machine-a",
            "FirmwareVersion": "1.0",
        }
        with (
            mock.patch.object(
                runtime_pull_bundle,
                "redfish_request_json",
                return_value=manager,
            ),
            mock.patch.object(
                runtime_pull_bundle,
                "run_redfish_bundle_flow_with_session",
                side_effect=lambda _args, *, ip, local_dir, session, manager_payload: (
                    seen_sessions.append(session.number) or stage
                ),
            ),
        ):
            lease = target_runtime_adapter.LogBundleRuntimeLease(
                args=args(),
                task_id="mcp-task",
                redfish_transport=redfish,
                ssh_transport=ScriptedSshTransport([]),
            )
            try:
                first = lease.collect(
                    local_dir=Path("/tmp/one"),
                    search_roots=["/tmp"],
                    name_globs=["*.tar.gz"],
                )
                second = lease.collect(
                    local_dir=Path("/tmp/two"),
                    search_roots=["/tmp"],
                    name_globs=["*.tar.gz"],
                )
            finally:
                lease.close()

        self.assertEqual((first.transport, second.transport), ("redfish", "redfish"))
        self.assertEqual(redfish.opens, 1)
        self.assertEqual(seen_sessions, [1, 1])
        self.assertEqual(
            redfish.operations,
            [
                "log-analyzer-manager-identity",
                "log-analyzer-bundle-collect",
                "log-analyzer-manager-identity",
                "log-analyzer-bundle-collect",
            ],
        )

    def test_auto_falls_back_only_to_public_ssh_bundle_lane(self) -> None:
        redfish = CallbackRedfishTransport()
        ssh = ScriptedSshTransport(
            [
                subprocess.CompletedProcess(
                    ["ssh"],
                    0,
                    "/tmp/openUBMC-dump.tar.gz\n",
                    "",
                )
            ]
        )
        with mock.patch.object(
            runtime_pull_bundle,
            "redfish_request_json",
            side_effect=runtime_pull_bundle.BundlePullError(
                "redfish_auth_failed",
                "authentication rejected",
            ),
        ):
            with tempfile.TemporaryDirectory() as raw:
                lease = target_runtime_adapter.LogBundleRuntimeLease(
                    args=args(),
                    task_id="one-shot-task",
                    redfish_transport=redfish,
                    ssh_transport=ssh,
                )
                try:
                    result = lease.collect(
                        local_dir=Path(raw),
                        search_roots=["/tmp"],
                        name_globs=["*.tar.gz"],
                    )
                finally:
                    status = lease.runtime_status()
                    lease.close()

        self.assertEqual(result.transport, "ssh")
        self.assertEqual(ssh.opens, 1)
        self.assertEqual(len(ssh.commands), 1)
        self.assertEqual(ssh.downloads[0][0], "/tmp/openUBMC-dump.tar.gz")
        targets = status["targets"]
        self.assertTrue(any(target["ssh_leases"] for target in targets))
        self.assertTrue(all(not target["telnet_leases"] for target in targets))

    def test_forced_redfish_does_not_open_ssh_on_failure(self) -> None:
        redfish = CallbackRedfishTransport()
        ssh = ScriptedSshTransport([])
        with mock.patch.object(
            runtime_pull_bundle,
            "redfish_request_json",
            side_effect=runtime_pull_bundle.BundlePullError(
                "redfish_auth_failed",
                "authentication rejected",
            ),
        ):
            lease = target_runtime_adapter.LogBundleRuntimeLease(
                args=args(transport="redfish"),
                task_id="forced-redfish",
                redfish_transport=redfish,
                ssh_transport=ssh,
            )
            try:
                with self.assertRaises(runtime_pull_bundle.BundlePullError):
                    lease.collect(
                        local_dir=Path("/tmp"),
                        search_roots=["/tmp"],
                        name_globs=["*.tar.gz"],
                    )
            finally:
                lease.close()

        self.assertEqual(ssh.opens, 0)


if __name__ == "__main__":
    unittest.main()
