"""Local SSH protocol tests for the Windows-capable Runtime transport seam."""
from __future__ import annotations

from pathlib import Path
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
        def __init__(self):
            self.command = threading.Event()

        def check_auth_password(self, username, password):
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ("fixture", "private-fixture-password") else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return "password"

        def check_channel_request(self, kind, channel_id):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel, command):
            self.command.set()
            return command == b"echo marker"

    def setUp(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.key = paramiko.RSAKey.generate(2048)
        self.errors = []

        def serve():
            try:
                client, _ = self.listener.accept()
                with client:
                    transport = paramiko.Transport(client)
                    transport.add_server_key(self.key)
                    server = self.Server()
                    transport.start_server(server=server)
                    channel = transport.accept(5)
                    if channel and server.command.wait(5):
                        channel.sendall(b"marker\n")
                        channel.send_exit_status(0)
                        channel.shutdown_write()
                        channel.close()
                    transport.close()
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


if __name__ == "__main__":
    unittest.main()
