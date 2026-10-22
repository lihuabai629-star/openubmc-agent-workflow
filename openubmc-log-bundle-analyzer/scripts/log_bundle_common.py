"""Shared data contracts for the openUBMC log-bundle analyzer."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from log_bundle_redaction import redact_sensitive_text


DEFAULT_ERROR_MESSAGE = "The requested operation could not be completed."


class BundleAnalysisError(RuntimeError):
    """A stable error code and user-facing message for CLI payloads."""

    def __init__(
        self,
        code: str,
        message: str,
    ) -> None:
        raw_message = str(message)
        safe_message, _ = redact_sensitive_text(raw_message)
        if not safe_message.strip():
            safe_message = DEFAULT_ERROR_MESSAGE
        super().__init__(safe_message)
        self.code = code
        # The public exception string is always safe to log.  Keep the raw
        # diagnostic only for narrowly scoped internal classification.
        self.message = safe_message
        self.raw_message = raw_message
        self.redacted_message = safe_message


@dataclass(frozen=True)
class ExtractionResult:
    extract_dir: pathlib.Path
    bundle_root: pathlib.Path
