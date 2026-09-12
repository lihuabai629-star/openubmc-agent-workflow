"""Local browser maintenance API against a real immutable plugin and isolated home."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
if __package__:
    from .plugin_fixture import package_fixture
else:
    from plugin_fixture import package_fixture

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('maintenance_page', ROOT/'openubmc-environment-setup/scripts/config_page.py')
page = importlib.util.module_from_spec(spec)
spec.loader.exec_module(page)

class PluginHealthTests(unittest.TestCase):
    def test_unprepared_plugin_opens_recovery_page_without_downloading_dependencies(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = package_fixture(root)
            home = root/'home'; home.mkdir()
            env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home/'codex'),
                       XDG_DATA_HOME=str(home/'data'), XDG_CONFIG_HOME=str(home/'.config'),
                       PIP_NO_INDEX='1')
            process = subprocess.Popen([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'configure', '--no-browser'],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            timer = threading.Timer(15, process.kill); timer.start()
            try:
                url = process.stdout.readline().strip()
                self.assertTrue(url.startswith('http://127.0.0.1:'), 'Recovery page must open without dependencies')
                origin, token = url.split('/#')
                request = Request(origin+'/api/close', data=b'{}', headers={
                    'X-OpenUBMC-Session':token, 'Origin':origin, 'Content-Type':'application/json'})
                with urlopen(request, timeout=5) as response:
                    self.assertTrue(json.load(response)['closed'])
                process.wait(timeout=5)
            finally:
                if process.poll() is None: process.kill(); process.wait()
                process.stdout.close(); timer.cancel()

    def test_status_preview_apply_and_undo_use_private_transactions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = package_fixture(root)
            home = root/'home'; codex = home/'.codex'; codex.mkdir(parents=True)
            old = codex/'plugins/cache/openubmc-public/openubmc/2.0.12/scripts/pluginctl.py'
            config = codex/'config.toml'
            original = ('[plugins."openubmc@openubmc-public"]\nenabled=true\n'
                        '[mcp_servers.openubmc-kb]\ncommand="python3"\n'
                        f'args={json.dumps(["-I", str(old), "kb"])}\n')
            config.write_text(original)
            env = dict(os.environ, HOME=str(home), CODEX_HOME=str(codex), XDG_DATA_HOME=str(home/'data'), XDG_CONFIG_HOME=str(home/'.config'))
            control = page.PluginMaintenance(plugin, environment=env)
            with page.LocalConfigurationServer(home/'.config', maintenance=control) as server:
                def request(action, **data):
                    req = Request(server.origin+'/api/plugin', data=json.dumps({'action':action, **data}).encode(), headers={'X-OpenUBMC-Session':server.session_token, 'Origin':server.origin, 'Content-Type':'application/json'})
                    with urlopen(req, timeout=15) as response: return json.load(response)
                state = request('status')
                self.assertTrue(state['integrity'])
                self.assertFalse(state['configuration']['ready'])
                self.assertEqual(state['configuration']['servers'], ['openubmc-kb'])
                preview = request('preview')
                self.assertEqual(config.read_text(), original)
                self.assertEqual(preview['servers'], ['openubmc-kb'])
                config.write_text(original + '# concurrent edit\n')
                with self.assertRaises(HTTPError) as error:
                    request('apply', preview_id=preview['preview_id'])
                self.assertEqual(error.exception.code, 409)
                self.assertTrue(config.read_text().endswith('# concurrent edit\n'))
                config.write_text(original)
                preview = request('preview')
                applied = request('apply', preview_id=preview['preview_id'])
                self.assertTrue(applied['changed'])
                self.assertNotIn('mcp_servers', config.read_text())
                request('undo', transaction=applied['transaction'])
                self.assertEqual(config.read_text(), original)
