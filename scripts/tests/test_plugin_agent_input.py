"""The immutable plugin must run the same Agent input Adapter as source."""
from __future__ import annotations

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


_PACKAGED_MCP_FIXTURE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from openubmc_target_runtime import JsonRpcMcpEndpoint, RuntimeMcpService

class Task:
    def __init__(self, task_id):
        self.task_id = task_id

class Backend:
    def __init__(self):
        self.calls = []
    def open_task(self, task_id):
        return Task(task_id)
    def close_task(self, task):
        pass
    def maintain_task(self, task):
        return 0
    def task_status(self, task):
        return {"task_id": task.task_id}
    def debug_collect(self, task, arguments, context):
        context.raise_if_stopped()
        self.calls.append(("debug_collect", dict(arguments)))
        return {"ok": True, "observed_at": "2026-08-19T00:00:00Z",
                "result": {"capabilities": {"ssh_transport": True}, "lanes": {}}}
    def observe_query(self, task, arguments, context):
        value = self.debug_collect(task, arguments, context)
        value["observation_timing"] = {
            "started_at": value["observed_at"], "completed_at": value["observed_at"],
            "selectors": [{"selector_id": selector.get("id", "selector-1"),
                           "kind": selector["kind"], "status": "observed",
                           "started_at": value["observed_at"],
                           "completed_at": value["observed_at"]}
                          for selector in arguments["selectors"]]}
        return value
    def debug_run(self, task, arguments, context):
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {"ok": True, "schema": "openubmc-debug.v1", "task": task.task_id,
                "root_cause": "controlled fixture diagnosis", "summary": "diagnosis completed",
                "observed_at": "2026-08-19T00:00:00Z",
                "freshness": {"status": "fresh"}}

def call(name, arguments):
    backend = Backend()
    service = RuntimeMcpService(backend)
    try:
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="packaged-input")
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        result = endpoint.handle(message)["result"]
        return result, backend.calls
    finally:
        service.close()

canonical_observe = {"target": "192.0.2.10",
                     "selectors": [{"kind": "capability", "names": ["ssh"]}]}
legacy_observe = {"bmc_ip": "192.0.2.10",
                  "selectors": '[{"kind":"capability","names":["ssh"]}]'}
canonical_execute = {"kind": "start", "target": "192.0.2.10",
                     "intent": "diagnosis-only"}
legacy_execute = {"actionKind": "START", "target_ip": "192.0.2.10",
                  "intent": "diagnosis-only"}
observed = [call("observe", item) for item in (canonical_observe, legacy_observe)]
started = [call("execute", item) for item in (canonical_execute, legacy_execute)]
assert all(not result["isError"] for result, _ in observed + started)
assert all(len(calls) == 1 for _, calls in observed + started)
assert observed[0][0]["structuredContent"]["coverage"] == observed[1][0]["structuredContent"]["coverage"]
assert observed[0][0]["structuredContent"]["results"] == observed[1][0]["structuredContent"]["results"]
assert started[0][0]["structuredContent"]["state"] == started[1][0]["structuredContent"]["state"]
assert started[0][0]["structuredContent"]["gate"]["name"] == started[1][0]["structuredContent"]["gate"]["name"]
print(json.dumps({"observe_calls": len(observed[0][1]) + len(observed[1][1]),
                  "execute_calls": len(started[0][1]) + len(started[1][1]),
                  "state": started[0][0]["structuredContent"]["state"]}))
'''


class PackagedAgentInputTests(unittest.TestCase):
    def test_packaged_mcp_matches_canonical_fixture_without_device_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = package_fixture(Path(temporary))
            runtime = plugin / "skills/openubmc-target-runtime"
            self.assertTrue((runtime / "openubmc_target_runtime/agent_input.py").is_file())
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", _PACKAGED_MCP_FIXTURE, str(runtime)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {
                "observe_calls": 2,
                "execute_calls": 2,
                "state": "waiting_response",
            })


if __name__ == "__main__":
    unittest.main()
