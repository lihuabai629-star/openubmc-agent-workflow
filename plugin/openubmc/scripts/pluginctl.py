#!/usr/bin/env python3
"""Verify and operate an installed OpenUBMC Codex plugin."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

ROOT = Path(__file__).resolve().parents[1]


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n').encode()


def verify(root: Path = ROOT) -> tuple[dict, dict[str, bytes]]:
    lock_path = root/'plugin-lock.json'
    if root.is_symlink() or lock_path.is_symlink():
        raise ValueError('plugin root and lock must not be symbolic links')
    lock = json.loads(lock_path.read_bytes())
    unsigned = dict(lock)
    digest = unsigned.pop('content_digest', None)
    if digest != hashlib.sha256(canonical(unsigned)).hexdigest():
        raise ValueError('plugin lock digest mismatch')
    if lock.get('schema') != 'openubmc.codex-plugin.v1':
        raise ValueError('unsupported plugin lock schema')
    content = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('plugin contains symbolic link: ' + str(path.relative_to(root)))
        if path.is_file() and path != lock_path:
            content[path.relative_to(root).as_posix()] = path.read_bytes()
    if {name: hashlib.sha256(data).hexdigest() for name, data in content.items()} != lock['files']:
        raise ValueError('plugin file inventory mismatch; reinstall the verified archive')
    for name in content:
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('invalid plugin member')
    manifest = json.loads(content['.codex-plugin/plugin.json'])
    if manifest['version'] != lock['version'] or manifest['name'] != lock['name']:
        raise ValueError('plugin manifest identity mismatch')
    return lock, content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify'])
    parser.parse_args()
    try:
        lock, _ = verify()
        print(json.dumps({'ok': True, 'source_commit': lock['source_commit'], 'version': lock['version'],
                          'content_digest': lock['content_digest'], 'skills': lock['skills']}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
