from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib import request as urlrequest


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "openubmc-target-runtime"))
sys.path.insert(0, str(REPO_ROOT / "openubmc-upgrade"))

from openubmc_target_runtime import (  # noqa: E402
    MutationAuthorizationDenied,
    MutationJournalStore,
    MutationOperationConflict,
    RuntimeMcpService,
    TaskAuthorizationPolicy,
)
from openubmc_upgrade.runtime_backend import (  # noqa: E402
    RedfishHttpSession,
    RedfishHttpError,
    RedfishResponse,
    RedfishTransportError,
    UpgradeArtifact,
    UpgradeActivationReverted,
    UpgradeMcpBackend,
    UpgradeRuntimeAdapter,
    _SharedArtifactSource,
    _UpgradeTask,
    _default_credential_loader,
    _legacy_http_push_uses_multipart,
    _multipart_body,
    _multipart_parts,
    _resolved_mutation_options,
    _upgrade_upload_plan,
)
from openubmc_upgrade.webui import WebUiHttpError, WebUiResponse  # noqa: E402


TEST_DEADLINE_SECONDS = 30


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
                payload={
                    "FirmwareVersion": "2.0.0",
                    "LastResetTime": (
                        "2026-09-05T00:01:00Z" if self.number > 1
                        else "2026-09-05T00:00:00Z"
                    ),
                },
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
    ) -> None:
        super().__init__()
        self.manager_versions = manager_versions
        self.active_version = active_version
        self.available_version = available_version
        self.pending = pending
        self.manager_reads = 0
        self.upload_attempts = 0

    def open_session(self, *, target, credentials) -> FakeRedfishSession:
        self.opens += 1
        session = UncertainUpgradeSession(self.opens, self)
        self.sessions.append(session)
        return session


class FakeWebUiClient:
    def __init__(self, session: "WebUiUpgradeSession") -> None:
        self.session = session
        self.logged_in = False

    def login(self) -> WebUiResponse:
        self.logged_in = True
        self.session.transport.web_logins += 1
        return WebUiResponse(200, {}, {"Token": "csrf", "Session": {"SessionID": "1"}})

    def upload(self, *, body, boundary, content_length, timeout) -> WebUiResponse:
        self.session.transport.web_uploads += 1
        payload = body if isinstance(body, bytes) else b"".join(body)
        self.session.transport.uploaded_bodies.append(payload)
        self.session.transport.upload_boundaries.append(boundary)
        self.session.transport.upload_lengths.append(content_length)
        self.session.transport.upload_timeouts.append(timeout)
        return WebUiResponse(200, {}, {})

    def start(self, file_path: str) -> WebUiResponse:
        self.session.transport.web_starts += 1
        self.session.transport.started_paths.append(file_path)
        return WebUiResponse(200, {}, {"url": "/UI/Rest/Task/1"})

    def progress(self, task_id: str = "") -> WebUiResponse:
        self.session.transport.web_progress_reads += 1
        self.session.transport.progress_task_ids.append(task_id)
        return WebUiResponse(
            200,
            {},
            {
                "UpgradeMode": "Serial",
                "UpgradeTasks": [
                    {
                        "TaskName": "HWSR Upgrade Task",
                        "Component": "HWSR",
                        "FileName": self.session.transport.artifact_name,
                        "Percentage": "100%",
                        "TaskState": "Completed",
                        "ErrorCode": 0,
                        "Version": "1.54",
                    }
                ],
            },
        )

    def close(self) -> dict[str, object]:
        self.logged_in = False
        self.session.transport.web_closes += 1
        return {
            "attempted": True,
            "completed": True,
            "http_status": 200,
            "error": "",
        }


class WebUiUpgradeSession(FakeRedfishSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number)
        self.transport = transport
        self.webui = FakeWebUiClient(self)

    def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
        self.calls.append((method, path))
        if path == "/redfish/v1/UpdateService":
            return RedfishResponse(
                status=200,
                headers={},
                payload={
                    "HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory",
                    "Actions": {
                        "#UpdateService.SimpleUpdate": {"target": "/simple"}
                    },
                },
            )
        raise AssertionError(f"unexpected Redfish request: {method} {path}")


class WebUiUpgradeTransport(FakeRedfishTransport):
    def __init__(self, artifact_name: str) -> None:
        super().__init__()
        self.artifact_name = artifact_name
        self.web_logins = 0
        self.web_uploads = 0
        self.web_starts = 0
        self.web_progress_reads = 0
        self.web_closes = 0
        self.uploaded_bodies: list[bytes] = []
        self.upload_boundaries: list[str] = []
        self.upload_lengths: list[int] = []
        self.upload_timeouts: list[float] = []
        self.started_paths: list[str] = []
        self.progress_task_ids: list[str] = []

    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = WebUiUpgradeSession(self.opens, self)
        self.sessions.append(session)
        return session


class StartRejectedWebUiClient(FakeWebUiClient):
    def start(self, file_path: str) -> WebUiResponse:
        self.session.transport.web_starts += 1
        self.session.transport.started_paths.append(file_path)
        raise WebUiHttpError(400, "unsupported start property")


class StartRejectedWebUiSession(WebUiUpgradeSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number, transport)
        self.webui = StartRejectedWebUiClient(self)


class StartRejectedWebUiTransport(WebUiUpgradeTransport):
    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = StartRejectedWebUiSession(self.opens, self)
        self.sessions.append(session)
        return session


class UncertainWebUiClient(FakeWebUiClient):
    def upload(self, *, body, boundary, content_length, timeout) -> WebUiResponse:
        self.session.transport.web_uploads += 1
        raise OSError("WebUI upload connection lost")


class RecoverableUncertainWebUiClient(UncertainWebUiClient):
    def progress(self, task_id: str = "") -> WebUiResponse:
        self.session.transport.web_progress_reads += 1
        self.session.transport.progress_task_ids.append(task_id)
        current = self.session.transport.web_uploads > 0
        return WebUiResponse(
            200,
            {},
            {
                "UpgradeTasks": [
                    {
                        "TaskName": (
                            "Current HWSR Upgrade Task"
                            if current
                            else "Historical HWSR Upgrade Task"
                        ),
                        "Component": "HWSR",
                        "FileName": self.session.transport.artifact_name,
                        "Percentage": "100%",
                        "TaskState": "Completed",
                        "ErrorCode": 0,
                        "Version": "1.54" if current else "1.53",
                    }
                ]
            },
        )


class ReorderedHistoricalUncertainWebUiClient(UncertainWebUiClient):
    def progress(self, task_id: str = "") -> WebUiResponse:
        self.session.transport.web_progress_reads += 1
        self.session.transport.progress_task_ids.append(task_id)
        tasks = [
            {
                "TaskName": "Historical HWSR Upgrade Task A",
                "Component": "HWSR",
                "FileName": self.session.transport.artifact_name,
                "Percentage": "100%",
                "TaskState": "Completed",
                "ErrorCode": 0,
                "Version": "1.52",
            },
            {
                "TaskName": "Historical HWSR Upgrade Task B",
                "Component": "HWSR",
                "FileName": self.session.transport.artifact_name,
                "Percentage": "100%",
                "TaskState": "Completed",
                "ErrorCode": 0,
                "Version": "1.53",
            },
        ]
        if self.session.transport.web_uploads:
            tasks.reverse()
        return WebUiResponse(200, {}, {"UpgradeTasks": tasks})


class StateChangedHistoricalUncertainStartWebUiClient(FakeWebUiClient):
    def start(self, file_path: str) -> WebUiResponse:
        self.session.transport.web_starts += 1
        self.session.transport.started_paths.append(file_path)
        raise OSError("WebUI start response lost")

    def progress(self, task_id: str = "") -> WebUiResponse:
        self.session.transport.web_progress_reads += 1
        self.session.transport.progress_task_ids.append(task_id)
        completed = self.session.transport.web_starts > 0
        return WebUiResponse(
            200,
            {},
            {
                "UpgradeTasks": [
                    {
                        "TaskName": "Historical HWSR Upgrade Task",
                        "Component": "HWSR",
                        "FileName": self.session.transport.artifact_name,
                        "Percentage": "100%" if completed else "50%",
                        "TaskState": "Completed" if completed else "Running",
                        "ErrorCode": 0,
                        "Version": "1.53",
                    }
                ]
            },
        )


class UncertainWebUiSession(WebUiUpgradeSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number, transport)
        self.webui = UncertainWebUiClient(self)


class UncertainWebUiTransport(WebUiUpgradeTransport):
    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = UncertainWebUiSession(self.opens, self)
        self.sessions.append(session)
        return session


class RecoverableUncertainWebUiSession(WebUiUpgradeSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number, transport)
        self.webui = RecoverableUncertainWebUiClient(self)


class RecoverableUncertainWebUiTransport(WebUiUpgradeTransport):
    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = RecoverableUncertainWebUiSession(self.opens, self)
        self.sessions.append(session)
        return session


class ReorderedHistoricalUncertainWebUiSession(WebUiUpgradeSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number, transport)
        self.webui = ReorderedHistoricalUncertainWebUiClient(self)


class ReorderedHistoricalUncertainWebUiTransport(WebUiUpgradeTransport):
    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = ReorderedHistoricalUncertainWebUiSession(self.opens, self)
        self.sessions.append(session)
        return session


class StateChangedHistoricalUncertainStartWebUiSession(WebUiUpgradeSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number, transport)
        self.webui = StateChangedHistoricalUncertainStartWebUiClient(self)


class StateChangedHistoricalUncertainStartWebUiTransport(WebUiUpgradeTransport):
    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = StateChangedHistoricalUncertainStartWebUiSession(self.opens, self)
        self.sessions.append(session)
        return session


class CleanupFailingWebUiClient(FakeWebUiClient):
    def close(self) -> dict[str, object]:
        self.logged_in = False
        self.session.transport.web_closes += 1
        return {
            "attempted": True,
            "completed": False,
            "http_status": 500,
            "error": "session cleanup returned HTTP 500",
        }


class CleanupFailingWebUiSession(WebUiUpgradeSession):
    def __init__(self, number: int, transport: "WebUiUpgradeTransport") -> None:
        super().__init__(number, transport)
        self.webui = CleanupFailingWebUiClient(self)


class CleanupFailingWebUiTransport(WebUiUpgradeTransport):
    def open_session(self, *, target, credentials) -> WebUiUpgradeSession:
        self.opens += 1
        session = CleanupFailingWebUiSession(self.opens, self)
        self.sessions.append(session)
        return session


class UpgradeRuntimeBackendTests(unittest.TestCase):
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
    def batch_context(operation_id: str = "batch-upgrade"):
        class Context:
            task_id = "batch-task"

            def __init__(self, selected_id: str) -> None:
                self.operation_id = selected_id

            def derive(self, selected_id: str):
                return Context(selected_id)

            @staticmethod
            def raise_if_stopped() -> None:
                return None

            @staticmethod
            def remaining() -> float:
                return TEST_DEADLINE_SECONDS

            @staticmethod
            def wait(seconds: float) -> None:
                time.sleep(min(seconds, 0.001))

        return Context(operation_id)

    @staticmethod
    def batch_arguments() -> dict[str, object]:
        return {
            "intent": "upgrade-and-verify",
            "targets": [
                {"target_id": "bmc-a", "ip": "192.0.2.10"},
                {"target_id": "bmc-b", "ip": "192.0.2.11"},
            ],
            "artifact_path": "/tmp/openubmc-batch.hpm",
            "artifact_sha256": "a" * 64,
            "product_version": "2.0.0",
            "max_concurrency": 2,
            "preflight": False,
        }

    def test_batch_runs_targets_in_parallel_and_preserves_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")
            barrier = threading.Barrier(2, timeout=2)
            active = 0
            maximum_active = 0
            lock = threading.Lock()

            def run_one(_task, arguments, context):
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                barrier.wait()
                with lock:
                    active -= 1
                return {
                    "operation_id": context.operation_id,
                    "target_fingerprint": arguments["ip"],
                    "epoch_after": 1,
                    "journal": {"stage": "verified"},
                }

            try:
                with mock.patch.object(backend, "_upgrade_one", side_effect=run_one):
                    result = backend.upgrade_batch(
                        task,
                        self.batch_arguments(),
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(maximum_active, 2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [item["target_id"] for item in result["targets"]],
            ["bmc-a", "bmc-b"],
        )
        self.assertEqual(
            len({item["operation_id"] for item in result["targets"]}),
            2,
        )

    def test_batch_failure_does_not_cancel_successful_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")

            def run_one(_task, arguments, context):
                if arguments["ip"] == "192.0.2.10":
                    raise ValueError("target rejected upgrade")
                return {
                    "operation_id": context.operation_id,
                    "target_fingerprint": arguments["ip"],
                    "epoch_after": 2,
                    "journal": {"stage": "verified"},
                }

            try:
                with mock.patch.object(backend, "_upgrade_one", side_effect=run_one):
                    result = backend.upgrade_batch(
                        task,
                        self.batch_arguments(),
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["unknown"], 0)
        self.assertEqual(result["targets"][0]["status"], "failed")
        self.assertEqual(result["targets"][1]["status"], "completed")

    def test_batch_enforces_concurrency_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")
            arguments = self.batch_arguments()
            arguments["targets"] = [
                {"target_id": f"bmc-{index}", "ip": f"192.0.2.{index}"}
                for index in range(1, 5)
            ]
            arguments["max_concurrency"] = 2
            active = 0
            maximum_active = 0
            lock = threading.Lock()

            def run_one(_task, target_arguments, context):
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.02)
                with lock:
                    active -= 1
                return {
                    "operation_id": context.operation_id,
                    "target_fingerprint": target_arguments["ip"],
                    "epoch_after": 1,
                    "journal": {"stage": "verified"},
                }

            try:
                with mock.patch.object(backend, "_upgrade_one", side_effect=run_one):
                    result = backend.upgrade_batch(
                        task,
                        arguments,
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(maximum_active, 2)
        self.assertEqual(result["succeeded"], 4)

    def test_batch_classifies_non_success_journal_per_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")

            def run_one(_task, target_arguments, context):
                stage = (
                    "replan_required"
                    if target_arguments["ip"] == "192.0.2.10"
                    else "verified"
                )
                return {
                    "operation_id": context.operation_id,
                    "target_fingerprint": target_arguments["ip"],
                    "epoch_after": 1,
                    "journal": {"stage": stage},
                }

            try:
                with mock.patch.object(backend, "_upgrade_one", side_effect=run_one):
                    result = backend.upgrade_batch(
                        task,
                        self.batch_arguments(),
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["targets"][0]["status"], "failed")

    def test_batch_reports_all_uncertain_targets_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")

            def run_one(_task, _arguments, _context):
                error = OSError("connection lost after upload")
                error.mutation_outcome = "unknown"
                error.mutation_effects_started = True
                raise error

            try:
                with mock.patch.object(backend, "_upgrade_one", side_effect=run_one):
                    result = backend.upgrade_batch(
                        task,
                        self.batch_arguments(),
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["unknown"], 2)
        self.assertEqual(
            [target["status"] for target in result["targets"]],
            ["unknown", "unknown"],
        )

    def test_batch_mcp_caches_unknown_aggregate_without_blind_replay(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            service = RuntimeMcpService(backend)

            def run_one(_task, _arguments, _context):
                error = OSError("connection lost after upload")
                error.mutation_outcome = "unknown"
                error.mutation_effects_started = True
                raise error

            try:
                with mock.patch.object(
                    backend,
                    "_upgrade_one",
                    side_effect=run_one,
                ) as upgrade_one:
                    first = service.call_tool(
                        "upgrade_batch",
                        self.batch_arguments(),
                        task_id="unknown-batch-task",
                        operation_id="unknown-batch",
                    )
                    second = service.call_tool(
                        "upgrade_batch",
                        self.batch_arguments(),
                        task_id="unknown-batch-task",
                        operation_id="unknown-batch",
                    )
            finally:
                service.close()

        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(upgrade_one.call_count, 2)

    def test_batch_rejects_duplicate_target_before_starting_workers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")
            arguments = self.batch_arguments()
            arguments["targets"] = [
                {"target_id": "first", "ip": "192.0.2.10"},
                {"target_id": "second", "ip": "192.0.2.10"},
            ]
            try:
                with mock.patch.object(backend, "_upgrade_one") as upgrade_one:
                    with self.assertRaisesRegex(ValueError, "duplicate Redfish target"):
                        backend.upgrade_batch(
                            task,
                            arguments,
                            self.batch_context(),
                        )
                upgrade_one.assert_not_called()
            finally:
                backend.close_task(task)

    def test_batch_rejects_equivalent_ipv6_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")
            arguments = self.batch_arguments()
            arguments["targets"] = [
                {"target_id": "first", "ip": "2001:0db8::1"},
                {"target_id": "second", "ip": "[2001:db8::1]"},
            ]
            try:
                with mock.patch.object(backend, "_upgrade_one") as upgrade_one:
                    with self.assertRaisesRegex(ValueError, "duplicate Redfish target"):
                        backend.upgrade_batch(
                            task,
                            arguments,
                            self.batch_context(),
                        )
                upgrade_one.assert_not_called()
            finally:
                backend.close_task(task)

    def test_target_deadline_starts_when_worker_starts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")
            arguments = self.batch_arguments()
            arguments["max_concurrency"] = 1
            arguments["target_deadline"] = 0.2
            observed: list[float] = []

            def run_one(_task, target_arguments, context):
                if target_arguments["target_id"] == "bmc-a":
                    time.sleep(0.08)
                observed.append(context.remaining())
                return {
                    "operation_id": context.operation_id,
                    "target_fingerprint": target_arguments["ip"],
                    "epoch_after": 1,
                    "journal": {"stage": "verified"},
                }

            try:
                with mock.patch.object(backend, "_upgrade_one", side_effect=run_one):
                    result = backend.upgrade_batch(
                        task,
                        arguments,
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(observed), 2)
        self.assertGreater(observed[1], 0.15)

    def test_canary_failure_stops_remaining_targets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(Path(raw) / "journals")
            )
            task = backend.open_task("batch-task")
            arguments = self.batch_arguments()
            arguments["targets"] = [
                {"target_id": f"bmc-{index}", "ip": f"192.0.2.{index}"}
                for index in range(1, 4)
            ]
            arguments["canary_count"] = 1

            def run_one(_task, target_arguments, _context):
                raise ValueError(f"{target_arguments['target_id']} rejected upgrade")

            try:
                with mock.patch.object(
                    backend,
                    "_upgrade_one",
                    side_effect=run_one,
                ) as upgrade_one:
                    result = backend.upgrade_batch(
                        task,
                        arguments,
                        self.batch_context(),
                    )
            finally:
                backend.close_task(task)

        self.assertEqual(upgrade_one.call_count, 1)
        self.assertEqual(result["stop_reason"], "canary_failed")
        self.assertEqual(result["skipped"], 2)
        self.assertEqual(
            [target.get("skipped", False) for target in result["targets"]],
            [False, True, True],
        )

    def test_batch_preflight_failure_aborts_all_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class DiscoveryFailureSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService":
                        raise OSError("preflight discovery failed")
                    return super().request_json(method, path, **kwargs)

            class DiscoveryFailureTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = DiscoveryFailureSession(self.opens)
                    self.sessions.append(session)
                    return session

            transports: dict[str, FakeRedfishTransport] = {}
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda arguments: transports.setdefault(
                    str(arguments["ip"]),
                    DiscoveryFailureTransport()
                    if str(arguments["ip"]) == "192.0.2.30"
                    else FakeRedfishTransport(),
                ),
            )
            task = backend.open_task("batch-preflight-task")
            arguments = {
                "intent": "upgrade-and-verify",
                "targets": [
                    {"target_id": "bad", "ip": "192.0.2.30"},
                    {"target_id": "good", "ip": "192.0.2.31"},
                ],
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "max_concurrency": 2,
                "preflight": True,
                "preflight_timeout": 1,
            }
            try:
                result = backend.upgrade_batch(task, arguments, self.batch_context())
            finally:
                backend.close_task(task)

        self.assertEqual(result["stop_reason"], "preflight_failed")
        self.assertEqual(result["targets"][1]["skipped"], True)
        good_uploads = [
            call
            for session in transports["192.0.2.31"].sessions
            for call in session.calls
            if call == ("POST", "/redfish/v1/UpdateService/upload")
        ]
        self.assertEqual(good_uploads, [])

    def test_batch_artifact_version_mismatch_aborts_before_all_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "rootfs_openUBMC_12.00.05.03_release.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transports: dict[str, FakeRedfishTransport] = {}
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda arguments: transports.setdefault(
                    str(arguments["ip"]), FakeRedfishTransport()
                ),
            )
            task = backend.open_task("batch-version-lock-task")
            arguments = {
                "intent": "upgrade-and-verify",
                "targets": [
                    {"target_id": "bmc-a", "ip": "192.0.2.32"},
                    {"target_id": "bmc-b", "ip": "192.0.2.33"},
                ],
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "12.00.05.15",
                "max_concurrency": 2,
                "preflight": True,
            }
            try:
                result = backend.upgrade_batch(task, arguments, self.batch_context())
            finally:
                backend.close_task(task)

        self.assertEqual(result["stop_reason"], "artifact_preflight_failed")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 2)
        for transport in transports.values():
            uploads = [
                call
                for session in transport.sessions
                for call in session.calls
                if call == ("POST", "/redfish/v1/UpdateService/upload")
            ]
            self.assertEqual(uploads, [])

    def test_batch_reconciles_new_outer_identity_without_reuploading_completed_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            completed_transport = FakeRedfishTransport()
            uncertain_transport = UncertainUpgradeTransport(
                manager_versions=("1.0.0",),
                active_version="1.0.0",
            )
            transports = {
                "192.0.2.40": completed_transport,
                "192.0.2.41": uncertain_transport,
            }
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda arguments: transports[str(arguments["ip"])],
            )
            service = RuntimeMcpService(backend)
            arguments = {
                "intent": "upgrade-and-verify",
                "targets": [
                    {"target_id": "completed", "ip": "192.0.2.40"},
                    {"target_id": "unknown", "ip": "192.0.2.41"},
                ],
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "max_concurrency": 2,
                "deadline": TEST_DEADLINE_SECONDS,
            }
            try:
                first = service.call_tool(
                    "upgrade_batch",
                    arguments,
                    task_id="batch-reconcile-task",
                    operation_id="batch-first",
                )
                second = service.call_tool(
                    "upgrade_batch",
                    arguments,
                    task_id="batch-reconcile-task",
                    operation_id="batch-second",
                )
            finally:
                service.close()

        self.assertEqual(first["targets"][0]["status"], "completed")
        self.assertEqual(second["targets"][0]["status"], "completed")
        completed_uploads = [
            call
            for session in completed_transport.sessions
            for call in session.calls
            if call == ("POST", "/redfish/v1/UpdateService/upload")
        ]
        self.assertEqual(len(completed_uploads), 1)
        self.assertEqual(uncertain_transport.upload_attempts, 1)

    def test_batch_terminal_replay_does_not_require_local_artifact(self) -> None:
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
            task = backend.open_task("batch-terminal-replay-task")
            arguments = {
                "intent": "upgrade-and-verify",
                "targets": [{"target_id": "bmc-a", "ip": "192.0.2.50"}],
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "max_concurrency": 1,
            }
            try:
                first = backend.upgrade_batch(
                    task,
                    arguments,
                    self.batch_context("batch-first"),
                )
                artifact.unlink()
                second = backend.upgrade_batch(
                    task,
                    arguments,
                    self.batch_context("batch-second"),
                )
            finally:
                backend.close_task(task)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertTrue(second["targets"][0]["result"]["idempotent_replay"])
        uploads = [
            call
            for session in transport.sessions
            for call in session.calls
            if call == ("POST", "/redfish/v1/UpdateService/upload")
        ]
        self.assertEqual(len(uploads), 1)

    def test_batch_artifact_change_during_upload_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware-bytes")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class MutatingSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService/upload":
                        self.calls.append((method, path))
                        self.transport.upload_attempts += 1
                        body = iter(kwargs["data"])
                        next(body)
                        artifact.write_bytes(b"changed-firmware")
                        list(body)
                        raise AssertionError("artifact mutation must abort the upload")
                    return super().request_json(method, path, **kwargs)

            class MutatingTransport(FakeRedfishTransport):
                def __init__(self) -> None:
                    super().__init__()
                    self.upload_attempts = 0

                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = MutatingSession(self.opens)
                    session.transport = self
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
            task = backend.open_task("batch-stream-change-task")
            arguments = {
                "intent": "upgrade-and-verify",
                "targets": [{"target_id": "bmc-a", "ip": "192.0.2.51"}],
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "max_concurrency": 1,
            }
            try:
                result = backend.upgrade_batch(
                    task,
                    arguments,
                    self.batch_context("batch-stream-change"),
                )
            finally:
                backend.close_task(task)

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["targets"][0]["status"], "unknown")
        self.assertEqual(transport.upload_attempts, 1)

    def test_streaming_multipart_body_is_length_delimited_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact_path = Path(raw) / "openubmc.hpm"
            artifact_path.write_bytes(b"firmware-bytes")
            artifact = UpgradeArtifact(
                path=str(artifact_path),
                sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                product_version="2.0.0",
            )
            source = _SharedArtifactSource(artifact_path)

            class CapturingSession:
                def request_json(self, method, path, **kwargs):
                    self.body = kwargs["data"]
                    self.headers = kwargs["headers"]
                    self.payload = b"".join(self.body)
                    return RedfishResponse(status=202, headers={}, payload={})

            session = CapturingSession()
            result = UpgradeMcpBackend._upload(
                session,
                {"HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory"},
                artifact,
                None,
                {},
                upload_timeout=10,
                artifact_source=source,
            )

        self.assertNotIsInstance(session.body, (bytes, bytearray))
        self.assertEqual(int(session.headers["Content-Length"]), len(session.payload))
        self.assertIn(b"firmware-bytes", session.payload)
        self.assertEqual(result["encoding"], "multipart/form-data")

    def test_streaming_artifact_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact_path = Path(raw) / "openubmc.hpm"
            artifact_path.write_bytes(b"firmware-bytes")
            source = _SharedArtifactSource(artifact_path)
            body = iter(source.octet_stream())
            next(body)
            artifact_path.write_bytes(b"changed-firmware")
            with self.assertRaises(OSError):
                list(body)

    def test_sidecar_version_mismatch_is_rejected_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            content = b"firmware"
            artifact.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            Path(f"{artifact}.metadata.json").write_text(
                '{"artifact":{"sha256":"'
                + digest
                + '","size":8},"product_version":"12.00.05.15"}',
                encoding="utf-8",
            )
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
                with self.assertRaisesRegex(
                    ValueError,
                    "metadata product version does not match",
                ):
                    service.call_tool(
                        "upgrade_run",
                        {
                            **self.arguments(artifact, digest),
                            "product_version": "12.00.05.03",
                        },
                        task_id="task-upgrade-sidecar-version",
                        operation_id="upgrade-sidecar-version",
                    )
            finally:
                service.close()

        uploads = [
            call
            for session in transport.sessions
            for call in session.calls
            if call == ("POST", "/redfish/v1/UpdateService/upload")
        ]
        self.assertEqual(uploads, [])

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

    def test_batch_mcp_reuses_one_artifact_read_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transports: dict[str, FakeRedfishTransport] = {}
            backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda arguments: transports.setdefault(
                    str(arguments["ip"]),
                    FakeRedfishTransport(),
                ),
            )
            service = RuntimeMcpService(backend)
            arguments = {
                "intent": "upgrade-and-verify",
                "targets": [
                    {"target_id": "bmc-a", "ip": "192.0.2.20"},
                    {"target_id": "bmc-b", "ip": "192.0.2.21"},
                ],
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "2.0.0",
                "max_concurrency": 2,
                "deadline": TEST_DEADLINE_SECONDS,
                "preflight": False,
            }

            try:
                with mock.patch(
                    "openubmc_upgrade.runtime_backend._snapshot_stable_artifact",
                    wraps=sys.modules[
                        "openubmc_upgrade.runtime_backend"
                    ]._snapshot_stable_artifact,
                ) as snapshot_artifact:
                    first = service.call_tool(
                        "upgrade_batch",
                        arguments,
                        task_id="task-upgrade-batch",
                        operation_id="upgrade-batch",
                    )
                    second = service.call_tool(
                        "upgrade_batch",
                        arguments,
                        task_id="task-upgrade-batch",
                        operation_id="upgrade-batch",
                    )
            finally:
                service.close()

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(snapshot_artifact.call_count, 1)
        self.assertEqual(
            len({item["target_fingerprint"] for item in first["targets"]}),
            2,
        )
        for transport in transports.values():
            uploads = [
                call
                for session in transport.sessions
                for call in session.calls
                if call == ("POST", "/redfish/v1/UpdateService/upload")
            ]
            self.assertEqual(len(uploads), 1)

    def run_uncertain_then_recover(
        self,
        *,
        root: Path,
        artifact: Path,
        digest: str,
        transport: UncertainUpgradeTransport,
        task_id: str,
        remove_artifact: bool = True,
        version_poll_interval: float = 0.001,
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
            "version_poll_interval": version_poll_interval,
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
        self.assertEqual(result["encoding"], "multipart/form-data")
        self.assertTrue(result["legacy_multipart"])

    def test_legacy_http_push_uses_multipart_on_first_write(self) -> None:
        class CaptureSession:
            def request_json(self, method, path, **kwargs):
                self.method = method
                self.path = path
                self.data = kwargs.get("data")
                self.headers = kwargs.get("headers", {})
                return RedfishResponse(status=202, headers={}, payload={})

        session = CaptureSession()
        result = UpgradeMcpBackend._upload(
            session,
            {"HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory"},
            SimpleNamespace(path="/tmp/openubmc.hpm"),
            b"firmware",
            {},
            upload_timeout=600,
        )

        self.assertTrue(_legacy_http_push_uses_multipart(
            {"HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory"}
        ))
        self.assertEqual(result["method"], "HttpPushUri")
        self.assertEqual(result["encoding"], "multipart/form-data")
        self.assertIn("multipart/form-data; boundary=", session.headers["Content-Type"])
        self.assertIn(b' name="UpdateFile"', session.data)
        self.assertIn(b"firmware", session.data)

    def test_legacy_multipart_activation_timeout_is_reconnectable(self) -> None:
        class Context:
            operation_id = "activation-timeout"

            @staticmethod
            def remaining() -> float:
                return 30

            @staticmethod
            def raise_if_stopped() -> None:
                return None

            @staticmethod
            def wait(_seconds: float) -> None:
                return None

        class StagedSession:
            timeout = 30

            def request_json(self, method, path, **kwargs):
                if path == "/redfish/v1/UpdateService":
                    return RedfishResponse(
                        status=200,
                        headers={},
                        payload={
                            "HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory",
                            "Actions": {
                                "#UpdateService.SimpleUpdate": {"target": "/simple"}
                            },
                        },
                    )
                if path == "/redfish/v1/UpdateService/FirmwareInventory":
                    return RedfishResponse(status=202, headers={}, payload={})
                if path == "/simple":
                    raise OSError("BMC rebooted after activation")
                raise AssertionError(f"unexpected request: {method} {path}")

        artifact = SimpleNamespace(path="/tmp/openubmc.hpm", sha256="sha", product_version="2.0.0")
        backend = UpgradeMcpBackend.__new__(UpgradeMcpBackend)
        result = backend._apply_with_session(
            StagedSession(),
            artifact,
            b"firmware",
            {"image_uri": "/tmp/web/openubmc.hpm"},
            Context(),
            lambda: None,
        )

        self.assertEqual(result["staging_monitor"]["state"], "not_advertised")
        self.assertEqual(result["activation"]["state"], "connection_lost")
        self.assertEqual(result["monitor"]["state"], "activation_connection_lost")
        self.assertEqual(result["image_uri"], "/tmp/web/openubmc.hpm")

    def test_auto_uses_webui_for_legacy_staged_target_without_image_uri(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact_bytes = b"component-firmware"
            artifact.write_bytes(artifact_bytes)
            digest = hashlib.sha256(artifact_bytes).hexdigest()
            transport = WebUiUpgradeTransport(artifact.name)
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
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            try:
                result = service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="task-webui-upgrade",
                    operation_id="webui-upgrade-1",
                )
                replay = service.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="task-webui-upgrade",
                    operation_id="webui-upgrade-1",
                )
            finally:
                service.close()

        self.assertEqual(result["mutation"]["protocol"], "webui")
        self.assertEqual(result["mutation"]["method"], "WebUI")
        self.assertEqual(result["mutation"]["monitor"]["state"], "completed")
        self.assertEqual(
            result["verification"]["verification_mode"],
            "task-completion",
        )
        self.assertIsNone(result["verification"]["installed_version"])
        self.assertEqual(
            result["verification"]["version"]["components"],
            ["HWSR"],
        )
        self.assertEqual(transport.web_uploads, 1)
        self.assertEqual(transport.web_starts, 1)
        self.assertGreaterEqual(transport.web_progress_reads, 2)
        self.assertEqual(
            transport.started_paths,
            [f"/tmp/web/{artifact.name}"],
        )
        self.assertEqual(len(transport.uploaded_bodies), 1)
        self.assertIn(b'name="imgfile"', transport.uploaded_bodies[0])
        self.assertIn(artifact_bytes, transport.uploaded_bodies[0])
        self.assertTrue(replay["idempotent_replay"])

    def test_auto_pins_resolved_webui_protocol_in_mutation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact_path = root / "component-CSR_1.54.hpm"
            artifact_path.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            store = MutationJournalStore(root / "journals")
            transport = WebUiUpgradeTransport(artifact_path.name)
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
            task = backend.open_task("task-webui-protocol")
            context = self.batch_context("webui-protocol")
            arguments = {
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact_path),
                "artifact_sha256": digest,
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            try:
                backend.upgrade_run(task, arguments, context)
                binding = task.binding_for(arguments)
                adapter = UpgradeRuntimeAdapter(
                    task_run=binding.task_run,
                    target=binding.target,
                    redfish_selector=binding.redfish_selector,
                    ssh_selector=binding.ssh_selector,
                    redfish_transport=binding.transport,
                )
                artifact = UpgradeArtifact(
                    path=str(artifact_path),
                    sha256=digest,
                    product_version="1.54",
                )
                expected = adapter.mutation_request(
                    operation_id=context.operation_id,
                    artifact=artifact,
                    mutation_options=_resolved_mutation_options(
                        arguments,
                        protocol="webui",
                        verification_mode="task-completion",
                    ),
                )
                journal = store.load(
                    "task-webui-protocol",
                    context.operation_id,
                )
            finally:
                backend.close_task(task)

        self.assertIsNotNone(journal)
        self.assertEqual(journal.operation_fingerprint, expected.fingerprint)

    def test_webui_cleanup_failure_is_returned_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = CleanupFailingWebUiTransport(artifact.name)
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
                        "intent": "upgrade-and-verify",
                        "ip": "bmc.example",
                        "artifact_path": str(artifact),
                        "artifact_sha256": digest,
                        "product_version": "1.54",
                        "deadline": TEST_DEADLINE_SECONDS,
                    },
                    task_id="task-webui-cleanup",
                    operation_id="webui-cleanup",
                )
            finally:
                service.close()

        cleanup = result["mutation"]["cleanup"]
        self.assertFalse(cleanup["completed"])
        self.assertEqual(cleanup["http_status"], 500)
        self.assertEqual(result["journal"]["stage"], "verified")

    def test_webui_start_rejection_after_upload_remains_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            store = MutationJournalStore(root / "journals")
            transport = StartRejectedWebUiTransport(artifact.name)
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
            arguments = {
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            try:
                with self.assertRaisesRegex(RuntimeError, "start returned HTTP 400"):
                    service.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-start-rejected",
                        operation_id="webui-start-rejected",
                    )
                journal = store.load(
                    "task-webui-start-rejected",
                    "webui-start-rejected",
                )
            finally:
                service.close()

        self.assertEqual(transport.web_uploads, 1)
        self.assertEqual(transport.web_starts, 1)
        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "mutation_failed")
        self.assertTrue(journal.effects_started)

    def test_uncertain_webui_recovery_rejects_unchanged_historical_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            store = MutationJournalStore(root / "journals")
            transport = UncertainWebUiTransport(artifact.name)
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
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(OSError, "upload connection lost"):
                    first.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-uncertain",
                        operation_id="webui-uncertain",
                    )
            finally:
                first.close()

            resumed_store = MutationJournalStore(root / "journals")
            resumed_backend = UpgradeMcpBackend(
                journal_store=resumed_store,
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            second = RuntimeMcpService(resumed_backend)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "cannot prove that the earlier upload had no remote effect",
                ):
                    second.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-uncertain",
                        operation_id="webui-uncertain",
                    )
                journal = resumed_store.load(
                    "task-webui-uncertain",
                    "webui-uncertain",
                )
            finally:
                second.close()

        self.assertEqual(transport.web_uploads, 1)
        self.assertIsNotNone(journal)
        self.assertTrue(journal.effects_started)
        self.assertNotEqual(journal.stage, "verified")

    def test_uncertain_webui_recovery_rejects_reordered_historical_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            store = MutationJournalStore(root / "journals")
            transport = ReorderedHistoricalUncertainWebUiTransport(artifact.name)
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
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(OSError, "upload connection lost"):
                    first.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-reordered-history",
                        operation_id="webui-reordered-history",
                    )
            finally:
                first.close()

            second = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "cannot prove that the earlier upload had no remote effect",
                ):
                    second.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-reordered-history",
                        operation_id="webui-reordered-history",
                    )
                journal = store.load(
                    "task-webui-reordered-history",
                    "webui-reordered-history",
                )
            finally:
                second.close()

        self.assertEqual(transport.web_uploads, 1)
        self.assertIsNotNone(journal)
        self.assertNotEqual(journal.stage, "verified")

    def test_uncertain_webui_start_recovery_rejects_historical_state_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            store = MutationJournalStore(root / "journals")
            transport = StateChangedHistoricalUncertainStartWebUiTransport(
                artifact.name
            )
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
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(OSError, "start response lost"):
                    first.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-state-history",
                        operation_id="webui-state-history",
                    )
            finally:
                first.close()

            second = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "cannot prove that the earlier upload had no remote effect",
                ):
                    second.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-state-history",
                        operation_id="webui-state-history",
                    )
                journal = store.load(
                    "task-webui-state-history",
                    "webui-state-history",
                )
            finally:
                second.close()

        self.assertEqual(transport.web_uploads, 1)
        self.assertEqual(transport.web_starts, 1)
        self.assertIsNotNone(journal)
        self.assertNotEqual(journal.stage, "verified")

    def test_uncertain_webui_recovery_accepts_a_fresh_matching_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "component-CSR_1.54.hpm"
            artifact.write_bytes(b"component-firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = RecoverableUncertainWebUiTransport(artifact.name)
            arguments = {
                "intent": "upgrade-and-verify",
                "ip": "bmc.example",
                "artifact_path": str(artifact),
                "artifact_sha256": digest,
                "product_version": "1.54",
                "deadline": TEST_DEADLINE_SECONDS,
            }
            first_backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            first = RuntimeMcpService(first_backend)
            try:
                with self.assertRaisesRegex(OSError, "upload connection lost"):
                    first.call_tool(
                        "upgrade_run",
                        arguments,
                        task_id="task-webui-recoverable",
                        operation_id="webui-recoverable",
                    )
            finally:
                first.close()

            resumed_backend = UpgradeMcpBackend(
                journal_store=MutationJournalStore(root / "journals"),
                credential_loader=lambda _arguments: {
                    "redfish": {
                        "user": "Administrator",
                        "password": "redfish-secret",
                    }
                },
                redfish_transport_factory=lambda _arguments: transport,
            )
            second = RuntimeMcpService(resumed_backend)
            try:
                result = second.call_tool(
                    "upgrade_run",
                    arguments,
                    task_id="task-webui-recoverable",
                    operation_id="webui-recoverable",
                )
            finally:
                second.close()

        self.assertEqual(transport.web_uploads, 1)
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertTrue(result["verification"]["version"]["completed"])

    def test_redfish_multipart_rejects_header_control_characters(self) -> None:
        artifact = SimpleNamespace(path="/tmp/bad\nname.hpm")

        with self.assertRaisesRegex(ValueError, "safe artifact filename"):
            _multipart_parts(artifact)

    def test_explicit_redfish_keeps_legacy_image_uri_requirement(self) -> None:
        update_service = {
            "HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory",
            "Actions": {"#UpdateService.SimpleUpdate": {"target": "/simple"}},
        }
        with self.assertRaisesRegex(ValueError, "requires an explicit"):
            _upgrade_upload_plan(
                update_service,
                {"upgrade_protocol": "redfish"},
            )

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

    def test_staged_activation_rejection_preserves_upload_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

            class ActivationRejectedSession(FakeRedfishSession):
                def request_json(self, method: str, path: str, **kwargs) -> RedfishResponse:
                    if path == "/redfish/v1/UpdateService":
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={
                                "HttpPushUri": (
                                    "/redfish/v1/UpdateService/FirmwareInventory"
                                ),
                                "Actions": {
                                    "#UpdateService.SimpleUpdate": {
                                        "target": "/simple"
                                    }
                                },
                            },
                        )
                    if path == "/redfish/v1/UpdateService/FirmwareInventory":
                        return RedfishResponse(
                            status=202,
                            headers={"Location": "/task/1"},
                            payload={},
                        )
                    if path == "/task/1":
                        return RedfishResponse(
                            status=200,
                            headers={},
                            payload={"TaskState": "Completed"},
                        )
                    if path == "/simple":
                        raise RedfishHttpError(400, "activation rejected")
                    return super().request_json(method, path, **kwargs)

            class ActivationRejectedTransport(FakeRedfishTransport):
                def open_session(self, *, target, credentials) -> FakeRedfishSession:
                    self.opens += 1
                    session = ActivationRejectedSession(self.opens)
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
                redfish_transport_factory=lambda _arguments: (
                    ActivationRejectedTransport()
                ),
            )
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaisesRegex(RedfishHttpError, "activation rejected"):
                    service.call_tool(
                        "upgrade_run",
                        {
                            **self.arguments(artifact, digest),
                            "image_uri": "/tmp/web/openubmc.hpm",
                        },
                        task_id="task-staged-activation-rejected",
                        operation_id="staged-activation-rejected",
                    )
                journal = store.load(
                    "task-staged-activation-rejected",
                    "staged-activation-rejected",
                )
            finally:
                service.close()

        self.assertIsNotNone(journal)
        self.assertEqual(journal.stage, "mutation_failed")
        self.assertTrue(journal.effects_started)

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
                manager_versions=("1.0.0", "2.0.0"),
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

    def test_uncertain_same_version_without_activation_remains_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"firmware")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            transport = UncertainUpgradeTransport(
                manager_versions=("2.0.0",), active_version="2.0.0",
            )
            # Enter the final observation directly without racing a 50 ms
            # wall-clock deadline against scheduler latency during polling.
            with self.assertRaisesRegex(ValueError, "fresh activation boundary"):
                self.run_uncertain_then_recover(
                    root=root, artifact=artifact, digest=digest,
                    transport=transport, task_id="same-version-no-activation",
                    version_poll_interval=TEST_DEADLINE_SECONDS,
                )
            journal = MutationJournalStore(root / "journals").load(
                "same-version-no-activation", "upgrade-recovery",
            )
            self.assertNotEqual(journal.stage, "verified")
            self.assertEqual(journal.target_identity.firmware_id, "2.0.0")
            self.assertEqual(transport.upload_attempts, 1)

    def test_manager_verification_requires_fresh_activation_when_baseline_is_missing(self) -> None:
        context = SimpleNamespace(
            raise_if_stopped=lambda: None, remaining=lambda: 0.001,
            wait=lambda _seconds: None,
        )
        verification = SimpleNamespace(
            artifact=SimpleNamespace(product_version="2.0.0"),
            redfish_request=lambda *_args, **_kwargs: {
                "version": "2.0.0", "last_reset_time": "2026-09-05T00:00:00Z",
            },
        )
        for baseline in ({}, {"version": "2.0.0", "last_reset_time": "2026-09-05T00:00:00Z"}):
            with self.subTest(baseline=baseline):
                with self.assertRaisesRegex(ValueError, "fresh activation boundary"):
                    UpgradeMcpBackend._wait_for_installed_version(
                        verification, context, {"version_poll_interval": 0.001},
                        {"manager_before": baseline},
                    )
        verified = UpgradeMcpBackend._wait_for_installed_version(
            verification, context, {"version_poll_interval": 0.001},
            {"manager_before": {"version": "2.0.0", "last_reset_time": "2026-09-04T00:00:00Z"}},
        )
        self.assertEqual(verified["version"], "2.0.0")

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
