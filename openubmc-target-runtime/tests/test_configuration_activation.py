from __future__ import annotations
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ConfigurationActivationTests(unittest.TestCase):
    def test_first_activation_is_discovered_without_an_existing_legacy_file(self):
        from openubmc_target_runtime import CredentialResolver
        from openubmc_target_runtime.configuration import LocalConfigurationStore
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'openubmc' / 'credentials.json'
            store = LocalConfigurationStore(source, kind='targets')
            saved = store.save({'schema_version': 1, 'credentials': {'common': {'user': 'fixture', 'password': 'fixture-local'}}, 'defaults': {'bmc': {'ssh': 'common'}}}, expected_revision=None)
            store.activate(saved['revision'], expected_active_revision=None)
            self.assertFalse(source.exists())
            resolver = CredentialResolver(environ={'XDG_CONFIG_HOME': raw})
            credential = resolver.resolve_local(task_id='fresh-computer', host='192.0.2.10', transport='ssh').credentials
            self.assertEqual(credential.password, 'fixture-local')

    def test_live_runtime_uses_new_credentials_only_after_explicit_activation(self):
        from unittest.mock import patch
        from test_agent_gateway import SemanticBackend
        from openubmc_target_runtime import RuntimeMcpService, OrchestratedMcpBackend
        from openubmc_target_runtime.configuration import LocalConfigurationStore
        from openubmc_target_runtime.credentials import CredentialConfigurationError
        observed = []
        class ConnectedBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                observed.append(arguments.get('_credential_values', {}).get('OPENUBMC_SSH_PASSWORD'))
                return super().observe_query(task, arguments, context)
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.json'
            source.write_text('{"schema_version":1,"credentials":{}}'); source.chmod(0o600)
            store = LocalConfigurationStore(source, kind='targets')
            def config(password):
                return {'schema_version': 1, 'credentials': {'common': {'user': 'fixture', 'password': password}}, 'defaults': {'bmc': {'ssh': 'common'}}}
            with patch.dict('os.environ', {'HOME': raw, 'XDG_CONFIG_HOME': raw, 'OPENUBMC_CREDENTIALS_CONFIG': str(source)}, clear=True):
                service = RuntimeMcpService(OrchestratedMcpBackend({'debug_collect': ConnectedBackend()}))
                def observe(operation):
                    return service.call_exposed_tool('observe', {'target': '192.0.2.10', 'selectors': [{'kind': 'capability', 'names': ['ssh']}]}, task_id='activation', operation_id=operation)
                try:
                    with self.assertRaises(CredentialConfigurationError):
                        observe('missing')
                    saved = store.save(config('fixture-first'), expected_revision=None)
                    with self.assertRaises(CredentialConfigurationError):
                        observe('saved-only')
                    self.assertEqual(observed, [])
                    store.activate(saved['revision'], expected_active_revision=None)
                    observe('first-active')
                    changed = store.save(config('fixture-second'), expected_revision=saved['revision'])
                    observe('still-first')
                    store.activate(changed['revision'], expected_active_revision=saved['revision'])
                    result = observe('second-active')
                    self.assertEqual(observed, ['fixture-first', 'fixture-first', 'fixture-second'])
                    self.assertNotIn('fixture-second', json.dumps(result))
                finally:
                    service.close()

    def test_saving_and_activating_are_distinct_and_stale_saves_do_not_overwrite(self):
        from openubmc_target_runtime.configuration import LocalConfigurationStore, ConfigurationConflict, activated_source
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.json'
            store = LocalConfigurationStore(source, kind='targets')
            config = {'schema_version': 1, 'credentials': {'common': {'user': 'fixture', 'password': 'fixture-local'}}, 'defaults': {'bmc': {'ssh': 'common'}}}
            saved = store.save(config, expected_revision=None)
            self.assertTrue(saved['saved'])
            self.assertFalse(saved['verified'])
            self.assertIsNone(saved['active_revision'])
            self.assertEqual(activated_source(source), (source, None))
            active = store.activate(saved['revision'], expected_active_revision=None)
            self.assertEqual(active['active_revision'], saved['revision'])
            snapshot, revision = activated_source(source)
            self.assertEqual(revision, saved['revision'])
            self.assertEqual(json.loads(snapshot.read_text()), config)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ConfigurationConflict):
                store.save(config, expected_revision=None)
            self.assertEqual(store.status()['revision'], revision)
            self.assertNotIn('fixture-local', json.dumps(store.status()))

    def test_kb_configuration_can_be_saved_incomplete_then_activated_without_verification(self):
        from openubmc_target_runtime.configuration import LocalConfigurationStore, ConfigurationError, activated_source
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'kb-mcp.json'
            store = LocalConfigurationStore(source, kind='kb')
            saved = store.save({'username': 'fixture'}, expected_revision=None)
            active = store.activate(saved['revision'], expected_active_revision=None)
            self.assertFalse(active['verified'])
            self.assertEqual(json.loads(activated_source(source)[0].read_text()), {'username': 'fixture'})
            with self.assertRaises(ConfigurationError):
                store.save({'lightragUrl': 'file:///tmp/private'}, expected_revision=saved['revision'])
            self.assertEqual(store.status(), active)

    def test_inflight_runtime_request_keeps_its_lease_until_next_request_and_recovers_auth(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from unittest.mock import patch
        from test_agent_gateway import SemanticBackend
        from openubmc_target_runtime import RuntimeMcpService, OrchestratedMcpBackend
        from openubmc_target_runtime.configuration import LocalConfigurationStore
        entered, release = threading.Event(), threading.Event()
        opened, closed, credentials = [], [], []
        class ConnectedBackend(SemanticBackend):
            def open_task(self, task_id):
                resource = super().open_task(task_id); opened.append(resource); return resource
            def close_task(self, resource):
                closed.append(resource)
            def observe_query(self, task, arguments, context):
                password = arguments['_credential_values']['OPENUBMC_SSH_PASSWORD']
                credentials.append(password)
                if len(credentials) == 1:
                    entered.set()
                    if not release.wait(5): raise TimeoutError('fixture request was not released')
                    if task in closed: raise AssertionError('in-flight lease closed')
                if password == 'fixture-invalid':
                    raise PermissionError('Fixture authentication failed')
                return super().observe_query(task, arguments, context)
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.json'
            store = LocalConfigurationStore(source, kind='targets')
            def config(password):
                return {'schema_version': 1, 'credentials': {'common': {'user': 'fixture', 'password': password}}, 'defaults': {'bmc': {'ssh': 'common'}}}
            first = store.save(config('fixture-invalid'), expected_revision=None)
            store.activate(first['revision'], expected_active_revision=None)
            with patch.dict('os.environ', {'HOME': raw, 'XDG_CONFIG_HOME': raw, 'OPENUBMC_CREDENTIALS_CONFIG': str(source)}, clear=True):
                service = RuntimeMcpService(OrchestratedMcpBackend({'debug_collect': ConnectedBackend()}))
                def observe(operation):
                    return service.call_exposed_tool('observe', {'target': '192.0.2.10', 'selectors': [{'kind': 'capability', 'names': ['ssh']}]}, task_id='inflight', operation_id=operation)
                try:
                    with ThreadPoolExecutor(1) as pool:
                        pending = pool.submit(observe, 'first')
                        try:
                            self.assertTrue(entered.wait(3))
                            second = store.save(config('fixture-valid'), expected_revision=first['revision'])
                            store.activate(second['revision'], expected_active_revision=first['revision'])
                            self.assertEqual(closed, [])
                        finally:
                            release.set()
                        with self.assertRaises(PermissionError):
                            pending.result(timeout=3)
                    receipt = observe('second')
                    self.assertEqual(set(credentials[:-1]), {'fixture-invalid'})
                    self.assertEqual(credentials[-1], 'fixture-valid')
                    self.assertEqual(len(opened), 2)
                    self.assertEqual(closed, [opened[0]])
                    self.assertNotIn('fixture-valid', json.dumps(receipt))
                finally:
                    release.set(); service.close()

    def test_concurrent_saves_have_one_winner_and_failed_commit_preserves_active_config(self):
        import os
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from openubmc_target_runtime.configuration import LocalConfigurationStore, ConfigurationConflict, activated_source
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'kb.json'
            store = LocalConfigurationStore(source, kind='kb')
            first = store.save({'username': 'first'}, expected_revision=None)
            store.activate(first['revision'], expected_active_revision=None)
            def save(name):
                try:
                    return store.save({'username': name}, expected_revision=first['revision'])
                except ConfigurationConflict:
                    return 'conflict'
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(save, ['second', 'third']))
            self.assertEqual(results.count('conflict'), 1)
            before = store.status()
            replace = os.replace
            def fail_marker(src, dst):
                if Path(dst).name == '.kb.json.saved.json': raise OSError('fixture disk failure')
                return replace(src, dst)
            with patch('os.replace', side_effect=fail_marker):
                with self.assertRaises(OSError):
                    store.save({'username': 'lost'}, expected_revision=before['revision'])
            self.assertEqual(store.status(), before)
            self.assertEqual(json.loads(activated_source(source)[0].read_text()), {'username': 'first'})

    def test_invalid_reference_or_exposed_snapshot_directory_cannot_replace_valid_config(self):
        from openubmc_target_runtime.configuration import LocalConfigurationStore, ConfigurationError
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'credentials.json'
            store = LocalConfigurationStore(source, kind='targets')
            saved = store.save({'schema_version': 1}, expected_revision=None)
            with self.assertRaises(ConfigurationError):
                store.save({'schema_version': 1, 'defaults': {'bmc': {'ssh': 'missing'}}}, expected_revision=saved['revision'])
            snapshots = Path(raw) / '.credentials.json.revisions'
            snapshots.chmod(0o755)
            with self.assertRaises(ConfigurationError):
                store.save({'schema_version': 1}, expected_revision=saved['revision'])
            self.assertEqual(store.status()['revision'], saved['revision'])
