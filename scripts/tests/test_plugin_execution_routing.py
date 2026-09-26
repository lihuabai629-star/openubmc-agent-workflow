"""Installed bootstrap routing tests with controlled MCP and WSL adapters."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

if __package__:
    from .plugin_fixture import package_fixture
else:
    from plugin_fixture import package_fixture


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class InstalledExecutionRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.plugin = package_fixture(cls.root)
        cls.control = cls.root / "routing-control.json"
        cls.launcher = cls.root / "fake-backend"
        cls.launcher.write_text(
            """#!/usr/bin/env python3
import json, pathlib, sys
control = json.loads(pathlib.Path(__CONTROL__).read_text())
args = sys.argv[1:]
if args == ['--list', '--quiet']:
    print('Ubuntu-24.04')
    raise SystemExit(0)
if 'wslpath' in args:
    print('/mnt/c/plugin')
    raise SystemExit(0)
if '-c' in args or 'cleanup-retired' in args:
    print('{}')
    raise SystemExit(0)
if 'doctor' in args:
    health = control['health']
    print(json.dumps({
        'ok': health, 'version': 'fixture', 'source_commit': 'a' * 40,
        'package_integrity': True,
        'capabilities': {'runtime': {'dependencies_ready': True, 'startup_ready': True},
                         'kb': {'dependencies_ready': True, 'startup_ready': True}},
        'mcp_health': {'runtime': {'ok': health, 'tools': control.get('tools', ['observe', 'execute'])},
                       'kb': {'ok': True}},
        'codex_configuration': {'ready': True, 'changes': {'mcp_servers': [], 'skills': []}},
    }))
    raise SystemExit(0)
if args[-1] != 'runtime':
    raise SystemExit(43)
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'serverInfo': {'name': 'fixture-runtime'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'observe'}, {'name': 'execute'}]}
    elif method == 'tools/call':
        with pathlib.Path(control['call_log']).open('a') as stream:
            stream.write(request['params']['name'] + '\\n')
        result = {'content': [{'type': 'text', 'text': 'fixture'}],
                  'structuredContent': {'status': 'fixture'}, 'isError': False}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
""".replace("__CONTROL__", repr(str(cls.control))),
            encoding="utf-8",
        )
        cls.launcher.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def invoke(
        self, *, windows: bool, healthy: bool, doctor_tools: list[str] | None = None,
        arguments: dict | None = None, raw_input: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        call_log = self.root / f"calls-{'windows' if windows else 'linux'}-{'healthy' if healthy else 'unhealthy'}"
        call_log.unlink(missing_ok=True)
        self.control.write_text(json.dumps({"health": healthy, "call_log": str(call_log),
                                            "tools": (["observe", "execute"] if doctor_tools is None
                                                      else doctor_tools)}), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({
            "HOME": str(self.root / "home"),
            "LOCALAPPDATA": str(self.root / "local-app-data"),
            "OPENUBMC_PLUGIN_HOST_PLATFORM": "win32" if windows else "linux",
            "OPENUBMC_PLUGIN_WSL_EXE": str(self.launcher),
            "OPENUBMC_PLUGIN_PYTHON": str(self.launcher),
            "USERPROFILE": r"C:\Users\fixture",
            "CODEX_HOME": r"C:\Users\fixture\.codex",
        })
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "execute", "arguments": arguments if arguments is not None else {
                 "kind": "start", "intent": "diagnosis-only", "target": "fixture.invalid"}}},
        ]
        if not healthy:
            messages.insert(2, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "openubmc_setup_status", "arguments": {}}})
        result = subprocess.run(
            ["node", str(self.plugin / "scripts/openubmc-mcp-bootstrap.js"), "runtime"],
            input=(raw_input if raw_input is not None
                   else "".join(json.dumps(message) + "\n" for message in messages)),
            env=environment, capture_output=True, text=True, timeout=20,
        )
        return result, call_log

    def test_installed_linux_path_receipts_before_structured_call(self) -> None:
        result, call_log = self.invoke(windows=False, healthy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {item["id"]: item for item in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(replies[3]["result"]["structuredContent"], {"status": "fixture"})
        self.assertEqual(call_log.read_text(), "execute\n")
        audit = [line.removeprefix("openubmc-routing ") for line in result.stderr.splitlines()
                 if line.startswith("openubmc-routing ")]
        self.assertEqual(len(audit), 1)
        receipt = json.loads(audit[0])
        self.assertEqual(receipt["path"], "structured-runtime-mcp")
        self.assertEqual(receipt["operation"], "diagnose")
        self.assertEqual(receipt["tool"], "execute")
        self.assertEqual(receipt["execution_host"], "linux")
        self.assertTrue(receipt["requested_scope_mac"].startswith("hmac-sha256:"))
        self.assertNotIn("fixture.invalid", result.stderr)

    def test_installed_windows_wsl_path_uses_selected_distribution(self) -> None:
        result, call_log = self.invoke(windows=True, healthy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(call_log.read_text(), "execute\n")
        audit = next(json.loads(line.removeprefix("openubmc-routing "))
                     for line in result.stderr.splitlines() if line.startswith("openubmc-routing "))
        self.assertEqual(audit["client_environment"], "windows")
        self.assertEqual(audit["execution_host"], "wsl")
        self.assertEqual(audit["selected_wsl"], "Ubuntu-24.04")
        self.assertEqual(audit["protocol"]["probe"], "initialize/tools/list")

    def test_unhealthy_protocol_blocks_runtime_call_before_backend(self) -> None:
        result, call_log = self.invoke(windows=False, healthy=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {item["id"]: item for item in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "openubmc-runtime-setup")
        status = replies[4]["result"]["structuredContent"]
        self.assertEqual(status["reason"], "mcp_protocol_unhealthy")
        self.assertFalse(call_log.exists())
        self.assertNotIn("openubmc-routing ", result.stderr)

    def test_route_policy_rejects_missing_tool_before_backend_call(self) -> None:
        result, call_log = self.invoke(windows=False, healthy=True, doctor_tools=["observe"])
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = {item["id"]: item for item in map(json.loads, result.stdout.splitlines())}
        self.assertEqual(replies[3]["error"]["message"], "mcp_protocol_unhealthy")
        self.assertFalse(call_log.exists())

    def test_low_entropy_synthetic_secret_has_no_plaintext_or_raw_digest(self) -> None:
        arguments = {"kind": "start", "intent": "diagnosis-only",
                     "target": "fixture.invalid", "password": "1234"}
        raw_sha = hashlib.sha256(json.dumps(arguments, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        result, _ = self.invoke(windows=False, healthy=True, arguments=arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        audit = next(json.loads(line.removeprefix("openubmc-routing "))
                     for line in result.stderr.splitlines() if line.startswith("openubmc-routing "))
        self.assertNotIn("1234", result.stderr)
        self.assertNotIn(raw_sha, result.stderr)
        self.assertNotIn("requested_scope_digest", audit)
        self.assertTrue(audit["requested_scope_mac"].startswith("hmac-sha256:"))
        second, _ = self.invoke(windows=False, healthy=True, arguments=arguments)
        second_audit = next(json.loads(line.removeprefix("openubmc-routing "))
                            for line in second.stderr.splitlines() if line.startswith("openubmc-routing "))
        self.assertNotEqual(audit["requested_scope_mac"], second_audit["requested_scope_mac"])

    def test_malformed_and_oversized_requests_never_reach_backend(self) -> None:
        malformed, call_log = self.invoke(windows=False, healthy=True, raw_input="{not-json}\n")
        self.assertEqual(malformed.returncode, 0, malformed.stderr)
        self.assertEqual(json.loads(malformed.stdout)["error"]["message"], "routing_request_invalid")
        self.assertFalse(call_log.exists())
        self.assertNotIn("openubmc-routing ", malformed.stderr)

        malformed_shape = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": None}
        invalid, call_log = self.invoke(windows=False, healthy=True,
                                        raw_input=json.dumps(malformed_shape) + "\n")
        self.assertEqual(json.loads(invalid.stdout)["error"]["message"], "routing_request_invalid")
        self.assertFalse(call_log.exists())

        too_large = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "execute", "arguments": {"password": "x" * (130 * 1024)}}}
        oversized, call_log = self.invoke(windows=False, healthy=True,
                                          raw_input=json.dumps(too_large) + "\n")
        self.assertEqual(oversized.returncode, 0, oversized.stderr)
        self.assertEqual(json.loads(oversized.stdout)["error"]["message"],
                         "routing_request_too_large")
        self.assertFalse(call_log.exists())
        self.assertNotIn("openubmc-routing ", oversized.stderr)

    def test_argument_size_and_depth_limits_block_before_forwarding(self) -> None:
        arguments = {"payload": "x" * (65 * 1024)}
        oversized, call_log = self.invoke(windows=False, healthy=True, arguments=arguments)
        replies = {item["id"]: item for item in map(json.loads, oversized.stdout.splitlines())}
        self.assertEqual(replies[3]["error"]["message"], "routing_request_too_large")
        self.assertFalse(call_log.exists())
        nested = {"leaf": "fixture"}
        for _ in range(34):
            nested = {"next": nested}
        deep, call_log = self.invoke(windows=False, healthy=True, arguments=nested)
        replies = {item["id"]: item for item in map(json.loads, deep.stdout.splitlines())}
        self.assertEqual(replies[3]["error"]["message"], "routing_request_too_large")
        self.assertFalse(call_log.exists())


if __name__ == "__main__":
    unittest.main()
