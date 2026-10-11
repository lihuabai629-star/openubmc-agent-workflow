"""Exercise the legacy WebUI path through Runtime mutation journals."""
from pathlib import Path
import hashlib
import tempfile
import unittest

from test_runtime_backend import FakeRedfishSession, FakeRedfishTransport
from openubmc_target_runtime import MutationEffectsRejected, MutationJournalStore, OperationDeadlineExceeded, RuntimeMcpService
from openubmc_upgrade.runtime_backend import UpgradeMcpBackend
from openubmc_upgrade.webui import WebUiHttpError, WebUiResponse, WebUiTransportError


class LegacySession(FakeRedfishSession):
    def request_json(self, method, path, **kwargs):
        if path == "/redfish/v1/UpdateService":
            return WebUiResponse(200, {}, {"HttpPushUri": "/redfish/v1/UpdateService/FirmwareInventory"})
        return super().request_json(method, path, **kwargs)


class LegacyTransport(FakeRedfishTransport):
    def open_session(self, *, target, credentials):
        self.opens += 1
        session = LegacySession(self.opens)
        self.sessions.append(session)
        return session


class WebSession:
    def __init__(self, *, upload_error=None, start_error=None, task_id=True,
                 filename="openubmc.hpm", active_baseline=False):
        self.upload_error = upload_error
        self.start_error = start_error
        self.task_id = task_id
        self.filename = filename
        self.active_baseline = active_baseline
        self.uploads = []
        self.starts = []
        self.closed = 0

    def login(self):
        return WebUiResponse(200, {}, {})

    def progress(self, task_id=""):
        if not self.starts:
            tasks = ([{"TaskState": "Running", "FileName": "old.hpm", "ErrorCode": 0}]
                     if self.active_baseline else [])
        else:
            tasks = [{"TaskName": "BMC", "FileName": self.filename,
                      "TaskState": "Completed", "ErrorCode": 0, "Version": "2.0.0"}]
        return WebUiResponse(200, {}, {"UpgradeTasks": tasks})

    def upload(self, *, body, boundary, content_length, timeout):
        data = b"".join(body)
        assert len(data) == content_length
        self.uploads.append(data)
        if self.upload_error:
            raise self.upload_error
        return WebUiResponse(200, {}, {"FilePath": "/tmp/web/openubmc.hpm"})

    def start(self, file_path):
        self.starts.append(file_path)
        if self.start_error:
            raise self.start_error
        return WebUiResponse(200, {}, {"TaskId": "1"} if self.task_id else {})

    def close(self):
        self.closed += 1
        return {"attempted": True, "completed": True}


class LegacyWebUiTests(unittest.TestCase):
    def execute(self, web, *, protocol=None, deadline=5):
        self.root = Path(self.temp.name)
        artifact = self.root/"openubmc.hpm"
        artifact.write_bytes(b"verified-firmware")
        self.transport = LegacyTransport()
        self.store = MutationJournalStore(self.root/"journals")
        self.backend = UpgradeMcpBackend(journal_store=self.store,
            credential_loader=lambda _: {"redfish": {"user": "test", "password": "test"}},
            redfish_transport_factory=lambda _: self.transport,
            webui_session_factory=lambda _: web)
        self.service = RuntimeMcpService(self.backend)
        self.addCleanup(self.service.close)
        self.arguments = dict(intent="upgrade-and-verify", ip="bmc.example",
            artifact_path=str(artifact), artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
            product_version="2.0.0", deadline=deadline)
        if protocol is not None:
            self.arguments["upgrade_protocol"] = protocol
        return self.call()

    def call(self):
        return self.service.call_tool("upgrade_run", self.arguments,
            task_id="dsh-session", operation_id="dsh-tool-call")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_auto_legacy_uploads_multipart_once_and_replays_verified_journal(self):
        web = WebSession()
        result = self.execute(web)
        replay = self.call()
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(result["verification"]["installed_version"], "2.0.0")
        self.assertEqual(result["mutation"]["protocol"], "webui")
        self.assertEqual(len(web.uploads), 1)
        self.assertIn(b"verified-firmware", web.uploads[0])
        self.assertEqual(web.starts, ["/tmp/web/openubmc.hpm"])
        self.assertEqual(web.closed, 1)
        self.assertTrue(replay["idempotent_replay"])

    def test_explicit_redfish_legacy_fails_before_any_upload(self):
        web = WebSession()
        with self.assertRaises(ValueError):
            self.execute(web, protocol="redfish")
        self.assertEqual(web.uploads, [])
        self.assertEqual(web.starts, [])

    def test_active_existing_task_prevents_new_upload_and_closes_session(self):
        web = WebSession(active_baseline=True)
        with self.assertRaises(ValueError):
            self.execute(web)
        self.assertEqual(web.uploads, [])
        self.assertEqual(web.closed, 1)

    def test_upload_rejection_is_replan_required_without_activation(self):
        web = WebSession(upload_error=WebUiHttpError(400, "invalid package"))
        with self.assertRaisesRegex(MutationEffectsRejected, "upload rejected with HTTP 400"):
            self.execute(web)
        self.assertEqual(web.starts, [])
        self.assertEqual(web.closed, 1)
        journals = self.backend.open_task("dsh-session").binding_for(self.arguments).task_run.mutation_journals()
        self.assertEqual(journals[0].stage, "replan_required")

    def test_upload_connection_loss_stays_unknown_without_start_or_retry(self):
        error = WebUiTransportError(method="POST", path="/UI/Rest/FirmwareInventory",
            request_bytes=100, timeout=5, cause=TimeoutError())
        web = WebSession(upload_error=error)
        with self.assertRaises(WebUiTransportError):
            self.execute(web)
        self.assertEqual(len(web.uploads), 1)
        self.assertEqual(web.starts, [])
        self.assertEqual(web.closed, 1)
        journals = self.backend.open_task("dsh-session").binding_for(self.arguments).task_run.mutation_journals()
        self.assertFalse(journals[0].terminal)
        self.assertNotEqual(journals[0].stage, "replan_required")

    def test_lost_activation_response_verifies_new_manager_version_without_reupload(self):
        error = WebUiTransportError(method="POST", path="/UI/Rest/BMCSettings/UpdateService/FirmwareUpdate",
            request_bytes=100, timeout=5, cause=TimeoutError())
        web = WebSession(start_error=error)
        result = self.execute(web)
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertTrue(result["mutation"]["monitor"]["activation_connection_lost"])
        self.assertEqual(len(web.uploads), 1)
        self.assertEqual(web.closed, 1)

    def test_completed_tasks_for_another_file_do_not_complete_this_upgrade(self):
        web = WebSession(filename="another.hpm", task_id=False)
        with self.assertRaises(OperationDeadlineExceeded):
            self.execute(web, deadline=0.05)
        self.assertEqual(len(web.uploads), 1)
        self.assertEqual(web.closed, 1)

    def test_new_named_task_without_task_id_completes(self):
        web = WebSession(task_id=False)
        result = self.execute(web)
        self.assertEqual(result["journal"]["stage"], "verified")
        self.assertEqual(len(web.uploads), 1)


if __name__ == "__main__":
    unittest.main()
