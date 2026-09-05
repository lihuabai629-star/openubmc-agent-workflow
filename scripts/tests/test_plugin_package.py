"""Behavioral tests for the Codex plugin distribution boundary."""
import hashlib
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
        files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
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
            self.assertEqual(len(report['skills']), 11)
            self.assertEqual(len(report['source_commit']), 40)
            self.assertNotIn(str(ROOT).encode(), (plugin/'scripts/launch_runtime.py').read_bytes())
            self.assertFalse(any(p.name == '.git' for p in plugin.rglob('*')))

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
                launch = Path(transport['cwd'])/transport['args'][1]
                self.assertTrue(launch.is_file(), str(launch))
                check = subprocess.run([transport['command'], '-I', str(launch), 'verify'], capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)
            result = subprocess.run(['codex', 'plugin', 'remove', 'openubmc@runtime-test'], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
