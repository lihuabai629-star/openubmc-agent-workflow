from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        f"openubmc_debug_development_mode_{name}",
        SCRIPTS / f"{name}.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class InternalDevelopmentModeTests(unittest.TestCase):
    def test_mdb_property_names_are_not_blocked_or_redacted(self) -> None:
        module = load_script("mdbctl_remote")
        command = ["getprop", "Account", "1", "Password"]

        self.assertIn("Password", module.build_login_shell_cmd(command))
        self.assertIn("Password", module.build_direct_skynet_cmd(command))

    def test_dbus_property_names_are_not_blocked(self) -> None:
        module = load_script("busctl_remote")
        args = module.parse_args(
            [
                "--ip",
                "target.example",
                "--action",
                "get-property",
                "--service",
                "com.example.Service",
                "--path",
                "/com/example/Object",
                "--interface",
                "com.example.Interface",
                "--property",
                "Password",
            ]
        )

        self.assertIn("Password", module.build_busctl_cmd(args))

    def test_remote_paths_and_runtime_evidence_are_preserved(self) -> None:
        remote_file = load_script("read_remote_file")
        workflow_runtime = load_script("_workflow_runtime")
        evidence = {
            "password": "debug-password",
            "command": ["--password", "debug-password"],
        }

        self.assertEqual(remote_file.normalize_remote_path("/etc/shadow"), "/etc/shadow")
        compact = workflow_runtime.compact_tool_result(
            {
                "name": "file:/etc/shadow",
                "ok": True,
                "code": "ok",
                "returncode": 0,
                "command": ["read", "/etc/shadow"],
                "payload": {
                    "request": evidence,
                    "result": {"lines": ["password=debug-password"]},
                    "warnings": [],
                },
            }
        )
        self.assertEqual(compact["request"], evidence)
        self.assertEqual(compact["result"]["lines"], ["password=debug-password"])

    def test_remote_file_paths_remain_explicit_and_absolute(self) -> None:
        remote_file = load_script("read_remote_file")

        self.assertEqual(
            remote_file.normalize_remote_path("/etc/../etc/shadow"),
            "/etc/shadow",
        )
        for invalid in ("etc/shadow", "/", "/etc/shadow\nnext"):
            with self.subTest(path=invalid):
                with self.assertRaises(ValueError):
                    remote_file.normalize_remote_path(invalid)

    def test_debug_dump_preserves_raw_content_and_metadata(self) -> None:
        module = load_script("_debug_dump")
        with tempfile.TemporaryDirectory() as output_dir:
            dumper = module.DebugDumper(output_dir, secrets=["debug-password"])
            artifact_path = dumper.write_text(
                "transport",
                "session",
                "password=debug-password\nAuthorization: Bearer debug-token\n",
                metadata={"password": "debug-password"},
            )

            self.assertEqual(
                artifact_path.read_text(encoding="utf-8"),
                "password=debug-password\nAuthorization: Bearer debug-token\n",
            )
            summary = json.loads(
                (Path(output_dir) / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["redacted_artifact_count"], 0)
            self.assertFalse(summary["artifacts"][0]["redacted"])
            self.assertEqual(
                summary["artifacts"][0]["metadata"]["password"],
                "debug-password",
            )

    def test_doctor_preserves_proxy_and_ssh_config_values(self) -> None:
        module = load_script("doctor")
        proxy = "https://debug-user:debug-password@proxy.example/path?token=raw#part"
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy}, clear=True):
            self.assertEqual(module.proxy_env_summary(), {"HTTPS_PROXY": proxy})
        with mock.patch.object(
            module,
            "run_text_command",
            return_value=(
                0,
                "identityfile /tmp/debug-key\n"
                f"proxycommand connect-proxy {proxy}\n",
            ),
        ):
            self.assertEqual(
                module.ssh_config_summary("target.example"),
                {
                    "command_returncode": "0",
                    "identityfile": "/tmp/debug-key",
                    "proxycommand": f"connect-proxy {proxy}",
                },
            )


if __name__ == "__main__":
    unittest.main()
