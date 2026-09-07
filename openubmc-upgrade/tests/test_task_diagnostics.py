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
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact = root / 'fixture.hpm'; artifact.write_bytes(b'fictional firmware')
            messages = [{'MessageId': f'Update.1.0.Warning{index}', 'Message': 'fictional-secret ' + 'x' * 1000} for index in range(40)]
            transport = WarningTransport({'TaskState': 'Completed', 'TaskStatus': 'Warning', 'Messages': messages})
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            service = RuntimeMcpService(backend)
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
                reopened = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'))
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
                {'MessageId': 'Update.1.0.RebootRequired', 'Severity': 'Warning', 'Message': 'Reboot needed; password=fictional-secret', 'Resolution': 'Check active firmware'}]})
            backend = UpgradeMcpBackend(journal_store=MutationJournalStore(root / 'journals'), credential_loader=lambda _args: {'redfish': {'user': 'fixture', 'password': 'fictional-secret'}}, redfish_transport_factory=lambda _args: transport)
            service = RuntimeMcpService(backend)
            try:
                result = service.call_tool('upgrade_run', {'intent': 'upgrade-and-verify', 'ip': '192.0.2.10', 'artifact_path': str(artifact), 'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(), 'product_version': '2.0.0', 'deadline': 5}, task_id='warning', operation_id='upgrade')
            finally:
                service.close()
            monitor = result['mutation']['monitor']
            self.assertEqual(monitor.get('task_status'), 'Warning')
            self.assertEqual(monitor['messages'][0]['MessageId'], 'Update.1.0.RebootRequired')
            self.assertIn('Reboot needed', monitor['messages'][0]['Message'])
            self.assertNotIn('fictional-secret', json.dumps(result))
            self.assertEqual(result['verification']['installed_version'], '2.0.0')
            self.assertEqual(result['journal']['stage'], 'verified')
            self.assertEqual(transport.opens, 2)
