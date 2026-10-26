"""Codex stdio discovery, native analysis, isolation and verified packaging."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


@unittest.skipUnless(shutil.which("node"), "Node is required by the Codex MCP entry")
class OfflineMcpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="offline-mcp 中文 '")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin = self.root / "plugin"
        scripts = self.plugin / "scripts"
        scripts.mkdir(parents=True)
        for name in ("openubmc-offline-mcp.js", "openubmc-bootstrap-host.js"):
            shutil.copyfile(ROOT / "plugin/openubmc/scripts" / name, scripts / name)
        skill = self.plugin / "skills/openubmc-log-bundle-analyzer"
        for name in ("scripts", "references"):
            shutil.copytree(ROOT / "openubmc-log-bundle-analyzer" / name, skill / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        manifest = self.plugin / ".codex-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text('{"name":"offline-fixture","version":"0.0.0"}\n')
        self.write_lock()
        self.bundle = self.root / "bundle"
        logs = self.bundle / "dump_info/LogDump"
        logs.mkdir(parents=True)
        (self.bundle / "dump_info/dump_log").write_text("capture\n")
        (logs / "security.log").write_text("2026-09-02 13:00:00 login failed password=never-export\n")
        self.environment = {**os.environ, "OPENUBMC_PLUGIN_PYTHON": sys.executable,
                            "OPENUBMC_OFFLINE_OUTPUT_DIR": str(self.root / "artifacts")}

    def write_lock(self):
        files = {path.relative_to(self.plugin).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in self.plugin.rglob("*") if path.is_file() and path.name != "plugin-lock.json"}
        lock = {"version": "0.0.0", "source_commit": "fixture", "files": files,
                "manifest_digest": files[".codex-plugin/plugin.json"]}
        lock["content_digest"] = hashlib.sha256(canonical(lock)).hexdigest()
        (self.plugin / "plugin-lock.json").write_bytes(canonical(lock))

    def run_mcp(self, messages, **environment):
        process = subprocess.run([shutil.which("node"), str(self.plugin / "scripts/openubmc-offline-mcp.js")],
            input=("\n".join(json.dumps(message) for message in messages) + "\n").encode("utf-8"),
            capture_output=True, timeout=30, env={**self.environment, **environment})
        return process, [json.loads(line) for line in process.stdout.splitlines()]

    def start(self):
        process = subprocess.Popen([shutil.which("node"), str(self.plugin / "scripts/openubmc-offline-mcp.js")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.environment)
        def close():
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
        self.addCleanup(close)
        return process

    def request(self, process, identity, method, params):
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}) + "\n").encode())
        process.stdin.flush()
        line = process.stdout.readline()
        self.assertTrue(line, "MCP closed before replying")
        return json.loads(line)["result"]

    def test_discovers_both_tools_without_device_runtime_or_credentials(self):
        process, replies = self.run_mcp([{"id": 1, "method": "tools/list"}])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual({tool["name"] for tool in replies[0]["result"]["tools"]},
                         {"openubmc_analyze_bundle", "openubmc_read_evidence"})

    def test_reads_original_evidence_after_structured_analysis(self):
        process = self.start()
        initialized = self.request(process, 1, "initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(initialized["serverInfo"]["name"], "openubmc-offline")
        result = self.request(process, 2, "tools/call", {"name": "openubmc_analyze_bundle",
            "arguments": {"source": str(self.bundle), "problem": "login failed"}})
        self.assertFalse(result["isError"], result)
        value = result["structuredContent"]
        pointer = value["evidence"]["items"][0]
        evidence = self.request(process, 3, "tools/call", {"name": "openubmc_read_evidence",
            "arguments": {"bundle_dir": value["bundle_dir"], "path": pointer["path"],
                          "start_line": pointer["line_number"], "line_count": 1}})
        self.assertFalse(evidence["isError"], evidence)
        self.assertIn("login failed", json.dumps(evidence))
        self.assertNotIn("never-export", json.dumps(evidence))
        self.assertTrue(Path(value["artifact"]).is_file())

    def test_rejects_relative_paths_before_starting_a_worker(self):
        process, replies = self.run_mcp([{"id": 1, "method": "tools/call", "params": {
            "name": "openubmc_analyze_bundle", "arguments": {"source": "relative", "problem": "login"}}}])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(replies[0]["result"]["structuredContent"]["code"], "absolute_path_required")
        self.assertFalse((self.root / "artifacts").exists())

    def test_refuses_modified_package_sources(self):
        entry = self.plugin / "skills/openubmc-log-bundle-analyzer/scripts/offline_tools.py"
        entry.write_text("raise ValueError('modified')\n")
        process, replies = self.run_mcp([{"id": 1, "method": "tools/list"}])
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(replies, [])
        self.assertIn(b"integrity failed", process.stderr)

    def test_cancellation_joins_the_owned_worker(self):
        worker = self.plugin / "skills/openubmc-log-bundle-analyzer/scripts/offline_tools.py"
        marker = self.root / "worker-started"
        worker.write_text("import os,sys,time\nfrom pathlib import Path\ndef main():\n"
            f" Path({str(marker)!r}).write_text(str(os.getpid()))\n time.sleep(300)\n")
        self.write_lock()
        process = self.start()
        process.stdin.write((json.dumps({"id": 1, "method": "tools/call", "params": {"name": "openubmc_analyze_bundle",
            "arguments": {"source": str(self.bundle), "problem": "login"}}}) + "\n").encode())
        process.stdin.flush()
        import time
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        process.stdin.write(b'{"method":"notifications/cancelled","params":{"requestId":1}}\n')
        process.stdin.flush()
        result = json.loads(process.stdout.readline())["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["code"], "cancelled")


if __name__ == "__main__":
    unittest.main()
