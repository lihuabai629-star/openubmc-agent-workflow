"""Exercise native SOL over real local SSH, framed probes and Runtime receipts."""
import codecs
import contextlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import os_console_observation as console

BOOT = 'f77b7a58-664c-49e2-91f7-71f68146d022'
VALUES = {'OPENUBMC_SSH_USER': 'bmc-user', 'OPENUBMC_SSH_PASSWORD': 'bmc-fixture-private',
          'OPENUBMC_OS_SSH_USER': 'os-user', 'OPENUBMC_OS_SSH_PASSWORD': 'os-fixture-private'}
HOST_KEY = paramiko.RSAKey.generate(2048)
CLIENT_KEY = paramiko.RSAKey.generate(2048)


class SshFixture(paramiko.ServerInterface):
    def __init__(self, owner):
        self.owner = owner
        self.ready = threading.Event()

    def check_auth_password(self, username, password):
        self.owner.rows.append({'event': 'bmc_auth', 'user': username, 'password': password})
        return (paramiko.AUTH_SUCCESSFUL if self.owner.mode != 'bmc_auth'
                and username == VALUES['OPENUBMC_SSH_USER'] and password == VALUES['OPENUBMC_SSH_PASSWORD']
                else paramiko.AUTH_FAILED)

    def check_auth_publickey(self, username, key):
        self.owner.rows.append({'event': 'bmc_key_auth', 'user': username})
        return paramiko.AUTH_SUCCESSFUL if username == 'bmc-user' and key == CLIENT_KEY else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password,publickey'

    def check_channel_request(self, kind, channel_id):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        self.owner.rows.append({'event': 'pty'})
        return True

    def check_channel_shell_request(self, channel):
        self.ready.set()
        return True


class NativeSolServer:
    """Local SSH peer implementing the BMC CLI and an OS serial login, never a device."""
    def __init__(self, mode='chinese'):
        self.mode = mode
        self.rows = []
        self.transports = []
        self.workers = []
        self.stopped = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(.05)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self.serve, args=(conn,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, conn):
        transport = paramiko.Transport(conn)
        self.transports.append(transport)
        transport.add_server_key(HOST_KEY)
        try:
            interface = SshFixture(self)
            transport.start_server(server=interface)
            channel = transport.accept(3)
            if channel is None or not interface.ready.wait(3):
                return
            channel.settimeout(.05)
            self.dialogue(channel)
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()
            self.rows.append({'event': 'closed'})

    def dialogue(self, channel):
        mode = self.mode
        def emit(text):
            channel.sendall(text.encode() if isinstance(text, str) else text)
        prompt = '~ $ ' if mode == 'bmc_shell' else 'openUBMC:/->'
        emit('Last login: fixture\r\n' + ('~ ' if mode == 'bmc_shell' else '') + prompt)
        state, pending, commands = 'bmc', '', 0
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        while not self.stopped.is_set():
            try:
                data = channel.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            pending += decoder.decode(data)
            if mode in {'interrupt_flush', 'probe_blocked'} and '\x03' in pending:
                self.rows.append({'event': 'interrupt_flush'})
                pending = ''
                emit('\r\n[os-user@host ~]# ')
                continue
            if '\x1b(' in pending:
                self.rows.append({'event': 'detach'})
                pending = ''
                if mode != 'close_stuck':
                    state = 'bmc'
                    emit('\r\nSOL disconnected.\r\n' + prompt)
                continue
            if '\x03\x04' in pending:
                pending = ''
                state = 'user'
                emit('\r\nlocalhost login: ')
                continue
            pending = pending.replace('\x03', '')
            while '\r' in pending:
                line, pending = pending.split('\r', 1)
                if state == 'bmc':
                    self.rows.append({'event': 'bmc_command', 'value': line})
                    emit(line + '\r\n')
                    if line == console.HELP_COMMAND:
                        if mode == 'unsupported':
                            emit('Unknown command\r\n')
                        else:
                            option = '1' if mode == 'old_option' else '2'
                            emit('Usage: ipmcset -t sol -d activate -v <option> <mode>\r\n')
                            emit(option + '    SYS COM\r\n3    BMC COM\r\n0    Shared mode\r\n1    Private mode\r\n')
                    elif line == console.SESSIONS_COMMAND:
                        if mode != 'malformed_sessions':
                            emit('Index   Type    Mode    LoginTime                   IP             Name\r\n')
                            emit('1       SSH Shared 2026-10-26 01:00:00 192.0.2.5 other\r\n' if mode == 'busy'
                                 else '1       N/A     N/A     N/A    N/A    N/A\r\n')
                            emit('2       N/A     N/A     N/A    N/A    N/A\r\n')
                    elif line.startswith('ipmcset -t sol -d activate -v '):
                        self.rows.append({'event': 'activate', 'value': line})
                        if mode == 'activation_rejected':
                            emit('Insufficient privilege\r\n' + prompt)
                            continue
                        expected = '1' if mode == 'old_option' else '2'
                        if line != 'ipmcset -t sol -d activate -v ' + expected + ' 0':
                            emit('Invalid option\r\n' + prompt)
                            continue
                        emit("[Connect SOL successfully! Use 'Esc(' to exit.]\r\n")
                        emit('Warning! The SOL session is in shared mode, the operation can be viewed on another terminal.\r\n')
                        state = 'user'
                        if mode == 'existing_shell':
                            emit('[root@host ~]# ')
                        elif mode == 'firmware':
                            emit('UEFI boot menu\r\n1) Reset host\r\n')
                        elif mode == 'flood':
                            emit('x' * (300 * 1024))
                        elif mode not in {'no_prompt', 'delayed_prompt'}:
                            emit('localhost login: ')
                        continue
                    else:
                        emit('Unknown command\r\n')
                    emit(prompt)
                elif not line:
                    if mode != 'no_prompt':
                        emit('localhost login: ')
                elif state == 'user':
                    self.rows.append({'event': 'username', 'value': line})
                    if mode == 'english':
                        emit('Password: ')
                    else:
                        raw = '密码：'.encode('utf-8')
                        emit(raw[:2]); time.sleep(.01); emit(raw[2:])
                    state = 'password'
                elif state == 'password':
                    self.rows.append({'event': 'password', 'value': line})
                    if mode == 'reject':
                        emit('\r\nLogin incorrect\r\nlocalhost login: '); state = 'user'
                    else:
                        emit('\r\nWelcome\r\n\x1b[32m[os-user@host ~]#\x1b[0m '); state = 'shell'
                elif line == 'exit':
                    self.rows.append({'event': 'logout'})
                    if mode == 'logout_delay':
                        time.sleep(1.8)
                    if mode != 'logout_stuck':
                        emit('\r\nlocalhost login: ')
                    state = 'user'
                else:
                    commands += 1
                    self.rows.append({'event': 'command', 'value': line})
                    if mode == 'probe_blocked':
                        continue
                    boot = BOOT if mode != 'boot_changed' or commands == 1 else 'a77b7a58-664c-49e2-91f7-71f68146d022'
                    shell = "id() { printf '0\\n'; }; tty() { printf '/dev/ttyAMA0\\n'; }; "
                    shell += "uname() { printf 'Linux 5.10 aarch64 GNU/Linux\\n'; }; hostname() { printf 'host\\n'; }; "
                    shell += "cat() { printf '%s\\n' '" + boot + "'; }; "
                    shell += "ip() { case \"$2\" in address) printf '%s\\n' '[{\"ifname\":\"eno8\",\"operstate\":\"UP\",\"addr_info\":[{\"local\":\"192.0.2.20\",\"prefixlen\":24}]}]';; route) printf '%s\\n' '[{\"dst\":\"default\",\"gateway\":\"192.0.2.1\",\"dev\":\"eno8\"}]';; esac; }; "
                    if mode == 'probe_failed':
                        shell += 'id() { return 1; }; '
                    if mode == 'malformed_identity':
                        shell += "tty() { printf '/dev/pts/5\\n'; }; "
                    if mode == 'secret_echo':
                        shell += "hostname() { printf 'os-fixture-private\\n'; }; "
                    reply = subprocess.run(['/bin/sh', '-c', shell + line], capture_output=True, timeout=1)
                    emit(line + '\r\n'); emit(reply.stdout + reply.stderr); emit('\r\n[os-user@host ~]# ')

    def close(self):
        self.stopped.set()
        self.listener.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(1)
        for worker in self.workers:
            worker.join(1)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @contextlib.contextmanager
    def transport(self, mode='chinese'):
        server = NativeSolServer(mode)
        try:
            yield server
        finally:
            server.close()

    def collect(self, mode='chinese', *, seconds=5, values=None):
        sessions = []
        def factory(*args, **kwargs):
            session = console.SolSession(*args, **kwargs)
            sessions.append(session)
            return session
        with self.transport(mode) as server:
            value = console.collect_os_console(['identity', 'network'], '127.0.0.1', values or VALUES,
                deadline=time.monotonic() + seconds, ssh_port=server.port, session_factory=factory)
            rows = server.rows
        for session in sessions:
            self.assertTrue(session.channel.closed)
            self.assertIsNone(session.client.get_transport())
        for secret in ('bmc-fixture-private', 'os-fixture-private'):
            self.assertNotIn(secret, json.dumps(value))
        return value, rows

    def test_bilingual_login_fixed_probes_and_owned_logout_over_real_ssh(self):
        for mode in ('chinese', 'english', 'delayed_prompt', 'old_option', 'bmc_shell',
                     'interrupt_flush', 'logout_delay'):
            with self.subTest(mode=mode):
                result, rows = self.collect(mode)
                self.assertTrue(result['complete'], result['gaps'])
                self.assertEqual(result['transport'], 'bmc-ssh-sol')
                self.assertEqual(result['identity']['boot_id'], BOOT)
                self.assertEqual(result['identity']['tty'], '/dev/ttyAMA0')
                self.assertEqual(result['network']['interfaces'][0]['addresses'], ['192.0.2.20/24'])
                self.assertEqual([row['value'] for row in rows if row['event'] == 'password'],
                                 [VALUES['OPENUBMC_OS_SSH_PASSWORD']])
                self.assertEqual(sum(row['event'] == 'logout' for row in rows), 1)
                self.assertEqual(sum(row['event'] == 'detach' for row in rows), 1)
                self.assertEqual(sum(row['event'] == 'command' for row in rows), 3)
                self.assertNotIn('deactivate', json.dumps(rows))
                self.assertEqual(next(row['value'] for row in rows if row['event'] == 'activate'),
                    'ipmcset -t sol -d activate -v ' + ('1' if mode == 'old_option' else '2') + ' 0')

    def test_failed_auth_busy_and_existing_shell_never_run_probes(self):
        for mode, gap in (('busy', 'sol_busy'), ('bmc_auth', 'bmc_authentication_failed'),
                          ('unsupported', 'sol_unavailable'), ('malformed_sessions', 'sol_session_state_unverified'),
                          ('activation_rejected', 'sol_permission_denied'), ('existing_shell', 'console_not_at_login'),
                          ('firmware', 'console_not_at_login'), ('reject', 'os_authentication_failed')):
            with self.subTest(mode=mode):
                result, rows = self.collect(mode)
                self.assertFalse(result['complete'])
                self.assertIn(gap, result['gaps'])
                self.assertFalse(any(row['event'] == 'command' for row in rows))
                self.assertLessEqual(sum(row['event'] == 'password' for row in rows), 1)
                if mode in {'busy', 'unsupported', 'malformed_sessions', 'bmc_auth'}:
                    self.assertFalse(any(row['event'] == 'activate' for row in rows))

    def test_incomplete_evidence_and_cleanup_stay_explicit(self):
        for mode, gap in (('boot_changed', 'os_boot_changed'), ('probe_failed', 'os_probe_failed'),
                          ('malformed_identity', 'os_identity_unverified'), ('flood', 'output_limit'),
                          ('no_prompt', 'deadline_exceeded'), ('logout_stuck', 'os_logout_unconfirmed'),
                          ('close_stuck', 'sol_close_unconfirmed')):
            with self.subTest(mode=mode):
                started = time.monotonic()
                result, _ = self.collect(mode, seconds=2.8 if mode == 'no_prompt' else 5)
                self.assertFalse(result['complete'])
                self.assertIn(gap, result['gaps'])
                self.assertLess(time.monotonic() - started, 5.8)

    def test_secret_in_typed_evidence_is_redacted(self):
        result, _ = self.collect('secret_echo')
        self.assertTrue(result['complete'], result['gaps'])
        self.assertNotEqual(result['identity']['hostname'], VALUES['OPENUBMC_OS_SSH_PASSWORD'])

    def test_timed_out_probe_is_interrupted_before_separate_logout(self):
        result, rows = self.collect('probe_blocked', seconds=3.5)
        self.assertEqual(result['gaps'], ['deadline_exceeded'])
        self.assertFalse(result['complete'])
        self.assertEqual(sum(row['event'] == 'interrupt_flush' for row in rows), 1)
        self.assertEqual(sum(row['event'] == 'logout' for row in rows), 1)
        self.assertEqual(sum(row['event'] == 'detach' for row in rows), 1)

    def test_printable_tilde_password_is_literal_in_native_sol(self):
        result, rows = self.collect(values={**VALUES, 'OPENUBMC_OS_SSH_PASSWORD': '~Bfixture'})
        self.assertTrue(result['complete'], result['gaps'])
        self.assertEqual(next(row['value'] for row in rows if row['event'] == 'password'), '~Bfixture')

    def test_bmc_key_authentication_uses_selected_key_and_independent_os_password(self):
        key = self.root / 'id_rsa'
        CLIENT_KEY.write_private_key_file(str(key))
        result, rows = self.collect(values={**VALUES, 'OPENUBMC_SSH_PASSWORD': '',
                                          'OPENUBMC_SSH_IDENTITY_FILE': str(key)})
        self.assertTrue(result['complete'], result['gaps'])
        self.assertTrue(any(row['event'] == 'bmc_key_auth' for row in rows))
        self.assertFalse(any(row['event'] == 'bmc_auth' for row in rows))
        self.assertEqual(next(row['value'] for row in rows if row['event'] == 'password'),
                         VALUES['OPENUBMC_OS_SSH_PASSWORD'])

    def test_cancellation_after_login_still_logs_out_and_closes_owned_channel(self):
        class Cancelled(Exception):
            pass
        armed = False
        sessions = []
        class Session(console.SolSession):
            def command(self, command):
                nonlocal armed
                result = super().command(command)
                armed = True
                return result
        def check():
            if armed:
                raise Cancelled()
        def factory(*args, **kwargs):
            session = Session(*args, **kwargs)
            sessions.append(session)
            return session
        with self.transport() as server:
            with self.assertRaises(Cancelled):
                console.collect_os_console(['identity', 'network'], '127.0.0.1', VALUES,
                    deadline=time.monotonic() + 5, ssh_port=server.port,
                    check_cancelled=check, session_factory=factory)
        self.assertTrue(sessions[0].channel.closed)
        self.assertIsNone(sessions[0].client.get_transport())
        self.assertTrue(any(row['event'] == 'logout' for row in server.rows))
        self.assertTrue(any(row['event'] == 'detach' for row in server.rows))

    def test_missing_accounts_dependency_and_invalid_input_do_not_connect(self):
        for values, gap in (({}, 'bmc_ssh_credentials_missing'),
                            ({**VALUES, 'OPENUBMC_OS_SSH_PASSWORD': ''}, 'os_password_credentials_missing'),
                            ({**VALUES, 'OPENUBMC_OS_SSH_USER': 'root\rreboot'}, 'console_credentials_invalid'),
                            ({**VALUES, 'OPENUBMC_OS_SSH_PASSWORD': '\x1b(secret'}, 'console_credentials_invalid')):
            with self.subTest(gap=gap), patch.object(console, 'SolSession') as factory:
                result = console.collect_os_console(['identity'], '127.0.0.1', values,
                    deadline=time.monotonic() + 5, session_factory=factory)
                factory.assert_not_called()
                self.assertEqual(result['gaps'], [gap])
        with patch.dict(sys.modules, {'paramiko': None}):
            result = console.collect_os_console(['identity'], '127.0.0.1', VALUES,
                deadline=time.monotonic() + 5)
        self.assertEqual(result['gaps'], ['ssh_transport_unavailable'])

    def test_connect_timeout_closes_resources_within_overall_deadline(self):
        closed = threading.Event()
        def stalled_connect(*args, **kwargs):
            closed.wait(2)
            raise OSError('fixture transport closed')
        started = time.monotonic()
        with patch.object(console.socket, 'create_connection') as create, patch.object(paramiko, 'SSHClient') as factory:
            create.return_value.close.side_effect = closed.set
            factory.return_value.connect.side_effect = stalled_connect
            result = console.collect_os_console(['identity'], '127.0.0.1', VALUES,
                deadline=time.monotonic() + 2.5)
            factory.return_value.close.assert_called()
            create.return_value.close.assert_called()
        self.assertFalse(result['complete'])
        self.assertEqual(result['gaps'], ['deadline_exceeded'])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_help_and_session_tables_fail_closed_on_ambiguous_or_malformed_data(self):
        for output in ('1 SYS COM\n2 SYS COM\n0 Shared mode', '1 BMC COM\n0 Shared mode',
                       '2 SYS COM\n1 Private mode', '2;reboot SYS COM\n0 Shared mode'):
            with self.subTest(output=output), self.assertRaises(console.CollectionGap):
                console._sys_com_option(output)
        for output in ('', '1 N/A N/A N/A N/A N/A\n2 N/A N/A N/A N/A N/A',
                       'Index Type Mode LoginTime IP Name\n1 N/A N/A N/A N/A N/A'):
            with self.subTest(output=output), self.assertRaises(console.CollectionGap):
                console._require_idle_sessions(output)

    def test_network_parser_retains_gaps_for_unsupported_or_malformed_output(self):
        for output in ('{}\n[]', '[]\n[]\ntrailing', '[]', '[{"ifname":"x","addr_info":{}}]\n[]',
                       '[]\n[[]]', '[]\n[{"dst":"instruction"}]'):
            with self.subTest(output=output), self.assertRaises(console.CollectionGap):
                console._network(output)

    def test_runtime_observe_without_os_ip_or_linux_bmc_shell_persists_and_seeds_diagnosis(self):
        import os
        import target_runtime_mcp
        runtime = target_runtime_mcp._load_runtime_module()
        config = self.root / 'credentials.json'
        config.write_text(json.dumps({'schema_version': 1, 'credentials': {
            'bmc': {'user': VALUES['OPENUBMC_SSH_USER'], 'password': VALUES['OPENUBMC_SSH_PASSWORD']},
            'os': {'user': VALUES['OPENUBMC_OS_SSH_USER'], 'password': VALUES['OPENUBMC_OS_SSH_PASSWORD']}},
            'defaults': {'bmc': {'ssh': 'bmc'}, 'os': {'ssh': 'os'}}}))
        config.chmod(0o600)
        blobs = runtime.InMemoryBlobRepository()
        with self.transport() as server, patch.dict(os.environ, {'OPENUBMC_CREDENTIALS_CONFIG': str(config)}), patch.object(
                target_runtime_mcp.workflow_remote, 'build_typed_debug_tool_runner', side_effect=AssertionError('no Linux SSH preflight')):
            backend = target_runtime_mcp.DebugMcpBackend()
            service = runtime.RuntimeMcpService(runtime.OrchestratedMcpBackend(
                {'debug_collect': backend, 'debug_run': backend}), blob_repository=blobs)
            self.addCleanup(service.close)
            receipt = service.call_exposed_tool('observe', {'target': '127.0.0.1', 'ssh_port': server.port, 'deadline': 5,
                'selectors': [{'id': 'host', 'kind': 'os_console', 'names': ['identity', 'network']}]},
                task_id='sol-e2e', operation_id='observe')
            self.assertEqual(receipt['status'], 'complete', receipt)
            self.assertEqual(receipt['results']['host']['transport'], 'bmc-ssh-sol')
            source = blobs.read(receipt['observation_ref']['handle'].removeprefix('blob://'))
            self.assertNotIn(b'bmc-fixture-private', source)
            self.assertNotIn(b'os-fixture-private', source)
            self.assertIn(b'192.0.2.20/24', source)
            turn = service.call_exposed_tool('execute', {'kind': 'start', 'target': '127.0.0.1',
                'intent': 'diagnosis-only', 'observation_ref': receipt['observation_ref']},
                task_id='sol-e2e', operation_id='start')
            self.assertEqual(turn['gate']['name'], 'diagnosis.acceptance', turn)
            evidence = turn['diagnostic_receipt']['results']
            self.assertTrue(any(row['kind'] == 'os-console' and row['status'] == 'available' for row in evidence))
            previous_attempts = sum(row['event'] == 'password' for row in server.rows)
            server.mode = 'reject'
            rejected = service.call_exposed_tool('observe', {'target': '127.0.0.1', 'ssh_port': server.port, 'deadline': 5,
                'selectors': [{'id': 'host', 'kind': 'os_console', 'names': ['identity']}]},
                task_id='sol-e2e', operation_id='reject')
            self.assertNotEqual(rejected['status'], 'complete')
            self.assertEqual(sum(row['event'] == 'password' for row in server.rows) - previous_attempts, 1)

    def test_runtime_does_not_retry_rejected_console_authentication_for_assurance(self):
        import target_runtime_mcp
        from types import SimpleNamespace
        AssuranceUnavailable = target_runtime_mcp._load_runtime_module().AssuranceUnavailable
        backend = target_runtime_mcp.DebugMcpBackend()
        arguments = {'ip': '127.0.0.1', 'mdb_queries': [], 'capability_names': [], 'deadline': 5,
                     'selectors': [{'id': 'host', 'kind': 'os_console', 'names': ['identity']}],
                     '_credential_values': VALUES}
        context = SimpleNamespace(raise_if_stopped=lambda: None, remaining=lambda: 5)
        with self.transport('reject') as server:
            arguments['ssh_port'] = server.port
            result = backend.observe_query(None, arguments, context)
            with self.assertRaises(AssuranceUnavailable):
                backend.observe_query(None, {**arguments, 'assured': True, 'prior_observation': result}, context)
        self.assertEqual(sum(row['event'] == 'password' for row in server.rows), 1)


if __name__ == '__main__':
    unittest.main()
