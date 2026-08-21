#!/usr/bin/env python3
"""Validate and restore the execute A/B release evidence bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import tarfile


EXPECTED_MEMBERS = (
    "all_metrics.json",
    "run_evidence.json",
    "schedule.json",
    "summary.json",
)


def restore_bundle(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:xz") as archive:
        members = archive.getmembers()
        names = sorted(member.name for member in members)
        if names != list(EXPECTED_MEMBERS):
            raise ValueError("execute AB bundle contains unexpected members")
        if any(not member.isfile() for member in members):
            raise ValueError("execute AB bundle members must be regular files")
        destination.mkdir()
        archive.extractall(destination, members=members, filter="data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        restore_bundle(args.archive, args.destination)
    except (OSError, tarfile.TarError, ValueError) as exc:
        parser.exit(2, f"execute AB bundle error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
