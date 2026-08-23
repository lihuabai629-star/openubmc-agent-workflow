"""Shared canonical evidence-report helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


def evidence_fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def resolve_source_commit(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    value = completed.stdout.strip().lower()
    return value if _is_lower_hex(value, length=40) else "unknown"


def source_commit(value: str, *, workspace: Path) -> str:
    selected = value.strip().lower() or resolve_source_commit(workspace)
    if not _is_lower_hex(selected, length=40):
        raise ValueError("source commit must be a 40-character Git commit")
    return selected


def _is_lower_hex(value: str, *, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )
