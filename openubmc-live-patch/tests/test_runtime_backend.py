from __future__ import annotations

import base64
import gzip
import hashlib
from pathlib import Path
import re
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-live-patch"))

from openubmc_target_runtime import (  # noqa: E402
    MutationAuthorizationDenied,
    MutationJournalStore,
    MutationOperationConflict,
    RuntimeMcpService,
    TelnetCommandResult,
)
from openubmc_target_runtime.capability import (  # noqa: E402
    EffectRecoveryMode,
)
from openubmc_live_patch.runtime_backend import (  # noqa: E402
    LivePatchMcpBackend,
    _LivePatchTask,
)


TEST_DEADLINE_SECONDS = 30


def decoded_shell_script(command: str) -> str:
    match = re.search(
        r"printf %s '?([A-Za-z0-9+/=]+)'?\|busybox base64 -d",
        command,
    )
    if match is None:
        raise AssertionError("compressed Live Patch script is unavailable")
    return gzip.decompress(base64.b64decode(match.group(1))).decode("utf-8")


class FakeSshTransport:
    def __init__(self) -> None:
        self.opens = 0
        self.uploads: list[tuple[str, str]] = []

    def open_master(self, *, target, credentials):
        self.opens += 1
        return object()

    @staticmethod
    def check_master(_master) -> bool:
        return True

    def upload_file(self, _master, local_path: str, remote_path: str, **_kwargs):
        self.uploads.append((local_path, remote_path))
        return type("UploadResult", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    @staticmethod
    def run_channel(_master, _command: str, **_kwargs):
        return type("ChannelResult", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    @staticmethod
    def channel_lost_master(_master, _result) -> bool:
        return False

    @staticmethod
    def close_master(_master) -> None:
        return None


class FakeTelnetTransport:
    def __init__(
        self,
        digest: str,
        *,
        target_exists: bool = False,
        target_mode: str = "440",
        target_uid: int = 104,
        target_gid: int = 104,
    ) -> None:
        self.digest = digest
        self.target_exists = target_exists
        self.target_mode = target_mode
        self.target_uid = target_uid
        self.target_gid = target_gid
        self.current_mode = target_mode if target_exists else "644"
        self.current_uid = target_uid if target_exists else 0
        self.current_gid = target_gid if target_exists else 0
        self.opens = 0
        self.commands: list[str] = []

    def open_session(self, *, target, credentials):
        self.opens += 1
        return object()

    def run_command(self, _session, command: str, **_kwargs):
        self.commands.append(command)
        if "live_patch_paths_safe" in command:
            stdout = "live_patch_paths_safe"
        elif "live_patch_codec_ready" in command:
            stdout = "live_patch_codec_ready"
        elif "/proc/mounts" in command:
            stdout = "rw,relatime"
        elif "live_patch_recovery_inspected" in command:
            stdout = (
                f"remote_sha256={self.digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\n"
                "remote_exists\nlive_patch_recovery_inspected"
                if self.target_exists
                else "remote_missing\nlive_patch_recovery_inspected"
            )
        elif "backup_exists" in command:
            stdout = (
                f"backup_sha256={self.digest}\n"
                f"backup_mode={self.target_mode}\n"
                f"backup_uid={self.target_uid}\n"
                f"backup_gid={self.target_gid}\nbackup_exists"
            )
        elif "live_patch_restart_observed" in command:
            stdout = "live_patch_restart_observed"
        elif "target_exists" in command:
            stdout = (
                f"{self.digest}  /opt/bmc/apps/demo/unit.lua\n"
                f"target_mode={self.target_mode}\n"
                f"target_uid={self.target_uid}\n"
                f"target_gid={self.target_gid}\n"
                "target_exists"
                if self.target_exists
                else "target_missing"
            )
        elif "p=b;" in command:
            stdout = (
                f"backup_sha256={self.digest}\n"
                f"backup_mode={self.target_mode}\n"
                f"backup_uid={self.target_uid}\n"
                f"backup_gid={self.target_gid}\nbackup_ok"
            )
        elif "p=r;" in command:
            script = decoded_shell_script(command)
            if "remove_ok" in script:
                self.target_exists = False
                stdout = f"removed_sha256={self.digest}\nremove_ok"
            else:
                mode = re.search(r"chmod ([0-7]{3,4}) ", script)
                self.current_mode = mode.group(1) if mode else self.target_mode
                self.current_uid = self.target_uid
                self.current_gid = self.target_gid
                stdout = (
                    f"backup_sha256={self.digest}\n"
                    f"remote_sha256={self.digest}\n"
                    f"backup_mode={self.target_mode}\n"
                    f"remote_mode={self.current_mode}\n"
                    f"backup_uid={self.target_uid}\n"
                    f"remote_uid={self.current_uid}\n"
                    f"backup_gid={self.target_gid}\n"
                    f"remote_gid={self.current_gid}\nrestore_ok"
                )
        elif "verify_missing" in command:
            stdout = "verify_missing" if not self.target_exists else "target_still_exists"
        elif "p=i;" in command:
            script = decoded_shell_script(command)
            mode = re.search(r"chmod ([0-7]{3,4}) ", script)
            owner = re.search(r"chown ([0-9]+):([0-9]+) ", script)
            self.target_exists = True
            self.current_mode = mode.group(1) if mode else "644"
            if owner:
                self.current_uid = int(owner.group(1))
                self.current_gid = int(owner.group(2))
            stdout = (
                f"remote_sha256={self.digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\ndeploy_ok"
            )
        elif "verify_sha256" in command:
            stdout = (
                f"remote_sha256={self.digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\nverify_sha256"
            )
        elif "restart_ok" in command:
            stdout = "restart_ok"
        else:
            stdout = "ok"
        return TelnetCommandResult(
            stdout=stdout,
            returncode=0,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            raw=stdout.encode(),
        )

    @staticmethod
    def command_invalidates_session(_session, result) -> bool:
        return not result.ok

    @staticmethod
    def close_session(_session) -> None:
        return None


class FailRestartOnceTelnetTransport(FakeTelnetTransport):
    def __init__(self, digest: str, **kwargs) -> None:
        super().__init__(digest, **kwargs)
        self.failed_restart = False

    def run_command(self, session, command: str, **kwargs):
        if "restart_ok" in command and not self.failed_restart:
            self.commands.append(command)
            self.failed_restart = True
            raise OSError("connection lost after Live Patch install")
        return super().run_command(session, command, **kwargs)


class MissingBackupTelnetTransport(FakeTelnetTransport):
    def run_command(self, session, command: str, **kwargs):
        if "backup_exists" in command:
            self.commands.append(command)
            return TelnetCommandResult(
                stdout="backup_missing",
                returncode=0,
                framing_complete=True,
                timed_out=False,
                connection_closed=False,
                raw=b"backup_missing",
            )
        return super().run_command(session, command, **kwargs)


class LivePatchRuntimeBackendTests(unittest.TestCase):
    def test_artifact_digest_mismatch_is_rejected_before_remote_effects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport("a" * 64)
            service = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=MutationJournalStore(root / "journals"),
                    credential_loader=lambda _arguments: {
                        "ssh": {"user": "root", "password": "ssh-secret"},
                        "telnet": {"user": "root", "password": "telnet-secret"},
                    },
                    ssh_transport_factory=lambda _arguments: ssh,
                    telnet_transport_factory=lambda _arguments: telnet,
                )
            )
            try:
                with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
                    service.call_tool(
                        "live_patch_run",
                        {
                            "intent": "diagnose-and-fix",
                            "delivery_strategy": "live-patch",
                            "ip": "bmc.example",
                            "local_path": str(local),
                            "artifact_sha256": "0" * 64,
                            "remote_path": "/opt/bmc/apps/demo/unit.lua",
                            "restart_scope": "none",
                            "deadline": TEST_DEADLINE_SECONDS,
                        },
                        task_id="task-live-patch-artifact-mismatch",
                        operation_id="apply-artifact-mismatch",
                    )
            finally:
                service.close()

        self.assertEqual(ssh.uploads, [])
        self.assertEqual(telnet.commands, [])

    def test_high_risk_flags_require_task_authorized_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            credential_loads = 0

            def credentials(_arguments):
                nonlocal credential_loads
                credential_loads += 1
                return {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                }

            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=credentials,
                ssh_transport_factory=lambda _arguments: FakeSshTransport(),
                telnet_transport_factory=lambda _arguments: FakeTelnetTransport(
                    digest
                ),
            )
            service = RuntimeMcpService(backend)
            try:
                for index, (name, remote_path) in enumerate(
                    (
                        ("force_path", "/var/lib/openubmc/unit.lua"),
                        ("no_backup", "/opt/bmc/apps/demo/unit.lua"),
                        ("no_remount", "/opt/bmc/apps/demo/unit.lua"),
                    )
                ):
                    with self.subTest(exception=name):
                        with self.assertRaises(MutationAuthorizationDenied):
                            service.call_tool(
                                "live_patch_run",
                                {
                                    "intent": "live-patch",
                                    "ip": "bmc.example",
                                    "local_path": str(local),
                                    "remote_path": remote_path,
                                    name: True,
                                    "deadline": TEST_DEADLINE_SECONDS,
                                },
                                task_id=f"task-denied-{name}",
                                operation_id=f"denied-{index}",
                            )
                self.assertEqual(credential_loads, 0)
            finally:
                service.close()

    def test_task_authorized_force_path_reaches_the_mutation_backend(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            credential_loads = 0

            def credentials(_arguments):
                nonlocal credential_loads
                credential_loads += 1
                return {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                }

            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=credentials,
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool(
                    "live_patch_run",
                    {
                        "intent": "live-patch",
                        "ip": "bmc.example",
                        "local_path": str(local),
                        "remote_path": "/var/lib/openubmc/unit.lua",
                        "restart_scope": "none",
                        "force_path": True,
                        "authorized_exceptions": {"force_path": True},
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    task_id="task-authorized-force-path",
                    operation_id="authorized-force-path",
                )
            finally:
                service.close()

        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(credential_loads, 1)
        self.assertEqual(len(ssh.uploads), 1)

    def test_high_risk_flags_reject_non_boolean_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {},
                ssh_transport_factory=lambda _arguments: FakeSshTransport(),
                telnet_transport_factory=lambda _arguments: FakeTelnetTransport(
                    digest
                ),
            )
            service = RuntimeMcpService(backend)
            try:
                for index, name in enumerate(
                    ("force_path", "no_backup", "no_remount")
                ):
                    with self.subTest(flag=name):
                        with self.assertRaises(TypeError):
                            service.call_tool(
                                "live_patch_run",
                                {
                                    "intent": "live-patch",
                                    "ip": "bmc.example",
                                    "local_path": str(local),
                                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                                    name: "false",
                                    "deadline": TEST_DEADLINE_SECONDS,
                                },
                                task_id=f"task-non-bool-{name}",
                                operation_id=f"non-bool-{index}",
                            )
            finally:
                service.close()
    def test_binding_identity_changes_with_direct_credentials_and_ssh_policy(self) -> None:
        base = {
            "ip": "bmc.example",
            "ssh_password": "password-a",
            "telnet_password": "telnet-a",
            "ssh_host_key_policy": "insecure",
            "ssh_known_hosts_file": "/tmp/known-a",
        }
        first = _LivePatchTask._key(base)
        self.assertNotEqual(
            first,
            _LivePatchTask._key({**base, "ssh_password": "password-b"}),
        )
        self.assertNotEqual(
            first,
            _LivePatchTask._key(
                {
                    **base,
                    "ssh_host_key_policy": "accept-new",
                    "ssh_known_hosts_file": "/tmp/known-b",
                }
            ),
        )

    def test_backend_splits_path_guards_below_telnet_input_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            telnet = FakeTelnetTransport(digest)
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: FakeSshTransport(),
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(backend)
            try:
                service.call_tool(
                    "live_patch_run",
                    {
                        "intent": "live_patch",
                        "ip": "bmc.example",
                        "local_path": str(local),
                        "remote_path": "/opt/bmc/apps/demo/unit.lua",
                        "restart_scope": "none",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    task_id="task-live-patch-bounded-guards",
                    operation_id="apply-bounded-guards",
                )
            finally:
                service.close()

        guards = [
            command
            for command in telnet.commands
            if "live_patch_paths_safe" in command
        ]
        self.assertEqual(len(guards), 5)
        self.assertTrue(
            all(len(command.encode("utf-8")) <= 700 for command in guards)
        )
        self.assertTrue(
            all(
                len(command.encode("utf-8")) <= 700
                for command in telnet.commands
            )
        )

    def test_backend_runs_one_typed_mutation_and_fresh_checksum_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(backend)
            try:
                arguments = {
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                    "ip": "bmc.example",
                    "local_path": str(local),
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "none",
                    "_minimum_target_epoch": 4,
                    "deadline": TEST_DEADLINE_SECONDS,
                }
                result = service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch",
                    operation_id="apply-1",
                )
                replayed = service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch",
                    operation_id="apply-1",
                )
            finally:
                service.close()

        self.assertEqual(result["epoch_before"], 4)
        self.assertEqual(result["epoch_after"], 5)
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(result["verification"]["remote_sha256"], digest)
        self.assertEqual(len(ssh.uploads), 1)
        self.assertEqual(telnet.opens, 2)
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(len(ssh.uploads), 1)

    def test_recovery_without_a_durable_journal_never_applies_live_patch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            service = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=MutationJournalStore(root / "journals"),
                    credential_loader=lambda _arguments: {
                        "ssh": {"user": "root", "password": "ssh-secret"},
                        "telnet": {"user": "root", "password": "telnet-secret"},
                    },
                    ssh_transport_factory=lambda _arguments: ssh,
                    telnet_transport_factory=lambda _arguments: telnet,
                )
            )
            try:
                descriptor = service.catalog.require("live_patch_run")
                with self.assertRaisesRegex(
                    OSError, "no durable mutation journal"
                ):
                    service._execute_domain_value(
                        "live_patch_run",
                        descriptor,
                        {
                            "intent": "diagnose-and-fix",
                            "delivery_strategy": "live-patch",
                            "ip": "bmc.example",
                            "local_path": str(local),
                            "artifact_sha256": digest,
                            "remote_path": "/opt/bmc/apps/demo/unit.lua",
                            "restart_scope": "none",
                            "deadline": TEST_DEADLINE_SECONDS,
                        },
                        task_id="live-patch-recovery-without-journal",
                        operation_id="live-patch-recovery-without-journal",
                        recovery_mode=EffectRecoveryMode.RECONCILE,
                    )
            finally:
                service.close()

        self.assertEqual(ssh.uploads, [])
        self.assertEqual(telnet.commands, [])

    def test_terminal_journal_replays_after_local_patch_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            arguments = {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "artifact_sha256": digest,
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=lambda _arguments: {
                        "ssh": {"user": "root", "password": "ssh-secret"},
                        "telnet": {
                            "user": "root",
                            "password": "telnet-secret",
                        },
                    },
                    ssh_transport_factory=lambda _arguments: FakeSshTransport(),
                    telnet_transport_factory=lambda _arguments: FakeTelnetTransport(
                        digest
                    ),
                )
            )
            try:
                first.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="terminal-live-patch",
                    operation_id="terminal-live-patch-effect",
                )
            finally:
                first.close()
            local.unlink()

            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            second = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=lambda _arguments: {
                        "ssh": {"user": "root", "password": "ssh-secret"},
                        "telnet": {
                            "user": "root",
                            "password": "telnet-secret",
                        },
                    },
                    ssh_transport_factory=lambda _arguments: ssh,
                    telnet_transport_factory=lambda _arguments: telnet,
                )
            )
            try:
                descriptor = second.catalog.require("live_patch_run")
                replayed = second._execute_domain_value(
                    "live_patch_run",
                    descriptor,
                    arguments,
                    task_id="terminal-live-patch",
                    operation_id="terminal-live-patch-effect",
                    recovery_mode=EffectRecoveryMode.RECONCILE,
                )
            finally:
                second.close()

        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(replayed["journal"]["stage"], "verified")
        self.assertEqual(ssh.uploads, [])
        self.assertEqual(telnet.commands, [])

    def test_unknown_live_patch_is_reconciled_read_first_without_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            first_telnet = FailRestartOnceTelnetTransport(digest)
            first_ssh = FakeSshTransport()
            credentials = lambda _arguments: {
                "ssh": {"user": "root", "password": "ssh-secret"},
                "telnet": {"user": "root", "password": "telnet-secret"},
            }
            arguments = {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=credentials,
                    ssh_transport_factory=lambda _arguments: first_ssh,
                    telnet_transport_factory=lambda _arguments: first_telnet,
                )
            )
            try:
                with self.assertRaises(OSError):
                    first.call_tool(
                        "live_patch_run",
                        arguments,
                        task_id="task-live-patch-recovery",
                        operation_id="apply-recovery",
                    )
            finally:
                first.close()
            local.unlink()

            second_telnet = FakeTelnetTransport(
                digest,
                target_exists=True,
                target_mode="644",
                target_uid=0,
                target_gid=0,
            )
            second_ssh = FakeSshTransport()
            second = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=credentials,
                    ssh_transport_factory=lambda _arguments: second_ssh,
                    telnet_transport_factory=lambda _arguments: second_telnet,
                )
            )
            try:
                recovered = second.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch-recovery",
                    operation_id="apply-recovery",
                )
            finally:
                second.close()

        self.assertEqual(len(first_ssh.uploads), 1)
        self.assertEqual(second_ssh.uploads, [])
        self.assertEqual(recovered["journal"]["stage"], "verified")
        self.assertEqual(recovered["mutation"]["recovery"]["decision"], "verify")
        inspection_index = next(
            index
            for index, command in enumerate(second_telnet.commands)
            if "live_patch_recovery_inspected" in command
        )
        verification_index = next(
            index
            for index, command in enumerate(second_telnet.commands)
            if "verify_sha256" in command
        )
        self.assertLess(inspection_index, verification_index)

    def test_unknown_live_patch_with_backup_reconciles_without_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            first_telnet = FailRestartOnceTelnetTransport(
                digest,
                target_exists=True,
            )
            first_ssh = FakeSshTransport()
            credentials = lambda _arguments: {
                "ssh": {"user": "root", "password": "ssh-secret"},
                "telnet": {"user": "root", "password": "telnet-secret"},
            }
            arguments = {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=credentials,
                    ssh_transport_factory=lambda _arguments: first_ssh,
                    telnet_transport_factory=lambda _arguments: first_telnet,
                )
            )
            try:
                with self.assertRaises(OSError):
                    first.call_tool(
                        "live_patch_run",
                        arguments,
                        task_id="task-live-patch-backup-recovery",
                        operation_id="apply-backup-recovery",
                    )
            finally:
                first.close()
            local.unlink()

            second_telnet = FakeTelnetTransport(
                digest,
                target_exists=True,
                target_mode="644",
            )
            second_ssh = FakeSshTransport()
            second = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=credentials,
                    ssh_transport_factory=lambda _arguments: second_ssh,
                    telnet_transport_factory=lambda _arguments: second_telnet,
                )
            )
            try:
                recovered = second.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch-backup-recovery",
                    operation_id="apply-backup-recovery",
                )
            finally:
                second.close()

        self.assertEqual(len(first_ssh.uploads), 1)
        self.assertEqual(second_ssh.uploads, [])
        self.assertEqual(recovered["journal"]["stage"], "verified")
        self.assertEqual(recovered["mutation"]["recovery"]["decision"], "verify")
        backup_index = next(
            index
            for index, command in enumerate(second_telnet.commands)
            if "backup_exists" in command
        )
        verification_index = next(
            index
            for index, command in enumerate(second_telnet.commands)
            if "verify_sha256" in command
        )
        self.assertLess(backup_index, verification_index)

    def test_unknown_live_patch_blocks_when_durable_backup_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            credentials = lambda _arguments: {
                "ssh": {"user": "root", "password": "ssh-secret"},
                "telnet": {"user": "root", "password": "telnet-secret"},
            }
            arguments = {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
                "ip": "bmc.example",
                "local_path": str(local),
                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                "restart_scope": "none",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first_ssh = FakeSshTransport()
            first = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=credentials,
                    ssh_transport_factory=lambda _arguments: first_ssh,
                    telnet_transport_factory=lambda _arguments: (
                        FailRestartOnceTelnetTransport(
                            digest,
                            target_exists=True,
                        )
                    ),
                )
            )
            try:
                with self.assertRaises(OSError):
                    first.call_tool(
                        "live_patch_run",
                        arguments,
                        task_id="task-live-patch-missing-backup",
                        operation_id="apply-missing-backup",
                    )
            finally:
                first.close()
            local.unlink()

            second_telnet = MissingBackupTelnetTransport(
                digest,
                target_exists=True,
                target_mode="644",
            )
            second_ssh = FakeSshTransport()
            second = RuntimeMcpService(
                LivePatchMcpBackend(
                    journal_store=journals,
                    credential_loader=credentials,
                    ssh_transport_factory=lambda _arguments: second_ssh,
                    telnet_transport_factory=lambda _arguments: second_telnet,
                )
            )
            try:
                recovered = second.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch-missing-backup",
                    operation_id="apply-missing-backup",
                )
            finally:
                second.close()

        self.assertEqual(len(first_ssh.uploads), 1)
        self.assertEqual(second_ssh.uploads, [])
        self.assertEqual(recovered["journal"]["stage"], "recovery_blocked")
        self.assertEqual(recovered["mutation"]["recovery"]["decision"], "manual")
        self.assertFalse(
            any("verify_sha256" in command for command in second_telnet.commands)
        )

    def test_backend_rolls_back_without_upload_and_freshly_verifies_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            digest = "a" * 64
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(backend)
            try:
                arguments = {
                    "intent": "rollback",
                    "action": "rollback",
                    "ip": "bmc.example",
                    "backup_path": "/tmp/unit.lua.bak.1",
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "none",
                    "deadline": TEST_DEADLINE_SECONDS,
                }
                result = service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch-rollback",
                    operation_id="rollback-1",
                )
                replayed = service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch-rollback",
                    operation_id="rollback-1",
                )
            finally:
                service.close()

        self.assertEqual(result["action"], "rollback")
        self.assertEqual(result["epoch_before"], 0)
        self.assertEqual(result["epoch_after"], 1)
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(result["verification"]["remote_sha256"], digest)
        self.assertEqual(ssh.uploads, [])
        self.assertEqual(telnet.opens, 2)
        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(
            sum("p=r;" in command for command in telnet.commands),
            1,
        )
        restore_wrapper = next(
            command for command in telnet.commands if "p=r;" in command
        )
        restore_command = decoded_shell_script(restore_wrapper)
        self.assertIn("mkdir", restore_command)
        self.assertIn("cp -pP", restore_command)
        self.assertIn("mv -f", restore_command)
        self.assertEqual(
            result["verification"]["remote_metadata"],
            {"mode": "644", "uid": 104, "gid": 104},
        )

    def test_backend_removes_a_checksum_matched_created_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            digest = "c" * 64
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest, target_exists=True)
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool(
                    "live_patch_run",
                    {
                        "intent": "rollback",
                        "action": "rollback",
                        "ip": "bmc.example",
                        "remove_created": True,
                        "expected_current_sha256": digest,
                        "remote_path": "/tmp/openubmc-live-patch-smoke",
                        "restart_scope": "none",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    task_id="task-live-patch-remove-created",
                    operation_id="rollback-remove-created",
                )
            finally:
                service.close()

        self.assertTrue(result["mutation"]["remote_removed"])
        self.assertTrue(result["verification"]["remote_removed"])
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(ssh.uploads, [])
        remove_wrapper = next(
            command for command in telnet.commands if "p=r;" in command
        )
        remove_command = decoded_shell_script(remove_wrapper)
        self.assertIn("rm -f", remove_command)
        self.assertIn(digest, remove_command)

    def test_backend_preserves_atomic_backup_install_and_before_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            telnet = FakeTelnetTransport(digest, target_exists=True)
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: FakeSshTransport(),
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool(
                    "live_patch_run",
                    {
                        "intent": "live_patch",
                        "ip": "bmc.example",
                        "local_path": str(local),
                        "remote_path": "/opt/bmc/apps/demo/unit.lua",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    task_id="task-live-patch-atomic",
                    operation_id="apply-atomic",
                )
            finally:
                service.close()

        backup_wrapper = next(
            command for command in telnet.commands if "p=b;" in command
        )
        install_wrapper = next(
            command for command in telnet.commands if "p=i;" in command
        )
        backup_command = decoded_shell_script(backup_wrapper)
        install_command = decoded_shell_script(install_wrapper)
        self.assertIn("mkdir", backup_command)
        self.assertIn("cp -pP", backup_command)
        self.assertIn("mv", backup_command)
        self.assertIn("mkdir", install_command)
        self.assertIn("chown 104:104", install_command)
        self.assertIn("mv -f", install_command)
        self.assertEqual(result["journal"]["before_checksum"], digest)
        self.assertEqual(
            result["mutation"]["remote_before_metadata"],
            {"mode": "440", "uid": 104, "gid": 104},
        )

    def test_same_operation_id_rejects_a_different_staging_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            backend = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: FakeSshTransport(),
                telnet_transport_factory=lambda _arguments: FakeTelnetTransport(
                    digest
                ),
            )
            service = RuntimeMcpService(backend)
            try:
                arguments = {
                    "intent": "live_patch",
                    "ip": "bmc.example",
                    "local_path": str(local),
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "staging_path": "/tmp/stage-a",
                    "deadline": TEST_DEADLINE_SECONDS,
                }
                service.call_tool(
                    "live_patch_run",
                    arguments,
                    task_id="task-live-patch-conflict",
                    operation_id="apply-conflict",
                )
                with self.assertRaises(MutationOperationConflict):
                    service.call_tool(
                        "live_patch_run",
                        {**arguments, "staging_path": "/tmp/stage-b"},
                        task_id="task-live-patch-conflict",
                        operation_id="apply-conflict",
                    )
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
