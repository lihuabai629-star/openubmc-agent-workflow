#!/usr/bin/env python3
"""Bootstrap the managed openUBMC agent workflow without a preliminary clone."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_REPO_URL = "https://github.com/lihuabai629-star/openubmc-agent-workflow.git"
INSTALLER_API_TEMPLATE = (
    "https://api.github.com/repos/lihuabai629-star/openubmc-agent-workflow/contents/"
    "openubmc-environment-setup/scripts/install_environment.py?ref={ref}"
)
CLIENT_CONFIG_API_TEMPLATE = (
    "https://api.github.com/repos/lihuabai629-star/openubmc-agent-workflow/contents/"
    "openubmc-environment-setup/scripts/client_config.py?ref={ref}"
)
INSTALLER_ASSETS = {
    "install_environment.py": INSTALLER_API_TEMPLATE,
    "client_config.py": CLIENT_CONFIG_API_TEMPLATE,
}
LEGACY_OPTIONAL_ASSETS = frozenset({"client_config.py"})
FULL_COMMIT = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
MUTABLE_REFS = frozenset({"head", "main", "master", "develop", "development", "trunk"})
FORBIDDEN_FORWARDED_OPTIONS = frozenset(
    {"--installer-url", "--ref", "--repo-url", "--source", "--source-mode"}
)


def github_token() -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token
    return None


def github_file_request(template: str, ref: str) -> urllib.request.Request:
    url = template.format(ref=urllib.parse.quote(ref, safe=""))
    headers = {
        "Accept": "application/vnd.github.raw",
        "User-Agent": "openubmc-agent-workflow-bootstrap",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


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
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help=argparse.SUPPRESS)
    parser.add_argument("--ref", type=release_ref)
    parser.add_argument("--installer-url", help=argparse.SUPPRESS)
    parser.add_argument("--interactive", action="store_true")
    known, remaining = parser.parse_known_args(argv)
    if known.ref is None:
        parser.error("--ref must name an explicit release tag or full commit")
    if known.repo_url != DEFAULT_REPO_URL or known.installer_url is not None:
        parser.error(
            "bootstrap must use the primary GitHub release source; "
            "--repo-url and --installer-url overrides are unsupported"
        )
    blocked = next(
        (
            argument.split("=", 1)[0]
            for argument in remaining
            if (
                argument.split("=", 1)[0] in FORBIDDEN_FORWARDED_OPTIONS
                or argument.split("=", 1)[0].startswith("--source-")
            )
        ),
        None,
    )
    if blocked is not None:
        parser.error(
            "bootstrap must use the primary GitHub release source; "
            f"forwarding {blocked} is unsupported"
        )
    assets: dict[str, bytes] = {}
    for name, template in INSTALLER_ASSETS.items():
        request = github_file_request(template, known.ref)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                assets[name] = response.read()
        except OSError as error:
            if (
                isinstance(error, urllib.error.HTTPError)
                and error.code == 404
                and name in LEGACY_OPTIONAL_ASSETS
            ):
                continue
            hint = (
                "; authenticate private GitHub access with GH_TOKEN or GITHUB_TOKEN"
                if github_token() is None
                else ""
            )
            print(
                f"error: unable to download {name}: {error}{hint}",
                file=sys.stderr,
            )
            return 2
    with tempfile.TemporaryDirectory(prefix="openubmc-bootstrap-") as directory:
        path = Path(directory) / "install_environment.py"
        for name, content in assets.items():
            (Path(directory) / name).write_bytes(content)
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
