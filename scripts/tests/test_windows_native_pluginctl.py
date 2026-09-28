"""Installed CLI checks executed with native Windows Python and no WSL."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == "win32", "requires native Windows")
class WindowsNativePluginCliTests(unittest.TestCase):
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
