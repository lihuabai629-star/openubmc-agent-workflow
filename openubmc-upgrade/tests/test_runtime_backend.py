from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib import request as urlrequest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-upgrade"))

from openubmc_target_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    OrchestratedMcpBackend,
    MutationAuthorizationDenied,
    MutationJournalStore,
    MutationOperationConflict,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
    TaskAuthorizationPolicy,
)
from openubmc_target_runtime.capability import EffectRecoveryMode  # noqa: E402
from openubmc_upgrade.runtime_backend import (  # noqa: E402
    RedfishHttpSession,
    RedfishHttpError,
    RedfishResponse,
    RedfishTransportError,
    UpgradeActivationReverted,
    UpgradeMcpBackend,
    _UpgradeTask,
    _default_credential_loader,
    _multipart_body,
)


TEST_DEADLINE_SECONDS = 30


def artifact_ref(
    path: Path,
    *,
    target: str,
    run_id: str,
    version: str,
) -> dict[str, object]:
    body = path.read_bytes()
    return {
        "handle": str(path),
        "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
        "kind": "openubmc-hpm",
        "size": len(body),
        "provenance": "upgrade-fault-matrix",
        "retention_hint": "run-lifetime",
        "target": target,
        "run_id": run_id,
        "version": version,
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


class FakeRedfishSession:
    def __init__(self, number: int, *, simple_update: bool = False) -> None:
        self.number = number
        self.simple_update = simple_update
        self.calls: list[tuple[str, str]] = []

    def request_json(self, method: str, path: str, **_kwargs) -> RedfishResponse:
        self.calls.append((method, path))
        if path == "/redfish/v1/UpdateService":
            if self.simple_update:
                return RedfishResponse(
                    status=200,
                    headers={},
                    payload={
                        "Actions": {
                            "#UpdateService.SimpleUpdate": {
                                "target": "/redfish/v1/UpdateService/Actions/SimpleUpdate"
                            }
                        }
                    },
                )
            return RedfishResponse(
                status=200,
                headers={},
                payload={"HttpPushUri": "/redfish/v1/UpdateService/upload"},
            )
        if path == "/redfish/v1/UpdateService/Actions/SimpleUpdate":
            return RedfishResponse(
                status=202,
                headers={"Location": "/redfish/v1/TaskService/Tasks/1"},
                payload={},
            )
        if path == "/redfish/v1/UpdateService/upload":
            return RedfishResponse(
                status=202,
                headers={"Location": "/redfish/v1/TaskService/Tasks/1"},
                payload={},
            )
        if path == "/redfish/v1/TaskService/Tasks/1":
            return RedfishResponse(
                status=200,
                headers={},
                payload={"TaskState": "Completed"},
            )
        if path == "/redfish/v1/Managers":
            return RedfishResponse(
                status=200,
                headers={},
                payload={"Members": [{"@odata.id": "/redfish/v1/Managers/1"}]},
            )
        if path == "/redfish/v1/Managers/1":
            return RedfishResponse(
                status=200,
                headers={},
                payload={"FirmwareVersion": "2.0.0"},
            )
        raise AssertionError(f"unexpected Redfish request: {method} {path}")


class FakeRedfishTransport:
    def __init__(self, *, simple_update: bool = False) -> None:
        self.opens = 0
        self.simple_update = simple_update
        self.sessions: list[FakeRedfishSession] = []

    def open_session(self, *, target, credentials) -> FakeRedfishSession:
        self.opens += 1
        session = FakeRedfishSession(
            self.opens,
            simple_update=self.simple_update,
        )
        self.sessions.append(session)
        return session

    @staticmethod
    def request(session, _operation: str, **kwargs):
        return kwargs["callback"](session)

    @staticmethod
    def is_authentication_failure(_error: BaseException) -> bool:
        return False

    @staticmethod
    def close_session(_session) -> None:
        return None


class UncertainUpgradeSession(FakeRedfishSession):
    def __init__(self, number: int, transport: "UncertainUpgradeTransport") -> None:
        super().__init__(number)
        self.transport = transport

    def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
        if path == "/redfish/v1/UpdateService/upload":
            self.calls.append((method, path))
            self.transport.upload_attempts += 1
            if self.transport.faulted is not None:
                self.transport.faulted.set()
            raise OSError("upload connection lost")
        if path == "/redfish/v1/Managers/1":
            self.calls.append((method, path))
            self.transport.manager_reads += 1
            versions = self.transport.manager_versions
            version = versions[min(self.transport.manager_reads - 1, len(versions) - 1)]
            return RedfishResponse(
                status=200,
                headers={},
                payload={"FirmwareVersion": version},
            )
        if path == "/redfish/v1/UpdateService" and self.transport.upload_attempts:
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={
                    "FirmwareInventory": {
                        "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"
                    },
                    "Task": ({"State": "Running"} if self.transport.pending else None),
                    "Oem": {
                        "openUBMC": {
                            "FirmwareToTakeEffect": [],
                            "BackgroundUpdateTasks": [],
                            "SyncUpdateState": None,
                        }
                    },
                },
            )
        if path == "/redfish/v1/UpdateService/FirmwareInventory":
            self.calls.append((method, path))
            members = [
                {
                    "@odata.id": (
                        "/redfish/v1/UpdateService/FirmwareInventory/ActiveBMC"
                    )
                }
            ]
            if self.transport.available_version:
                members.append(
                    {
                        "@odata.id": (
                            "/redfish/v1/UpdateService/FirmwareInventory/AvailableBMC"
                        )
                    }
                )
            return RedfishResponse(
                status=200,
                headers={},
                payload={"Members": members},
            )
        if path.endswith("/ActiveBMC"):
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={"Version": self.transport.active_version},
            )
        if path.endswith("/AvailableBMC"):
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={"Version": self.transport.available_version},
            )
        return super().request_json(method, path, **kwargs)


class UncertainUpgradeTransport(FakeRedfishTransport):
    def __init__(
        self,
        *,
        manager_versions: tuple[str, ...],
        active_version: str,
        available_version: str = "",
        pending: bool = False,
        faulted: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.manager_versions = manager_versions
        self.active_version = active_version
        self.available_version = available_version
        self.pending = pending
        self.manager_reads = 0
        self.upload_attempts = 0
        self.faulted = faulted

    def open_session(self, *, target, credentials) -> FakeRedfishSession:
        self.opens += 1
        session = UncertainUpgradeSession(self.opens, self)
        self.sessions.append(session)
        return session


class MissingTaskUriSession(FakeRedfishSession):
    def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
        if path == "/redfish/v1/UpdateService/upload":
            self.calls.append((method, path))
            return RedfishResponse(status=202, headers={}, payload={})
        return super().request_json(method, path, **kwargs)


class MissingTaskUriTransport(FakeRedfishTransport):
    def open_session(self, *, target, credentials) -> FakeRedfishSession:
        self.opens += 1
        session = MissingTaskUriSession(self.opens)
        self.sessions.append(session)
        return session


class MonitorDisconnectSession(FakeRedfishSession):
    def __init__(self, number: int, transport: "MonitorDisconnectTransport") -> None:
        super().__init__(number)
        self.transport = transport

    def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
        if path == "/redfish/v1/UpdateService/upload":
            self.calls.append((method, path))
            self.transport.upload_attempts += 1
            return RedfishResponse(
                status=202,
                headers={"Location": "/redfish/v1/TaskService/Tasks/1"},
                payload={},
            )
        if path == "/redfish/v1/TaskService/Tasks/1":
            self.calls.append((method, path))
            raise OSError("task monitor disconnected during BMC reboot")
        if path == "/redfish/v1/Managers/1":
            self.calls.append((method, path))
            self.transport.manager_reads += 1
            versions = self.transport.manager_versions
            version = versions[min(self.transport.manager_reads - 1, len(versions) - 1)]
            return RedfishResponse(
                status=200,
                headers={},
                payload={"FirmwareVersion": version},
            )
        if path == "/redfish/v1/UpdateService" and self.transport.upload_attempts:
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={
                    "FirmwareInventory": {
                        "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"
                    },
                    "Task": {"State": "Running"},
                    "Oem": {
                        "openUBMC": {
                            "FirmwareToTakeEffect": ["BMC"],
                            "BackgroundUpdateTasks": [],
                            "SyncUpdateState": "Activating",
                        }
                    },
                },
            )
        if path == "/redfish/v1/UpdateService/FirmwareInventory":
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={
                    "Members": [
                        {
                            "@odata.id": (
                                "/redfish/v1/UpdateService/FirmwareInventory/ActiveBMC"
                            )
                        },
                        {
                            "@odata.id": (
                                "/redfish/v1/UpdateService/FirmwareInventory/AvailableBMC"
                            )
                        },
                    ]
                },
            )
        if path.endswith("/ActiveBMC"):
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={"Version": self.transport.active_version},
            )
        if path.endswith("/AvailableBMC"):
            self.calls.append((method, path))
            return RedfishResponse(
                status=200,
                headers={},
                payload={"Version": "2.0.0"},
            )
        return super().request_json(method, path, **kwargs)


class MonitorDisconnectTransport(FakeRedfishTransport):
    def __init__(self, *, manager_versions: tuple[str, ...]) -> None:
        super().__init__()
        self.manager_versions = manager_versions
        self.manager_reads = 0
        self.upload_attempts = 0
        self.active_version = manager_versions[0]

    def open_session(self, *, target, credentials) -> FakeRedfishSession:
        self.opens += 1
        session = MonitorDisconnectSession(self.opens, self)
        self.sessions.append(session)
        return session


class FastPollingUpgradeBackend(UpgradeMcpBackend):
    def upgrade_run(self, task, arguments, context):
        selected = dict(arguments)
        selected.setdefault("version_poll_interval", 0.01)
        return super().upgrade_run(task, selected, context)


class UpgradeRuntimeBackendTests(unittest.TestCase):
    def run_public_upgrade(
        self,
        transport: FakeRedfishTransport,
        *,
        deadline: float = 1.0,
        backend_type=UpgradeMcpBackend,
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware-2.0.0")
            debug = WorkflowDebugBackend()
            upgrade = backend_type(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(
                OrchestratedMcpBackend(
                    {
                        "debug_run": debug,
                        "debug_collect": debug,
                        "upgrade_run": upgrade,
                    }
                )
            )
            try:
                developer = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "build-upgrade",
                    },
                    task_id="public-upgrade",
                    operation_id="public-upgrade-start",
                )
                build = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer["run_id"],
                        **gate_binding(developer),
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed",
                            "payload": {
                                "source_revision": "public-upgrade-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["build and target verification"],
                            },
                        },
                    },
                    task_id="public-upgrade",
                    operation_id="public-upgrade-developer",
                )
                turn = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer["run_id"],
                        **gate_binding(build),
                        "response": {
                            "status": "completed",
                            "summary": "firmware artifact completed",
                            "payload": {
                                "source_revision": "public-upgrade-source",
                                "artifact_ref": artifact_ref(
                                    artifact,
                                    target="bmc.example",
                                    run_id=developer["run_id"],
                                    version="2.0.0",
                                ),
                            },
                        },
                        "deadline": deadline,
                    },
                    task_id="public-upgrade",
                    operation_id="public-upgrade-build",
                )
                first_turn = turn
                if turn["state"] == "running":
                    turn = service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "resume",
                            "run_id": developer["run_id"],
                            "deadline": 5.0,
                        },
                        task_id="public-upgrade-resume",
                        operation_id="public-upgrade-resume",
                    )
                projection = service.context_runtime.read_case(developer["run_id"])
            finally:
                service.close()
        return {"first_turn": first_turn, "final": turn, "projection": projection}

    def test_execute_upgrade_without_task_uri_verifies_installed_version(self) -> None:
        transport = MissingTaskUriTransport()
        result = self.run_public_upgrade(transport)

        self.assertEqual(result["final"]["state"], "completed", result["final"])
        uploads = [
            call
            for session in transport.sessions
            for call in session.calls
            if call[0] == "POST"
        ]
        self.assertEqual(len(uploads), 1)
        self.assertNotIn("TaskService", str(result["final"]))

    def test_execute_upgrade_survives_monitor_disconnect_and_reboot(self) -> None:
        transport = MonitorDisconnectTransport(
            manager_versions=("1.0.0", "2.0.0"),
        )
        result = self.run_public_upgrade(
            transport,
            deadline=0.000001,
            backend_type=FastPollingUpgradeBackend,
        )

        self.assertEqual(result["first_turn"]["state"], "running")
        self.assertEqual(result["final"]["state"], "completed", result["final"])
        self.assertEqual(transport.upload_attempts, 1)
        self.assertGreaterEqual(transport.manager_reads, 2)
        self.assertNotIn("TaskService", str(result["final"]))

    def test_execute_restart_recovers_accepted_upload_without_uploading_twice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "upgrade-crash.sqlite3"
            blobs = root / "blobs"
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware-2.0.0")
            journals = MutationJournalStore(root / "journals")
            faulted = threading.Event()
            transport = UncertainUpgradeTransport(
                manager_versions=("1.0.0", "2.0.0"),
                active_version="1.0.0",
                available_version="2.0.0",
                pending=True,
                faulted=faulted,
            )
            debug = WorkflowDebugBackend()
            upgrade = UpgradeMcpBackend(
                journal_store=journals,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )

            def service() -> RuntimeMcpService:
                return RuntimeMcpService(
                    OrchestratedMcpBackend(
                        {
                            "debug_run": debug,
                            "debug_collect": debug,
                            "upgrade_run": upgrade,
                        }
                    ),
                    context_repository=SQLiteRuntimeRepository(database),
                    blob_repository=FilesystemBlobRepository(blobs),
                )

            first = service()
            try:
                developer = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "bmc.example",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "build-upgrade",
                    },
                    task_id="upgrade-upload-loss",
                    operation_id="upgrade-upload-loss-start",
                )
                build = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer["run_id"],
                        **gate_binding(developer),
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed",
                            "payload": {
                                "source_revision": "upgrade-upload-loss-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["build and target verification"],
                            },
                        },
                    },
                    task_id="upgrade-upload-loss",
                    operation_id="upgrade-upload-loss-developer",
                )
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer["run_id"],
                        **gate_binding(build),
                        "response": {
                            "status": "completed",
                            "summary": "firmware artifact completed",
                            "payload": {
                                "source_revision": "upgrade-upload-loss-source",
                                "artifact_ref": artifact_ref(
                                    artifact,
                                    target="bmc.example",
                                    run_id=developer["run_id"],
                                    version="2.0.0",
                                ),
                            },
                        },
                        "deadline": 0.000001,
                    },
                    task_id="upgrade-upload-loss",
                    operation_id="upgrade-upload-loss-build",
                )
                self.assertTrue(faulted.wait(timeout=1))
                first_projection = first.context_runtime.read_case(
                    developer["run_id"]
                )
                effect_id = first_projection["effect_intents"][-1]["effect_id"]
                mutation_id = journals.load_for_task(
                    developer["run_id"]
                )[0].operation_id
            finally:
                first.close()

            second = service()
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": developer["run_id"]},
                    task_id="upgrade-upload-loss-resume",
                    operation_id="upgrade-upload-loss-resume",
                )
                projection = second.context_runtime.read_case(developer["run_id"])
                recovered_mutation_ids = {
                    journal.operation_id
                    for journal in journals.load_for_task(developer["run_id"])
                    if journal.action == "upgrade"
                }
            finally:
                second.close()

        self.assertEqual(running["state"], "running")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(transport.upload_attempts, 1)
        self.assertEqual(recovered_mutation_ids, {mutation_id})
        self.assertEqual(
            {
                operation["operation_id"]
                for operation in projection["operations"]
                if operation.get("operation") == "upgrade_run"
            },
            {effect_id},
        )

    def test_internal_runtime_disables_tls_verification_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
            )
            binding = backend._create_binding(
                "tls-default",
                {"ip": "bmc.example"},
            )
            try:
                self.assertFalse(binding.transport.verify_tls)
                self.assertEqual(
                    _UpgradeTask._key({"ip": "bmc.example"}),
                    _UpgradeTask._key(
                        {"ip": "bmc.example", "allow_insecure_tls": True}
                    ),
                )
            finally:
                binding.close()

    def test_direct_redfish_password_is_resolved_and_changes_binding_identity(self) -> None:
        first_arguments = {
            "ip": "bmc.example",
            "redfish_user": "Administrator",
            "redfish_password": "password-a",
        }
        credentials = _default_credential_loader(first_arguments)
        self.assertEqual(credentials["redfish"]["password"], "password-a")
        self.assertNotEqual(
            _UpgradeTask._key(first_arguments),
            _UpgradeTask._key(
                {**first_arguments, "redfish_password": "password-b"}
            ),
        )

    @staticmethod
    def arguments(artifact: Path, digest: str) -> dict[str, object]:
        return {
            "intent": "upgrade-and-verify",
            "ip": "bmc.example",
            "artifact_path": str(artifact),
            "artifact_sha256": digest,
            "product_version": "2.0.0",
            "deadline": TEST_DEADLINE_SECONDS,
        }

    def test_insecure_tls_override_rejects_non_boolean_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {},
            )
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaises(TypeError):
                    service.call_tool(
                        "upgrade_run",
                        {
                            **self.arguments(artifact, digest),
                            "allow_insecure_tls": "false",
                        },
                        task_id="task-upgrade-non-bool-tls",
                        operation_id="upgrade-non-bool-tls",
                    )
            finally:
                service.close()

    def test_frozen_policy_blocks_insecure_tls_before_transport_creation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: (_ for _ in ()).throw(
                    AssertionError("credentials must not be loaded")
                ),
            )
            task = backend.open_task("task-upgrade-tls-denied")
            arguments = {
                **self.arguments(artifact, digest),
                "allow_insecure_tls": True,
                "_task_authorization_policy": (
                    TaskAuthorizationPolicy.from_original_intent(
                        "upgrade-and-verify"
                    ).to_public_dict()
                ),
            }

            try:
                with self.assertRaises(MutationAuthorizationDenied):
                    backend.upgrade_run(
                        task,
                        arguments,
                        SimpleNamespace(raise_if_stopped=lambda: None),
                    )
            finally:
                backend.close_task(task)

    def test_explicit_insecure_tls_authorization_reaches_upgrade_transport(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = FakeRedfishTransport()
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda arguments: (
                    transport
                    if arguments["allow_insecure_tls"] is True
                    else (_ for _ in ()).throw(
                        AssertionError("insecure TLS flag was not preserved")
                    )
                ),
            )
            service = RuntimeMcpService(backend)

            try:
                result = service.call_tool(
                    "upgrade_run",
                    {
                        **self.arguments(artifact, digest),
                        "allow_insecure_tls": True,
                    },
                    task_id="task-upgrade-tls-authorized",
                    operation_id="upgrade-tls-authorized",
                )
            finally:
                service.close()

        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertGreaterEqual(transport.opens, 1)

    def run_uncertain_then_recover(
        self,
        *,
        root: Path,
        artifact: Path,
        digest: str,
        transport: UncertainUpgradeTransport,
        task_id: str,
        remove_artifact: bool = True,
    ) -> tuple[object, MutationJournalStore]:
        store = MutationJournalStore(root / "journals")
        backend = UpgradeMcpBackend(
            journal_store=store,
            credential_loader=lambda _arguments: {
                "redfish": {
                    "user": "Administrator",
                    "password": "redfish-secret",
                }
            },
            redfish_transport_factory=lambda _arguments: transport,
        )
        arguments = {
            **self.arguments(artifact, digest),
            "version_poll_interval": 0.001,
        }
        first = RuntimeMcpService(backend)
        try:
            with self.assertRaisesRegex(OSError, "upload connection lost"):
                first.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id=task_id,
                    operation_id="upgrade-recovery",
                )
        finally:
            first.close()
        self.assertEqual(transport.upload_attempts, 1)
        if remove_artifact:
            artifact.unlink()

        second = RuntimeMcpService(backend)
        try:
            result = second.call_tool(
                "upgrade_run",
                arguments,
                task_id=task_id,
                operation_id="upgrade-recovery",
            )
        finally:
            second.close()
        return result, store

    def test_redfish_session_bypasses_environment_proxies(self) -> None:
        class FakeResponse:
            status = 200
            headers = {}

            @staticmethod
            def read() -> bytes:
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, *, timeout):
                self.requests.append((request, timeout))
                return FakeResponse()

        opener = FakeOpener()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "HTTP_PROXY": "http://127.0.0.1:7890",
                    "HTTPS_PROXY": "http://127.0.0.1:7890",
                },
                clear=False,
            ),
            mock.patch(
                "openubmc_upgrade.runtime_backend.urlrequest.build_opener",
                return_value=opener,
            ) as build_opener,
            mock.patch(
                "openubmc_upgrade.runtime_backend.urlrequest.urlopen",
                side_effect=AssertionError("process-global opener must not be used"),
            ),
        ):
            session = RedfishHttpSession(
                target=SimpleNamespace(host="192.0.2.10", redfish_port=443),
                credentials=SimpleNamespace(user="admin", password="secret"),
                verify_tls=False,
                timeout=7,
            )
            response = session.request_json("GET", "/redfish/v1/UpdateService")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload, {"ok": True})
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0][1], 7)
        handlers = build_opener.call_args.args
        proxy_handlers = [
            handler
            for handler in handlers
            if isinstance(handler, urlrequest.ProxyHandler)
        ]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})

    def test_redfish_transport_failure_reports_request_size_and_timeout(self) -> None:
        class FailingOpener:
            def __init__(self) -> None:
                self.timeout = None

            def open(self, _request, *, timeout):
                self.timeout = timeout
                raise ConnectionResetError("peer reset")

        opener = FailingOpener()
        with mock.patch(
            "openubmc_upgrade.runtime_backend.urlrequest.build_opener",
            return_value=opener,
        ):
            session = RedfishHttpSession(
                target=SimpleNamespace(host="192.0.2.10", redfish_port=443),
                credentials=SimpleNamespace(user="admin", password="secret"),
                verify_tls=False,
                timeout=7,
            )
            with self.assertRaises(RedfishTransportError) as raised:
                session.request_json(
                    "POST",
                    "/redfish/v1/UpdateService/FirmwareInventory",
                    data=b"firmware-bytes",
                    timeout=321,
                )

        self.assertEqual(opener.timeout, 321)
        self.assertEqual(raised.exception.request_bytes, len(b"firmware-bytes"))
        self.assertEqual(raised.exception.timeout, 321)
        self.assertIn("cause=ConnectionResetError", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_http_push_uses_the_separate_upload_timeout(self) -> None:
        class TimeoutSession:
            def __init__(self) -> None:
                self.timeout = None

            def request_json(self, method, path, **kwargs):
                self.timeout = kwargs.get("timeout")
                self.method = method
                self.path = path
                return RedfishResponse(status=202, headers={}, payload={})

        session = TimeoutSession()
        result = UpgradeMcpBackend._upload(
            session,
            {"HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory"},
            SimpleNamespace(path="/tmp/openubmc.hpm"),
            b"firmware",
            {},
            upload_timeout=600,
        )

        self.assertEqual(session.method, "POST")
        self.assertEqual(
            session.path,
            "/redfish/v1/UpdateService/FirmwareInventory",
        )
        self.assertEqual(session.timeout, 600)
        self.assertEqual(result["upload_timeout_seconds"], 600)

    def test_multipart_body_includes_parameters_and_update_file(self) -> None:
        artifact = SimpleNamespace(path="/tmp/openubmc.hpm")
        body, boundary = _multipart_body(artifact, b"firmware-bytes")

        self.assertIn(
            b'Content-Disposition: form-data; name="UpdateParameters"',
            body,
        )
        self.assertIn(b"Content-Type: application/json\r\n\r\n{}", body)
        self.assertIn(
            b'Content-Disposition: form-data; name="UpdateFile"; filename="openubmc.hpm"',
            body,
        )
        self.assertIn(b"Content-Type: application/octet-stream", body)
        self.assertIn(b"firmware-bytes", body)
        self.assertTrue(body.endswith(f"\r\n--{boundary}--\r\n".encode("ascii")))

    def test_discovery_failure_before_upload_is_replan_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class DiscoveryFailureSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService":
                        return RedfishResponse(status=200, headers={}, payload=[])
                    return super().request_json(method, path, **kwargs)

            class DiscoveryFailureTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = DiscoveryFailureSession(self.opens)
                    self.sessions.append(session)
                    return session

            store = MutationJournalStore(root / "journals")
            backend = UpgradeMcpBackend(
                journal_store=store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: DiscoveryFailureTransport(),
            )
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(ValueError, "must be an object"):
                    service.call_tool(
                        "upgrade_run",
                        self.arguments(artifact, digest),
                        task_id="task-upgrade-discovery",
                        operation_id="upgrade-discovery",
                    )
            finally:
                service.close()

            journal = store.load("task-upgrade-discovery", "upgrade-discovery")

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "replan_required")
        self.assertFalse(journal.effects_started)

    def test_missing_upload_method_does_not_cross_the_effect_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class NoUploadMethodSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService":
                        return RedfishResponse(status=200, headers={}, payload={})
                    return super().request_json(method, path, **kwargs)

            class NoUploadMethodTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = NoUploadMethodSession(self.opens)
                    self.sessions.append(session)
                    return session

            store = MutationJournalStore(root / "journals")
            backend = UpgradeMcpBackend(
                journal_store=store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: NoUploadMethodTransport(),
            )
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "advertises no supported upload method",
                ):
                    service.call_tool(
                        "upgrade_run",
                        self.arguments(artifact, digest),
                        task_id="task-upgrade-no-upload-method",
                        operation_id="upgrade-no-upload-method",
                    )
            finally:
                service.close()

            journal = store.load(
                "task-upgrade-no-upload-method",
                "upgrade-no-upload-method",
            )

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "replan_required")
        self.assertFalse(journal.effects_started)

    def test_upload_failure_after_effect_boundary_remains_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class UploadFailureSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService/upload":
                        return RedfishResponse(status=500, headers={}, payload={})
                    return super().request_json(method, path, **kwargs)

            class UploadFailureTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = UploadFailureSession(self.opens)
                    self.sessions.append(session)
                    return session

            store = MutationJournalStore(root / "journals")
            transport = UploadFailureTransport()
            backend = UpgradeMcpBackend(
                journal_store=store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(RuntimeError, "returned HTTP 500"):
                    service.call_tool(
                        "upgrade_run",
                        self.arguments(artifact, digest),
                        task_id="task-upgrade-upload",
                        operation_id="upgrade-upload",
                    )
            finally:
                service.close()

            journal = store.load("task-upgrade-upload", "upgrade-upload")

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "mutation_failed")
        self.assertTrue(journal.effects_started)

    def test_explicit_upload_rejection_is_replan_required(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class RejectedSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService/upload":
                        raise RedfishHttpError(400, "missing multipart part")
                    return super().request_json(method, path, **kwargs)

            class RejectedTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = RejectedSession(self.opens)
                    self.sessions.append(session)
                    return session

            store = MutationJournalStore(root / "journals")
            backend = UpgradeMcpBackend(
                journal_store=store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: RejectedTransport(),
            )
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "explicitly rejected with HTTP 400",
                ):
                    service.call_tool(
                        "upgrade_run",
                        self.arguments(artifact, digest),
                        task_id="task-upgrade-rejected",
                        operation_id="upgrade-rejected",
                    )
            finally:
                service.close()

            journal = store.load("task-upgrade-rejected", "upgrade-rejected")

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "replan_required")
        self.assertFalse(journal.effects_started)

    def test_uploaded_bytes_are_the_bytes_bound_to_the_verified_digest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            verified_bytes = b"verified-firmware"
            artifact.write_bytes(verified_bytes)
            digest = hashlib.sha256(verified_bytes).hexdigest()
            uploaded: list[bytes] = []

            class MutatingSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService":
                        artifact.write_bytes(b"changed-after-digest-check")
                    if path == "/redfish/v1/UpdateService/upload":
                        uploaded.append(kwargs["data"])
                    return super().request_json(method, path, **kwargs)

            class MutatingTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = (
                        MutatingSession(self.opens)
                        if self.opens == 1
                        else FakeRedfishSession(self.opens)
                    )
                    self.sessions.append(session)
                    return session

            transport = MutatingTransport()
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(backend)
            try:
                service.call_tool(
                    "upgrade_run",
                    {
                        "intent": "upgrade-and-verify",
                        "ip": "bmc.example",
                        "artifact_path": str(artifact),
                        "artifact_sha256": digest,
                        "product_version": "2.0.0",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    task_id="task-upgrade",
                    operation_id="upgrade-stable-artifact",
                )
            finally:
                service.close()

        self.assertEqual(uploaded, [verified_bytes])

    def test_same_operation_id_rejects_a_different_simple_update_uri(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = FakeRedfishTransport(simple_update=True)
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(backend)
            arguments = {
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "image_uri": "https://files.example/first.hpm",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            try:
                service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="task-upgrade",
                    operation_id="upgrade-1",
                )
                with self.assertRaises(MutationOperationConflict):
                    service.call_tool(
                        "upgrade_run",
                        {
                            **arguments,
                            "image_uri": "https://files.example/second.hpm",
                        },
                        task_id="task-upgrade",
                        operation_id="upgrade-1",
                    )
            finally:
                service.close()

    def test_backend_uploads_once_then_reconnects_for_version_verification(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = FakeRedfishTransport()
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(backend)
            try:
                arguments = {
                    "intent": "upgrade-and-verify",
                    "ip": "bmc.example",
                    "artifact_path": str(artifact),
                    "artifact_sha256": digest,
                    "product_version": "2.0.0",
                    "_minimum_target_epoch": 6,
                    "deadline": TEST_DEADLINE_SECONDS,
                }
                result = service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="task-upgrade",
                    operation_id="upgrade-1",
                )
                replayed = service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="task-upgrade",
                    operation_id="upgrade-1",
                )
            finally:
                service.close()

        self.assertEqual(result["epoch_before"], 6)
        self.assertEqual(result["epoch_after"], 7)
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(result["journal"]["expected_checksum"], digest)
        self.assertEqual(result["mutation"]["artifact_path"], str(artifact))
        self.assertEqual(result["mutation"]["product_version"], "2.0.0")
        self.assertEqual(result["verification"]["installed_version"], "2.0.0")
        self.assertEqual(transport.opens, 2)
        uploads = [
            call
            for session in transport.sessions
            for call in session.calls
            if call[1] == "/redfish/v1/UpdateService/upload"
        ]
        self.assertEqual(len(uploads), 1)
        self.assertTrue(replayed["idempotent_replay"])

    def test_recovery_without_a_durable_journal_never_uploads_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = FakeRedfishTransport()
            service = RuntimeMcpService(
                UpgradeMcpBackend(
                    journal_store=MutationJournalStore(root / "journals"),
                    credential_loader=lambda _arguments: {
                        "redfish": {
                            "user": "Administrator",
                            "password": "redfish-secret",
                        }
                    },
                    redfish_transport_factory=lambda _arguments: transport,
                )
            )
            try:
                descriptor = service.catalog.require("upgrade_run")
                with self.assertRaisesRegex(
                    OSError, "no durable mutation journal"
                ):
                    service._execute_domain_value(
                        "upgrade_run",
                        descriptor,
                        {
                            "intent": "upgrade-and-verify",
                            "delivery_strategy": "build-upgrade",
                            "ip": "bmc.example",
                            "artifact_path": str(artifact),
                            "artifact_sha256": digest,
                            "product_version": "2.0.0",
                            "deadline": TEST_DEADLINE_SECONDS,
                        },
                        task_id="upgrade-recovery-without-journal",
                        operation_id="upgrade-recovery-without-journal",
                        recovery_mode=EffectRecoveryMode.RECONCILE,
                    )
            finally:
                service.close()

        uploads = [
            call
            for session in transport.sessions
            for call in session.calls
            if call[1] == "/redfish/v1/UpdateService/upload"
        ]
        self.assertEqual(uploads, [])

    def test_terminal_journal_replays_after_upgrade_artifact_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            journals = MutationJournalStore(root / "journals")
            arguments = {
                "intent": "upgrade-and-verify",
                "delivery_strategy": "build-upgrade",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(
                UpgradeMcpBackend(
                    journal_store=journals,
                    credential_loader=lambda _arguments: {
                        "redfish": {
                            "user": "Administrator",
                            "password": "redfish-secret",
                        }
                    },
                    redfish_transport_factory=lambda _arguments: (
                        FakeRedfishTransport()
                    ),
                )
            )
            try:
                first.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="terminal-upgrade",
                    operation_id="terminal-upgrade-effect",
                )
            finally:
                first.close()
            artifact.unlink()

            transport = FakeRedfishTransport()
            second = RuntimeMcpService(
                UpgradeMcpBackend(
                    journal_store=journals,
                    credential_loader=lambda _arguments: {
                        "redfish": {
                            "user": "Administrator",
                            "password": "redfish-secret",
                        }
                    },
                    redfish_transport_factory=lambda _arguments: transport,
                )
            )
            try:
                descriptor = second.catalog.require("upgrade_run")
                replayed = second._execute_domain_value(
                    "upgrade_run",
                    descriptor,
                    arguments,
                    task_id="terminal-upgrade",
                    operation_id="terminal-upgrade-effect",
                    recovery_mode=EffectRecoveryMode.RECONCILE,
                )
            finally:
                second.close()

        self.assertTrue(replayed["idempotent_replay"])
        self.assertEqual(replayed["journal"]["stage"], "verified")
        self.assertEqual(transport.sessions, [])

    def test_version_verification_waits_through_old_version_after_reboot(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class DelayedVersionSession(FakeRedfishSession):
                def __init__(self, number: int) -> None:
                    super().__init__(number)
                    self.version_reads = 0

                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/Managers/1":
                        self.calls.append((method, path))
                        self.version_reads += 1
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={
                                "FirmwareVersion": (
                                    "1.0.0" if self.version_reads == 1 else "2.0.0"
                                )
                            },
                        )
                    return super().request_json(method, path, **kwargs)

            class DelayedVersionTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = (
                        DelayedVersionSession(self.opens)
                        if self.opens == 2
                        else FakeRedfishSession(self.opens)
                    )
                    self.sessions.append(session)
                    return session

            transport = DelayedVersionTransport()
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool(
                    "upgrade_run",
                    {
                        **self.arguments(artifact, digest),
                        "version_poll_interval": 0.001,
                    },
                    task_id="task-upgrade-delayed-version",
                    operation_id="upgrade-delayed-version",
                )
            finally:
                service.close()

        self.assertEqual(result["verification"]["installed_version"], "2.0.0")
        verification_session = transport.sessions[1]
        self.assertEqual(verification_session.version_reads, 2)

    def test_version_verification_detects_fallback_to_previous_active_image(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class FallbackSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if self.number == 1 and path == "/redfish/v1/TaskService/Tasks/1":
                        self.calls.append((method, path))
                        raise OSError("target rebooted")
                    if self.number == 2 and path == "/redfish/v1/Managers/1":
                        self.calls.append((method, path))
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={"FirmwareVersion": "1.0.0"},
                        )
                    if self.number == 2 and path == "/redfish/v1/UpdateService":
                        self.calls.append((method, path))
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={
                                "FirmwareInventory": {
                                    "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory"
                                },
                                "Task": None,
                                "Oem": {
                                    "openUBMC": {
                                        "FirmwareToTakeEffect": [],
                                        "BackgroundUpdateTasks": [],
                                        "SyncUpdateState": None,
                                    }
                                },
                            },
                        )
                    if (
                        self.number == 2
                        and path == "/redfish/v1/UpdateService/FirmwareInventory"
                    ):
                        self.calls.append((method, path))
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={
                                "Members": [
                                    {
                                        "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/ActiveBMC"
                                    },
                                    {
                                        "@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/AvailableBMC"
                                    },
                                ]
                            },
                        )
                    if self.number == 2 and path.endswith("/ActiveBMC"):
                        self.calls.append((method, path))
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={"Version": "1.0.0"},
                        )
                    if self.number == 2 and path.endswith("/AvailableBMC"):
                        self.calls.append((method, path))
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={"Version": "2.0.0"},
                        )
                    return super().request_json(method, path, **kwargs)

            class FallbackTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = FallbackSession(self.opens)
                    self.sessions.append(session)
                    return session

            store = MutationJournalStore(root / "journals")
            transport = FallbackTransport()
            backend = UpgradeMcpBackend(
                journal_store=store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            service = RuntimeMcpService(backend)
            try:
                arguments = {
                    **self.arguments(artifact, digest),
                    "version_poll_interval": 0.001,
                }
                with self.assertRaisesRegex(
                    UpgradeActivationReverted,
                    "remains only in AvailableBMC",
                ):
                    service.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-upgrade-fallback",
                        operation_id="upgrade-fallback",
                    )
                with self.assertRaisesRegex(
                    UpgradeActivationReverted,
                    "was not uploaded again",
                ):
                    service.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-upgrade-fallback",
                        operation_id="upgrade-fallback",
                    )
            finally:
                service.close()

            journal = store.load("task-upgrade-fallback", "upgrade-fallback")

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "verification_failed_terminal")
        self.assertEqual(journal.last_known_state, "activation-fallback")
        self.assertEqual(journal.recovery_decision, "none")
        self.assertTrue(journal.terminal)
        self.assertFalse(journal.blocks_target)
        self.assertTrue(journal.effects_started)
        uploads = [
            call
            for session in transport.sessions
            for call in session.calls
            if call[1] == "/redfish/v1/UpdateService/upload"
        ]
        self.assertEqual(len(uploads), 1)

    def test_uncertain_upgrade_with_no_effect_becomes_replan_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = UncertainUpgradeTransport(
                manager_versions=("1.0.0",),
                active_version="1.0.0",
            )

            result, store = self.run_uncertain_then_recover(
                root=root,
                artifact=artifact,
                digest=digest,
                transport=transport,
                task_id="task-upgrade-recovery-no-effect",
            )
            journal = store.load(
                "task-upgrade-recovery-no-effect",
                "upgrade-recovery",
            )

        self.assertEqual(result["mutation"]["recovery"]["decision"], "replan")
        self.assertIsNone(result["verification"])
        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "replan_required")
        self.assertFalse(journal.effects_started)
        self.assertFalse(journal.blocks_target)
        self.assertEqual(transport.upload_attempts, 1)

    def test_uncertain_upgrade_already_installed_verifies_without_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = UncertainUpgradeTransport(
                manager_versions=("2.0.0",),
                active_version="2.0.0",
            )

            result, store = self.run_uncertain_then_recover(
                root=root,
                artifact=artifact,
                digest=digest,
                transport=transport,
                task_id="task-upgrade-recovery-installed",
            )
            journal = store.load(
                "task-upgrade-recovery-installed",
                "upgrade-recovery",
            )

        self.assertEqual(result["verification"]["installed_version"], "2.0.0")
        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "verified")
        self.assertEqual(transport.upload_attempts, 1)

    def test_uncertain_upgrade_pending_activation_waits_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = UncertainUpgradeTransport(
                manager_versions=("1.0.0", "2.0.0"),
                active_version="1.0.0",
                pending=True,
            )

            result, store = self.run_uncertain_then_recover(
                root=root,
                artifact=artifact,
                digest=digest,
                transport=transport,
                task_id="task-upgrade-recovery-pending",
            )
            journal = store.load(
                "task-upgrade-recovery-pending",
                "upgrade-recovery",
            )

        self.assertEqual(result["verification"]["installed_version"], "2.0.0")
        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "verified")
        self.assertGreaterEqual(transport.manager_reads, 2)
        self.assertEqual(transport.upload_attempts, 1)

    def test_uncertain_upgrade_available_without_pending_is_terminal_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = UncertainUpgradeTransport(
                manager_versions=("1.0.0",),
                active_version="1.0.0",
                available_version="2.0.0",
            )
            store = MutationJournalStore(root / "journals")
            backend = UpgradeMcpBackend(
                journal_store=store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            arguments = self.arguments(artifact, digest)
            first = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(OSError, "upload connection lost"):
                    first.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-upgrade-recovery-fallback",
                        operation_id="upgrade-recovery",
                    )
            finally:
                first.close()
            artifact.unlink()

            second = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(
                    UpgradeActivationReverted,
                    "available but not active",
                ):
                    second.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-upgrade-recovery-fallback",
                        operation_id="upgrade-recovery",
                    )
            finally:
                second.close()
            journal = store.load(
                "task-upgrade-recovery-fallback",
                "upgrade-recovery",
            )

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "verification_failed_terminal")
        self.assertEqual(journal.last_known_state, "activation-fallback")
        self.assertFalse(journal.blocks_target)
        self.assertEqual(transport.upload_attempts, 1)


if __name__ == "__main__":
    unittest.main()
