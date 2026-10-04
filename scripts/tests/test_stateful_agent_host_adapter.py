from __future__ import annotations

import os
import http.server
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
import urllib.request

from scripts import stateful_agent_host_adapter as adapter


class StatefulAgentHostAdapterTests(unittest.TestCase):
    def test_resume_request_initializes_the_bound_fake_runtime(self) -> None:
        from scripts import stateful_agent_evaluation as evaluation
        manifest = evaluation.load_manifest()
        case = next(item for item in manifest['scenarios'] if item['id'] == 'diagnosis-resume')
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            request = {
                'schema': f'{evaluation.SCHEMA}/adapter-request',
                'scenario_id': case['id'], 'scenario_version': case['version'],
                'trial': 1, 'task_id': 'eval-diagnosis-resume-v1-t1',
                'prompt': case['prompt'], 'prompt_digest': evaluation._digest(case['prompt']),
                'fixture_target': manifest['fixture_target'], 'backend': 'runtime-fake',
                'execution_mode': 'controlled-scripted-responses', 'model_invoked': False,
                'output_directory': str(directory),
                'source_commit': subprocess.check_output(
                    ['git', 'rev-parse', 'HEAD'], cwd=adapter.ROOT, text=True).strip(),
                'plan_digest': 'sha256:' + 'a' * 64,
                'schedule_digest': 'sha256:' + 'b' * 64,
            }
            path = directory / 'request.json'
            path.write_text(json.dumps(request), encoding='utf-8')
            response = subprocess.run(
                [sys.executable, '-B', str(Path(adapter.__file__)), 'serve', str(path)],
                input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                  'params': {'protocolVersion': '2025-06-18',
                                             'capabilities': {},
                                             'clientInfo': {'name': 'fixture', 'version': '1'}}}) + '\n',
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(response.returncode, 0, response.stderr)
            self.assertIn('result', json.loads(response.stdout))

    def test_resume_reuses_rpc_id_after_mcp_restart_without_repeating_backend(self) -> None:
        from scripts import stateful_agent_evaluation as evaluation
        manifest = evaluation.load_manifest()
        case = next(item for item in manifest['scenarios'] if item['id'] == 'diagnosis-resume')
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            request = {
                'schema': f'{evaluation.SCHEMA}/adapter-request',
                'scenario_id': case['id'], 'scenario_version': 1, 'trial': 1,
                'task_id': 'eval-diagnosis-resume-v1-t1', 'prompt': case['prompt'],
                'prompt_digest': evaluation._digest(case['prompt']),
                'fixture_target': manifest['fixture_target'], 'backend': 'runtime-fake',
                'execution_mode': 'controlled-scripted-responses', 'model_invoked': False,
                'output_directory': str(directory),
                'source_commit': subprocess.check_output(
                    ['git', 'rev-parse', 'HEAD'], cwd=adapter.ROOT, text=True).strip(),
                'plan_digest': 'sha256:' + 'a' * 64, 'schedule_digest': 'sha256:' + 'b' * 64,
            }
            path = directory / 'request.json'
            path.write_text(json.dumps(request), encoding='utf-8')

            def invoke(arguments):
                messages = [
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                     'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                                'clientInfo': {'name': 'fixture', 'version': '1'}}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                     'params': {'name': 'execute', 'arguments': arguments,
                                '_meta': {'threadId': '12345678-1234-1234-1234-123456789abc'}}},
                ]
                result = subprocess.run(
                    [sys.executable, '-B', str(Path(adapter.__file__)), 'serve', str(path)],
                    input=''.join(json.dumps(m) + '\n' for m in messages),
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout.splitlines()[-1])['result']['structuredContent']

            initial = invoke({'kind': 'start', 'target': '192.0.2.10', 'intent': 'diagnosis-only'})
            self.assertEqual(initial['state'], 'waiting_response')
            for _ in range(2):
                resumed = invoke({'kind': 'resume', 'run_id': initial['run_id']})
                self.assertEqual(resumed['state'], 'waiting_response', resumed)
                self.assertEqual(resumed['run_id'], initial['run_id'])
                self.assertEqual(resumed['gate']['gate_id'], initial['gate']['gate_id'])
            conflict = invoke({'kind': 'start', 'target': '192.0.2.11', 'intent': 'diagnosis-only'})
            self.assertEqual(conflict['state'], 'failed')
            self.assertEqual(conflict['error']['code'], 'CommandConflict')
            self.assertEqual(len((directory / 'backend-invocations.jsonl').read_text().splitlines()), 1)


    def test_known_read_only_mcp_cancellation_blocks_before_credential_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            binary = Path(raw) / "codex"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o700)
            with (mock.patch.object(adapter, "_request", return_value={
                    "client_version": "codex-cli/0.144.6"}),
                  mock.patch.object(adapter.shutil, "which", return_value=str(binary)),
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
            self.assertIn('mcp_servers.runtime_fake.enabled_tools=["execute"]', command)
            self.assertIn('mcp_servers.runtime_fake.tools.execute.approval_mode="approve"', command)
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
