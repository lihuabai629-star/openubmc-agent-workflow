from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openubmc_target_runtime import (
    CredentialResolver, OrchestratedMcpBackend, ResolvedSshCredentials, RuntimeMcpService,
)
from openubmc_target_runtime.credential_memory import VerifiedCredentialMemory
from openubmc_target_runtime.credentials import CredentialConfigurationError
from test_agent_gateway import SemanticBackend


class BmcCredentialScopeTests(unittest.TestCase):
    def test_bmc_projection_defaults_do_not_require_legacy_os_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.env'
            source.write_text('OPENUBMC_SSH_USER=fixture\nOPENUBMC_SSH_PASSWORD=bmc-fixture\n'
                              'OPENUBMC_OS_IP=192.0.2.20\nOPENUBMC_OS_SSH_USER=os-fixture\n')
            source.chmod(0o600)
            memory = VerifiedCredentialMemory(config_path=source, environ={})
            self.assertTrue(memory.remember(host='192.0.2.10', purpose='bmc', transport='ssh',
                credentials=ResolvedSshCredentials(user='fixture', password='bmc-fixture'))['remembered'])
            values = CredentialResolver(config_path=source, environ={}).resolve_local_values(
                task_id='bmc-only', host='192.0.2.10', transports=('ssh',))
            self.assertEqual(values['OPENUBMC_SSH_PASSWORD'], 'bmc-fixture')
            self.assertFalse(any(name.startswith('OPENUBMC_OS_') for name in values))

    def test_bmc_observe_survives_remembered_legacy_credentials_with_incomplete_os(self):
        observed = []

        class BmcBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                observed.append(dict(arguments['_credential_values']))
                return super().observe_query(task, arguments, context)

        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.env'
            original = ('OPENUBMC_SSH_USER=fixture\nOPENUBMC_SSH_PASSWORD=bmc-fixture\n'
                        'OPENUBMC_OS_IP=192.0.2.20\nOPENUBMC_OS_SSH_USER=os-fixture\n')
            source.write_text(original)
            source.chmod(0o600)
            environment = {'HOME': raw, 'XDG_CONFIG_HOME': raw,
                           'OPENUBMC_CREDENTIALS_FILE': str(source)}
            if os.name == 'nt':
                environment.update(USERPROFILE=raw, LOCALAPPDATA=str(Path(raw) / 'AppData/Local'),
                                   SystemRoot=os.environ.get('SystemRoot', 'C:\\Windows'))
            with patch.dict(os.environ, environment, clear=True):
                service = RuntimeMcpService(OrchestratedMcpBackend({'debug_collect': BmcBackend()}))
                try:
                    query = {'target': '192.0.2.10', 'selectors': [
                        {'kind': 'mdb', 'queries': ['lsmc']}]}
                    service.call_exposed_tool('observe', query, task_id='bmc-only', operation_id='legacy')
                    memory = VerifiedCredentialMemory(config_path=source, environ=environment)
                    saved = memory.remember(host='192.0.2.10', purpose='bmc', transport='ssh',
                                            credentials=ResolvedSshCredentials(user='fixture', password='bmc-fixture'))
                    self.assertTrue(saved['remembered'])
                    receipt = service.call_exposed_tool('observe', query, task_id='bmc-only', operation_id='remembered')
                    self.assertEqual(len(observed), 2)
                    for values in observed:
                        self.assertEqual(values['OPENUBMC_SSH_PASSWORD'], 'bmc-fixture')
                        self.assertFalse(any(name.startswith('OPENUBMC_OS_') for name in values))
                    self.assertNotIn('bmc-fixture', json.dumps(receipt))
                    self.assertEqual(source.read_text(), original)
                    resolver = CredentialResolver(config_path=source, environ=environment)
                    with self.assertRaises(CredentialConfigurationError) as missing:
                        resolver.resolve_local(task_id='explicit-os', host='192.0.2.20', purpose='os', transport='ssh')
                    self.assertEqual(missing.exception.code, 'credentials_missing')
                finally:
                    service.close()

    def test_bmc_projection_does_not_resolve_os_even_with_explicit_os_address(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.json'
            source.write_text(json.dumps({'schema_version': 1, 'credentials': {
                'bmc': {'user': 'fixture', 'password': 'bmc-fixture'},
                'os': {'user': 'os-fixture'}},
                'defaults': {'bmc': {'ssh': 'bmc'}, 'os': {'ssh': 'os'}}}))
            source.chmod(0o600)
            resolver = CredentialResolver(config_path=source, environ={})
            arguments = {'os_ip': '192.0.2.20'}
            values = resolver.resolve_local_values(task_id='bmc', host='192.0.2.10',
                arguments=arguments, transports=('ssh',), include_os=False)
            self.assertEqual(values['OPENUBMC_SSH_PASSWORD'], 'bmc-fixture')
            self.assertFalse(any(name.startswith('OPENUBMC_OS_') for name in values))
            with self.assertRaises(CredentialConfigurationError):
                resolver.resolve_local_values(task_id='os', host='192.0.2.10',
                    arguments=arguments, transports=('ssh',), include_os=True)
            self.assertEqual(resolver.resolve_console_values(task_id='console', bmc_host='192.0.2.10')[
                '__console_gap__'], 'os_password_credentials_missing')

    def test_explicit_os_projection_preserves_independent_records_and_bmc_errors(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.json'
            config = {'schema_version': 1, 'credentials': {
                'bmc': {'user': 'fixture', 'password': 'bmc-fixture'},
                'os': {'user': 'os-fixture', 'password': 'os-secret'}},
                'defaults': {'bmc': {'ssh': 'bmc'}, 'os': {'ssh': 'os'}}}
            source.write_text(json.dumps(config))
            source.chmod(0o600)
            resolver = CredentialResolver(config_path=source, environ={})
            values = resolver.resolve_local_values(task_id='explicit-os', host='192.0.2.10',
                arguments={'os_ip': '192.0.2.20'}, transports=('ssh',), include_os=True)
            self.assertEqual(values['OPENUBMC_OS_SSH_PASSWORD'], 'os-secret')
            self.assertEqual(values['OPENUBMC_SSH_PASSWORD'], 'bmc-fixture')
            config['credentials']['bmc'].pop('password')
            source.write_text(json.dumps(config))
            with self.assertRaises(CredentialConfigurationError) as missing:
                CredentialResolver(config_path=source, environ={}).resolve_local_values(
                    task_id='invalid-bmc', host='192.0.2.10', transports=('ssh',), include_os=False)
            self.assertEqual(missing.exception.code, 'credentials_missing')


if __name__ == '__main__':
    unittest.main()
