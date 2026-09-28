"""Behavioral tests for the Codex plugin distribution boundary."""
import hashlib
from datetime import datetime, timedelta
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / 'scripts' / 'package_plugin.py'


class PluginPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_directory = tempfile.TemporaryDirectory()
        cls.source = Path(cls.source_directory.name)
        files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
        for name in files:
            if name and (ROOT/name).is_file():
                target = cls.source/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT/name, target)
        subprocess.run(['git', 'init', '-q', str(cls.source)], check=True)
        subprocess.run(['git', 'add', '.'], cwd=cls.source, check=True)
        subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'Fixture'], cwd=cls.source, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.source_directory.cleanup()


    def test_immutable_source_build_is_reproducible_and_relocatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ('one.tar.gz', 'two.tar.gz'):
                result = subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((base/'one.tar.gz').read_bytes(), (base/'two.tar.gz').read_bytes())
            with tarfile.open(base/'one.tar.gz') as archive:
                archive.extractall(base/'relocated', filter='data')
            plugin = base/'relocated'/'openubmc'
            check = subprocess.run([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'verify'], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            report = json.loads(check.stdout)
            self.assertTrue(report['ok'])
            self.assertTrue((plugin/'hooks/hooks.json').is_file())
            self.assertTrue((plugin/'scripts/openubmc-continuity-hook.js').is_file())
            self.assertTrue((plugin/'skills/openubmc-debug/scripts/host_continuity.py').is_file())
            self.assertEqual(len(report['skills']), 13)
            for skill in ('openubmc-bingo-build', 'openubmc-bingo-development'):
                self.assertIn(skill, report['skills'])
                self.assertTrue((plugin/'skills'/skill/'SKILL.md').is_file())
            self.assertEqual(len(report['source_commit']), 40)
            self.assertNotIn(str(ROOT).encode(), (plugin/'scripts/launch_runtime.py').read_bytes())
            self.assertFalse(any(p.name == '.git' for p in plugin.rglob('*')))
            self.assertEqual(
                (plugin/'skills/openubmc-environment-setup/references/credential-exposure-response.md').read_bytes(),
                (self.source/'docs/credential-exposure-response.md').read_bytes(),
            )

    def test_packaging_rejects_a_skill_catalog_over_the_prompt_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            oversized = base/'source'
            subprocess.run(['git', 'clone', '-q', str(self.source), str(oversized)], check=True)
            prompt = oversized/'openubmc-bingo-build/SKILL.md'
            prompt.write_text(prompt.read_text(encoding='utf-8') + '\n' + ('x' * 16384), encoding='utf-8')
            subprocess.run(['git', '-C', str(oversized), 'add', str(prompt.relative_to(oversized))], check=True)
            subprocess.run([
                'git', '-C', str(oversized), '-c', 'user.name=Fixture',
                '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'Oversized prompt',
            ], check=True)
            result = subprocess.run([
                sys.executable, str(BUILDER), 'build', '--source', str(oversized),
                '--output', str(base/'bundle.tar.gz'),
            ], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('prompt budget', result.stderr)

    def test_verify_rejects_added_or_changed_plugin_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            result = subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/'bundle.tar.gz')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(base/'bundle.tar.gz') as archive:
                archive.extractall(base, filter='data')
            plugin = base/'openubmc'
            extra = plugin/'skills/openubmc-debug/scripts/sitecustomize.py'
            extra.write_text('raise RuntimeError("unmanaged code")\n')
            check = subprocess.run([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'verify'], capture_output=True, text=True)
            self.assertNotEqual(check.returncode, 0)
            extra.unlink()
            target = plugin/'skills/openubmc-build/SKILL.md'
            target.write_text(target.read_text()+'\nUnexpected instructions\n')
            check = subprocess.run([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'verify'], capture_output=True, text=True)
            self.assertNotEqual(check.returncode, 0)

    def test_unprepared_runtime_fails_before_serving_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            result = subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/'bundle.tar.gz')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(base/'bundle.tar.gz') as archive:
                archive.extractall(base, filter='data')
            env = dict(os.environ, XDG_DATA_HOME=str(base/'data'))
            check = subprocess.run([sys.executable, '-I', str(base/'openubmc/scripts/pluginctl.py'), 'runtime'], env=env, capture_output=True, text=True)
            self.assertNotEqual(check.returncode, 0)
            self.assertIn('prepare', check.stderr)
            self.assertEqual(check.stdout, '')

    @unittest.skipUnless(shutil.which('codex'), 'Codex executable is required')
    def test_codex_installs_and_resolves_plugin_mcp_launchers(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            result = subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/'bundle.tar.gz')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            marketplace = base/'marketplace'
            (marketplace/'plugins').mkdir(parents=True)
            with tarfile.open(base/'bundle.tar.gz') as archive:
                archive.extractall(marketplace/'plugins', filter='data')
            path = marketplace/'.agents/plugins/marketplace.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'name': 'runtime-test', 'plugins': [{'name': 'openubmc', 'source': {'source': 'local', 'path': './plugins/openubmc'}, 'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'}, 'category': 'Productivity'}]}))
            codex_home = base/'codex'
            codex_home.mkdir()
            env = dict(os.environ, CODEX_HOME=str(codex_home))
            for command in (['plugin', 'marketplace', 'add', str(marketplace)], ['plugin', 'add', 'openubmc@runtime-test', '--json']):
                result = subprocess.run(['codex', *command], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(['codex', 'mcp', 'list', '--json'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            servers = {item['name']: item for item in json.loads(result.stdout)}
            for name in ('openubmc-target-runtime', 'openubmc-kb'):
                server = servers[name]
                transport = server['transport']
                self.assertEqual(transport['command'], 'node')
                launch_arg = next(value for value in transport['args'] if value.endswith('.js'))
                launch = Path(transport['cwd'])/launch_arg
                self.assertTrue(launch.is_file(), str(launch))
                check = subprocess.run([sys.executable, '-I', str(launch.parent/'pluginctl.py'), 'verify'], capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)
            result = subprocess.run(['codex', 'plugin', 'remove', 'openubmc@runtime-test'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which('codex') and shutil.which('node'), 'Codex and Node are required')
    def test_installed_hook_reads_terminal_run_and_rollout_audit_confirms_delivery(self):
        # Only the disposable package has an empty dependency lock. Codex is
        # installed into a disposable CODEX_HOME; no global hook trust is edited.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base/'source'
            subprocess.run(['git', 'clone', '-q', str(self.source), str(source)], check=True)
            (source/'requirements-ci.lock').write_bytes(b'')
            subprocess.run(['git', '-C', str(source), 'add', 'requirements-ci.lock'], check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.test', 'commit', '-qm',
                            'Offline hook qualification fixture'], check=True)
            archive = base/'bundle.tar.gz'
            built = subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(source),
                                    '--output', str(archive)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            marketplace = base/'marketplace'
            (marketplace/'plugins').mkdir(parents=True)
            with tarfile.open(archive) as contents:
                contents.extractall(marketplace/'plugins', filter='data')
            listing = marketplace/'.agents/plugins/marketplace.json'
            listing.parent.mkdir(parents=True)
            listing.write_text(json.dumps({'name': 'continuity-test', 'plugins': [{
                'name': 'openubmc', 'source': {'source': 'local', 'path': './plugins/openubmc'},
                'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
                'category': 'Productivity',
            }]}))
            codex_home = base/'codex'; codex_home.mkdir()
            state = base/'state'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('CODEX_', 'OPENUBMC_', 'PYTHON', 'XDG_'))}
            env.update(CODEX_HOME=str(codex_home), XDG_CACHE_HOME=str(base/'cache'),
                       OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(state),
                       OPENUBMC_PLUGIN_PYTHON=sys.executable, PIP_NO_INDEX='1')
            for command in (['plugin', 'marketplace', 'add', str(marketplace)],
                            ['plugin', 'add', 'openubmc@continuity-test', '--json']):
                installed = subprocess.run(['codex', *command], env=env, capture_output=True,
                                           text=True, timeout=30)
                self.assertEqual(installed.returncode, 0, installed.stderr)
            listed = subprocess.run(['codex', 'mcp', 'list', '--json'], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            server = next(item for item in json.loads(listed.stdout)
                          if item['name'] == 'openubmc-target-runtime')
            plugin = Path(server['transport']['cwd'])
            hook_file = plugin/'hooks/hooks.json'
            hook = plugin/'scripts/openubmc-continuity-hook.js'
            self.assertTrue(hook_file.is_file())
            self.assertIn('openubmc-continuity-hook.js', hook_file.read_text())
            self.assertTrue(hook.is_file())
            prepared = subprocess.run([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'),
                                       'prepare', '--capability', 'runtime', '--offline'], env=env,
                                      capture_output=True, text=True, timeout=60)
            self.assertEqual(prepared.returncode, 0, prepared.stderr)

            runtime_paths = [str(ROOT/'openubmc-target-runtime'),
                             str(ROOT/'openubmc-target-runtime/tests')]
            sys.path[:0] = runtime_paths
            self.addCleanup(lambda: [sys.path.remove(path) for path in runtime_paths
                                     if path in sys.path])
            from openubmc_target_runtime import JsonRpcMcpEndpoint, RuntimeMcpService, SQLiteRuntimeRepository
            from openubmc_target_runtime.host_continuity import HostContinuity, read_runtime_projection
            from test_mcp_contracts import FakeDebugBackend
            backend = FakeDebugBackend()
            store = HostContinuity(state/'host-continuity')
            service = RuntimeMcpService(backend,
                                        context_repository=SQLiteRuntimeRepository(state/'context-runtime.sqlite3'),
                                        host_continuity=store)
            try:
                endpoint = JsonRpcMcpEndpoint(service, session_task_id='fixture-task')
                def call(action):
                    response = endpoint.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                'params': {'name': 'execute', 'arguments': action}})
                    self.assertFalse(response['result']['isError'], response)
                    return response['result']['structuredContent']
                started = call({'kind': 'start', 'target': '192.0.2.1', 'intent': 'diagnosis-only'})
                gate = started['gate']
                call({'kind': 'control', 'run_id': started['run_id'], 'command': 'cancel',
                      **{key: gate[key] for key in ('gate_id', 'gate_version', 'schema_digest')}})
            finally:
                service.close()
            read = lambda rid: read_runtime_projection(state/'context-runtime.sqlite3', rid)
            answer = store.handoff('fixture-task', read_run=read)['runs'][0]['terminal_answer']
            def invoke_hook(event):
                result = subprocess.run(['node', str(hook)], env=env, input=json.dumps(event),
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            context = invoke_hook({'session_id': 'fixture-task', 'hook_event_name': 'SessionStart'})
            self.assertIn('cancelled', context['hookSpecificOutput']['additionalContext'])
            pending = invoke_hook({'session_id': 'fixture-task', 'hook_event_name': 'Stop',
                                   'last_assistant_message': None, 'stop_hook_active': False})
            self.assertEqual(pending['decision'], 'block')
            self.assertIn(answer['text'], pending['reason'])
            candidate = invoke_hook({'session_id': 'fixture-task', 'hook_event_name': 'Stop',
                                     'last_assistant_message': answer['text'], 'stop_hook_active': True})
            self.assertEqual(candidate, {})
            self.assertFalse(store.handoff('fixture-task', read_run=read)['runs'][0]['terminal_answer']['delivery_confirmed'])
            observed_at = (datetime.fromisoformat(answer['prepared_at']) + timedelta(seconds=1)).isoformat()
            rollout = base/'rollout.jsonl'
            rollout.write_text('\n'.join(json.dumps(event) for event in (
                {'type': 'session_meta', 'payload': {'id': 'fixture-task'}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'final-turn'}},
                {'type': 'response_item', 'timestamp': observed_at, 'payload': {
                    'type': 'message', 'id': 'final-id', 'role': 'assistant', 'phase': 'final_answer',
                    'content': [{'type': 'output_text', 'text': answer['text']}]}},
                {'type': 'event_msg', 'timestamp': observed_at,
                 'payload': {'type': 'task_complete', 'turn_id': 'final-turn'}},
            )) + '\n')
            audited = subprocess.run([sys.executable, str(plugin/'skills/openubmc-debug/scripts/host_continuity.py'),
                                      'audit', '--task-id', 'fixture-task', '--run-id', started['run_id'],
                                      '--rollout', str(rollout)], env=env,
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(audited.returncode, 0, audited.stderr)
            self.assertEqual(json.loads(audited.stdout)['delivery_source'], 'codex-rollout-v1')
            self.assertTrue(store.handoff('fixture-task', read_run=read)['runs'][0]['terminal_answer']['delivery_confirmed'])
            self.assertEqual(sum(task.calls.count('debug_run') for task in backend.created), 1)
            verified = subprocess.run([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'verify'],
                                      env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_legacy_migration_is_reversible_and_preserves_unrelated_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/'bundle.tar.gz')], check=True, capture_output=True)
            with tarfile.open(base/'bundle.tar.gz') as archive:
                archive.extractall(base, filter='data')
            home = base/'home'
            codex = home/'.codex'
            (codex/'skills').mkdir(parents=True)
            config = codex/'config.toml'
            launcher = str(home/'.local/share/openubmc/target-runtime/openubmc-target-runtime-mcp')
            source = base/'legacy/openubmc-build'
            source.mkdir(parents=True)
            link = codex/'skills/openubmc-build'
            link.symlink_to(source)
            original = f'model = "test"\n[mcp_servers.openubmc-target-runtime]\ncommand = {json.dumps(launcher)}\nargs = []\n[mcp_servers.unrelated]\ncommand = "keep"\n[[skills.config]]\npath = {json.dumps(str(source))}\nenabled = true\n'
            config.write_text(original)
            state = home/'.config/openubmc/environment-state.json'
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({'source_root': str(source.parent), 'links': {str(link): str(source)}, 'runtime_mcp': {'codex': {'command': launcher, 'args': [], 'created_entry': True}}, 'codex_skill_center': {'targets': [str(source)]}}))
            credentials = state.with_name('credentials.env')
            credentials.write_bytes(b'preserved-private-configuration')
            env = dict(os.environ, XDG_CONFIG_HOME=str(home/'.config'), XDG_DATA_HOME=str(home/'.local/share'), CODEX_HOME=str(codex))
            cli = [sys.executable, '-I', str(base/'openubmc/scripts/pluginctl.py')]
            result = subprocess.run([*cli, 'migrate', '--remove', '--home', str(home)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn('transaction', report)
            self.assertNotIn('openubmc-target-runtime', config.read_text())
            self.assertIn('command = "keep"', config.read_text())
            self.assertFalse(link.is_symlink())
            self.assertEqual(credentials.read_bytes(), b'preserved-private-configuration')
            result = subprocess.run([*cli, 'restore-legacy', '--home', str(home), '--transaction', report['transaction']], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), original)
            self.assertEqual(link.resolve(), source.resolve())

    def test_install_rejects_archive_digest_before_configuration_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/'bundle.tar.gz')], check=True, capture_output=True)
            with tarfile.open(base/'bundle.tar.gz') as archive:
                archive.extractall(base, filter='data')
            home = base/'home'
            result = subprocess.run([sys.executable, '-I', str(base/'openubmc/scripts/install_plugin.py'), str(base/'bundle.tar.gz'), '--home', str(home), '--sha256', '0'*64], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('archive digest', result.stderr)
            self.assertFalse((home/'.codex/config.toml').exists())
            check = subprocess.run([sys.executable, '-I', str(base/'openubmc/scripts/pluginctl.py'), 'verify'], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    @unittest.skipUnless(shutil.which('codex'), 'Codex executable is required')
    def test_packaged_audit_preserves_plugin_integrity(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(self.source), '--output', str(base/'bundle.tar.gz')], check=True, capture_output=True)
            with tarfile.open(base/'bundle.tar.gz') as archive:
                archive.extractall(base, filter='data')
            plugin = base/'openubmc'
            (base/'home/.codex').mkdir(parents=True)
            audit = subprocess.run([sys.executable, '-I', str(plugin/'scripts/plugin_admin.py'), 'audit', '--home', str(base/'home')], capture_output=True, text=True, timeout=30)
            self.assertEqual(audit.returncode, 0, audit.stderr)
            self.assertFalse(json.loads(audit.stdout)['active']['installed'])
            check = subprocess.run([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'verify'], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
