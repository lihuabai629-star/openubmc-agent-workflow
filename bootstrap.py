#!/usr/bin/env python3
"""Bootstrap the managed openUBMC agent workflow without a preliminary clone."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request


DEFAULT_REPO_URL = "http://10.121.177.79/liqinghua/openubmc-agent-workflow.git"
DEFAULT_REF = "main"
RAW_INSTALLER_TEMPLATE = (
    "http://10.121.177.79/liqinghua/openubmc-agent-workflow/-/raw/"
    "{ref}/openubmc-environment-setup/scripts/install_environment.py"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--installer-url")
    parser.add_argument("--interactive", action="store_true")
    known, remaining = parser.parse_known_args(argv)
    installer_url = known.installer_url or RAW_INSTALLER_TEMPLATE.format(ref=known.ref)
    try:
        with urllib.request.urlopen(installer_url, timeout=30) as response:
            installer = response.read()
    except OSError as error:
        print(f"error: unable to download installer: {error}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="openubmc-bootstrap-") as directory:
        path = Path(directory) / "install_environment.py"
        path.write_bytes(installer)
        command = [
            sys.executable,
            str(path),
            "install",
            "--source-mode",
            "managed",
            "--repo-url",
            known.repo_url,
            "--ref",
            known.ref,
        ]
        if not known.interactive and "--non-interactive" not in remaining:
            command.append("--non-interactive")
        command.extend(remaining)
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
