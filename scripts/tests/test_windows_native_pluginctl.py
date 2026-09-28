"""Installed CLI checks executed with native Windows Python and no WSL."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
