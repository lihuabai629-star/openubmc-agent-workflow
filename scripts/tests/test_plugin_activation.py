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
        self.root = Path(self.temporary.name)
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
    def test_native_install_update_and_rollback_preserve_unrelated_config(self):
        first = self.activate()
        self.assertEqual(first['status'], 'committed')
        self.assertTrue(Path(first['codex_install']['installedPath']).is_dir())
        second = self.activate(1)
        self.assertEqual(second['version'], '2.0.8')
        restored = self.activate()
        self.assertEqual(restored['version'], '2.0.7')
        self.assertIn('model="before"', (self.codex/'config.toml').read_text())
        self.assertEqual(installer.identity(self.home/'plugins/openubmc'), first['content_digest'])
        store = self.home/'.local/share/openubmc/plugin-store'
        self.assertTrue((store/'archives'/(first['archive_sha256']+'.tar.gz')).is_file())
        self.assertEqual(json.loads((store/'install-audits'/(first['content_digest'][:16]+'.json')).read_bytes())['status'], 'committed')

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
                # Recovery is safe to call twice, including after it restored
                # the prior source while another restoration was interrupted.
                installer.compensate(pending[0].parent, record)
                installer.compensate(pending[0].parent, record)
                self.assertEqual(installer.identity(self.home/'plugins/openubmc'), initial)
                self.assertIn('model="before"', (self.codex/'config.toml').read_text())
