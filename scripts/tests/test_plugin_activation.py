"""Filesystem recovery tests at the plugin activation boundary."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import install_plugin as installer
from plugin_archive import canonical

spec = importlib.util.spec_from_file_location('migration_test', ROOT/'plugin/openubmc/scripts/plugin_install.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class RecoveryTests(unittest.TestCase):
    def test_native_config_verifier_allows_only_marketplace_timestamp(self):
        home = Path('/private/tmp/synthetic-openubmc-home')
        before = b'model="before"\n'
        entry = ('[marketplaces.personal]\n'
                 'source_type = "local"\n'
                 f'source = {json.dumps(str(home))}\n'
                 'last_updated = "2026-09-26T19:47:19Z"\n'
                 '[plugins."openubmc@personal"]\n'
                 'enabled = true\n')
        after = ('model="before"\n' + entry).encode()
        installer.verify_staged_config(before, after, home, 'personal')
        installer.verify_staged_config(after, after.replace(b'19:47:19Z', b'19:48:19Z'), home, 'personal')
        for changed in (
            after.replace(b'19:47:19Z', b'99:47:19Z'),
            after.replace(b'model="before"', b'model="changed"'),
            after.replace(b'source_type = "local"', b'source_type = "remote"'),
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    installer.verify_staged_config(before, changed, home, 'personal')

    def test_compensation_preserves_concurrent_config_and_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root/'config.toml'
            config.write_bytes(b'model="concurrent"\n')
            journal = root/'journal'
            journal.mkdir()
            (journal/'config-before').write_bytes(b'model="before"\n')
            record = {'config': str(config), 'market': str(root/'market'), 'source': str(root/'source'),
                      'config_written': True, 'config_before': migration.digest(b'model="before"\n'),
                      'config_after': migration.digest(b'model="after"\n'), 'legacy_links': []}
            with self.assertRaisesRegex(ValueError, 'subsequent config'):
                installer.compensate(journal, record)
            self.assertEqual(config.read_bytes(), b'model="concurrent"\n')

    def test_interrupted_migration_never_replays_into_another_codex_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            first, second = home/'codex-a', home/'codex-b'
            first.mkdir()
            second.mkdir()
            (first/'config.toml').write_text('model="first"\n')
            (second/'config.toml').write_text('model="first"\n')
            record, before, after = migration.plan(home, [], first)
            transaction = 'a'*32
            record['transaction'] = transaction
            root = migration.journal_root(home)/transaction
            root.mkdir(parents=True)
            (root/'before.toml').write_bytes(before)
            (root/'after.toml').write_bytes(after)
            (root/'transaction.json').write_bytes(canonical(record))
            with self.assertRaisesRegex(ValueError, 'another home'):
                migration.migrate(home, [], second)
            self.assertEqual((second/'config.toml').read_bytes(), before)

    def test_interrupted_migration_rejects_tampered_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            record, before, after = migration.plan(home, [])
            transaction = 'a'*32
            record['transaction'] = transaction
            root = migration.journal_root(home)/transaction
            root.mkdir(parents=True)
            (root/'before.toml').write_bytes(before)
            (root/'after.toml').write_text('model="injected"\n')
            (root/'transaction.json').write_bytes(canonical(record))
            with self.assertRaisesRegex(ValueError, 'snapshot'):
                migration.migrate(home, [])
            self.assertFalse((home/'.codex/config.toml').exists())


class ActivationTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        import io
        import tarfile
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root/'home'
        self.codex = self.root/'custom-codex'
        self.codex.mkdir()
        (self.codex/'config.toml').write_text('model="before"\n')
        self.archives = []
        for version in ('2.0.7', '2.0.8'):
            files = {'.codex-plugin/plugin.json': canonical({'name':'openubmc','version':version}),
                     'workflow.json': canonical({'skills':[]}),
                     'scripts/plugin_install.py': (ROOT/'plugin/openubmc/scripts/plugin_install.py').read_bytes()}
            lock = {'schema':'openubmc.codex-plugin.v1','name':'openubmc','version':version,'source_commit':'a'*40,
                    'manifest_digest':hashlib.sha256(files['.codex-plugin/plugin.json']).hexdigest(),
                    'files':{key:hashlib.sha256(value).hexdigest() for key,value in files.items()}}
            lock['content_digest'] = hashlib.sha256(canonical(lock)).hexdigest()
            files['plugin-lock.json'] = canonical(lock)
            archive = self.root/(version+'.tar.gz')
            with tarfile.open(archive, 'w:gz') as bundle:
                for name, data in files.items():
                    info = tarfile.TarInfo('openubmc/'+name)
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
            self.archives.append((archive, hashlib.sha256(archive.read_bytes()).hexdigest()))
        self.native = installer.command

    def external(self, argv, env):
        # Dependency installation and MCP readiness have separate integration
        # coverage. Native Codex and all activation filesystem effects are real.
        return '{}' if argv[0] == sys.executable else self.native(argv, env)

    def activate(self, index=0):
        from unittest.mock import patch
        with patch.object(installer, 'command', side_effect=self.external):
            return installer.activate(*self.archives[index], self.home, self.codex)

    @unittest.skipUnless(__import__('shutil').which('codex'), 'Codex required')
    def test_home_override_isolates_parent_personal_marketplace(self):
        from unittest.mock import patch
        outer = self.root/'parent-home'
        manifest = outer/'plugins/openubmc/.codex-plugin/plugin.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(canonical({'name':'openubmc','version':'9.9.9'}))
        market = outer/'.agents/plugins/marketplace.json'
        market.parent.mkdir(parents=True)
        market.write_bytes(canonical({'name':'personal','plugins':[{'name':'openubmc','source':{'source':'local','path':'./plugins/openubmc'},'policy':{'installation':'AVAILABLE','authentication':'ON_INSTALL'},'category':'Productivity'}]}))
        before = market.read_bytes()
        with patch.dict(os.environ, HOME=str(outer)):
            installed = self.activate()
        self.assertEqual(installed['version'], '2.0.7')
        self.assertEqual(installer.identity(self.home/'plugins/openubmc'), installed['content_digest'])
        self.assertEqual(market.read_bytes(), before)
        self.assertEqual(json.loads(manifest.read_bytes())['version'], '9.9.9')

    @unittest.skipUnless(__import__('shutil').which('codex'), 'Codex required')
    def test_native_install_update_and_rollback_preserve_unrelated_config(self):
        first = self.activate()
        self.assertEqual(first['status'], 'committed')
        self.assertTrue(Path(first['codex_install']['installedPath']).is_dir())
        second = self.activate(1)
        self.assertEqual(second['version'], '2.0.8')
        restored = self.activate()
        self.assertEqual(restored['version'], '2.0.7')
        native_list = json.loads(self.native(['codex','plugin','list','--json'], dict(os.environ, CODEX_HOME=str(self.codex))))
        self.assertEqual(native_list['installed'][0]['version'], '2.0.7')
        self.assertIn('model="before"', (self.codex/'config.toml').read_text())
        self.assertEqual(installer.identity(self.home/'plugins/openubmc'), first['content_digest'])
        store = self.home/'.local/share/openubmc/plugin-store'
        self.assertTrue((store/'archives'/(first['archive_sha256']+'.tar.gz')).is_file())
        self.assertEqual(json.loads((store/'install-audits'/(first['content_digest'][:16]+'.json')).read_bytes())['status'], 'committed')

    @unittest.skipUnless(__import__('shutil').which('codex'), 'Codex required')
    def test_upgrade_atomically_removes_recognized_override_for_retired_cache(self):
        first = self.activate()
        config = self.codex/'config.toml'
        stale = Path(first['codex_install']['installedPath'])/'scripts/pluginctl.py'
        config.write_text(
            config.read_text()
            + '[mcp_servers.openubmc-target-runtime]\n'
            + 'command = "python3"\n'
            + f'args = ["-I", "-B", {json.dumps(str(stale))}, "runtime", "--prepare-on-start"]\n'
        )

        upgraded = self.activate(1)

        document = __import__('tomllib').loads(config.read_text())
        self.assertEqual(upgraded['version'], '2.0.8')
        self.assertNotIn('openubmc-target-runtime', document.get('mcp_servers', {}))
        self.assertFalse(stale.exists())
        journal = self.home/'.local/share/openubmc/plugin-store/transactions'/upgraded['transaction']
        backup = journal/'config-before'
        self.assertIn('openubmc-target-runtime', backup.read_text())

    @unittest.skipUnless(__import__('shutil').which('codex'), 'Codex required')
    def test_activation_migrates_owned_loose_override_before_native_cache_checks(self):
        loose = self.home/'legacy-openubmc/scripts/pluginctl.py'
        arguments = ['-I', str(loose), 'runtime']
        config = self.codex/'config.toml'
        config.write_text(
            config.read_text()
            + '[mcp_servers.openubmc-target-runtime]\n'
            + 'command = "python3"\n'
            + f'args = {json.dumps(arguments)}\n'
        )
        state = self.home/'.config/openubmc/environment-state.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({
            'runtime_mcp': {
                'codex': {
                    'created_entry': True,
                    'command': 'python3',
                    'args': arguments,
                },
            },
        }))

        installed = self.activate()

        document = __import__('tomllib').loads(config.read_text())
        self.assertEqual(installed['version'], '2.0.7')
        self.assertNotIn('openubmc-target-runtime', document.get('mcp_servers', {}))

    @unittest.skipUnless(__import__('shutil').which('codex'), 'Codex required')
    def test_reinstall_repairs_stale_override_when_plugin_registration_is_missing(self):
        stale = self.codex/'plugins/cache/personal/openubmc/2.0.6/scripts/pluginctl.py'
        config = self.codex/'config.toml'
        config.write_text(
            'model="before"\n'
            + '[mcp_servers.openubmc-target-runtime]\n'
            + 'command = "python3"\n'
            + f'args = ["-I", "-B", {json.dumps(str(stale))}, "runtime"]\n'
        )

        installed = self.activate()

        document = __import__('tomllib').loads(config.read_text())
        self.assertEqual(installed['version'], '2.0.7')
        self.assertTrue(document['plugins']['openubmc@personal']['enabled'])
        self.assertNotIn('openubmc-target-runtime', document.get('mcp_servers', {}))

    def test_native_failure_does_not_bless_concurrent_user_config(self):
        from unittest.mock import patch
        config = self.codex/'config.toml'
        def failed(argv, env):
            if argv[0] == sys.executable:
                return '{}'
            config.write_text('model="concurrent-user-change"\n')
            raise ValueError('native failure')
        with patch.object(installer, 'command', side_effect=failed):
            with self.assertRaisesRegex(ValueError, 'native failure'):
                installer.activate(*self.archives[0], self.home, self.codex)
        self.assertEqual(config.read_text(), 'model="concurrent-user-change"\n')
        self.assertFalse((self.home/'plugins/openubmc').exists())

    @unittest.skipUnless(__import__('shutil').which('codex'), 'Codex required')
    def test_process_death_at_each_activation_boundary_recovers(self):
        import subprocess
        from unittest.mock import patch
        self.activate()
        initial = installer.identity(self.home/'plugins/openubmc')
        for boundary in ('old-source', 'new-source', 'market', 'cache', 'config', 'audit'):
            with self.subTest(boundary=boundary):
                script = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install_plugin as m
native = m.command
m.command = lambda argv, env: '{}' if argv[0] == sys.executable else native(argv, env)
original_write, original_rename = m.write, m.rename
boundary = sys.argv[6]
def write(path, data):
    original_write(path, data)
    if (boundary == 'market' and path.name == 'marketplace.json') or (boundary == 'config' and path == Path(sys.argv[5])/'config.toml') or (boundary == 'audit' and path.parent.name == 'install-audits'):
        os._exit(77)
def rename(source, destination):
    original_rename(source, destination)
    if (boundary == 'old-source' and destination.name == 'previous-source') or (boundary == 'new-source' and destination == Path(sys.argv[4])/'plugins/openubmc') or (boundary == 'cache' and destination.parent.name == 'openubmc' and 'codex-stage' not in str(destination)):
        os._exit(77)
m.write, m.rename = write, rename
m.activate(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5]))
'''
                archive, sha = self.archives[1]
                result = subprocess.run([sys.executable, '-c', script, str(ROOT/'scripts'), str(archive), sha, str(self.home), str(self.codex), boundary], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 77, result.stderr)
                pending = [path for path in (self.home/'.local/share/openubmc/plugin-store/transactions').glob('*/transaction.json') if json.loads(path.read_bytes())['status'] not in {'committed','rolled_back'}]
                self.assertEqual(len(pending), 1)
                record = json.loads(pending[0].read_bytes())
                if boundary == 'audit':
                    recovery = """
import os,sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import install_plugin as m
journal=Path(sys.argv[2])
original=m.write
def cut(path,data):
 original(path,data)
 if path.parent.name == 'install-audits': os._exit(78)
m.write=cut
m.compensate(journal,json.loads((journal/'transaction.json').read_bytes()))
"""
                    interrupted = subprocess.run([sys.executable,'-c',recovery,str(ROOT/'scripts'),str(pending[0].parent)],capture_output=True,text=True)
                    self.assertEqual(interrupted.returncode,78,interrupted.stderr)
                # Recovery is safe to call twice, including after it restored
                # the prior source while another restoration was interrupted.
                installer.compensate(pending[0].parent, record)
                installer.compensate(pending[0].parent, record)
                self.assertEqual(installer.identity(self.home/'plugins/openubmc'), initial)
                self.assertIn('model="before"', (self.codex/'config.toml').read_text())
                if boundary == 'audit':
                    audit = self.home/'.local/share/openubmc/plugin-store/install-audits'/(record['content_digest'][:16]+'.json')
                    self.assertEqual(json.loads(audit.read_bytes())['status'], 'rolled_back')
