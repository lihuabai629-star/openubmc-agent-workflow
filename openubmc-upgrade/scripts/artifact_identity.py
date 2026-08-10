#!/usr/bin/env python3
"""Re-hash one stable upgrade artifact without following symlinks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def stable_sha256(path: Path) -> dict[str, object]:
    path = path.absolute()
    before_path = os.lstat(path)
    if not stat.S_ISREG(before_path.st_mode) or stat.S_ISLNK(before_path.st_mode):
        raise ValueError(f"artifact must be a regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino):
            raise ValueError(f"artifact changed while opening: {path}")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = os.lstat(path)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, item) != getattr(after, item) for item in fields):
        raise ValueError(f"artifact changed while hashing: {path}")
    if any(getattr(after, item) != getattr(after_path, item) for item in fields):
        raise ValueError(f"artifact path changed while hashing: {path}")
    return {"path": str(path), "sha256": digest.hexdigest(), "size": after.st_size}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--expected-sha256", required=True)
    args = parser.parse_args()
    try:
        result = stable_sha256(Path(args.path))
        if result["sha256"] != args.expected_sha256.lower():
            raise ValueError("artifact SHA-256 does not match the expected value")
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
