from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _cli_common  # noqa: E402


class DebugCredentialBundleTests(unittest.TestCase):
    def test_one_shot_ssh_reuses_the_activated_target_record(self):
        from _target_runtime_adapter import _load_runtime_module
        runtime = _load_runtime_module()
        import importlib
        configuration = importlib.import_module(runtime.__name__ + ".configuration")
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "openubmc" / "credentials.json"
            source.parent.mkdir()
            source.write_text(json.dumps({"schema_version": 1}))
            source.chmod(0o600)
            store = configuration.LocalConfigurationStore(source, kind="targets")
            config = {
                "schema_version": 1,
                "credentials": {"specific": {"user": "target-user", "password": "fixture-secret"}},
                "targets": {"192.0.2.39": {"bmc": {"ssh": "specific"}}},
            }
            store.save_and_activate(
                config, expected_revision=None, expected_active_revision=None,
                expected_source_text=source.read_text(),
            )
            args = argparse.Namespace(ip="192.0.2.39", ssh_port=22)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": raw}, clear=True):
                selected = _cli_common.resolve_ssh_credentials(args)
                self.assertEqual(selected["user"], "target-user")
                self.assertEqual(selected["password"], "fixture-secret")
                with self.assertRaises(Exception) as raised:
                    _cli_common.resolve_ssh_credentials(argparse.Namespace(ip="192.0.2.40", ssh_port=22))
                self.assertEqual(raised.exception.code, "credentials_missing")

    def test_explicit_one_shot_account_keeps_legacy_selection(self):
        args = argparse.Namespace(ip="192.0.2.39", ssh_user="explicit-user", ssh_password="explicit-password")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(_cli_common, "load_credentials_file", return_value={}):
            selected = _cli_common.resolve_ssh_credentials(args)
        self.assertEqual(selected["user"], "explicit-user")
        self.assertEqual(selected["password"], "explicit-password")

    def test_combined_debug_credentials_parse_the_selected_source_once(self) -> None:
        args = argparse.Namespace(
            ssh_user="",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            telnet_user="",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
        )
        values = {
            "OPENUBMC_SSH_USER": "ssh-user",
            "OPENUBMC_SSH_PASSWORD": "ssh-password",
            "OPENUBMC_TELNET_USER": "telnet-user",
            "OPENUBMC_TELNET_PASSWORD": "telnet-password",
        }
        with mock.patch.object(
            _cli_common,
            "load_credentials_file",
            return_value=values,
        ) as load:
            bundle = _cli_common.resolve_debug_credentials(args)

        self.assertEqual(load.call_count, 1)
        self.assertEqual(bundle["ssh"]["user"], "ssh-user")
        self.assertEqual(bundle["telnet"]["user"], "telnet-user")

    def test_task_cached_credentials_skip_a_second_file_parse(self) -> None:
        args = argparse.Namespace(
            ssh_user="",
            ssh_user_env="",
            ssh_password_env="",
            ssh_identity_file="",
            ssh_port=22,
            telnet_user="",
            telnet_user_env="",
            telnet_password_env="",
            telnet_port=23,
        )
        values = {
            "OPENUBMC_SSH_USER": "ssh-user",
            "OPENUBMC_SSH_PASSWORD": "ssh-password",
            "OPENUBMC_TELNET_USER": "telnet-user",
            "OPENUBMC_TELNET_PASSWORD": "telnet-password",
        }
        with mock.patch.object(
            _cli_common,
            "load_credentials_file",
            side_effect=AssertionError("credential file parsed twice"),
        ):
            bundle = _cli_common.resolve_debug_credentials(
                args,
                credentials=values,
            )

        self.assertEqual(bundle["ssh"]["user"], "ssh-user")
        self.assertEqual(bundle["telnet"]["user"], "telnet-user")


if __name__ == "__main__":
    unittest.main()
