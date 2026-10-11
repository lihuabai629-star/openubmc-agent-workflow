from __future__ import annotations

import os
import json
import http.server
from pathlib import Path
import tempfile
import threading
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
import urllib.request

from scripts import stateful_agent_host_adapter as adapter


class StatefulAgentHostAdapterTests(unittest.TestCase):
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
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertIn('mcp_servers.runtime_fake.enabled_tools=["execute"]', command)
            self.assertIn('mcp_servers.runtime_fake.tools.execute.approval_mode="approve"', command)
            self.assertNotIn("read-only", command)
            self.assertEqual((Path(raw) / 'host-instructions.md').read_text(), adapter.HOST_INSTRUCTIONS)
            self.assertTrue(any(item.startswith('model_instructions_file=') for item in command))

    def test_selected_http_provider_does_not_authorize_a_different_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            adapter._ProviderRelay("http://example.com/v1", "fixture-key",
                                   configured_url="http://other.example/v1")
        selected = adapter._ProviderRelay("http://example.com/v1", "fixture-key",
                                          configured_url="http://example.com/v1")
        selected.server_close()

    def test_native_rollout_rejects_reduced_actual_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            session = '11111111-1111-1111-1111-111111111111'
            sessions = home / '.codex/sessions'
            sessions.mkdir(parents=True)
            source = sessions / (session + '.jsonl')
            events = [{'type': 'session_meta', 'payload': {'id': session, 'base_instructions': {'text': adapter.HOST_INSTRUCTIONS}}},
                      {'type': 'turn_context', 'payload': {'sandbox_policy': {'type': 'read-only'},
                                                          'approval_policy': 'never'}}]
            source.write_text('\n'.join(map(json.dumps, events))+'\n')
            with self.assertRaisesRegex(ValueError, 'requested full access'):
                adapter._native_rollout(home, session, home / 'rollout.jsonl')
            events[1]['payload']['sandbox_policy']['type'] = 'danger-full-access'
            source.write_text('\n'.join(map(json.dumps, events))+'\n')
            adapter._native_rollout(home, session, home / 'rollout.jsonl')
            self.assertEqual(json.loads((home/'permissions.json').read_text())['verified_turns'], 1)

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

    def test_outer_timeout_reaps_owned_native_group_and_preserves_other_process(self) -> None:
        from scripts import stateful_agent_evaluation as evaluation
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            worker=root/'worker.py'
            worker.write_text('import os,subprocess,sys,time\n'
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"])\n'
                'open(sys.argv[1],"w").write(str(os.getpid())+"\\n"+str(child.pid))\n'
                'time.sleep(120)\n')
            launcher=root/'adapter'
            launcher.write_text('#!'+sys.executable+'\nimport signal,sys,os\n'
                'from pathlib import Path\nsys.path.insert(0,'+repr(str(adapter.ROOT))+')\n'
                'from scripts import stateful_agent_host_adapter as a\n'
                'signal.signal(signal.SIGTERM,a._cancel_adapter)\n'
                'p=Path(sys.argv[1]).parent\n'
                'a._invoke([sys.executable,'+repr(str(worker))+',str(p/"owned-pids")],'
                'dict(os.environ),p/"native.jsonl",credential="fixture-key",timeout=120)\n')
            launcher.chmod(0o700)
            manifest=evaluation.load_manifest()
            plan=evaluation.build_plan(manifest,model='fixture',client_version='fixture',source_commit='a'*40)
            other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])
            try:
                with mock.patch.object(evaluation,'_git_commit',return_value='a'*40):
                    result=evaluation.run_one_trial(manifest=manifest,plan=plan,adapter=launcher,
                        output_root=root/'trials',timeout_seconds=2,scenario_id='diagnosis-complete',trial=1)
                self.assertEqual(result['attempts'][0]['adapter_exit_code'],124)
                self.assertIsNone(other.poll())
                pids=[int(v) for v in (root/'trials/eval-diagnosis-complete-v1-t1/owned-pids').read_text().splitlines()]
                for pid in pids:
                    stat=Path(f'/proc/{pid}/stat')
                    self.assertTrue(not stat.exists() or stat.read_text().split()[2]=='Z', f'owned child {pid} remains running')
            finally:
                other.kill();other.wait()


if __name__ == "__main__":
    unittest.main()
