"""Artifact content validation behind the Runtime-owned ArtifactStore boundary."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
from pathlib import Path
from urllib.parse import unquote, urlparse

from .semantic_runtime import ArtifactRef, ReferenceViolation


class LocalArtifactStore:
    """Resolve developer-local artifacts without placing their bytes in Run state."""

    @staticmethod
    def _path(handle: str) -> Path:
        parsed = urlparse(handle)
        if parsed.scheme not in {"", "file"}:
            raise ReferenceViolation(
                "ArtifactRef handle is not available from the local ArtifactStore"
            )
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                raise ReferenceViolation("ArtifactRef file handle must be local")
            return Path(unquote(parsed.path))
        return Path(handle)

    def path_for(self, reference: ArtifactRef) -> Path:
        """Resolve only the handle syntax without reading artifact content."""

        return self._path(reference.handle)

    @staticmethod
    def _digest(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def resolve(
        self,
        reference: ArtifactRef,
        *,
        expected_kinds: Iterable[str],
        expected_target: str = "",
        expected_run_id: str = "",
    ) -> Path:
        allowed = frozenset(
            str(kind).strip()
            for kind in expected_kinds
            if str(kind).strip()
        )
        if allowed and reference.kind not in allowed:
            raise ReferenceViolation(
                "ArtifactRef kind does not match the current Gate"
            )
        if expected_target and reference.target != expected_target:
            raise ReferenceViolation("ArtifactRef target does not match the Run target")
        if expected_run_id and reference.run_id != expected_run_id:
            raise ReferenceViolation("ArtifactRef run_id does not match the current Run")
        path = self._path(reference.handle)
        if not path.is_file():
            raise ReferenceViolation("ArtifactRef content is unavailable")
        actual_digest, actual_size = self._digest(path)
        if actual_digest != reference.digest:
            raise ReferenceViolation("ArtifactRef digest does not match stored content")
        if actual_size != reference.size:
            raise ReferenceViolation("ArtifactRef size does not match stored content")
        return path
