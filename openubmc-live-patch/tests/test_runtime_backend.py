from __future__ import annotations

import base64
from collections.abc import Mapping
import gzip
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-live-patch"))

from openubmc_target_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    OrchestratedMcpBackend,
    MutationAuthorizationDenied,
    MutationJournalStore,
    MutationOperationConflict,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
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


def artifact_ref(path: Path, *, target: str, run_id: str) -> dict[str, object]:
    body = path.read_bytes()
    return {
        "handle": str(path),
        "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
        "kind": "openubmc-live-patch",
        "size": len(body),
        "provenance": "live-patch-fault-matrix",
        "retention_hint": "run-lifetime",
        "target": target,
        "run_id": run_id,
    }


def gate_binding(turn: Mapping[str, object]) -> dict[str, object]:
    gate = turn["gate"]
    assert isinstance(gate, Mapping)
    return {
        "gate_id": gate["gate_id"],
        "gate_version": gate["gate_version"],
        "schema_digest": gate["schema_digest"],
    }


class WorkflowDebugBackend:
    class Task:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id

    @staticmethod
    def open_task(task_id: str):
        return WorkflowDebugBackend.Task(task_id)

    @staticmethod
    def close_task(_task) -> None:
        return None

    @staticmethod
    def maintain_task(_task) -> int:
        return 0

    @staticmethod
    def task_status(task) -> dict[str, object]:
        return {"task_id": task.task_id}

    @staticmethod
    def debug_run(_task, _arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {"ok": True, "summary": "diagnosis completed"}

    @staticmethod
    def debug_collect(_task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {
            "ok": True,
            "observed_at": "2026-08-20T00:00:00Z",
            "target_epoch": int(arguments.get("_minimum_target_epoch", 0)),
            "business_acceptance": "passed",
            "result": {
                "capabilities": {
                    "ssh_transport": True,
                    "mdbctl": True,
                    "busctl": False,
                    "active_alarm_transport": True,
                    "active_alarm_endpoint_verified": False,
                    "active_alarms": True,
                },
                "lanes": {"ssh": {}},
            },
        }


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


class LoseInstallResponseOnceTelnetTransport(FakeTelnetTransport):
    def __init__(self, digest: str, **kwargs) -> None:
        super().__init__(digest, **kwargs)
        self.install_commands = 0
        self.response_lost = False
        self.faulted = threading.Event()

    def run_command(self, session, command: str, **kwargs):
        if "p=i;" in command:
            self.install_commands += 1
            result = super().run_command(session, command, **kwargs)
            if not self.response_lost:
                self.response_lost = True
                self.faulted.set()
                raise OSError("connection lost after atomic replacement")
            return result
        return super().run_command(session, command, **kwargs)


class CrashCutSshTransport(FakeSshTransport):
    def __init__(self, cut: str, faulted: threading.Event) -> None:
        super().__init__()
        self.cut = cut
        self.faulted = faulted
        self.response_lost = False

    def upload_file(self, master, local_path: str, remote_path: str, **kwargs):
        result = super().upload_file(
            master,
            local_path,
            remote_path,
            **kwargs,
        )
        if self.cut == "upload" and not self.response_lost:
            self.response_lost = True
            self.faulted.set()
            raise OSError("connection lost after staging upload")
        return result


class CrashCutTelnetTransport(FakeTelnetTransport):
    def __init__(
        self,
        expected_digest: str,
        before_digest: str,
        *,
        cut: str,
        faulted: threading.Event,
    ) -> None:
        super().__init__(expected_digest, target_exists=True)
        self.before_digest = before_digest
        self.current_digest = before_digest
        self.backup_digest = ""
        self.backup_exists = False
        self.cut = cut
        self.faulted = faulted
        self.root_mount_mode = "ro" if cut == "remount" else "rw"
        self.response_lost = False
        self.backup_commands = 0
        self.install_commands = 0
        self.restart_commands = 0
        self.verification_commands = 0
        self.recovery_reads: list[str] = []

    def _lose_once(self, cut: str, message: str) -> None:
        if self.cut == cut and not self.response_lost:
            self.response_lost = True
            self.faulted.set()
            raise OSError(message)

    @staticmethod
    def _result(stdout: str) -> TelnetCommandResult:
        return TelnetCommandResult(
            stdout=stdout,
            returncode=0,
            framing_complete=True,
            timed_out=False,
            connection_closed=False,
            raw=stdout.encode(),
        )

    def run_command(self, session, command: str, **kwargs):
        if "mount -o remount,rw /" in command:
            self.commands.append(command)
            self.root_mount_mode = "rw"
            result = self._result("remount_rw_ok")
            self._lose_once("remount", "connection lost after root remount")
            return result
        if "mount -o remount,ro /" in command:
            self.commands.append(command)
            self.root_mount_mode = "ro"
            return self._result("remount_ro_ok")
        if "live_patch_recovery_inspected" in command:
            self.commands.append(command)
            self.recovery_reads.append("target")
            return self._result(
                f"remote_sha256={self.current_digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\n"
                "remote_exists\nlive_patch_recovery_inspected"
            )
        if "backup_exists" in command:
            self.commands.append(command)
            self.recovery_reads.append("backup")
            if not self.backup_exists:
                return self._result("backup_missing")
            return self._result(
                f"backup_sha256={self.backup_digest}\n"
                f"backup_mode={self.target_mode}\n"
                f"backup_uid={self.target_uid}\n"
                f"backup_gid={self.target_gid}\nbackup_exists"
            )
        if "/proc/mounts" in command:
            self.commands.append(command)
            self.recovery_reads.append("mount")
            return self._result(f"{self.root_mount_mode},relatime")
        if "live_patch_restart_observed" in command:
            self.commands.append(command)
            self.recovery_reads.append("restart")
            return self._result("live_patch_restart_observed")
        if "target_exists" in command:
            self.commands.append(command)
            return self._result(
                f"{self.current_digest}  /opt/bmc/apps/demo/unit.lua\n"
                f"target_mode={self.current_mode}\n"
                f"target_uid={self.current_uid}\n"
                f"target_gid={self.current_gid}\n"
                "target_exists"
            )
        if "p=b;" in command:
            self.backup_commands += 1
            self.backup_exists = True
            self.backup_digest = self.current_digest
            result = super().run_command(session, command, **kwargs)
            self._lose_once("backup", "connection lost after atomic backup")
            return result
        if "p=i;" in command:
            self.install_commands += 1
            result = super().run_command(session, command, **kwargs)
            self.current_digest = self.digest
            self._lose_once("install", "connection lost after atomic replacement")
            return result
        if "restart_ok" in command:
            self.restart_commands += 1
            result = super().run_command(session, command, **kwargs)
            self._lose_once("restart", "connection lost after restart")
            return result
        if "verify_sha256" in command:
            self.verification_commands += 1
            self.commands.append(command)
            result = self._result(
                f"remote_sha256={self.current_digest}\n"
                f"remote_mode={self.current_mode}\n"
                f"remote_uid={self.current_uid}\n"
                f"remote_gid={self.current_gid}\nverify_sha256"
            )
            self._lose_once("verification", "verification response lost")
            return result
        return super().run_command(session, command, **kwargs)


class DispatchFailOnceLivePatchBackend(LivePatchMcpBackend):
    def __init__(self, *, faulted: threading.Event, **kwargs) -> None:
        super().__init__(**kwargs)
        self.faulted = faulted
        self.failed = False

    def live_patch_run(self, task, arguments, context):
        if not self.failed:
            self.failed = True
            self.faulted.set()
            raise OSError("adapter dispatch failed before Live Patch started")
        return super().live_patch_run(task, arguments, context)


class BlockAfterTerminalLivePatchBackend(LivePatchMcpBackend):
    def __init__(
        self,
        *,
        terminal: threading.Event,
        release: threading.Event,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.terminal = terminal
        self.release = release
        self.blocked_once = False

    def live_patch_run(self, task, arguments, context):
        value = super().live_patch_run(task, arguments, context)
        if not self.blocked_once:
            self.blocked_once = True
            self.terminal.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("terminal result was not released")
        return value


class LivePatchRuntimeBackendTests(unittest.TestCase):
    def test_execute_restart_replays_terminal_journal_before_run_fact_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-patch-terminal.sqlite3"
            blobs = root / "blobs"
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            terminal = threading.Event()
            release = threading.Event()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest, target_exists=True)
            debug = WorkflowDebugBackend()
            live_patch = BlockAfterTerminalLivePatchBackend(
                terminal=terminal,
                release=release,
                journal_store=journals,
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )

            def service(*, reclaim_pending: bool = False) -> RuntimeMcpService:
                return RuntimeMcpService(
                    OrchestratedMcpBackend(
                        {
                            "debug_run": debug,
                            "debug_collect": debug,
                            "live_patch_run": live_patch,
                        }
                    ),
                    context_repository=SQLiteRuntimeRepository(
                        database,
                        owner_is_active=(
                            (lambda _pid, _started: False)
                            if reclaim_pending
                            else (lambda pid, _started: pid == os.getpid())
                        ),
                    ),
                    blob_repository=FilesystemBlobRepository(blobs),
                )

            first = service()
            second = None
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="live-patch-terminal",
                    operation_id="live-patch-terminal-start",
                )
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "live-patch-terminal-source",
                                "authored_files": ["src/unit.lua"],
                                "verification_plan": ["fresh target verification"],
                                "artifact_ref": artifact_ref(
                                    local,
                                    target="bmc.example",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 0.000001,
                    },
                    task_id="live-patch-terminal",
                    operation_id="live-patch-terminal-submit",
                )
                self.assertTrue(terminal.wait(timeout=1))
                journal = journals.load_for_task(waiting["run_id"])[0]
                self.assertEqual(journal.stage, "verified")

                second = service(reclaim_pending=True)
                final = second.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id="live-patch-terminal-resume",
                    operation_id="live-patch-terminal-resume",
                )
                projection = second.context_runtime.read_case(waiting["run_id"])
            finally:
                release.set()
                first.close()
                if second is not None:
                    second.close()

        self.assertEqual(running["state"], "running")
        self.assertEqual(
            final["state"],
            "completed",
            {"turn": final, "operations": projection.get("operations")},
        )
        self.assertEqual(len(ssh.uploads), 1)
        self.assertEqual(
            len([command for command in telnet.commands if "p=i;" in command]),
            1,
        )

    def test_execute_restart_reports_incident_when_dispatch_failed_before_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-patch-dispatch.sqlite3"
            blobs = root / "blobs"
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            faulted = threading.Event()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest, target_exists=True)
            debug = WorkflowDebugBackend()
            live_patch = DispatchFailOnceLivePatchBackend(
                faulted=faulted,
                journal_store=journals,
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )

            def service() -> RuntimeMcpService:
                return RuntimeMcpService(
                    OrchestratedMcpBackend(
                        {
                            "debug_run": debug,
                            "debug_collect": debug,
                            "live_patch_run": live_patch,
                        }
                    ),
                    context_repository=SQLiteRuntimeRepository(database),
                    blob_repository=FilesystemBlobRepository(blobs),
                )

            first = service()
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="live-patch-dispatch",
                    operation_id="live-patch-dispatch-start",
                )
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "live-patch-dispatch-source",
                                "authored_files": ["src/unit.lua"],
                                "verification_plan": ["fresh target verification"],
                                "artifact_ref": artifact_ref(
                                    local,
                                    target="bmc.example",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 0.000001,
                    },
                    task_id="live-patch-dispatch",
                    operation_id="live-patch-dispatch-submit",
                )
                self.assertTrue(faulted.wait(timeout=1))
                effect_id = first.context_runtime.read_case(
                    waiting["run_id"]
                )["effect_intents"][-1]["effect_id"]
            finally:
                first.close()

            second = service()
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id="live-patch-dispatch-resume",
                    operation_id="live-patch-dispatch-resume",
                )
            finally:
                second.close()

        self.assertEqual(running["state"], "running")
        self.assertEqual(final["state"], "incident", final)
        self.assertEqual(final["incident"]["effect_id"], effect_id)
        self.assertEqual(journals.load_for_task(waiting["run_id"]), [])
        self.assertEqual(ssh.uploads, [])
        self.assertEqual(telnet.commands, [])

    def run_public_restart_fault(self, cut: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-patch-crash.sqlite3"
            blobs = root / "blobs"
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            before_digest = hashlib.sha256(b"previous version").hexdigest()
            journals = MutationJournalStore(root / "journals")
            faulted = threading.Event()
            fault_cut = cut.removesuffix("-deleted")
            ssh = CrashCutSshTransport(fault_cut, faulted)
            telnet = CrashCutTelnetTransport(
                digest,
                before_digest,
                cut=fault_cut,
                faulted=faulted,
            )
            debug = WorkflowDebugBackend()
            live_patch = LivePatchMcpBackend(
                journal_store=journals,
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )

            def service() -> RuntimeMcpService:
                return RuntimeMcpService(
                    OrchestratedMcpBackend(
                        {
                            "debug_run": debug,
                            "debug_collect": debug,
                            "live_patch_run": live_patch,
                        }
                    ),
                    context_repository=SQLiteRuntimeRepository(database),
                    blob_repository=FilesystemBlobRepository(blobs),
                )

            first = service()
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id=f"live-patch-{cut}",
                    operation_id=f"live-patch-{cut}-start",
                )
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": f"live-patch-{cut}-source",
                                "authored_files": ["src/unit.lua"],
                                "verification_plan": ["fresh target verification"],
                                "artifact_ref": artifact_ref(
                                    local,
                                    target="bmc.example",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                                "restart_scope": (
                                    "skynet" if fault_cut == "restart" else "none"
                                ),
                            },
                        },
                        "deadline": 0.000001,
                    },
                    task_id=f"live-patch-{cut}",
                    operation_id=f"live-patch-{cut}-submit",
                )
                self.assertTrue(faulted.wait(timeout=1), cut)
                first_projection = first.context_runtime.read_case(waiting["run_id"])
                effect_id = first_projection["effect_intents"][-1]["effect_id"]
                mutation_id = journals.load_for_task(waiting["run_id"])[0].operation_id
            finally:
                first.close()

            if cut.endswith("-deleted"):
                local.unlink()
            recovery_start = len(telnet.recovery_reads)
            second = service()
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id=f"live-patch-{cut}-resume",
                    operation_id=f"live-patch-{cut}-resume",
                )
                projection = second.context_runtime.read_case(waiting["run_id"])
                recovered_mutation_ids = {
                    journal.operation_id
                    for journal in journals.load_for_task(waiting["run_id"])
                    if journal.action == "live_patch"
                }
            finally:
                second.close()

            return {
                "running": running,
                "final": final,
                "projection": projection,
                "effect_id": effect_id,
                "mutation_id": mutation_id,
                "recovered_mutation_ids": recovered_mutation_ids,
                "ssh": ssh,
                "telnet": telnet,
                "recovery_reads": tuple(telnet.recovery_reads[recovery_start:]),
            }

    def test_execute_restart_replans_after_lost_backup_response(self) -> None:
        result = self.run_public_restart_fault("backup")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "failed", result["final"])
        self.assertEqual(result["telnet"].backup_commands, 1)
        self.assertEqual(result["telnet"].install_commands, 0)
        self.assertEqual(len(result["ssh"].uploads), 0)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(
            {
                item["operation_id"]
                for item in result["projection"]["operations"]
                if item.get("operation") == "live_patch_run"
            },
            {result["effect_id"]},
        )
        self.assertEqual(result["recovery_reads"][:3], ("target", "backup", "mount"))

    def test_execute_restart_returns_incident_after_lost_remount_response(self) -> None:
        result = self.run_public_restart_fault("remount")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "incident", result["final"])
        self.assertEqual(result["telnet"].backup_commands, 0)
        self.assertEqual(result["telnet"].install_commands, 0)
        self.assertEqual(len(result["ssh"].uploads), 0)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(result["recovery_reads"][:2], ("target", "mount"))
        self.assertEqual(
            result["final"]["incident"]["effect_id"],
            result["effect_id"],
        )

    def test_execute_restart_replans_after_lost_upload_response(self) -> None:
        result = self.run_public_restart_fault("upload")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "failed", result["final"])
        self.assertEqual(result["telnet"].backup_commands, 1)
        self.assertEqual(result["telnet"].install_commands, 0)
        self.assertEqual(len(result["ssh"].uploads), 1)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(result["recovery_reads"][:3], ("target", "backup", "mount"))

    def test_execute_restart_verifies_after_lost_install_response(self) -> None:
        result = self.run_public_restart_fault("install")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "completed", result["final"])
        self.assertEqual(result["telnet"].backup_commands, 1)
        self.assertEqual(result["telnet"].install_commands, 1)
        self.assertEqual(result["telnet"].restart_commands, 0)
        self.assertEqual(len(result["ssh"].uploads), 1)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(result["recovery_reads"][:3], ("target", "backup", "mount"))

    def test_execute_restart_recovers_install_after_local_patch_is_deleted(self) -> None:
        result = self.run_public_restart_fault("install-deleted")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "completed", result["final"])
        self.assertEqual(result["telnet"].install_commands, 1)
        self.assertEqual(len(result["ssh"].uploads), 1)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(result["recovery_reads"][:3], ("target", "backup", "mount"))

    def test_execute_restart_verifies_after_lost_restart_response(self) -> None:
        result = self.run_public_restart_fault("restart")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "completed", result["final"])
        self.assertEqual(result["telnet"].backup_commands, 1)
        self.assertEqual(result["telnet"].install_commands, 1)
        self.assertEqual(result["telnet"].restart_commands, 1)
        self.assertEqual(len(result["ssh"].uploads), 1)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(
            result["recovery_reads"][:4],
            ("target", "backup", "mount", "restart"),
        )

    def test_execute_restart_retries_only_fresh_verification_after_response_loss(
        self,
    ) -> None:
        result = self.run_public_restart_fault("verification")

        self.assertEqual(result["running"]["state"], "running")
        self.assertEqual(result["final"]["state"], "completed", result["final"])
        self.assertEqual(result["telnet"].backup_commands, 1)
        self.assertEqual(result["telnet"].install_commands, 1)
        self.assertEqual(result["telnet"].restart_commands, 1)
        self.assertEqual(result["telnet"].verification_commands, 2)
        self.assertEqual(len(result["ssh"].uploads), 1)
        self.assertEqual(result["recovered_mutation_ids"], {result["mutation_id"]})
        self.assertEqual(result["recovery_reads"][:3], ("target", "backup", "mount"))

    def test_execute_live_patch_returns_a_terminal_turn(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            ssh = FakeSshTransport()
            telnet = FakeTelnetTransport(digest)
            debug = WorkflowDebugBackend()
            live_patch = LivePatchMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )
            service = RuntimeMcpService(
                OrchestratedMcpBackend(
                    {
                        "debug_run": debug,
                        "debug_collect": debug,
                        "live_patch_run": live_patch,
                    }
                )
            )
            try:
                waiting = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="public-live-patch",
                    operation_id="public-live-patch-start",
                )
                final = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "public-live-patch-source",
                                "authored_files": ["src/unit.lua"],
                                "verification_plan": ["fresh target verification"],
                                "artifact_ref": artifact_ref(
                                    local,
                                    target="bmc.example",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 1.0,
                    },
                    task_id="public-live-patch",
                    operation_id="public-live-patch-submit",
                )
            finally:
                service.close()

        self.assertEqual(waiting["state"], "waiting_response")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(len(ssh.uploads), 1)

    def test_execute_restart_recovers_lost_install_response_without_replacing_twice(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-patch-crash.sqlite3"
            blobs = root / "blobs"
            local = root / "unit.lua"
            local.write_text("return true\n", encoding="utf-8")
            digest = hashlib.sha256(local.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            ssh = FakeSshTransport()
            telnet = LoseInstallResponseOnceTelnetTransport(
                digest,
                target_exists=True,
            )
            debug = WorkflowDebugBackend()
            live_patch = LivePatchMcpBackend(
                journal_store=journals,
                credential_loader=lambda _arguments: {
                    "ssh": {"user": "root", "password": "ssh-secret"},
                    "telnet": {"user": "root", "password": "telnet-secret"},
                },
                ssh_transport_factory=lambda _arguments: ssh,
                telnet_transport_factory=lambda _arguments: telnet,
            )

            first = RuntimeMcpService(
                OrchestratedMcpBackend(
                    {
                        "debug_run": debug,
                        "debug_collect": debug,
                        "live_patch_run": live_patch,
                    }
                ),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="lost-install-response",
                    operation_id="lost-install-response-start",
                )
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "lost-install-source",
                                "authored_files": ["src/unit.lua"],
                                "verification_plan": ["fresh target verification"],
                                "artifact_ref": artifact_ref(
                                    local,
                                    target="bmc.example",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/demo/unit.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 0.000001,
                    },
                    task_id="lost-install-response",
                    operation_id="lost-install-response-submit",
                )
                self.assertTrue(telnet.faulted.wait(timeout=1))
                first_projection = first.context_runtime.read_case(waiting["run_id"])
                effect_id = first_projection["effect_intents"][-1]["effect_id"]
                mutation_id = journals.load_for_task(waiting["run_id"])[
                    0
                ].operation_id
            finally:
                first.close()

            second = RuntimeMcpService(
                OrchestratedMcpBackend(
                    {
                        "debug_run": debug,
                        "debug_collect": debug,
                        "live_patch_run": live_patch,
                    }
                ),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": waiting["run_id"],
                    },
                    task_id="lost-install-response-resume",
                    operation_id="lost-install-response-resume",
                )
                projection = second.context_runtime.read_case(waiting["run_id"])
                operations = projection["operations"]
                recovered_mutation_ids = {
                    journal.operation_id
                    for journal in journals.load_for_task(waiting["run_id"])
                    if journal.action == "live_patch"
                }
            finally:
                second.close()

        self.assertEqual(running["state"], "running", running)
        self.assertEqual(
            final["state"],
            "completed",
            {
                "turn": final,
                "phase_records": projection.get("phase_records"),
                "stage_receipts": projection.get("stage_receipts"),
                "workflow_step_states": projection.get("workflow_step_states"),
                "operations": projection.get("operations"),
            },
        )
        self.assertEqual(telnet.install_commands, 1)
        self.assertEqual(len(ssh.uploads), 1)
        self.assertEqual(
            {
                item["operation_id"]
                for item in operations
                if item.get("operation") == "live_patch_run"
            },
            {effect_id},
        )
        self.assertEqual(recovered_mutation_ids, {mutation_id})

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
