#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
