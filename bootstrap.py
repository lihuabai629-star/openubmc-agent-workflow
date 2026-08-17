#!/usr/bin/env python3
"""Bootstrap the managed openUBMC agent workflow without a preliminary clone."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request


DEFAULT_REPO_URL = "https://github.com/lihuabai629-star/openubmc-agent-workflow.git"
RAW_INSTALLER_TEMPLATE = (
    "https://raw.githubusercontent.com/lihuabai629-star/openubmc-agent-workflow/"
    "{ref}/openubmc-environment-setup/scripts/install_environment.py"
)
FULL_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
MUTABLE_REFS = frozenset({"head", "main", "master", "develop", "development", "trunk"})


def release_ref(value: str) -> str:
    candidate = value.strip()
    lowered = candidate.lower()
    if FULL_COMMIT.fullmatch(candidate):
        return candidate
    if (
        not candidate
        or lowered in MUTABLE_REFS
        or lowered.startswith("refs/heads/")
        or candidate.startswith("-")
        or candidate.endswith(("/", ".", ".lock"))
        or ".." in candidate
        or "@{" in candidate
        or any(character.isspace() or character in "~^:?*[\\" for character in candidate)
    ):
        raise argparse.ArgumentTypeError(
            "--ref must name an explicit release tag or full commit"
        )
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--ref", type=release_ref)
    parser.add_argument("--installer-url")
    parser.add_argument("--interactive", action="store_true")
    known, remaining = parser.parse_known_args(argv)
    if known.ref is None:
        parser.error("--ref must name an explicit release tag or full commit")
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
