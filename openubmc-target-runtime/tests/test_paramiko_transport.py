"""Local SSH protocol tests for the Windows-capable Runtime transport seam."""
from __future__ import annotations

from pathlib import Path
import os
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import paramiko
except ImportError:
    paramiko = None


@unittest.skipUnless(paramiko is not None, "Paramiko is required")
class ParamikoTransportTests(unittest.TestCase):
    class Server(paramiko.ServerInterface if paramiko else object):
        def __init__(self, fixture):
            self.fixture = fixture
            self.command = threading.Event()
            self.received_command = b""
            self.command_channel = None

        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ("fixture", "private-fixture-password") else paramiko.AUTH_FAILED

        def check_auth_publickey(self, username, key):
            expected = self.fixture.client_key
            return (paramiko.AUTH_SUCCESSFUL if expected is not None and username == "fixture"
                    and key.get_fingerprint() == expected.get_fingerprint() else paramiko.AUTH_FAILED)

        def get_allowed_auths(self, username):
            return "password,publickey"

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            self.received_command = command
            self.command_channel = channel
            self.command.set()
            return (command == b"echo marker" or command.startswith(b"umask 077 && cat > ")
                    or command.startswith(b"scp -f -- "))

    class ReadOnlySftp(paramiko.SFTPServerInterface if paramiko else object):
        def __init__(self, server, *args, file_path=None, **kwargs):
            super().__init__(server, *args, **kwargs)
            self.file_path = Path(file_path)

        def stat(self, path):
            if path != "/fixture":
                return paramiko.SFTP_NO_SUCH_FILE
            return paramiko.SFTPAttributes.from_stat(self.file_path.stat())

        def open(self, path, flags, attr):
            if path != "/fixture" or flags & os.O_WRONLY:
                return paramiko.SFTP_PERMISSION_DENIED
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = self.file_path.open("rb")
            handle.stat = lambda: paramiko.SFTPAttributes.from_stat(self.file_path.stat())
            return handle

    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.key = paramiko.RSAKey.generate(2048)
        self.client_key = None
        self.mode = "command"
        self.uploaded = b""
        self.fixture_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture_dir.cleanup)
        self.sftp_file = Path(self.fixture_dir.name)/"download"
        self.sftp_file.write_bytes(b"fixture-download")
        self.errors = []

        def serve():
            try:
                client, _ = self.listener.accept()
                with client:
                    transport = paramiko.Transport(client)
                    transport.add_server_key(self.key)
                    server = self.Server(self)
                    if self.mode == "sftp":
                        transport.set_subsystem_handler("sftp", paramiko.SFTPServer,
                                                        self.ReadOnlySftp, file_path=self.sftp_file)
                    transport.start_server(server=server)
                    while transport.is_active():
                        channel = transport.accept(2)
                        if not channel:
                            break
                        if self.mode == "sftp":
                            while transport.is_active():
                                threading.Event().wait(0.05)
                            break
                        if not server.command.wait(0.2) or server.command_channel is not channel:
                            channel.close()
                            continue
                        if server.received_command == b"echo marker":
                            channel.sendall(b"marker\n")
                        elif server.received_command.startswith(b"scp -f -- "):
                            if channel.recv(1) != b"\0":
                                raise AssertionError("SCP client did not start the transfer")
                            payload = b"fixture-download"
                            channel.sendall(b"C0644 " + str(len(payload)).encode() + b" fixture\n")
                            if channel.recv(1) != b"\0":
                                raise AssertionError("SCP client did not accept the file")
                            channel.sendall(payload + b"\0")
                            if channel.recv(1) != b"\0":
                                raise AssertionError("SCP client did not acknowledge the file")
                        else:
                            chunks = []
                            while data := channel.recv(65536):
                                chunks.append(data)
                            self.uploaded = b"".join(chunks)
                        channel.send_exit_status(0)
                        channel.shutdown_write()
                        channel.close()
                        break
                    transport.close()
            except ConnectionError:
                pass  # Expected when host-key verification closes the test socket.
            except Exception as exc:
                self.errors.append(exc)

        self.worker = threading.Thread(target=serve, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.listener.close()
        self.worker.join(timeout=6)
        self.assertFalse(self.errors, self.errors)

    def test_password_authentication_uses_a_pinned_host_key_and_keeps_secret_local(self):
        from openubmc_target_runtime.contracts import TargetPolicy, TargetSpec
        from openubmc_target_runtime.openssh import ParamikoSshTransport
        from openubmc_target_runtime.runtime import ResolvedSshCredentials

        with tempfile.TemporaryDirectory() as raw:
            known_hosts = Path(raw) / "known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.key.get_name()} {self.key.get_base64()}\n")
            target = TargetSpec(host="127.0.0.1", ssh_port=self.port,
                                credential_selector_fingerprint="0" * 64,
                                policy=TargetPolicy(ssh_host_key_policy="strict"))
            credentials = ResolvedSshCredentials(user="fixture", password="private-fixture-password", port=self.port)
            adapter = ParamikoSshTransport(host_key_policy="strict", known_hosts_file=str(known_hosts))
            master = adapter.open_master(target=target, credentials=credentials)
            try:
                self.assertTrue(adapter.check_master(master))
                result = adapter.run_channel(master, "echo marker", timeout=3)
                self.assertEqual((result.returncode, result.stdout), (0, "marker\n"))
                self.assertNotIn(credentials.password, repr(master) + repr(result))
            finally:
                adapter.close_master(master)
            self.assertFalse(adapter.check_master(master))

    def test_key_authentication_and_upload_use_in_process_channels(self):
        from openubmc_target_runtime.contracts import TargetPolicy, TargetSpec
        from openubmc_target_runtime.openssh import ParamikoSshTransport
        from openubmc_target_runtime.runtime import ResolvedSshCredentials

        with tempfile.TemporaryDirectory() as raw:
            self.client_key = paramiko.RSAKey.generate(2048)
            key_path = Path(raw)/"client-key"
            self.client_key.write_private_key_file(str(key_path))
            payload = Path(raw)/"payload"
            payload.write_bytes(b"fixture-upload")
            known_hosts = Path(raw)/"known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.key.get_name()} {self.key.get_base64()}\n")
            target = TargetSpec(host="127.0.0.1", ssh_port=self.port,
                                credential_selector_fingerprint="0" * 64,
                                policy=TargetPolicy(ssh_host_key_policy="strict"))
            adapter = ParamikoSshTransport(host_key_policy="strict", known_hosts_file=str(known_hosts))
            master = adapter.open_master(target=target, credentials=ResolvedSshCredentials(
                user="fixture", identity_file=str(key_path), port=self.port))
            try:
                result = adapter.upload_file(master, str(payload), "/tmp/fixture", timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.uploaded, b"fixture-upload")
            finally:
                adapter.close_master(master)

    def test_sftp_download_preserves_file_bytes(self):
        from openubmc_target_runtime.contracts import TargetPolicy, TargetSpec
        from openubmc_target_runtime.openssh import ParamikoSshTransport
        from openubmc_target_runtime.runtime import ResolvedSshCredentials

        self.mode = "sftp"
        with tempfile.TemporaryDirectory() as raw:
            known_hosts = Path(raw)/"known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.key.get_name()} {self.key.get_base64()}\n")
            target = TargetSpec(host="127.0.0.1", ssh_port=self.port,
                                credential_selector_fingerprint="0" * 64,
                                policy=TargetPolicy(ssh_host_key_policy="strict"))
            adapter = ParamikoSshTransport(host_key_policy="strict", known_hosts_file=str(known_hosts))
            master = adapter.open_master(target=target, credentials=ResolvedSshCredentials(
                user="fixture", password="private-fixture-password", port=self.port))
            try:
                destination = Path(raw)/"downloaded"
                result = adapter.download_file(master, "/fixture", str(destination), timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(destination.read_bytes(), b"fixture-download")
            finally:
                adapter.close_master(master)

    def test_scp_fallback_downloads_when_sftp_is_unavailable(self):
        from openubmc_target_runtime.contracts import TargetPolicy, TargetSpec
        from openubmc_target_runtime.openssh import ParamikoSshTransport
        from openubmc_target_runtime.runtime import ResolvedSshCredentials

        self.mode = "scp"
        with tempfile.TemporaryDirectory() as raw:
            known_hosts = Path(raw)/"known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.key.get_name()} {self.key.get_base64()}\n")
            target = TargetSpec(host="127.0.0.1", ssh_port=self.port,
                                credential_selector_fingerprint="0" * 64,
                                policy=TargetPolicy(ssh_host_key_policy="strict"))
            adapter = ParamikoSshTransport(host_key_policy="strict", known_hosts_file=str(known_hosts))
            master = adapter.open_master(target=target, credentials=ResolvedSshCredentials(
                user="fixture", password="private-fixture-password", port=self.port))
            try:
                destination = Path(raw)/"downloaded"
                result = adapter.download_file(master, "/fixture", str(destination), timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(destination.read_bytes(), b"fixture-download")
            finally:
                adapter.close_master(master)

    def test_rejects_an_unpinned_host_key_without_exposing_credentials(self):
        from openubmc_target_runtime.contracts import TargetPolicy, TargetSpec
        from openubmc_target_runtime.paramiko_transport import ParamikoOpenError, ParamikoSshTransport
        from openubmc_target_runtime.runtime import ResolvedSshCredentials

        with tempfile.TemporaryDirectory() as raw:
            other_key = paramiko.RSAKey.generate(2048)
            known_hosts = Path(raw)/"known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:{self.port} {other_key.get_name()} {other_key.get_base64()}\n")
            target = TargetSpec(host="127.0.0.1", ssh_port=self.port,
                                credential_selector_fingerprint="0" * 64,
                                policy=TargetPolicy(ssh_host_key_policy="strict"))
            credentials = ResolvedSshCredentials(user="fixture", password="private-fixture-password", port=self.port)
            adapter = ParamikoSshTransport(host_key_policy="strict", known_hosts_file=str(known_hosts))
            with self.assertRaises(ParamikoOpenError) as caught:
                adapter.open_master(target=target, credentials=credentials)
            self.assertIn("host key verification failed", caught.exception.completed.stderr)
            self.assertNotIn(credentials.password, repr(caught.exception))

    def test_rejects_wrong_password_without_exposing_it(self):
        from openubmc_target_runtime.contracts import TargetPolicy, TargetSpec
        from openubmc_target_runtime.paramiko_transport import ParamikoOpenError, ParamikoSshTransport
        from openubmc_target_runtime.runtime import ResolvedSshCredentials

        with tempfile.TemporaryDirectory() as raw:
            known_hosts = Path(raw)/"known_hosts"
            known_hosts.write_text(f"[127.0.0.1]:{self.port} {self.key.get_name()} {self.key.get_base64()}\n")
            target = TargetSpec(host="127.0.0.1", ssh_port=self.port,
                                credential_selector_fingerprint="0" * 64,
                                policy=TargetPolicy(ssh_host_key_policy="strict"))
            credentials = ResolvedSshCredentials(user="fixture", password="wrong-fixture-password", port=self.port)
            adapter = ParamikoSshTransport(host_key_policy="strict", known_hosts_file=str(known_hosts))
            with self.assertRaises(ParamikoOpenError) as caught:
                adapter.open_master(target=target, credentials=credentials)
            self.assertIn("authentication failed", caught.exception.completed.stderr)
            self.assertNotIn(credentials.password, repr(caught.exception))


if __name__ == "__main__":
    unittest.main()
