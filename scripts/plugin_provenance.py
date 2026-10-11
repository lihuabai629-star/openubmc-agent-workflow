#!/usr/bin/env python3
"""Link a local plugin archive to its clean source and qualification evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

try:
    from .package_plugin import build, canonical
    from .plugin_archive import read_archive
except ImportError:
    from package_plugin import build, canonical
    from plugin_archive import read_archive


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "openubmc.codex-plugin.provenance.v1"
QUALIFICATION_SCHEMA = "openubmc.codex-plugin.qualification.v1"
DEPENDENCY_LOCKS = (
    ("python_validation", "requirements-ci.lock", "requirements.lock"),
    ("knowledge_mcp", "openubmc-kb-mcp/package-lock.json", "openubmc-kb-mcp/package-lock.json"),
)
MAX_QUALIFICATION_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


def _git(source: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("source Git identity is unavailable")
    return result.stdout.strip()


def _clean_source(source: Path) -> tuple[str, str]:
    source = source.resolve()
    if Path(_git(source, "rev-parse", "--show-toplevel")).resolve() != source:
        raise ValueError("source must be the Git repository root")
    if _git(source, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("source worktree is dirty")
    return _git(source, "rev-parse", "HEAD^{commit}"), _git(source, "rev-parse", "HEAD^{tree}")


def _qualification(path: Path, *, source_commit: str, archive_sha256: str, lock: dict) -> str:
    if path.stat().st_size > MAX_QUALIFICATION_BYTES:
        raise ValueError("qualification report exceeds size limit")
    data = path.read_bytes()
    if len(data) > MAX_QUALIFICATION_BYTES:
        raise ValueError("qualification report exceeds size limit")
    try:
        report = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid qualification report") from error
    if not isinstance(report, dict) or any(
        report.get(name) != expected
        for name, expected in (
            ("schema", QUALIFICATION_SCHEMA),
            ("source_commit", source_commit),
            ("archive_sha256", archive_sha256),
            ("content_digest", lock["content_digest"]),
            ("version", lock["version"]),
        )
    ) or any(report.get(name) is not True for name in (
        "ok", "deterministic_archive", "native_install", "native_uninstall",
        "reinstall", "external_state_preserved",
    )):
        raise ValueError("qualification report does not qualify this archive")
    return hashlib.sha256(data).hexdigest()


def local_manifest(source: Path, archive: Path, qualification: Path) -> dict[str, object]:
    """Rebuild and verify exact local evidence without making a release claim."""
    source = source.resolve()
    source_commit, source_tree = _clean_source(source)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("plugin archive exceeds size limit")
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    lock, files = read_archive(archive, archive_sha256)
    if lock["source_commit"] != source_commit:
        raise ValueError("archive source commit does not match checkout")
    with tempfile.TemporaryDirectory(prefix="openubmc-provenance-") as temporary:
        rebuilt = Path(temporary) / "rebuilt.tar.gz"
        build(source, source_commit, rebuilt)
        if hashlib.sha256(rebuilt.read_bytes()).hexdigest() != archive_sha256:
            raise ValueError("archive bytes do not match the clean source")

    inventory = []
    for name, source_path, package_path in DEPENDENCY_LOCKS:
        content = (source / source_path).read_bytes()
        if files.get(package_path) != content:
            raise ValueError(f"packaged dependency lock differs from source: {name}")
        inventory.append({
            "name": name,
            "source_path": source_path,
            "package_path": package_path,
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    if _clean_source(source) != (source_commit, source_tree):
        raise ValueError("source changed during provenance verification")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != archive_sha256:
        raise ValueError("archive changed during provenance verification")
    return {
        "schema": SCHEMA,
        "claim": "local-unpublished",
        "source": {"commit": source_commit, "tree": source_tree, "clean": True},
        "package": {
            "sha256": archive_sha256,
            "content_digest": lock["content_digest"],
            "version": lock["version"],
        },
        "dependencies": {
            "inventory": inventory,
            "sha256": hashlib.sha256(canonical(inventory)).hexdigest(),
        },
        "qualification": {
            "schema": QUALIFICATION_SCHEMA,
            "sha256": _qualification(
                qualification,
                source_commit=source_commit,
                archive_sha256=archive_sha256,
                lock=lock,
            ),
        },
    }


def verify_manifest(
    source: Path, archive: Path, qualification: Path, manifest: Path
) -> dict[str, object]:
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("provenance manifest exceeds size limit")
    try:
        recorded = json.loads(manifest.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid provenance manifest") from error
    expected = local_manifest(source, archive, qualification)
    if recorded != expected:
        raise ValueError("provenance manifest does not match local evidence")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("create", "verify"))
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            if args.manifest.exists():
                raise ValueError("provenance manifest already exists")
            if args.manifest.resolve().is_relative_to(args.source.resolve()):
                raise ValueError("provenance manifest must be outside source checkout")
            document = local_manifest(args.source, args.archive, args.qualification)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            with args.manifest.open("xb") as output:
                output.write(canonical(document))
        else:
            document = verify_manifest(
                args.source, args.archive, args.qualification, args.manifest
            )
        print(json.dumps({
            "ok": True,
            "source_commit": document["source"]["commit"],
            "archive_sha256": document["package"]["sha256"],
            "manifest_sha256": hashlib.sha256(canonical(document)).hexdigest(),
        }, sort_keys=True))
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
