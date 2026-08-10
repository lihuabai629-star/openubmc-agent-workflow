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
    MutationJournalStore,
    MutationOperationConflict,
    RuntimeMcpService,
    TelnetCommandResult,
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


class LivePatchRuntimeBackendTests(unittest.TestCase):
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
                    "intent": "live_patch",
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
                        "intent": "live_patch",
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
