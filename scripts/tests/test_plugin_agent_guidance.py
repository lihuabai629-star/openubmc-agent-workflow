"""Execute examples taken from the Skills shipped to real Agents."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

if __package__:
    from .plugin_fixture import package_fixture
else:
    from plugin_fixture import package_fixture


def json_examples(markdown):
    return [json.loads(block.split('```', 1)[0])
            for block in markdown.split('```json')[1:]]


class PluginAgentGuidanceTests(unittest.TestCase):
    def test_packaged_tls_guidance_distinguishes_transport_from_strict_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = package_fixture(Path(temporary))
            reference = ' '.join((plugin / 'skills/openubmc-debug/references/remote-automation.md').read_text().split())
            setup = ' '.join((plugin / 'skills/openubmc-environment-setup/SKILL.md').read_text().split())
            self.assertNotIn('Internal BMC workflows authorize insecure TLS by default', reference)
            for content in (reference, setup):
                self.assertIn('strict-check failure remains unverified', content)
                self.assertIn('does not qualify certificate validation', content)

    def test_packaged_debug_start_example_is_accepted_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = package_fixture(Path(temporary))
            skill = plugin / 'skills/openubmc-debug/SKILL.md'
            examples = [example for example in json_examples(skill.read_text())
                        if example.get('kind') == 'start']
            self.assertEqual(len(examples), 1, 'Debug entrypoint needs one usable start Action')
            action = {**examples[0], 'target': '192.0.2.10'}
            result = subprocess.run([
                sys.executable, '-I', '-B', '-c', '''
import json, sys
sys.path.insert(0, sys.argv[1])
from openubmc_target_runtime.semantic_runtime import decode_run_command
from openubmc_target_runtime.mutation import TaskAuthorizationPolicy
action = json.loads(sys.stdin.read())
command = decode_run_command(action, operation_id="documented-start")
policy = TaskAuthorizationPolicy.from_task_intent(
    command.intent, delivery_strategy=command.delivery_strategy)
for invalid in ({**action, "symptom": "reported failure"}, {**action, "intent": "diagnose"}):
    try:
        rejected = decode_run_command(invalid, operation_id="invalid-start")
        TaskAuthorizationPolicy.from_task_intent(
            rejected.intent, delivery_strategy=rejected.delivery_strategy)
    except ValueError:
        pass
    else:
        raise AssertionError("Undocumented aliases or fields must remain rejected")
print(json.dumps({"intent": command.intent, "purpose": command.purpose,
                  "deadline": command.caller_deadline,
                  "allowed_actions": sorted(policy.allowed_actions)}))
''', str(plugin / 'skills/openubmc-target-runtime')],
                input=json.dumps(action), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            decoded = json.loads(result.stdout)
            self.assertEqual(decoded['intent'], 'diagnosis-only')
            self.assertTrue(decoded['purpose'])
            self.assertGreater(decoded['deadline'], 0)
            self.assertLessEqual(decoded['deadline'], 120)
            self.assertEqual(decoded['allowed_actions'], [])

    def test_packaged_diagnosis_response_example_decodes_with_returned_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = package_fixture(Path(temporary))
            reference = plugin / 'skills/openubmc-debug/references/agent-gateway.md'
            examples = [example for example in json_examples(reference.read_text())
                        if example.get('kind') == 'respond'
                        and example.get('response', {}).get('status') == 'failed']
            self.assertEqual(len(examples), 1, 'Include a response for an unsupported diagnosis')
            action = {**examples[0], 'run_id': 'documented-run', 'gate_id': 'diagnosis-gate',
                      'gate_version': 3, 'schema_digest': 'a' * 64,
                      'submission_id': 'returned-submission'}
            result = subprocess.run([
                sys.executable, '-I', '-B', '-c', '''
import json, sys
sys.path.insert(0, sys.argv[1])
from openubmc_target_runtime.semantic_runtime import decode_run_command
command = decode_run_command(json.loads(sys.stdin.read()), operation_id="documented-response")
print(json.dumps({"run_id": command.run_id, "gate_id": command.gate_id,
                  "gate_version": command.gate_version, "schema_digest": command.schema_digest,
                  "submission_id": command.submission_id, "status": command.response["status"]}))
''', str(plugin / 'skills/openubmc-target-runtime')],
                input=json.dumps(action), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                'run_id': 'documented-run', 'gate_id': 'diagnosis-gate', 'gate_version': 3,
                'schema_digest': 'a' * 64, 'submission_id': 'returned-submission', 'status': 'failed',
            })

    def test_packaged_local_credential_example_uses_the_activated_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            plugin = package_fixture(base)
            skill = plugin / 'skills/openubmc-environment-setup/SKILL.md'
            examples = [block.split('```', 1)[0]
                        for block in skill.read_text().split('```python')[1:]]
            self.assertEqual(len(examples), 1, 'Include a secret-free local resolver example')
            example = examples[0].replace('<plugin-root>', str(plugin)).replace('<BMC IP>', '192.0.2.10')
            result = subprocess.run([
                sys.executable, '-I', '-B', '-c', '''
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
from openubmc_target_runtime.configuration import LocalConfigurationStore
config_home = pathlib.Path(sys.argv[2])
os.environ.clear()
os.environ["XDG_CONFIG_HOME"] = str(config_home)
source = config_home / "openubmc/credentials.json"
store = LocalConfigurationStore(source, kind="targets")
saved = store.save({"schema_version": 1,
    "credentials": {"local": {"user": "fixture", "password": "doc-local-only-secret"}},
    "defaults": {"bmc": {"ssh": "local", "redfish": "local"}}, "targets": {}}, expected_revision=None)
store.activate(saved["revision"], expected_active_revision=None)
assert not source.exists()
exec(compile(sys.stdin.read(), "<packaged-credential-example>", "exec"))
print(json.dumps({"expected_active_revision": saved["revision"]}))
''', str(plugin / 'skills/openubmc-target-runtime'), str(base / 'config')],
                input=example, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            report, expected = map(json.loads, result.stdout.splitlines())
            self.assertEqual(report, {
                'configured': True, 'cache_reused': True,
                'active_revision': expected['expected_active_revision'],
                'remote_authentication': 'not_checked',
            })
            self.assertNotIn('doc-local-only-secret', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
