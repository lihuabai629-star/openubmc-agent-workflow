"""Current service evidence through the bounded collector and an SSH transport."""
import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from systemd_observation import collect_systemd

BOOT = 'f77b7a58-664c-49e2-91f7-71f68146d022'
INVOCATION = '1' * 32
PROPERTIES = ('Id=fan.service\nLoadState=loaded\nActiveState=failed\nSubState=failed\n'
              'Result=exit-code\nExecMainCode=1\nExecMainStatus=7\nInvocationID=' + INVOCATION + '\n')


class CapturedSsh:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **limits):
        self.commands.append(command)
        if command == 'cat /proc/sys/kernel/random/boot_id':
            output = BOOT + '\n'
        elif 'systemctl' in command:
            output = PROPERTIES
        elif 'journalctl' in command:
            output = json.dumps({'_BOOT_ID': BOOT.replace('-', ''),
                '_SYSTEMD_INVOCATION_ID': INVOCATION, '_SYSTEMD_UNIT': 'fan.service',
                '__REALTIME_TIMESTAMP': '1789100000000000', 'MESSAGE': 'fan probe failed',
                'PRIORITY': '3'}) + '\n'
        else:
            raise AssertionError('unexpected command')
        return subprocess.CompletedProcess(command, 0, output, '')


class SystemdCollectorTests(unittest.TestCase):
    def test_failed_discovery_distinguishes_empty_success_and_failure(self):
        for code, output, complete, gap in (
            (0, '', True, []),
            (1, '', False, ['command_failed']),
            (0, 'bad output', False, ['malformed_discovery']),
        ):
            with self.subTest(code=code, output=output):
                ssh = CapturedSsh()
                def transport(command, **limits):
                    if 'list-units' in command:
                        return subprocess.CompletedProcess(command, code, output, '')
                    return ssh(command, **limits)
                value = collect_systemd(['failed'], transport, deadline=time.monotonic() + 10)
                self.assertEqual(value['complete'], complete)
                self.assertEqual(value['gaps'], gap)
                self.assertEqual(value['units'], [])

    def test_untrusted_or_inconsistent_evidence_is_never_complete(self):
        cases = [
            ('journalctl', '{secret-password', 'malformed_journal'),
            ('journalctl', '[]', 'malformed_journal'),
            ('journalctl', json.dumps({'_BOOT_ID': BOOT.replace('-', '')}), 'malformed_journal'),
            ('systemctl', PROPERTIES + 'Id=fan.service\n', 'malformed_properties'),
            ('systemctl', PROPERTIES.replace(INVOCATION, ''), 'invocation_identity_unavailable'),
            ('systemctl', PROPERTIES.replace('ActiveState=failed', 'ActiveState=secret-password'), 'malformed_properties'),
            ('systemctl', PROPERTIES.replace('ExecMainStatus=7', 'ExecMainStatus=garbage'), 'malformed_properties'),
        ]
        for match, output, gap in cases:
            with self.subTest(gap=gap, output=output):
                ssh = CapturedSsh()
                def transport(command, **limits):
                    if match in command:
                        return subprocess.CompletedProcess(command, 0, output, '')
                    return ssh(command, **limits)
                value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
                self.assertFalse(value['complete'])
                self.assertEqual(value['gaps'], [gap])
                self.assertNotIn('secret-password', json.dumps(value))

    def test_transport_failures_are_bounded_typed_gaps(self):
        failures = [
            (subprocess.TimeoutExpired('secret-command', 1), 'deadline_exceeded'),
            (OSError('secret-path'), 'transport_failed'),
            (ValueError('secret-password'), 'transport_failed'),
            (subprocess.CompletedProcess('', 255, '', 'secret-host'), 'transport_failed'),
            (subprocess.CompletedProcess('', 127, '', 'command not found'), 'unsupported'),
            (subprocess.CompletedProcess('', 1, '', 'Permission denied'), 'permission_denied'),
            (subprocess.CompletedProcess('', 0, 'x' * (256 * 1024 + 1), ''), 'output_limit'),
        ]
        for failure, gap in failures:
            with self.subTest(gap=gap):
                def transport(command, **limits):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
                self.assertFalse(value['complete'])
                self.assertEqual(value['gaps'], [gap])
                self.assertNotIn('secret-', json.dumps(value))

    def test_restart_during_collection_preserves_gap(self):
        ssh = CapturedSsh()
        boot_reads = 0
        def transport(command, **limits):
            nonlocal boot_reads
            if command == 'cat /proc/sys/kernel/random/boot_id':
                boot_reads += 1
                if boot_reads == 2:
                    return subprocess.CompletedProcess(command, 0, 'a' * 36, '')
            return ssh(command, **limits)
        value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
        self.assertFalse(value['complete'])
        self.assertEqual(value['gaps'], ['boot_changed'])

    def test_saturated_journal_is_explicitly_partial(self):
        ssh = CapturedSsh()
        def transport(command, **limits):
            reply = ssh(command, **limits)
            if 'journalctl' in command:
                reply.stdout *= 100
            return reply
        value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
        self.assertFalse(value['complete'])
        self.assertEqual(value['gaps'], ['journal_truncated'])
        self.assertEqual(len(value['units'][0]['journal']), 100)

    def test_real_debug_backend_collects_through_runtime_ssh_lease(self):
        import target_runtime_mcp
        from _target_runtime_adapter import open_debug_runtime_lease
        captured = CapturedSsh()
        class Transport:
            def __init__(self, **options):
                pass
            def open_master(self, **scope):
                return object()
            def check_master(self, master):
                return True
            def close_master(self, master):
                pass
            def channel_lost_master(self, master, result):
                return False
            def run_channel(self, master, remote_command, **limits):
                if 'uptime' in remote_command:
                    return subprocess.CompletedProcess('', 0, '2026-09-11 12:00:00 +0000\n up 1 day', '')
                reply = captured(remote_command, **limits)
                if 'journalctl' in remote_command:
                    reply.stdout = reply.stdout.replace('fan probe failed',
                        'fan probe failed fixture-secret --password other-secret')
                return reply
        class Task:
            def close(self):
                pass
            def maintain(self):
                return 0
            def status(self):
                return {}
            @contextlib.contextmanager
            def lease_scope(self, args, **kwargs):
                lease = open_debug_runtime_lease(args=args, task_id='systemd-real',
                    credential_bundle={'ssh': {'user': 'root', 'password': 'fixture-secret', 'port': 22},
                                       'telnet': {'user': '', 'password': '', 'port': 23}},
                    ssh_transport_factory=Transport)
                try:
                    yield lease
                finally:
                    lease.close()
        class Context:
            def raise_if_stopped(self):
                pass
            def remaining(self):
                return 10.0
        value = target_runtime_mcp.DebugMcpBackend().observe_query(Task(), {
            'ip': '192.0.2.10', 'deadline': 10, 'mdb_queries': [], 'capability_names': [],
            'selectors': [{'id': 'services', 'kind': 'systemd', 'names': ['fan.service']}],
        }, Context())
        self.assertTrue(value['result']['systemd']['services']['complete'])
        self.assertEqual(value['observation_timing']['selectors'][0]['status'], 'observed')
        self.assertNotIn('fixture-secret', json.dumps(value))

        runtime = target_runtime_mcp._load_runtime_module()
        class Backend(target_runtime_mcp.DebugMcpBackend):
            def open_task(self, task_id):
                return Task()
        blobs = runtime.InMemoryBlobRepository()
        service = runtime.RuntimeMcpService(Backend(), blob_repository=blobs)
        self.addCleanup(service.close)
        receipt = service.call_exposed_tool('observe', {
            'target': '192.0.2.10', 'selectors': [
                {'id': 'services', 'kind': 'systemd', 'names': ['fan.service']}],
        }, task_id='systemd-e2e', operation_id='observe')
        self.assertEqual(receipt['status'], 'complete')
        source = blobs.read(receipt['observation_ref']['handle'].removeprefix('blob://'))
        self.assertNotIn(b'fixture-secret', source)
        self.assertNotIn(b'other-secret', source)
        self.assertIn(b'fan probe failed', source)
        with self.assertRaises(ValueError):
            service.call_exposed_tool('execute', {
                'kind': 'start', 'target': '192.0.2.11', 'intent': 'diagnosis-only',
                'observation_ref': receipt['observation_ref'],
            }, task_id='systemd-wrong-target', operation_id='start-wrong')
        waiting = service.call_exposed_tool('execute', {
            'kind': 'start', 'target': '192.0.2.10', 'intent': 'diagnosis-only',
            'observation_ref': receipt['observation_ref'],
        }, task_id='systemd-e2e', operation_id='start')
        self.assertEqual(waiting['gate']['name'], 'diagnosis.acceptance')
        self.assertEqual(waiting['state'], 'waiting_response')
        gate = waiting['gate']
        final = service.call_exposed_tool('execute', {
            'kind': 'respond', 'run_id': waiting['run_id'],
            **{key: gate[key] for key in ('gate_id', 'gate_version', 'schema_digest')},
            'response': {'status': 'completed', 'summary': 'Fan probe failure verified',
                'payload': {'root_cause': 'fan probe failed with exit status 7',
                    'evidence_ids': [item['evidence_id'] for item in waiting['diagnostic_receipt']['evidence']],
                    'causal_chain': ['fan probe failed', 'main process exited with status 7'],
                    'code_owner': 'fan.service', 'contradictions': [], 'remaining_gaps': [],
                    'verification_status': 'verified'}},
        }, task_id='systemd-e2e', operation_id='accept')
        self.assertEqual(final['outcome']['status'], 'completed')

    def test_previous_invocation_journal_cannot_prove_current_failure(self):
        ssh = CapturedSsh()
        def transport(command, **limits):
            reply = ssh(command, **limits)
            if 'journalctl' in command:
                reply.stdout = reply.stdout.replace(INVOCATION, '2' * 32)
            return reply
        value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
        self.assertFalse(value['complete'])
        self.assertEqual(value['gaps'], ['journal_invocation_mismatch'])

    def test_system_manager_failure_explanation_is_collected(self):
        ssh = CapturedSsh()
        def transport(command, **limits):
            reply = ssh(command, **limits)
            if 'journalctl' in command:
                reply.stdout = json.dumps({'_BOOT_ID': BOOT.replace('-', ''),
                    'UNIT': 'fan.service', 'INVOCATION_ID': INVOCATION, '_PID': '1',
                    '__REALTIME_TIMESTAMP': '1789100000000000', 'PRIORITY': '3',
                    'MESSAGE': 'Failed to start fan.service: executable missing'})
            return reply
        value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
        self.assertTrue(value['complete'])
        self.assertIn('executable missing', value['units'][0]['journal'][0]['MESSAGE'])

    def test_transport_capture_metadata_prevents_false_complete(self):
        for attribute, gap in [('output_limit_exceeded', 'output_limit'),
                               ('timed_out', 'deadline_exceeded'),
                               ('stdout_read_error', 'transport_failed')]:
            with self.subTest(attribute=attribute):
                def transport(command, **limits):
                    reply = subprocess.CompletedProcess('', 0, BOOT, '')
                    setattr(reply, attribute, True)
                    return reply
                value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
                self.assertFalse(value['complete'])
                self.assertEqual(value['gaps'], [gap])

    def test_missing_unit_has_distinct_gap(self):
        ssh = CapturedSsh()
        def transport(command, **limits):
            if 'systemctl' in command:
                return subprocess.CompletedProcess(command, 4,
                    PROPERTIES.replace('LoadState=loaded', 'LoadState=not-found').replace(INVOCATION, ''), '')
            return ssh(command, **limits)
        value = collect_systemd(['fan.service'], transport, deadline=time.monotonic() + 10)
        self.assertEqual(value['gaps'], ['unit_not_found'])
        self.assertFalse(value['complete'])

    def test_invalid_selectors_never_access_target(self):
        for names in ([{}], ['--help'], ['*.service'], ['../fan.service'],
                      ['fan.service'] * 2, [], ['x.service'] * 17):
            with self.subTest(names=names):
                ssh = CapturedSsh()
                with self.assertRaises(ValueError):
                    collect_systemd(names, ssh, deadline=time.monotonic() + 10)
                self.assertEqual(ssh.commands, [])

    def test_failed_service_is_successfully_collected_current_evidence(self):
        ssh = CapturedSsh()
        value = collect_systemd(['fan.service'], ssh, deadline=time.monotonic() + 10)
        self.assertTrue(value['complete'])
        self.assertEqual(value['gaps'], [])
        unit = value['units'][0]
        self.assertEqual(unit['properties']['ActiveState'], 'failed')
        self.assertEqual(unit['properties']['ExecMainStatus'], '7')
        self.assertEqual(unit['journal'][0]['MESSAGE'], 'fan probe failed')
        self.assertEqual(value['boot_id'], BOOT)
        self.assertTrue(all('restart' not in c and 'sudo' not in c for c in ssh.commands))


if __name__ == '__main__':
    unittest.main()
