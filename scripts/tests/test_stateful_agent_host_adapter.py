from __future__ import annotations

import os
import http.server
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
import urllib.request

from scripts import stateful_agent_host_adapter as adapter


class StatefulAgentHostAdapterTests(unittest.TestCase):
    def test_known_read_only_mcp_cancellation_blocks_before_credential_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with (mock.patch.object(adapter, "_request", return_value={
                    "client_version": "codex-cli/0.144.6"}),
                  mock.patch.object(adapter.shutil, "which", return_value="/usr/bin/codex"),
                  mock.patch.object(adapter.subprocess, "check_output",
                                    return_value="codex-cli 0.144.6"),
                  mock.patch.object(adapter, "_credential",
                                    side_effect=AssertionError("credential should stay unused"))):
                with self.assertRaisesRegex(ValueError, "cancels fake Runtime MCP execute"):
                    adapter.run(Path(raw) / "request.json")

    def test_loopback_relay_adds_real_key_only_on_upstream_hop(self) -> None:
        class Upstream(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.server.observed = (
                    self.path, self.headers.get("Authorization"),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"event: response.completed\ndata: {}\n\n")

        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        relay = adapter._ProviderRelay(
            f"http://127.0.0.1:{upstream.server_port}/v1", "fixture-upstream-key")
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (upstream, relay)]
        for thread in threads:
            thread.start()
        try:
            for authorization in (None, "Bearer wrong-local-marker"):
                headers = {"Content-Type": "application/json"}
                if authorization:
                    headers["Authorization"] = authorization
                request = urllib.request.Request(
                    f"http://127.0.0.1:{relay.server_port}/v1/responses",
                    data=b"{}", headers=headers,
                )
                with self.assertRaises(HTTPError) as rejected:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(rejected.exception.code, 403)
                self.assertFalse(hasattr(upstream, "observed"))
            request = urllib.request.Request(
                f"http://127.0.0.1:{relay.server_port}/v1/responses", data=b"{}",
                headers={"Authorization": "Bearer " + relay.marker,
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"response.completed", response.read())
            self.assertEqual(upstream.observed, (
                "/v1/responses", "Bearer fixture-upstream-key", b"{}",
            ))
        finally:
            for server in (relay, upstream):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)

    def test_relay_rejects_remote_plain_http_before_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            adapter._ProviderRelay("http://example.com/v1", "fixture-upstream-key")
        secure = adapter._ProviderRelay("https://example.com/v1", "fixture-upstream-key")
        try:
            self.assertGreater(len(secure.marker), 32)
        finally:
            secure.server_close()

    def test_native_child_receives_only_disposable_relay_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.dict(os.environ, {
                "OPENUBMC_EVAL_API_KEY": "actual-secret-stays-in-adapter",
                "CLI_PROXY_API_KEY": "another-parent-secret",
            }):
                command, env = adapter._cli(
                    "http://127.0.0.1:12345/v1",
                    {"model": "fixture", "reasoning_effort": "low"},
                    Path(raw), "/usr/bin/codex", "random-trial-marker",
                )
            self.assertEqual(env["OPENUBMC_EVAL_API_KEY"], "random-trial-marker")
            self.assertNotIn("CLI_PROXY_API_KEY", env)
            self.assertIn("features.shell_tool=false", command)
            self.assertIn("features.shell_snapshot=false", command)
            self.assertIn("read-only", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_secret_artifact_is_removed_before_attempt_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            leaked = root / "shell_snapshot.sh"
            retained = root / "request.json"
            leaked.write_text("export TOKEN=fake-evaluation-key\n")
            retained.write_text("{}\n")
            with self.assertRaisesRegex(ValueError, "persisted an evaluation credential"):
                adapter._scrub_secret_artifacts(root, "fake-evaluation-key")
            self.assertFalse(leaked.exists())
            self.assertTrue(retained.exists())


if __name__ == "__main__":
    unittest.main()
