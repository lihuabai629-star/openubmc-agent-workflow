from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'openubmc-target-runtime'), str(ROOT / 'openubmc-upgrade')]
from openubmc_target_runtime import MutationJournalStore, RuntimeMcpService
from openubmc_upgrade.runtime_backend import UpgradeMcpBackend, RedfishResponse
from redfish_fixture import FakeRedfishSession, FakeRedfishTransport


class WarningSession(FakeRedfishSession):
    def request_json(self, method, path, **kwargs):
        if path == '/redfish/v1/TaskService/Tasks/1':
            return RedfishResponse(status=200, headers={}, payload=self.task_payload)
        return super().request_json(method, path, **kwargs)


class WarningTransport(FakeRedfishTransport):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def open_session(self, *, target, credentials):
        self.opens += 1
        session = WarningSession(self.opens)
        session.task_payload = self.payload
        self.sessions.append(session)
        return session


class TaskDiagnosticTests(unittest.TestCase):
    def test_task_uri_secrets_are_used_locally_but_not_persisted_or_returned(self):
        task_uri = '/redfish/v1/TaskService/Tasks/1?token=fictional-secret'
        requested = []
        class UriSession(FakeRedfishSession):
            def request_json(self, method, path, **kwargs):
                if path == '/redfish/v1/UpdateService/upload':
                    return RedfishResponse(status=202, headers={'Location': task_uri}, payload={})
                if path.startswith('/redfish/v1/TaskService/Tasks/'):
                    requested.append(path)
                    return RedfishResponse(status=200, headers={}, payload={'TaskState': 'Completed', 'TaskStatus': 'Warning'})
                return super().request_json(method, path, **kwargs)
        class UriTransport(FakeRedfishTransport):
            def open_session(self, *, target, credentials):
                self.opens += 1
                return UriSession(self.opens)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
            transport = UriTransport()
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='uri', operation_id='upgrade')
                self.assertEqual(requested, [task_uri])
                self.assertFalse('fictional-secret' in json.dumps(result))
                self.assertFalse('fictional-secret' in json.dumps(backend.operation_state_store.load('uri', 'upgrade')))
            finally:
                service.close()

    def test_earlier_warning_messages_survive_a_message_free_terminal_response(self):
        class ProgressSession(WarningSession):
            def request_json(self, method, path, **kwargs):
                if path == '/redfish/v1/TaskService/Tasks/1':
                    payload = self.responses.pop(0)
                    return RedfishResponse(status=200, headers={}, payload=payload)
                return super().request_json(method, path, **kwargs)
        class ProgressTransport(WarningTransport):
            def open_session(self, *, target, credentials):
                self.opens += 1
                session = ProgressSession(self.opens)
                session.responses = [{'TaskState': 'Running', 'TaskStatus': 'Warning', 'Messages': [{'MessageId': 'Update.1.0.EarlyWarning', 'Message': 'Check cooling'}]}, {'TaskState': 'Completed', 'TaskStatus': 'Warning'}]
                return session
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
            transport = ProgressTransport({})
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='progress', operation_id='upgrade')
                self.assertIn('Update.1.0.EarlyWarning', json.dumps(result))
                self.assertIn('Update.1.0.EarlyWarning', json.dumps(backend.operation_state_store.load('progress', 'upgrade')))
                self.assertEqual(result['mutation']['monitor']['messages_status'], 'missing')
            finally:
                service.close()

    def test_upgrade_and_log_share_the_runtime_artifact_store(self):
        from openubmc_target_runtime import LocalArtifactStore, OrchestratedMcpBackend
        sys.path.insert(0, str(ROOT / 'openubmc-log-analyzer'))
        from openubmc_log_analyzer import LogBundleMcpBackend
        with tempfile.TemporaryDirectory() as raw:
            store = LocalArtifactStore(content_root=Path(raw) / 'artifacts')
            log = LogBundleMcpBackend(artifact_store=store)
            upgrade = UpgradeMcpBackend(journal_store=MutationJournalStore(Path(raw) / 'journals'))
            service = RuntimeMcpService(OrchestratedMcpBackend({'log_bundle_collect': log, 'upgrade_run': upgrade}))
            try:
                self.assertIs(upgrade.artifact_store, store)
            finally:
                service.close()

    def test_missing_and_malformed_messages_are_reported_without_inventing_details(self):
        for payload, expected, invalid in [
            ({'TaskState': 'Completed'}, 'missing', 0),
            ({'TaskState': 'Completed', 'Messages': 'invalid-container'}, 'invalid', 0),
            ({'TaskState': 'Completed', 'Messages': [42]}, 'present', 1),
        ]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
                transport = WarningTransport(payload)
                backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
                service = RuntimeMcpService(backend)
                try:
                    result = service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='missing-messages', operation_id='upgrade')
                    monitor = result['mutation']['monitor']
                    self.assertEqual(monitor['messages_status'], expected)
                    self.assertEqual(monitor['invalid_message_count'], invalid)
                    self.assertEqual(monitor['messages'], [])
                    self.assertEqual(monitor['task_status'], '')
                finally:
                    service.close()

    def test_large_messages_remain_available_through_a_redacted_artifact(self):
        from openubmc_target_runtime import LocalArtifactStore, SQLiteArtifactRepository
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
            messages = [{'MessageId': f'Update.1.0.Warning{index}', 'Message': 'fictional-secret ' + 'x' * 1000} for index in range(40)]
            transport = WarningTransport({'TaskState': 'Completed', 'TaskStatus': 'Warning', 'Messages': messages})
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            store = LocalArtifactStore(content_root=root / 'artifacts', repository=SQLiteArtifactRepository(root / 'artifacts.sqlite3'))
            service = RuntimeMcpService(backend, artifact_store=store)
            try:
                result = service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='large', operation_id='upgrade')
                monitor = result['mutation']['monitor']
                self.assertIn('messages_artifact_ref', monitor)
                self.assertLess(len(json.dumps(monitor).encode()), 4096)
                reference = backend.artifact_store.reference(monitor['messages_artifact_ref'])
                body = json.loads(backend.artifact_store.resolve(reference, require_redacted=True).read_text())
                self.assertEqual(len(body['messages']), 40)
                self.assertEqual(body['messages'][-1]['MessageId'], 'Update.1.0.Warning39')
                self.assertNotIn('fictional-secret', json.dumps(body) + json.dumps(result))
                self.assertEqual(reference.target, '192.0.2.10')
                self.assertEqual(reference.run_id, 'large')
                reopened = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), artifact_store=LocalArtifactStore(content_root=root / 'artifacts', repository=SQLiteArtifactRepository(root / 'artifacts.sqlite3')))
                self.assertEqual(json.loads(reopened.artifact_store.resolve(reference, require_redacted=True).read_text()), body)
            finally:
                service.close()

    def test_failed_task_retains_safe_diagnostics_before_raising(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
            transport = WarningTransport({'TaskState': 'Exception', 'TaskStatus': 'Critical', 'Messages': [
                {'MessageId': 'Update.1.0.ValidationFailed', 'Message': 'Rejected fictional-secret while validating'}]})
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            service = RuntimeMcpService(backend)
            try:
                with self.assertRaises(RuntimeError) as raised:
                    service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='failure', operation_id='upgrade')
                diagnostics = getattr(raised.exception, 'task_diagnostics', {})
                self.assertEqual(diagnostics.get('task_status'), 'Critical')
                self.assertEqual(diagnostics['messages'][0]['MessageId'], 'Update.1.0.ValidationFailed')
                saved = backend.operation_state_store.load('failure', 'upgrade')
                self.assertEqual(saved['redfish_task'], diagnostics)
                self.assertNotIn('fictional-secret', json.dumps(saved) + str(raised.exception))
            finally:
                service.close()

    def test_completed_warning_preserves_messages_and_still_verifies_fresh_version(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
            transport = WarningTransport({'TaskState': 'Completed', 'TaskStatus': 'Warning', 'Messages': [
                {'MessageId': 'Update.1.0.RebootRequired', 'MessageSeverity': 'Warning', 'Message': 'Reboot needed; password=fictional-secret', 'Resolution': 'Check active firmware'}]})
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='warning', operation_id='upgrade')
            finally:
                service.close()
            monitor = result['mutation']['monitor']
            self.assertEqual(monitor.get('task_status'), 'Warning')
            self.assertEqual(monitor['messages'][0]['MessageId'], 'Update.1.0.RebootRequired')
            self.assertEqual(monitor['messages'][0].get('MessageSeverity'), 'Warning')
            self.assertIn('Reboot needed', monitor['messages'][0]['Message'])
            self.assertNotIn('fictional-secret', json.dumps(result))
            self.assertEqual(result['verification']['installed_version'], '2.0.0')
            self.assertEqual(result['journal']['stage'], 'verified')
            self.assertEqual(transport.opens, 2)
