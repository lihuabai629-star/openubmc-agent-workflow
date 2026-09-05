from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-live-patch"))

from live_patch_diagnosis import accept_diagnosis  # noqa: E402
from openubmc_live_patch.runtime_backend import LivePatchMcpBackend  # noqa: E402
from openubmc_target_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    MutationJournalStore,
    OrchestratedMcpBackend,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
    TelnetCommandResult,
)


def read_state(root: Path) -> dict[str, object]:
    path = root / "remote-state.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    local = root / "unit.lua"
    expected = hashlib.sha256(local.read_bytes()).hexdigest()
    state: dict[str, object] = {
        "expected_digest": expected,
        "before_digest": hashlib.sha256(b"previous version").hexdigest(),
        "current_digest": hashlib.sha256(b"previous version").hexdigest(),
        "target_exists": True,
        "backup_exists": False,
        "backup_digest": "",
        "staging_uploaded": False,
        "restart_observed": False,
        "skynet_pid": 100,
        "skynet_start": 1000,
        "backup_commands": 0,
        "uploads": 0,
        "install_commands": 0,
        "restart_commands": 0,
        "verification_commands": 0,
    }
    write_state(root, state)
    return state


def write_state(root: Path, state: dict[str, object]) -> None:
    (root / "remote-state.json").write_text(
        json.dumps(state, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class CrashController:
    def __init__(self, root: Path, cut: str, mode: str) -> None:
        self.root = root
        self.cut = cut
        self.mode = mode
        self.triggered = False

    def pause(self, cut: str) -> None:
        if self.mode != "crash" or self.cut != cut or self.triggered:
            return
        self.triggered = True
        (self.root / "marker").write_text(cut + "\n", encoding="utf-8")
        while True:
            time.sleep(1)


class PersistentSshTransport:
    def __init__(self, root: Path, controller: CrashController) -> None:
        self.root = root
        self.controller = controller

    @staticmethod
    def open_master(*, target, credentials):
        del target, credentials
        return object()

    @staticmethod
    def check_master(_master) -> bool:
        return True

    def upload_file(self, _master, local_path: str, remote_path: str, **_kwargs):
        del local_path, remote_path
        state = read_state(self.root)
        state["uploads"] = int(state["uploads"]) + 1
        state["staging_uploaded"] = True
        write_state(self.root, state)
        self.controller.pause("upload")
        return type(
            "UploadResult",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

    @staticmethod
    def run_channel(_master, _command: str, **_kwargs):
        return type(
            "ChannelResult",
            (),
            {"returncode": 0, "stdout": "", "stderr": ""},
        )()

    @staticmethod
    def channel_lost_master(_master, _result) -> bool:
        return False

    @staticmethod
    def close_master(_master) -> None:
        return None


class PersistentTelnetTransport:
    def __init__(self, root: Path, controller: CrashController) -> None:
        self.root = root
        self.controller = controller

    @staticmethod
    def open_session(*, target, credentials):
        del target, credentials
        return object()

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

    def run_command(self, _session, command: str, **_kwargs):
        state = read_state(self.root)
        current = str(state["current_digest"])
        expected = str(state["expected_digest"])
        before = str(state["before_digest"])
        if "live_patch_paths_safe" in command:
            return self._result("live_patch_paths_safe")
        if "live_patch_codec_ready" in command:
            return self._result("live_patch_codec_ready")
        if "live_patch_identity_inspected" in command:
            return self._result(
                "product_id=product-a\nmachine_id=machine-a\n"
                "firmware_id=firmware-1\nreboot_anchor=boot-a\n"
                "live_patch_identity_inspected"
            )
        if "rollback_backup_inspected" in command:
            return self._result(
                f"backup_sha256={before}\nbackup_mode=440\n"
                "backup_uid=104\nbackup_gid=104\n"
                "rollback_backup_inspected"
            )
        if "/proc/mounts" in command:
            return self._result("rw,relatime")
        if "live_patch_recovery_inspected" in command:
            return self._result(
                f"remote_sha256={current}\nremote_mode=440\n"
                "remote_uid=104\nremote_gid=104\n"
                "remote_exists\nlive_patch_recovery_inspected"
            )
        if "backup_exists" in command:
            if not bool(state["backup_exists"]):
                return self._result("backup_missing")
            return self._result(
                f"backup_sha256={state['backup_digest']}\nbackup_mode=440\n"
                "backup_uid=104\nbackup_gid=104\nbackup_exists"
            )
        if "live_patch_skynet_identity_inspected" in command:
            return self._result(
                f"skynet_process_identity={state['skynet_pid']}:"
                f"{state['skynet_start']}\n"
                "live_patch_skynet_identity_inspected"
            )
        if "target_exists" in command:
            return self._result(
                f"{current}  /opt/bmc/apps/demo/unit.lua\n"
                "target_mode=440\ntarget_uid=104\ntarget_gid=104\ntarget_exists"
            )
        if "p=b;" in command:
            state["backup_commands"] = int(state["backup_commands"]) + 1
            state["backup_exists"] = True
            state["backup_digest"] = before
            write_state(self.root, state)
            self.controller.pause("backup")
            return self._result(
                f"backup_sha256={before}\nbackup_mode=440\n"
                "backup_uid=104\nbackup_gid=104\nbackup_ok"
            )
        if "p=i;" in command:
            state["install_commands"] = int(state["install_commands"]) + 1
            state["current_digest"] = expected
            state["target_exists"] = True
            write_state(self.root, state)
            self.controller.pause("install")
            return self._result(
                f"remote_sha256={expected}\nremote_mode=440\n"
                "remote_uid=104\nremote_gid=104\ndeploy_ok"
            )
        if "restart_ok" in command:
            state["restart_commands"] = int(state["restart_commands"]) + 1
            state["restart_observed"] = True
            state["skynet_pid"] = int(state["skynet_pid"]) + 1
            state["skynet_start"] = int(state["skynet_start"]) + 1000
            write_state(self.root, state)
            self.controller.pause("restart")
            return self._result("restart_ok")
        if "verify_sha256" in command:
            state["verification_commands"] = (
                int(state["verification_commands"]) + 1
            )
            write_state(self.root, state)
            self.controller.pause("verification")
            return self._result(
                f"remote_sha256={current}\nremote_mode=440\n"
                "remote_uid=104\nremote_gid=104\nverify_sha256"
            )
        return self._result("ok")

    @staticmethod
    def command_invalidates_session(_session, result) -> bool:
        return not result.ok

    @staticmethod
    def close_session(_session) -> None:
        return None


class DebugBackend:
    class Task:
        def __init__(self, task_id: str) -> None:
            self.task_id = task_id

    @staticmethod
    def open_task(task_id: str):
        return DebugBackend.Task(task_id)

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
        return {
            "ok": True,
            "summary": "diagnosis completed",
            "root_cause": "the bounded live-patch fault was isolated",
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {"status": "fresh"},
        }

    @staticmethod
    def debug_collect(_task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {
            "ok": True,
            "observed_at": "2026-08-21T00:00:00Z",
            "target_epoch": int(arguments.get("_minimum_target_epoch", 0)),
            "business_acceptance": "passed",
            "result": {"capabilities": {"ssh_transport": True}, "lanes": {"ssh": {}}},
        }


def gate_binding(turn: dict[str, object]) -> dict[str, object]:
    gate = turn["gate"]
    assert isinstance(gate, dict)
    return {
        "gate_id": gate["gate_id"],
        "gate_version": gate["gate_version"],
        "schema_digest": gate["schema_digest"],
    }


def service(root: Path, cut: str, mode: str) -> RuntimeMcpService:
    controller = CrashController(root, cut, mode)
    live_patch = LivePatchMcpBackend(
        journal_store=MutationJournalStore(root / "journals"),
        credential_loader=lambda _arguments: {
            "ssh": {"user": "root", "password": "test"},
            "telnet": {"user": "root", "password": "test"},
        },
        ssh_transport_factory=lambda _arguments: PersistentSshTransport(
            root, controller
        ),
        telnet_transport_factory=lambda _arguments: PersistentTelnetTransport(
            root, controller
        ),
    )
    debug = DebugBackend()
    return RuntimeMcpService(
        OrchestratedMcpBackend(
            {
                "debug_run": debug,
                "debug_collect": debug,
                "live_patch_run": live_patch,
            }
        ),
        context_repository=SQLiteRuntimeRepository(root / "runtime.sqlite3"),
        blob_repository=FilesystemBlobRepository(root / "blobs"),
    )


def artifact_ref(path: Path, run_id: str) -> dict[str, object]:
    body = path.read_bytes()
    return {
        "handle": str(path),
        "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
        "kind": "openubmc-live-patch",
        "size": len(body),
        "provenance": "real-backend-crash-cut",
        "retention_hint": "run-lifetime",
        "target": "bmc.example",
        "run_id": run_id,
    }


def crash(root: Path, cut: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    local = root / "unit.lua"
    local.write_text("return true\n", encoding="utf-8")
    read_state(root)
    runtime = service(root, cut, "crash")
    waiting = runtime.call_exposed_tool(
        "execute",
        {
            "kind": "start",
            "target": "bmc.example",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "live-patch",
        },
        task_id=f"backend-crash-{cut}",
        operation_id=f"backend-crash-{cut}-start",
    )
    waiting = accept_diagnosis(
        runtime, waiting, task_id=f"backend-crash-{cut}",
        operation_id=f"backend-crash-{cut}-diagnosis",
    )
    (root / "run-id").write_text(str(waiting["run_id"]), encoding="utf-8")
    runtime.call_exposed_tool(
        "execute",
        {
            "kind": "respond",
            "run_id": waiting["run_id"],
            **gate_binding(waiting),
            "response": {
                "status": "completed",
                "summary": "crash-cut patch ready",
                "payload": {
                    "source_revision": f"backend-crash-{cut}-source",
                    "authored_files": ["src/unit.lua"],
                    "verification_plan": ["fresh target verification"],
                    "artifact_ref": artifact_ref(local, str(waiting["run_id"])),
                    "remote_path": "/opt/bmc/apps/demo/unit.lua",
                    "restart_scope": "skynet",
                },
            },
            "deadline": 0.000001,
        },
        task_id=f"backend-crash-{cut}",
        operation_id=f"backend-crash-{cut}-submit",
    )
    deadline = time.monotonic() + 20
    while not (root / "marker").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Live Patch backend did not reach {cut}")
        time.sleep(0.01)
    while True:
        time.sleep(1)


def recover(root: Path, cut: str) -> None:
    run_id = (root / "run-id").read_text(encoding="utf-8").strip()
    runtime = service(root, cut, "recover")
    try:
        turn = runtime.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": run_id},
            task_id=f"backend-crash-{cut}-resume",
            operation_id=f"backend-crash-{cut}-resume",
        )
        projection = runtime._test.context_runtime.read_case(run_id)
        journals = MutationJournalStore(root / "journals").load_for_task(run_id)
    finally:
        runtime.close()
    result = {
        "turn": turn,
        "journal_operation_ids": [journal.operation_id for journal in journals],
        "effect_operation_ids": [
            str(item.get("operation_id", ""))
            for item in projection.get("operations", [])
            if isinstance(item, dict) and item.get("operation") == "live_patch_run"
        ],
    }
    (root / "result.json").write_text(
        json.dumps(result, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    root = Path(sys.argv[1])
    cut = sys.argv[2]
    mode = sys.argv[3]
    if mode == "crash":
        crash(root, cut)
    elif mode == "recover":
        recover(root, cut)
    else:
        raise ValueError(f"unsupported worker mode: {mode}")


if __name__ == "__main__":
    main()
