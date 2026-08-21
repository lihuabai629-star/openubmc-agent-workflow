"""Artifact content validation behind the Runtime-owned ArtifactStore boundary."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
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

    @staticmethod
    def metadata_path(path: Path) -> Path:
        return Path(str(path) + ".metadata.json")

    @classmethod
    def _validate_version_metadata(
        cls,
        path: Path,
        reference: ArtifactRef,
        *,
        actual_digest: str,
        actual_size: int,
    ) -> None:
        if not reference.version:
            return
        metadata_path = cls.metadata_path(path)
        if not metadata_path.is_file():
            raise ReferenceViolation(
                "versioned ArtifactRef requires build artifact metadata"
            )
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReferenceViolation(
                "build artifact metadata is unreadable"
            ) from exc
        if not isinstance(metadata, dict) or metadata.get("schema") != (
            "openubmc-agent-workflow/artifact-metadata-v1"
        ):
            raise ReferenceViolation("build artifact metadata schema is unsupported")
        artifact = metadata.get("artifact")
        if not isinstance(artifact, dict):
            raise ReferenceViolation("build artifact metadata omits artifact identity")
        if str(artifact.get("sha256", "")).removeprefix("sha256:") != actual_digest:
            raise ReferenceViolation(
                "build artifact metadata digest does not match stored content"
            )
        size = artifact.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size != actual_size:
            raise ReferenceViolation(
                "build artifact metadata size does not match stored content"
            )
        if str(artifact.get("kind", "")) != reference.kind:
            raise ReferenceViolation(
                "build artifact metadata kind does not match ArtifactRef"
            )
        if str(metadata.get("product_version", "")) != reference.version:
            raise ReferenceViolation(
                "ArtifactRef version does not match artifact metadata"
            )
        if str(metadata.get("provenance", "")) != reference.provenance:
            raise ReferenceViolation(
                "ArtifactRef provenance does not match artifact metadata"
            )

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
        self._validate_version_metadata(
            path,
            reference,
            actual_digest=actual_digest,
            actual_size=actual_size,
        )
        return path
