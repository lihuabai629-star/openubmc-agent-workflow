"""Installed CLI checks executed with native Windows Python and no WSL."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class WindowsNativePluginCliTests(unittest.TestCase):
    def test_inherited_read_only_root_requires_explicit_snapshot_bound_repair(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            root = Path(raw)
            plugin = root / "plugin"
            files = {
                ".codex-plugin/plugin.json": b'{"name":"openubmc","version":"0.0.0"}',
                "openubmc-kb-mcp/package.json": b'{"name":"openubmc-kb-mcp","version":"0.0.0"}',
                "scripts/pluginctl.py": (ROOT / "plugin/openubmc/scripts/pluginctl.py").read_bytes(),
                "skills/openubmc-target-runtime/openubmc_target_runtime/windows_private.py":
                    (ROOT / "openubmc-target-runtime/openubmc_target_runtime/windows_private.py").read_bytes(),
            }
            for name, data in files.items():
                path = plugin / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            canonical = lambda value: (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
            unsigned = {
                "schema": "openubmc.codex-plugin.v1", "name": "openubmc", "version": "0.0.0",
                "source_commit": "a" * 40,
                "manifest_digest": hashlib.sha256(files[".codex-plugin/plugin.json"]).hexdigest(),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "skills": [],
            }
            (plugin / "plugin-lock.json").write_bytes(canonical({
                **unsigned, "content_digest": hashlib.sha256(canonical(unsigned)).hexdigest(),
            }))
            appdata = root / "local"
            appdata.mkdir()
            subprocess.run(["icacls.exe", str(appdata), "/grant", "*S-1-1-0:(OI)(CI)RX"],
                           check=True, capture_output=True, text=True, timeout=10)
            affected = appdata / "openubmc"
            affected.mkdir()
            retained = affected / "retained.json"
            retained.write_bytes(b'{"fixture":"unchanged"}')
            private = runpy.run_path(str(ROOT / "openubmc-target-runtime/openubmc_target_runtime/windows_private.py"))
            private["harden_new_file"](retained)
            environment = dict(os.environ, LOCALAPPDATA=str(appdata))
            for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
                environment.pop(name, None)
            def cli(*args):
                return subprocess.run([sys.executable, "-I", "-B", str(plugin / "scripts/pluginctl.py"), *args],
                                      env=environment, capture_output=True, text=True, timeout=20)

            blocked = cli("storage-status")
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            conflict = json.loads(blocked.stdout)
            self.assertEqual(conflict["error_code"], "windows_private_root_conflict")
            self.assertEqual(json.loads(cli("doctor").stdout)["error_code"], "windows_private_root_conflict")
            self.assertEqual(json.loads(cli("prepare", "--capability", "runtime", "--offline").stderr)["error_code"],
                             "windows_private_root_conflict")
            selected = next(item for item in conflict["roots"] if item["root_id"] == "data")
            self.assertEqual(selected["status"], "repairable")
            self.assertTrue(selected["snapshot_token"])
            self.assertEqual(cli("repair-storage", "--root-id", "data", "--expected-token", "stale").returncode, 2)
            subprocess.run(["icacls.exe", str(affected), "/grant", "*S-1-5-18:(RX)"],
                           check=True, capture_output=True, text=True, timeout=10)
            stale = cli("repair-storage", "--root-id", "data", "--expected-token", selected["snapshot_token"])
            self.assertEqual(stale.returncode, 2)
            self.assertEqual(json.loads(stale.stderr)["error"], "private_root_changed")
            selected = next(item for item in json.loads(cli("storage-status").stdout)["roots"]
                            if item["root_id"] == "data")
            with self.assertRaises(private["WindowsPrivateError"]):
                private["verify_private_path"](affected)
            repaired = cli("repair-storage", "--root-id", "data", "--expected-token", selected["snapshot_token"])
            self.assertEqual(repaired.returncode, 0, repaired.stderr)
            receipt = json.loads(repaired.stdout)
            self.assertEqual(receipt["status"], "repaired")
            private["verify_private_path"](affected)
            self.assertEqual(retained.read_bytes(), b'{"fixture":"unchanged"}')
            self.assertTrue(json.loads(cli("storage-status").stdout)["ok"])
            undone = cli("restore-storage", "--root-id", "data", "--transaction", receipt["transaction"])
            self.assertEqual(undone.returncode, 0, undone.stderr)
            self.assertEqual(json.loads(undone.stdout)["status"], "restored")
            reselected = json.loads(cli("storage-status").stdout)["roots"][0]
            self.assertEqual(reselected["status"], "repairable")
            rerepaired = cli("repair-storage", "--root-id", "data", "--expected-token", reselected["snapshot_token"])
            self.assertEqual(rerepaired.returncode, 0, rerepaired.stderr)
            self.assertEqual(retained.read_bytes(), b'{"fixture":"unchanged"}')

            subprocess.run(["icacls.exe", str(affected), "/inheritance:e"],
                           check=True, capture_output=True, text=True, timeout=10)
            state_root = appdata / "openubmc-agent-workflow"
            state_root.mkdir()
            state_file = state_root / "retained.json"
            state_file.write_bytes(b'{"state":"unchanged"}')
            private["harden_new_file"](state_file)
            spec = importlib.util.spec_from_file_location(
                "native_recovery_page", ROOT / "openubmc-environment-setup/scripts/config_page.py")
            page = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(page)
            maintenance = page.PluginMaintenance(plugin, environment=environment)
            with page.LocalConfigurationServer(appdata, kind="kb", maintenance=maintenance) as server:
                headers = {"X-OpenUBMC-Session": server.session_token, "Origin": server.origin,
                           "Content-Type": "application/json"}
                def state():
                    with urlopen(Request(server.origin + "/api/state", headers=headers), timeout=5) as response:
                        return json.load(response)
                def repair(root_id, token):
                    request = Request(server.origin + "/api/plugin", data=json.dumps({
                        "action": "repair-storage", "root_id": root_id, "snapshot_token": token,
                    }).encode(), headers=headers)
                    with urlopen(request, timeout=5) as response:
                        return json.load(response)
                def undo():
                    request = Request(server.origin + "/api/plugin", data=b'{"action":"undo-storage"}', headers=headers)
                    with urlopen(request, timeout=5) as response:
                        return json.load(response)
                pending = state()
                self.assertEqual(pending["storage"]["error_code"], "windows_private_root_conflict")
                self.assertEqual(pending["page_session"]["kind"], "kb")
                selected = next(item for item in pending["storage"]["roots"] if item["root_id"] == "data")
                self.assertEqual(repair("data", selected["snapshot_token"])["status"], "repaired")
                pending_state = state()
                self.assertTrue(pending_state["storage_repair"])
                selected_state = next(item for item in pending_state["storage"]["roots"] if item["root_id"] == "state")
                self.assertEqual(selected_state["status"], "repairable")
                self.assertEqual(repair("state", selected_state["snapshot_token"])["status"], "repaired")
                resumed = state()
                self.assertTrue(resumed["storage"]["ok"])
                self.assertEqual(resumed["page_session"]["kind"], "kb")
                self.assertTrue(resumed["storage_repair"])
                self.assertEqual(undo()["status"], "restored")
                restored_state = state()
                self.assertTrue(restored_state["storage_repair"])
                self.assertEqual(next(item for item in restored_state["storage"]["roots"]
                                      if item["root_id"] == "state")["status"], "repairable")
                self.assertEqual(undo()["status"], "restored")
                restored = state()
                self.assertFalse(restored["storage_repair"])
                for root_id in ("data", "state"):
                    selected = next(item for item in restored["storage"]["roots"] if item["root_id"] == root_id)
                    self.assertEqual(selected["status"], "repairable")
                    self.assertEqual(repair(root_id, selected["snapshot_token"])["status"], "repaired")
                self.assertEqual(retained.read_bytes(), b'{"fixture":"unchanged"}')
                self.assertEqual(state_file.read_bytes(), b'{"state":"unchanged"}')

            subprocess.run(["icacls.exe", str(affected), "/grant", "*S-1-1-0:R"],
                           check=True, capture_output=True, text=True, timeout=10)
            explicit = json.loads(cli("storage-status").stdout)
            self.assertEqual(explicit["roots"][0]["status"], "blocked")
            self.assertEqual(explicit["roots"][0]["reason_code"], "private_root_explicit_outside_access")
            subprocess.run(["icacls.exe", str(affected), "/remove:g", "*S-1-1-0"],
                           check=True, capture_output=True, text=True, timeout=10)
            subprocess.run(["icacls.exe", str(appdata), "/grant", "*S-1-1-0:(OI)(CI)M"],
                           check=True, capture_output=True, text=True, timeout=10)
            subprocess.run(["icacls.exe", str(affected), "/inheritance:e"],
                           check=True, capture_output=True, text=True, timeout=10)
            inherited_write = json.loads(cli("storage-status").stdout)
            self.assertEqual(inherited_write["roots"][0]["status"], "blocked")
            self.assertEqual(inherited_write["roots"][0]["reason_code"], "private_root_outside_write_access")
            self.assertEqual(retained.read_bytes(), b'{"fixture":"unchanged"}')

    def test_verified_installed_inventory_starts_without_posix_modules(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            plugin = Path(raw) / "openubmc"
            (plugin / "scripts").mkdir(parents=True)
            (plugin / ".codex-plugin").mkdir()
            (plugin / "openubmc-kb-mcp").mkdir()
            shutil.copyfile(ROOT / "plugin/openubmc/scripts/pluginctl.py", plugin / "scripts/pluginctl.py")
            manifest = b'{"name":"openubmc","version":"0.0.0"}'
            (plugin / ".codex-plugin/plugin.json").write_bytes(manifest)
            knowledge = b'{"name":"openubmc-kb-mcp","version":"0.0.0"}'
            (plugin / "openubmc-kb-mcp/package.json").write_bytes(knowledge)
            files = {
                ".codex-plugin/plugin.json": hashlib.sha256(manifest).hexdigest(),
                "openubmc-kb-mcp/package.json": hashlib.sha256(knowledge).hexdigest(),
                "scripts/pluginctl.py": hashlib.sha256((plugin / "scripts/pluginctl.py").read_bytes()).hexdigest(),
            }
            unsigned = {
                "schema": "openubmc.codex-plugin.v1", "name": "openubmc", "version": "0.0.0",
                "source_commit": "a" * 40, "manifest_digest": files[".codex-plugin/plugin.json"],
                "files": files, "skills": [],
            }
            canonical = lambda value: (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
            lock = {**unsigned, "content_digest": hashlib.sha256(canonical(unsigned)).hexdigest()}
            (plugin / "plugin-lock.json").write_bytes(canonical(lock))
            result = subprocess.run([sys.executable, "-I", "-B", str(plugin / "scripts/pluginctl.py"), "verify"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])

    def test_prepare_leaves_configuration_root_private_for_credential_activation(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as raw:
            root = Path(raw)
            plugin = root / "openubmc"
            files = {
                ".codex-plugin/plugin.json": b'{"name":"openubmc","version":"0.0.0"}',
                "openubmc-kb-mcp/package.json": b'{"name":"openubmc-kb-mcp","version":"0.0.0"}',
                "requirements.lock": b"",
                "scripts/pluginctl.py": (ROOT / "plugin/openubmc/scripts/pluginctl.py").read_bytes(),
                "skills/openubmc-target-runtime/openubmc_target_runtime/windows_private.py":
                    (ROOT / "openubmc-target-runtime/openubmc_target_runtime/windows_private.py").read_bytes(),
            }
            for name, data in files.items():
                path = plugin / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            canonical = lambda value: (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
            unsigned = {
                "schema": "openubmc.codex-plugin.v1", "name": "openubmc", "version": "0.0.0",
                "source_commit": "a" * 40,
                "manifest_digest": hashlib.sha256(files[".codex-plugin/plugin.json"]).hexdigest(),
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                "skills": [],
            }
            (plugin / "plugin-lock.json").write_bytes(canonical({
                **unsigned, "content_digest": hashlib.sha256(canonical(unsigned)).hexdigest(),
            }))
            appdata = root / "local"
            appdata.mkdir()
            subprocess.run(
                ["icacls.exe", str(appdata), "/grant", "*S-1-1-0:(OI)(CI)RX"],
                check=True, capture_output=True, text=True, timeout=10,
            )
            environment = dict(os.environ, LOCALAPPDATA=str(appdata))
            for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
                environment.pop(name, None)
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(plugin / "scripts/pluginctl.py"),
                 "prepare", "--capability", "runtime", "--offline"],
                env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            private = runpy.run_path(str(ROOT / "openubmc-target-runtime/openubmc_target_runtime/windows_private.py"))
            private["verify_private_path"](appdata / "openubmc")


if __name__ == "__main__":
    unittest.main()
