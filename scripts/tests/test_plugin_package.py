"""Behavioral tests for the Codex plugin distribution boundary."""
import hashlib
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
    def test_immutable_source_build_is_reproducible_and_relocatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ('one.tar.gz', 'two.tar.gz'):
                result = subprocess.run([sys.executable, str(BUILDER), 'build', '--source', str(ROOT), '--output', str(base/name)], capture_output=True, text=True)
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
