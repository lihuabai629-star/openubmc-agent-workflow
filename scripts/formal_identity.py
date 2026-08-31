"""Validated JSON identities shared by formal Codex qualification entrypoints."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json


def normalize_identity(
    value: Mapping[str, object] | None,
    *,
    label: str,
    required_message: str | None = None,
) -> dict[str, object]:
    if value is None:
        raise ValueError(required_message or f"{label} is required")
    try:
        decoded = json.loads(
            json.dumps(dict(value), ensure_ascii=True, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError(required_message or f"{label} must be a non-empty object")
    return decoded


def identity_argument(value: str) -> dict[str, object]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("identity must be valid JSON") from exc
    if not isinstance(document, dict) or not document:
        raise argparse.ArgumentTypeError(
            "identity must be a non-empty JSON object"
        )
    return document
