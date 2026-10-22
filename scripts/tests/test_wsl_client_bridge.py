import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT/'plugin/openubmc/scripts/openubmc-wsl-bridge.js'


class WslClientBridgeTests(unittest.TestCase):
    def command(self, **changes):
        selected = {'distribution': '0630', 'pluginRoot': '/opt/openubmc with space',
                    'capability': 'runtime', 'environment': {
                        'PATH': 'fixture-path', 'OPENUBMC_MCP_TASK_ID': 'existing-task',
                        'OPENUBMC_KB_PASSWORD': 'private-fixture', 'WSLENV': 'OPENUBMC_KB_PASSWORD/u',
                    }}
        selected.update(changes)
        return subprocess.run(['node', '-e',
            'const {wslCommand}=require(process.argv[1]); console.log(JSON.stringify(wslCommand(JSON.parse(process.argv[2]))))',
            str(BRIDGE), json.dumps(selected)], capture_output=True, text=True)

    def test_bridge_preserves_argv_and_task_identity_without_forwarding_credentials(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['command'], 'wsl.exe')
        self.assertEqual(value['args'][:6], ['--distribution', '0630', '--cd', '/', '--exec', '/usr/bin/env'])
        self.assertIn('OPENUBMC_MCP_TASK_ID=existing-task', value['args'])
        self.assertEqual(value['args'][-2:], ['/opt/openubmc with space/scripts/openubmc-mcp-bootstrap.js', 'runtime'])
        self.assertEqual(value['env'], {'PATH': 'fixture-path'})
        self.assertNotIn('private-fixture', result.stdout)
        self.assertNotIn('WSLENV', result.stdout)

    def test_each_capability_uses_the_existing_linux_entrypoint(self):
        for capability, entry in [('kb', 'scripts/openubmc-mcp-bootstrap.js'),
                                  ('panel', 'panel/native.mjs'), ('host-hook', 'scripts/openubmc-continuity-hook.js')]:
            result = self.command(capability=capability)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('/opt/openubmc with space/'+entry, json.loads(result.stdout)['args'])

    def test_missing_selection_and_unsupported_capabilities_fail_before_launch(self):
        for changes in ({'distribution': ''}, {'distribution': '--help'},
                        {'pluginRoot': 'C:\\openubmc'}, {'capability': 'execute'}):
            self.assertNotEqual(self.command(**changes).returncode, 0)
