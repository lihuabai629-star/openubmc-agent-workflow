"""Local browser maintenance API against a real immutable plugin and isolated home."""
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
import tempfile
import time
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
    def test_configure_opens_the_browser_and_still_prints_the_session_url(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = package_fixture(root)
            home = root/'home'; home.mkdir()
            browser = root/'browser.sh'
            opened = root/'opened-url'
            browser.write_text('#!/bin/sh\nprintf "%s" "$1" > "$OPENUBMC_TEST_BROWSER_LOG"\n')
            browser.chmod(0o700)
            binary = root/'bin'; binary.mkdir()
            (binary/'wslview').symlink_to(browser)
            env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home/'.config'),
                       BROWSER=str(browser), PATH=str(binary)+os.pathsep+os.environ['PATH'],
                       OPENUBMC_TEST_BROWSER_LOG=str(opened))
            process = subprocess.Popen([sys.executable, '-I', '-B', str(plugin/'scripts/pluginctl.py'),
                'configure', '--home', str(home)], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            timer = threading.Timer(15, process.kill); timer.start()
            try:
                url = process.stdout.readline().strip()
                self.assertTrue(url.startswith('http://127.0.0.1:'), url)
                deadline = time.monotonic() + 4
                while not opened.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(opened.read_text(), url)
                origin, token = url.split('/#')
                request = Request(origin+'/api/close', data=b'{}', headers={
                    'X-OpenUBMC-Session': token, 'Origin': origin, 'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    self.assertTrue(json.load(response)['closed'])
                process.wait(timeout=5)
            finally:
                timer.cancel()
                if process.poll() is None: process.kill(); process.wait()
                process.stdout.close()
                process.stderr.close()

    def test_packaged_focused_configuration_reports_completion_without_probing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            plugin = package_fixture(root)
            home = root/'home'; home.mkdir()
            env = {key: value for key, value in os.environ.items() if not key.startswith(('OPENUBMC_', 'XDG_', 'REDFISH_'))}
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home/'.config'))
            process = subprocess.Popen([sys.executable, '-I', '-B', str(plugin/'scripts/pluginctl.py'),
                'configure', '--home', str(home), '--kind', 'targets', '--focus-target', '192.0.2.10',
                '--no-browser', '--wait-for-save'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            timer = threading.Timer(15, process.kill); timer.start()
            try:
                url = process.stdout.readline().strip()
                self.assertTrue(url.startswith('http://127.0.0.1:'), url)
                origin, token = url.split('/#')
                def post(path, data):
                    request = Request(origin+path, data=json.dumps(data).encode(), headers={
                        'X-OpenUBMC-Session': token, 'Origin': origin, 'Content-Type': 'application/json'})
                    with urlopen(request, timeout=5) as response: return json.load(response)
                saved = post('/api/save', {'kind': 'targets', 'expected_revision': None,
                    'config': {'schema_version': 1, 'credentials': {
                        'bmc': {'user': 'fixture', 'password': {'action': 'replace', 'value': 'private-device-password'}},
                        'unused': {'user': 'incomplete'}},
                        'defaults': {'os': {'ssh': 'unused'}},
                        'targets': {'192.0.2.10': {'bmc': {'ssh': 'bmc', 'redfish': 'bmc'}}},
                        'devices': {'192.0.2.10': {'os_ip': '192.0.2.20'}}}})
                post('/api/activate', {'kind': 'targets', 'revision': saved['revision'], 'expected_active_revision': None})
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                receipt = json.loads(stdout)
                self.assertTrue(receipt['configured'])
                self.assertEqual(receipt['associated_os'], '192.0.2.20')
                self.assertEqual(receipt['checks'], [])
                self.assertNotIn('private-device-password', stdout+stderr)
            finally:
                timer.cancel()
                if process.poll() is None: process.kill()
                process.communicate()

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
