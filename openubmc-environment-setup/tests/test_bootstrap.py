#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "bootstrap.py"
SPEC = importlib.util.spec_from_file_location("openubmc_workflow_bootstrap", BOOTSTRAP)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)
INSTALLER = REPO_ROOT / "openubmc-environment-setup" / "scripts" / "install_environment.py"
INSTALLER_SPEC = importlib.util.spec_from_file_location(
    "openubmc_environment_installer_for_bootstrap_contract", INSTALLER
)
assert INSTALLER_SPEC is not None and INSTALLER_SPEC.loader is not None
installer = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return b"#!/usr/bin/env python3\n"


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_requires_an_explicit_immutable_ref_before_download(self) -> None:
        for arguments in ([], ["--ref", "main"]):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with (
                    mock.patch.object(bootstrap.urllib.request, "urlopen") as urlopen,
                    mock.patch.object(bootstrap.subprocess, "run") as run,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    bootstrap.main(arguments)

                self.assertEqual(raised.exception.code, 2)
                self.assertIn("release tag or full commit", stderr.getvalue())
                urlopen.assert_not_called()
                run.assert_not_called()

    def test_bootstrap_downloads_installer_and_forwards_managed_install_options(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.dict(bootstrap.os.environ, {"GH_TOKEN": "fixture-token"}, clear=True),
            mock.patch.object(bootstrap.urllib.request, "urlopen", return_value=_Response()) as urlopen,
            mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as run,
        ):
            result = bootstrap.main(
                [
                    "--ref",
                    "release-v1",
                    "--skill-profile",
                    "target-runtime",
                    "--clients",
                    "codex,claude",
                ]
            )

        self.assertEqual(result, 0)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            bootstrap.INSTALLER_API_TEMPLATE.format(ref="release-v1"),
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 30})
        command = run.call_args.args[0]
        self.assertNotIn("fixture-token", command)
        self.assertEqual(command[2:8], [
            "install",
            "--source-mode",
            "managed",
            "--repo-url",
            bootstrap.DEFAULT_REPO_URL,
            "--ref",
        ])
        self.assertEqual(command[8], "release-v1")
        self.assertIn("--non-interactive", command)
        self.assertEqual(command[-4:], [
            "--skill-profile",
            "target-runtime",
            "--clients",
            "codex,claude",
        ])

    def test_bootstrap_defaults_to_the_github_primary_release_source(self) -> None:
        completed = subprocess.CompletedProcess([], 0)
        with (
            mock.patch.dict(bootstrap.os.environ, {}, clear=True),
            mock.patch.object(bootstrap.urllib.request, "urlopen", return_value=_Response()) as urlopen,
            mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(bootstrap.main(["--ref", "v1.2.3"]), 0)

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/lihuabai629-star/openubmc-agent-workflow/"
            "contents/openubmc-environment-setup/scripts/install_environment.py?ref=v1.2.3",
        )
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 30})
        command = run.call_args.args[0]
        repo_index = command.index("--repo-url") + 1
        ref_index = command.index("--ref") + 1
        self.assertEqual(
            command[repo_index],
            "https://github.com/lihuabai629-star/openubmc-agent-workflow.git",
        )
        self.assertEqual(command[ref_index], "v1.2.3")

    def test_bootstrap_rejects_source_overrides_before_download(self) -> None:
        for arguments in (
            ["--ref", "v1.2.3", "--repo-url", "https://github.com/example/fork.git"],
            ["--ref", "v1.2.3", "--installer-url", "https://example.invalid/installer.py"],
            ["--ref", "v1.2.3", "--source", "/tmp/untrusted-checkout"],
            ["--ref", "v1.2.3", "--source-mode=linked"],
            ["--ref", "v1.2.3", "--source-m", "linked"],
        ):
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with (
                    mock.patch.object(bootstrap.urllib.request, "urlopen") as urlopen,
                    mock.patch.object(bootstrap.subprocess, "run") as run,
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    bootstrap.main(arguments)

                self.assertEqual(raised.exception.code, 2)
                self.assertIn("primary GitHub release source", stderr.getvalue())
                urlopen.assert_not_called()
                run.assert_not_called()

    def test_bootstrap_and_installer_share_the_release_ref_contract(self) -> None:
        cases = {
            "v1.2.3": True,
            "release-2026.08": True,
            "a" * 40: True,
            "main": False,
            "refs/heads/release": False,
            "release candidate": False,
            "release..candidate": False,
            "-release": False,
        }
        for ref, expected in cases.items():
            with self.subTest(ref=ref):
                try:
                    bootstrap.release_ref(ref)
                    bootstrap_accepts = True
                except argparse.ArgumentTypeError:
                    bootstrap_accepts = False
                try:
                    installer.release_ref_kind(ref)
                    installer_accepts = True
                except installer.SetupError:
                    installer_accepts = False

                self.assertEqual(bootstrap_accepts, expected)
                self.assertEqual(installer_accepts, expected)


if __name__ == "__main__":
    unittest.main()
