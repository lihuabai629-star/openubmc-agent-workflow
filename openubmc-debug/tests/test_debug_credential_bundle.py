from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "openubmc-debug" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import _cli_common  # noqa: E402


class DebugCredentialBundleTests(unittest.TestCase):
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
