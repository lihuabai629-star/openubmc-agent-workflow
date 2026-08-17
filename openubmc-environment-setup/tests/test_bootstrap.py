#!/usr/bin/env python3
from __future__ import annotations

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
            mock.patch.object(bootstrap.urllib.request, "urlopen", return_value=_Response()) as urlopen,
            mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as run,
        ):
            result = bootstrap.main(
                [
                    "--repo-url",
                    "https://git.example/workflow.git",
                    "--ref",
                    "release-v1",
                    "--skill-profile",
                    "target-runtime",
                    "--clients",
                    "codex,claude",
                ]
            )

        self.assertEqual(result, 0)
        urlopen.assert_called_once_with(
            bootstrap.RAW_INSTALLER_TEMPLATE.format(ref="release-v1"),
            timeout=30,
        )
        command = run.call_args.args[0]
        self.assertEqual(command[2:8], [
            "install",
            "--source-mode",
            "managed",
            "--repo-url",
            "https://git.example/workflow.git",
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
            mock.patch.object(bootstrap.urllib.request, "urlopen", return_value=_Response()) as urlopen,
            mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(bootstrap.main(["--ref", "v1.2.3"]), 0)

        urlopen.assert_called_once_with(
            "https://raw.githubusercontent.com/lihuabai629-star/"
            "openubmc-agent-workflow/v1.2.3/"
            "openubmc-environment-setup/scripts/install_environment.py",
            timeout=30,
        )
        command = run.call_args.args[0]
        repo_index = command.index("--repo-url") + 1
        ref_index = command.index("--ref") + 1
        self.assertEqual(
            command[repo_index],
            "https://github.com/lihuabai629-star/openubmc-agent-workflow.git",
        )
        self.assertEqual(command[ref_index], "v1.2.3")


if __name__ == "__main__":
    unittest.main()
