"""Behavior tests for the cross-platform public MCP launcher."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

if __package__:
    from .plugin_fixture import package_fixture
else:
    from plugin_fixture import package_fixture


@unittest.skipUnless(shutil.which("node"), "Node.js is required")
class PluginWindowsLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.base = Path(cls.directory.name)
        cls.plugin = package_fixture(cls.base)
        cls.fake_wsl = cls.base / "fake-wsl"
        cls.fake_wsl_control = cls.base / "fake-wsl-control.json"
        cls.fake_wsl.write_text(
            """#!/usr/bin/env python3
import json, os, sys
with open(__CONTROL__) as stream:
    control = json.load(stream)
args = sys.argv[1:]
if control.get('FAKE_WSL_ARGV_LOG'):
    with open(control['FAKE_WSL_ARGV_LOG'], 'a') as stream:
        stream.write(json.dumps(args) + '\\n')
if control.get('FAKE_WSL_ENV_LOG'):
    with open(control['FAKE_WSL_ENV_LOG'], 'a') as stream:
        stream.write(json.dumps({name: os.environ.get(name) for name in (
            'OPENUBMC_CREDENTIALS_FILE', 'OPENUBMC_KB_PASSWORD', 'OPENUBMC_KB_CLIENT_SECRET')}) + '\\n')
if args == ['--list', '--quiet']:
    print(control.get('FAKE_WSL_DISTROS', ''))
    raise SystemExit(0)
if 'wslpath' in args:
    print(control['FAKE_WSL_PLUGIN_ROOT'])
    raise SystemExit(0)
if '-c' in args:
    raise SystemExit(0)
if 'repair-overrides' in args or 'migrate' in args:
    command = 'repair-overrides' if 'repair-overrides' in args else 'migrate'
    changes = {'mcp_servers': ['openubmc-kb'] if command == 'repair-overrides' else [], 'skills': [] if command == 'repair-overrides' else ['/fixture/openubmc-debug/SKILL.md']}
    print(json.dumps({'ok': True, 'would_change': True, 'changed': '--preview' not in args, 'transaction': 'a' * 32, 'changes': changes}))
    raise SystemExit(0)
if 'prepare' in args:
    if control.get('FAKE_WSL_PREPARE_ERROR'):
        print(control['FAKE_WSL_PREPARE_ERROR'], file=sys.stderr)
    raise SystemExit(int(control.get('FAKE_WSL_PREPARE_STATUS', '42')))
if 'doctor' in args:
    if control.get('FAKE_WSL_DOCTOR_REPORT'):
        print(control['FAKE_WSL_DOCTOR_REPORT'])
    raise SystemExit(int(control.get('FAKE_WSL_DOCTOR_STATUS', '2')))
raise SystemExit(43)
""".replace("__CONTROL__", repr(str(cls.fake_wsl_control)))
        )
        cls.fake_wsl.chmod(0o755)
        cls.fake_python = cls.base / "fake-python3"
        cls.fake_python_control = cls.base / "fake-python-control.json"
        cls.fake_python_control.write_text("{}")
        cls.fake_python.write_text(
            """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(__CONTROL__) as stream:
    control = json.load(stream)
if control.get('FAKE_PYTHON_ARGV_LOG'):
    with open(control['FAKE_PYTHON_ARGV_LOG'], 'a') as stream:
        stream.write(json.dumps(args) + '\\n')
if control.get('FAKE_PYTHON_ENV_LOG'):
    with open(control['FAKE_PYTHON_ENV_LOG'], 'a') as stream:
        stream.write(json.dumps({name: os.environ.get(name) for name in (
            'OPENUBMC_CREDENTIALS_FILE', 'OPENUBMC_KB_PASSWORD',
            'OPENUBMC_MCP_TASK_ID', 'OPENUBMC_EXECUTION_HOST')}) + '\\n')
if '-c' in args or 'prepare' in args or 'cleanup-retired' in args:
    if 'prepare' in args and control.get('FAKE_PYTHON_PREPARE_ERROR'):
        print(control['FAKE_PYTHON_PREPARE_ERROR'], file=sys.stderr)
    if 'prepare' in args:
        raise SystemExit(int(control.get('FAKE_PYTHON_PREPARE_STATUS', 0)))
    raise SystemExit(0)
if 'repair-overrides' in args or 'migrate' in args:
    command = 'repair-overrides' if 'repair-overrides' in args else 'migrate'
    changes = {'mcp_servers': ['openubmc-kb'] if command == 'repair-overrides' else [],
               'skills': [] if command == 'repair-overrides' else ['/fixture/openubmc-debug/SKILL.md']}
    print(json.dumps({'ok': True, 'would_change': True, 'changed': '--preview' not in args,
                      'transaction': 'a' * 32, 'changes': changes}))
    raise SystemExit(0)
if 'doctor' in args:
    status = int(control.get('FAKE_PYTHON_DOCTOR_STATUS', os.environ.get('FAKE_PYTHON_DOCTOR_STATUS', '0')))
    ready = status == 0
    print(control.get('FAKE_PYTHON_DOCTOR_REPORT') or json.dumps({'ok': ready, 'version': '2.1.0', 'source_commit': 'a' * 40,
        'package_integrity': True,
        'capabilities': {'runtime': {'dependencies_ready': ready, 'startup_ready': ready},
                         'kb': {'dependencies_ready': ready, 'startup_ready': ready}},
        'mcp_health': {'runtime': {'ok': ready}, 'kb': {'ok': ready}},
        'local_configuration': {},
        'knowledge_authentication': 'not_checked', 'remote_target_authentication': 'not_checked',
        'codex_configuration': {'ready': ready, 'changes': {'mcp_servers': [], 'skills': []}}}))
    raise SystemExit(status)
if 'configure' in args:
    print('http://127.0.0.1:43123/#fixture-token', flush=True)
    import time
    time.sleep(60)
capability = args[-1]
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    if request.get('method') == 'initialize':
        result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'), 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake-' + capability, 'version': '1'}}
    elif request.get('method') == 'tools/list':
        result = {'tools': []}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
"""
            .replace("__CONTROL__", repr(str(cls.fake_python_control)))
        )
        cls.fake_python.chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def invoke(self, capability: str, messages: list[dict], **environment: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            HOME=str(self.base / "home"),
            LOCALAPPDATA=str(self.base / "local-app-data"),
            OPENUBMC_PLUGIN_HOST_PLATFORM="win32",
            OPENUBMC_PLUGIN_WSL_EXE=str(self.base / "missing-wsl.exe"),
        )
        control = {key: value for key, value in environment.items() if key.startswith("FAKE_WSL_")}
        self.fake_wsl_control.write_text(json.dumps(control))
        python_control = {key: value for key, value in environment.items() if key.startswith("FAKE_PYTHON_")}
        self.fake_python_control.write_text(json.dumps(python_control))
        env.update({key: value for key, value in environment.items()
                    if not key.startswith(("FAKE_WSL_", "FAKE_PYTHON_"))})
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        return subprocess.run(
            ["node", str(self.plugin / "scripts/openubmc-mcp-bootstrap.js"), capability],
            input=payload,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_packaged_windows_launcher_offers_setup_without_native_python(self) -> None:
        config = json.loads((self.plugin / ".mcp.json").read_text())
        secret_environment = {
            "OPENUBMC_CREDENTIALS_FILE",
            "OPENUBMC_KB_USERNAME",
            "OPENUBMC_KB_PASSWORD",
            "OPENUBMC_KB_CLIENT_SECRET",
        }
        for capability, name in (("runtime", "openubmc-target-runtime"), ("kb", "openubmc-kb")):
            with self.subTest(capability=capability):
                server = config["mcpServers"][name]
                self.assertEqual(server["command"], "node")
                self.assertEqual(server["args"], ["./scripts/openubmc-mcp-bootstrap.js", capability])
                self.assertTrue(secret_environment.isdisjoint(server["env_vars"]))
                result = self.invoke(
                    capability,
                    [
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                        },
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    ],
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                replies = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(replies[0]["id"], 1)
                self.assertEqual(replies[0]["result"]["serverInfo"]["name"], f"openubmc-{capability}-setup")
                tools = replies[1]["result"]["tools"]
                self.assertEqual(
                    {tool["name"] for tool in tools},
                    {"openubmc_setup_status", "openubmc_setup_prepare", "openubmc_setup_open_configuration", "openubmc_setup_repair_configuration"},
                )

    def test_windows_device_backend_uses_native_python_without_wsl(self) -> None:
        result = self.invoke(
            "runtime",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ],
            OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
            OPENUBMC_PLUGIN_WSL_EXE=str(self.base / "missing-wsl.exe"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "fake-runtime")
        self.assertEqual(replies[1]["result"]["tools"], [])
        self.assertNotIn("wsl_unavailable", result.stderr)

    def test_setup_status_explains_the_missing_windows_prerequisite(self) -> None:
        result = self.invoke(
            "runtime",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "openubmc_setup_status", "arguments": {}},
                },
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = [json.loads(line) for line in result.stdout.splitlines()][1]
        payload = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "setup_required")
        self.assertEqual(payload["host"], "windows")
        self.assertEqual(payload["reason"], "windows_python_unavailable")
        self.assertEqual(payload["execution_host"], "windows-native")
        lock = json.loads((self.plugin / "plugin-lock.json").read_text())
        self.assertEqual(payload["plugin"]["version"], lock["version"])
        self.assertEqual(payload["plugin"]["source_commit"], lock["source_commit"])
        self.assertTrue(payload["plugin"]["integrity"])
        self.assertNotIn(str(self.base), json.dumps(payload))

    def test_windows_private_root_conflict_points_to_the_local_recovery_page(self) -> None:
        report = {"ok": False, "error_code": "windows_private_root_conflict",
                  "roots": [{"root_id": "data", "capabilities": ["data", "cache", "config"],
                             "path": "C:\\Users\\fixture\\AppData\\Local\\openubmc",
                             "status": "repairable", "snapshot_token": "a" * 64}]}
        result = self.invoke(
            "runtime",
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "openubmc_setup_status", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "openubmc_setup_prepare", "arguments": {}}}],
            OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
            FAKE_PYTHON_DOCTOR_REPORT=json.dumps(report), FAKE_PYTHON_DOCTOR_STATUS="2",
            FAKE_PYTHON_PREPARE_ERROR=json.dumps(report), FAKE_PYTHON_PREPARE_STATUS="2",
            LOCALAPPDATA=str(self.base / "private-root-conflict"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads([json.loads(line) for line in result.stdout.splitlines()][1]["result"]["content"][0]["text"])
        self.assertEqual(payload["reason"], "windows_private_root_conflict")
        self.assertIn("openubmc_setup_open_configuration", payload["recovery_action"])
        self.assertEqual(payload["affected_roots"], [{"root_id": "data", "capabilities": ["data", "cache", "config"], "status": "repairable"}])
        self.assertNotIn("snapshot_token", json.dumps(payload))
        prepared = json.loads([json.loads(line) for line in result.stdout.splitlines()][2]["result"]["content"][0]["text"])
        self.assertEqual(prepared["reason"], "windows_private_root_conflict")
        self.assertEqual(prepared["affected_roots"], payload["affected_roots"])

    def test_native_windows_child_receives_task_identity_without_ambient_secrets(self) -> None:
        log = self.base / "native-python-argv.jsonl"
        environment_log = self.base / "native-python-environment.jsonl"
        result = self.invoke(
            "runtime",
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}],
            OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
            FAKE_PYTHON_ARGV_LOG=str(log),
            FAKE_PYTHON_ENV_LOG=str(environment_log),
            LOCALAPPDATA=str(self.base / "identity-local-app-data"),
            OPENUBMC_MCP_TASK_ID="task-fixture",
            OPENUBMC_MCP_SESSION_ID="session-fixture",
            OPENUBMC_CREDENTIALS_FILE="private-password-fixture",
            OPENUBMC_KB_PASSWORD="kb-password-fixture",
            OPENUBMC_KB_CLIENT_SECRET="kb-client-secret-fixture",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = log.read_text()
        self.assertIn("pluginctl.py", arguments)
        self.assertNotIn("private-password-fixture", arguments)
        child_environments = environment_log.read_text()
        self.assertIn("task-fixture", child_environments)
        self.assertIn("windows-native", child_environments)
        self.assertNotIn("private-password-fixture", child_environments)
        self.assertNotIn("kb-password-fixture", child_environments)
        self.assertNotIn("kb-client-secret-fixture", child_environments)

    def test_dependency_preparation_failures_have_stable_recovery_reasons(self) -> None:
        cases = (
            ('{"stage":"pip","status":"failed","error":"No module named pip"}', "pip_unavailable"),
            ('{"stage":"npm","status":"failed","error":"spawn npm ENOENT"}', "npm_unavailable"),
            ('{"stage":"pip","status":"failed","error":"Could not resolve registry host"}', "registry_unavailable"),
            ('{"stage":"npm","status":"failed","error":"ProxyError connection refused"}', "proxy_failure"),
            ('{"stage":"pip","status":"timeout"}', "dependency_prepare_timeout"),
            ('{"stage":"pip","status":"interrupted"}', "dependency_prepare_interrupted"),
        )
        for index, (detail, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                result = self.invoke(
                    "runtime",
                    [
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "openubmc_setup_prepare", "arguments": {}}},
                    ],
                    OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
                    FAKE_PYTHON_DOCTOR_STATUS="42",
                    FAKE_PYTHON_PREPARE_STATUS="130" if reason == "dependency_prepare_interrupted" else "42",
                    FAKE_PYTHON_PREPARE_ERROR=detail,
                    LOCALAPPDATA=str(self.base / f"prepare-failure-{index}"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                response = json.loads([json.loads(line) for line in result.stdout.splitlines()][1]["result"]["content"][0]["text"])
                self.assertEqual(response["reason"], reason)
                self.assertIsInstance(response["recovery_action"], str)
                self.assertTrue(response["recovery_action"])
                self.assertNotIn(detail, json.dumps(response))

    def test_setup_status_reports_each_local_readiness_fact(self) -> None:
        report = {
            "ok": False,
            "version": "2.1.0",
            "source_commit": "a" * 40,
            "package_integrity": True,
            "capabilities": {
                "runtime": {"dependencies_ready": True, "startup_ready": True},
                "kb": {"dependencies_ready": False, "startup_ready": False},
            },
            "mcp_health": {"runtime": {"ok": True}, "kb": {"ok": False}},
            "local_configuration": {
                "targets": {"configured": True, "active_revision": "b" * 32},
                "kb": {"configured": False, "active_revision": None},
                "conan": {"configured": False, "active_revision": None},
            },
            "knowledge_authentication": "not_checked",
            "remote_target_authentication": "not_checked",
            "codex_configuration": {
                "ready": False,
                "changes": {"mcp_servers": ["openubmc-kb"], "skills": ["/legacy/SKILL.md"]},
            },
        }
        result = self.invoke(
            "kb",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "openubmc_setup_status", "arguments": {}}},
            ],
            OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
            FAKE_PYTHON_DOCTOR_REPORT=json.dumps(report),
            FAKE_PYTHON_DOCTOR_STATUS="2",
            LOCALAPPDATA=str(self.base / "unified-status"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads([json.loads(line) for line in result.stdout.splitlines()][1]["result"]["content"][0]["text"])
        self.assertEqual(payload["plugin"], {"version": "2.1.0", "source_commit": "a" * 40, "integrity": True})
        self.assertEqual(payload["backend"]["execution_host"], "windows-native")
        self.assertTrue(payload["dependencies"]["runtime"]["ready"])
        self.assertFalse(payload["dependencies"]["kb"]["ready"])
        self.assertEqual(payload["configuration"]["mcp_servers"], ["openubmc-kb"])
        self.assertEqual(payload["local_configuration"]["targets"]["active_revision"], "b" * 32)
        self.assertEqual(payload["knowledge_authentication"], "not_checked")
        self.assertEqual(payload["remote_target_authentication"], "not_checked")
        self.assertTrue(payload["protocol_health"]["runtime"]["ok"])

    def test_linux_launcher_prepares_then_proxies_the_existing_backend(self) -> None:
        result = self.invoke(
            "runtime",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ],
            OPENUBMC_PLUGIN_HOST_PLATFORM="linux",
            OPENUBMC_PLUGIN_PYTHON=str(self.fake_python),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "fake-runtime")
        self.assertEqual(replies[1]["result"]["tools"], [])

    def test_clean_linux_home_initializes_before_dependencies_are_installed(self) -> None:
        result = self.invoke(
            "runtime",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "openubmc_setup_status", "arguments": {}}},
            ],
            OPENUBMC_PLUGIN_HOST_PLATFORM="linux",
            XDG_DATA_HOME=str(self.base / "clean-linux-data"),
            XDG_CACHE_HOME=str(self.base / "clean-linux-cache"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "openubmc-runtime-setup")
        payload = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(payload["reason"], "dependencies_not_prepared")

    def test_dependency_preparation_is_an_explicit_setup_tool(self) -> None:
        result = self.invoke(
            "runtime",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "openubmc_setup_prepare", "arguments": {}}},
            ],
            OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
            FAKE_PYTHON_DOCTOR_STATUS="2",
            FAKE_PYTHON_PREPARE_STATUS="0",
            LOCALAPPDATA=str(self.base / "prepare-local-app-data"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads([json.loads(line) for line in result.stdout.splitlines()][1]["result"]["content"][0]["text"])
        self.assertEqual(response["status"], "preparation_completed")
        self.assertEqual(response["capability"], "runtime")

    def test_windows_setup_repairs_recognized_host_configuration_natively(self) -> None:
        result = self.invoke(
            "runtime",
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "openubmc_setup_repair_configuration", "arguments": {}}},
            ],
            OPENUBMC_PLUGIN_WINDOWS_PYTHON=str(self.fake_python),
            FAKE_PYTHON_DOCTOR_STATUS="2",
            LOCALAPPDATA=str(self.base / "repair-local-app-data"),
            USERPROFILE="C:\\Users\\fixture",
            CODEX_HOME="C:\\Users\\fixture\\.codex",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads([json.loads(line) for line in result.stdout.splitlines()][1]["result"]["content"][0]["text"])
        self.assertEqual(response["status"], "configuration_repaired")
        self.assertEqual([item["operation"] for item in response["operations"]], ["repair-overrides", "migrate"])
        self.assertEqual(response["operations"][1]["skill_count"], 1)

    def test_setup_tool_opens_the_private_configuration_page(self) -> None:
        env = dict(os.environ)
        env.update(
            HOME=str(self.base / "config-home"),
            OPENUBMC_PLUGIN_HOST_PLATFORM="linux",
            OPENUBMC_PLUGIN_PYTHON=str(self.fake_python),
            FAKE_PYTHON_DOCTOR_STATUS="42",
        )
        process = subprocess.Popen(
            ["node", str(self.plugin / "scripts/openubmc-mcp-bootstrap.js"), "runtime"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        timer = threading.Timer(15, process.kill)
        timer.start()
        try:
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "openubmc_setup_open_configuration", "arguments": {"kind": "targets"}},
                },
            ]
            process.stdin.write("".join(json.dumps(request) + "\n" for request in requests))
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())["id"], 1)
            response = json.loads(process.stdout.readline())
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(payload["status"], "configuration_page_ready")
            self.assertEqual(payload["url"], "http://127.0.0.1:43123/#fixture-token")
            process.stdin.close()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0, process.stderr.read())
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    @unittest.skipUnless(Path("/proc/sys/kernel/pid_max").is_file(), "Linux process identity is required")
    def test_plugin_controller_retires_only_verified_orphans_from_an_old_release(self) -> None:
        lifecycle = self.base / "cleanup-home/.local/state/openubmc-agent-workflow/mcp-processes"
        lifecycle.mkdir(parents=True, exist_ok=True)
        lock = json.loads((self.plugin / "plugin-lock.json").read_text())
        missing_parent = int(Path("/proc/sys/kernel/pid_max").read_text()) + 1
        children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) for _ in range(2)]
        try:
            for index, child in enumerate(children):
                stat = Path(f"/proc/{child.pid}/stat").read_text()
                identity = stat[stat.rfind(")") + 2 :].split()[19]
                record = {
                    "schema": "openubmc.mcp-process-lifecycle.v1",
                    "component": "target-runtime",
                    "version": "2.0.12" if index == 0 else "current",
                    "client": "codex",
                    "task_id": f"task-{index}",
                    "session_id": f"session-{index}",
                    "source_commit": "a" * 40 if index == 0 else lock["source_commit"],
                    "parent_pid": missing_parent,
                    "parent_identity": "verified-parent-start",
                    "parent_identity_verified": True,
                    "process_id": child.pid,
                    "process_identity": identity,
                    "active_requests": 0,
                }
                (lifecycle / f"target-runtime-{child.pid}.json").write_text(json.dumps(record))
            env = dict(os.environ, HOME=str(self.base / "cleanup-home"))
            result = subprocess.run(
                [sys.executable, "-I", str(self.plugin / "scripts/pluginctl.py"), "cleanup-retired"],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            children[0].wait(timeout=5)
            self.assertEqual(children[0].returncode, -signal.SIGTERM)
            self.assertIsNone(children[1].poll())
            self.assertEqual(json.loads(result.stdout)["cleaned_processes"], [children[0].pid])
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
