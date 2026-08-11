from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
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
    UpgradeActivationReverted,
    UpgradeMcpBackend,
    _UpgradeTask,
    _default_credential_loader,
    _multipart_body,
)


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
