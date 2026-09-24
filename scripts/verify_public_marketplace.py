#!/usr/bin/env python3
"""Verify that a public marketplace entry is the qualified release archive."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from .plugin_archive import read_archive, verify_directory
except ImportError:
    from plugin_archive import read_archive, verify_directory


def verify_public_marketplace(archive: Path, marketplace: Path) -> dict[str, object]:
    archive = archive.resolve()
    marketplace = marketplace.resolve()
    archive_lock, _files = read_archive(
        archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    manifest_path = marketplace/'.agents/plugins/marketplace.json'
    manifest = json.loads(manifest_path.read_bytes())
    matches = [entry for entry in manifest.get('plugins', []) if entry.get('name') == 'openubmc']
    if len(matches) != 1:
        raise ValueError('public marketplace must contain exactly one openubmc entry')
    source = matches[0].get('source', {})
    if source.get('source') != 'local' or not isinstance(source.get('path'), str):
        raise ValueError('public marketplace openubmc entry must use a local payload')
    plugin = (marketplace/source['path']).resolve()
    if not plugin.is_relative_to(marketplace):
        raise ValueError('public marketplace plugin path escapes its repository')
    public_lock = verify_directory(plugin)
    fields = ('name', 'version', 'source_commit', 'content_digest', 'manifest_digest', 'files')
    if any(public_lock.get(field) != archive_lock.get(field) for field in fields):
        raise ValueError('public marketplace inventory does not match the release archive')
    return {
        'ok': True,
        'marketplace': manifest.get('name'),
        'version': archive_lock['version'],
        'source_commit': archive_lock['source_commit'],
        'content_digest': archive_lock['content_digest'],
        'files': len(archive_lock['files']),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--marketplace', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_public_marketplace(args.archive, args.marketplace), sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
