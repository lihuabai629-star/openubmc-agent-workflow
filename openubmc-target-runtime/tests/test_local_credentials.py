from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openubmc_target_runtime import CredentialResolver


class LocalCredentialTests(unittest.TestCase):
    def test_defaults_and_complete_ip_overrides_are_isolated_by_purpose_and_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'credentials.json'
            path.write_text(json.dumps({
                'schema_version': 1,
                'credentials': {
                    'common': {'user': 'fixture-bmc', 'password': 'fixture-default'},
                    'special': {'user': 'fixture-special', 'password': 'fixture-override'},
                    'os': {'user': 'fixture-os', 'password': 'fixture-os-password'},
                },
                'defaults': {'bmc': {'ssh': 'common', 'redfish': 'common'}, 'os': {'ssh': 'os'}},
                'targets': {'192.0.2.10': {'bmc': {'ssh': 'special'}}},
            }))
            path.chmod(0o600)
            resolver = CredentialResolver(config_path=path, environ={})
            bmc = resolver.resolve_local(task_id='one', host='192.0.2.10', transport='ssh')
            other = resolver.resolve_local(task_id='one', host='192.0.2.11', transport='ssh')
            os_access = resolver.resolve_local(task_id='one', host='192.0.2.10', transport='ssh', purpose='os')
            redfish = resolver.resolve_local(task_id='one', host='192.0.2.10', transport='redfish')
            self.assertEqual((bmc.credentials.user, bmc.credentials.password), ('fixture-special', 'fixture-override'))
            self.assertEqual(other.credentials.password, 'fixture-default')
            self.assertEqual(os_access.credentials.password, 'fixture-os-password')
            self.assertEqual(redfish.credentials.password, 'fixture-default')
            self.assertTrue(resolver.resolve_local(task_id='one', host='192.0.2.10', transport='ssh').cache_hit)

    def test_legacy_discovery_and_task_source_binding_are_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'openubmc'
            folder.mkdir()
            legacy = folder / 'credentials.env'
            legacy.write_text('OPENUBMC_SSH_USER=fixture-user\nOPENUBMC_SSH_PASSWORD=fixture-file\n')
            legacy.chmod(0o600)
            environment = {'XDG_CONFIG_HOME': str(root), 'OPENUBMC_SSH_PASSWORD': 'fixture-env'}
            resolver = CredentialResolver(environ=environment)
            first = resolver.resolve_local(task_id='one', host='192.0.2.1', transport='ssh')
            self.assertEqual(first.credentials.password, 'fixture-env')
            alternate = root / 'alternate.env'
            alternate.write_text('OPENUBMC_SSH_USER=second-user\nOPENUBMC_SSH_PASSWORD=second-password\n')
            alternate.chmod(0o600)
            environment.pop('OPENUBMC_SSH_PASSWORD')
            environment['OPENUBMC_CREDENTIALS_FILE'] = str(alternate)
            same_task = resolver.resolve_local(task_id='one', host='192.0.2.2', transport='ssh')
            other_task = resolver.resolve_local(task_id='two', host='192.0.2.2', transport='ssh')
            self.assertEqual(same_task.credentials.password, 'fixture-file')
            self.assertEqual(other_task.credentials.password, 'second-password')
            environment['OPENUBMC_DEBUG_CREDENTIALS_FILE'] = str(legacy)
            with self.assertRaises(Exception) as conflict:
                resolver.resolve_local(task_id='three', host='192.0.2.2', transport='ssh')
            self.assertEqual(conflict.exception.code, 'credentials_conflict')

    def test_normalized_ipv6_conflicts_and_incomplete_overrides_do_not_fall_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'credentials.json'
            config = {'schema_version': 1, 'credentials': {
                'default': {'user': 'fixture', 'password': 'default-secret'},
                'override': {'password': 'override-secret'},
            }, 'defaults': {'bmc': {'ssh': 'default'}}, 'targets': {'2001:0db8::1': {'bmc': {'ssh': 'override'}}}}
            path.write_text(json.dumps(config)); path.chmod(0o600)
            resolver = CredentialResolver(config_path=path, environ={})
            with self.assertRaises(Exception) as missing:
                resolver.resolve_local(task_id='one', host='2001:db8:0:0:0:0:0:1', transport='ssh')
            self.assertEqual(missing.exception.code, 'credentials_missing')
            self.assertNotIn('override-secret', str(missing.exception))
            config['credentials']['override']['user'] = 'other'
            path.write_text(json.dumps(config))
            fixed = resolver.resolve_local(task_id='one', host='2001:db8::1', transport='ssh')
            self.assertEqual(fixed.credentials.password, 'override-secret')
            self.assertTrue(resolver.resolve_local(task_id='one', host='2001:db8:0:0:0:0:0:1', transport='ssh').cache_hit)
            config['targets']['2001:db8::1'] = {'bmc': {'ssh': 'default'}}
            path.write_text(json.dumps(config))
            with self.assertRaises(Exception) as conflict:
                resolver.resolve_local(task_id='two', host='2001:db8::1', transport='ssh')
            self.assertEqual(conflict.exception.code, 'credentials_conflict')
            rendered = json.dumps(fixed.credentials.to_public_dict()) + repr(fixed)
            self.assertNotIn('override-secret', rendered)
            self.assertNotIn('default-secret', rendered)

    def test_observe_passes_ip_record_to_the_authorized_adapter_without_exposing_secrets(self):
        from unittest.mock import patch
        from test_mcp_contracts import FakeDebugBackend
        from openubmc_target_runtime import RuntimeMcpService, OrchestratedMcpBackend
        observed = []
        class ConnectionBackend(FakeDebugBackend):
            def debug_collect(self, task, arguments, context):
                sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'openubmc-debug/scripts'))
                import _cli_common
                from types import SimpleNamespace
                values = _cli_common.resolve_debug_credentials(SimpleNamespace(ip=arguments['ip']), include_telnet=False, credentials=arguments.get('_credential_values', {}))
                observed.append(values['ssh']['password'])
                return super().debug_collect(task, arguments, context)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'openubmc'; folder.mkdir()
            path = folder / 'credentials.json'
            path.write_text(json.dumps({'schema_version': 1, 'credentials': {'ip': {'user': 'fixture', 'password': 'only-local-secret'}}, 'targets': {'192.0.2.10': {'bmc': {'ssh': 'ip'}}}})); path.chmod(0o600)
            with patch.dict('os.environ', {'XDG_CONFIG_HOME': str(root), 'OPENUBMC_SSH_PASSWORD': 'wrong-global-secret'}, clear=True):
                service = RuntimeMcpService(OrchestratedMcpBackend({'debug_collect': ConnectionBackend()}))
                try:
                    result = service.call_exposed_tool('observe', {'target': '192.0.2.10', 'selectors': [{'kind': 'capability', 'names': ['ssh']}]}, task_id='credential-observe', operation_id='check')
                    self.assertEqual(observed, ['only-local-secret'])
                    self.assertNotIn('only-local-secret', json.dumps(result))
                finally:
                    service.close()

    def test_mcp_reports_incomplete_selected_record_before_contacting_adapter(self):
        from unittest.mock import patch
        from test_mcp_contracts import FakeDebugBackend
        from openubmc_target_runtime import RuntimeMcpService, OrchestratedMcpBackend, JsonRpcMcpEndpoint
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'credentials.json'
            path.write_text(json.dumps({'schema_version': 1, 'credentials': {'broken': {'password': 'never-send-this'}}, 'defaults': {'bmc': {'ssh': 'broken'}}})); path.chmod(0o600)
            with patch.dict('os.environ', {'OPENUBMC_CREDENTIALS_CONFIG': str(path)}, clear=True):
                domain = FakeDebugBackend()
                service = RuntimeMcpService(OrchestratedMcpBackend({'debug_collect': domain}))
                try:
                    response = JsonRpcMcpEndpoint(service).handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'observe', 'arguments': {'target': '192.0.2.1', 'selectors': [{'kind': 'capability', 'names': ['ssh']}]}}})
                    payload = response['result']['structuredContent']
                    self.assertEqual(payload['error']['code'], 'credentials_missing')
                    self.assertIn('local', payload['error']['message'])
                    self.assertFalse(any(task.calls for task in domain.created))
                    self.assertNotIn('never-send-this', json.dumps(response))
                finally:
                    service.close()

    def test_selected_json_missing_bmc_record_does_not_fall_back_to_ambient_password(self):
        from unittest.mock import patch
        from test_mcp_contracts import FakeDebugBackend
        from openubmc_target_runtime import RuntimeMcpService, OrchestratedMcpBackend, JsonRpcMcpEndpoint
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'credentials.json'
            path.write_text('{"schema_version":1,"credentials":{},"defaults":{}}'); path.chmod(0o600)
            with patch.dict('os.environ', {'OPENUBMC_CREDENTIALS_CONFIG': str(path), 'OPENUBMC_SSH_USER': 'ambient', 'OPENUBMC_SSH_PASSWORD': 'ambient-secret'}, clear=True):
                domain = FakeDebugBackend()
                service = RuntimeMcpService(OrchestratedMcpBackend({'debug_collect': domain}))
                try:
                    response = JsonRpcMcpEndpoint(service).handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'observe', 'arguments': {'target': '192.0.2.1', 'selectors': [{'kind': 'capability', 'names': ['ssh']}]}}})
                    self.assertEqual(response['result']['structuredContent'].get('error', {}).get('code'), 'credentials_missing')
                    self.assertFalse(any(task.calls for task in domain.created))
                finally:
                    service.close()
