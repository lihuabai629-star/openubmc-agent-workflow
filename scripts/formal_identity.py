"""Validated JSON identities shared by formal Codex qualification entrypoints."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json


PINNED_CODEX_VERSION = "codex-cli 0.151.0"
FORMAL_MODEL_IDENTITY_FIELDS = frozenset({"model"})
FORMAL_CODEX_IDENTITY_FIELDS = frozenset({"version"})


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


def normalize_codex_identity(
    value: Mapping[str, object] | None,
    *,
    required_message: str | None = None,
) -> dict[str, object]:
    identity = normalize_identity(
        value,
        label="Codex identity",
        required_message=required_message,
    )
    if set(identity) != FORMAL_CODEX_IDENTITY_FIELDS:
        raise ValueError("formal Codex identity may only contain version")
    if identity.get("version") != PINNED_CODEX_VERSION:
        raise ValueError(
            "Codex identity version must match the pinned process: "
            f"{PINNED_CODEX_VERSION}"
        )
    return identity


def normalize_model_identity(
    value: Mapping[str, object] | None,
    *,
    required_message: str | None = None,
) -> dict[str, object]:
    identity = normalize_identity(
        value,
        label="model identity",
        required_message=required_message,
    )
    if set(identity) != FORMAL_MODEL_IDENTITY_FIELDS:
        raise ValueError("formal model identity may only contain model")
    if not isinstance(identity.get("model"), str) or not str(
        identity["model"]
    ).strip():
        raise ValueError("formal model identity must include a non-empty model")
    identity["model"] = str(identity["model"]).strip()
    return identity


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
