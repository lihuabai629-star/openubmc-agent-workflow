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
    value = _resolve_git_ref(workspace, "HEAD")
    return value if _is_lower_hex(value, length=40) else "unknown"


def _resolve_git_ref(workspace: Path, ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=workspace,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip().lower() if completed.returncode == 0 else ""


def _release_lock_parent(workspace: Path, head: str) -> str:
    path = workspace / "release-lock.json"
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    source = str(lock.get("source_commit", "")).strip().lower()
    if (
        lock.get("schema") != "openubmc-agent-workflow.release-lock.v1"
        or lock.get("source_commit_policy") != "lock-finalization-parent-v1"
        or not _is_lower_hex(source, length=40)
    ):
        return ""
    parents = _resolve_git_ref(workspace, f"{head}^@").splitlines()
    return source if parents == [source] else ""


def source_commit(value: str, *, workspace: Path) -> str:
    head = resolve_source_commit(workspace)
    if head == "unknown":
        raise ValueError("workspace HEAD must resolve to a Git commit")
    selected = value.strip().lower() or head
    if not _is_lower_hex(selected, length=40):
        raise ValueError("source commit must be a 40-character Git commit")
    if selected not in {head, _release_lock_parent(workspace, head)}:
        raise ValueError(
            "source commit must match workspace HEAD or the release-lock parent"
        )
    return selected


def _is_lower_hex(value: str, *, length: int) -> bool:
    return len(value) == length and all(
        character in "0123456789abcdef" for character in value
    )
