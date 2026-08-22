#!/usr/bin/env python3
"""Write digest-bound metadata for an openUBMC build artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


SCHEMA = "openubmc-agent-workflow/artifact-metadata-v1"


def artifact_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def write_metadata(
    path: Path,
    *,
    kind: str,
    product_version: str,
    provenance: str = "openubmc-build",
) -> Path:
    artifact = path.expanduser().absolute()
    if not artifact.is_file():
        raise ValueError("artifact path must name an existing file")
    normalized_kind = kind.strip()
    normalized_version = product_version.strip()
    normalized_provenance = provenance.strip()
    if not normalized_kind:
        raise ValueError("artifact kind must not be empty")
    if not normalized_version:
        raise ValueError("product version must not be empty")
    if not normalized_provenance:
        raise ValueError("artifact provenance must not be empty")
    digest, size = artifact_identity(artifact)
    destination = Path(str(artifact) + ".metadata.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA,
        "artifact": {
            "sha256": digest,
            "size": size,
            "kind": normalized_kind,
        },
        "product_version": normalized_version,
        "provenance": normalized_provenance,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name + ".",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--kind", default="openubmc-hpm")
    parser.add_argument("--product-version", required=True)
    parser.add_argument("--provenance", default="openubmc-build")
    args = parser.parse_args(argv)
    output = write_metadata(
        args.path,
        kind=args.kind,
        product_version=args.product_version,
        provenance=args.provenance,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
