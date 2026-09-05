#!/usr/bin/env python3
"""Verify and operate an installed OpenUBMC Codex plugin."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import os
import fcntl
import platform
import shutil
import tempfile
import subprocess
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


def dependency_root(content: dict[str, bytes]) -> Path:
    node = subprocess.run(['node', '--version'], check=True, capture_output=True, text=True).stdout.strip()
    if int(node.removeprefix('v').split('.')[0]) < 20:
        raise ValueError('Node 20 or newer is required')
    identity = {'python': sys.version, 'machine': platform.machine(), 'platform': sys.platform, 'node': node,
                'python_lock': hashlib.sha256(content['requirements.lock']).hexdigest(),
                'node_lock': hashlib.sha256(content['openubmc-kb-mcp/package-lock.json']).hexdigest()}
    key = hashlib.sha256(canonical(identity)).hexdigest()
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')
    return data/'openubmc/plugin-dependencies'/key


def dependency_inventory(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if relative == 'receipt.json':
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('dependency symlink escapes its cache')
            result[relative] = {'link': os.readlink(path)}
        elif path.is_file():
            result[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'mode': path.stat().st_mode & 0o777}
    return result


def check_dependencies(root: Path) -> dict:
    receipt = root/'receipt.json'
    if not receipt.is_file():
        raise ValueError('Dependencies are not prepared; run pluginctl.py prepare')
    if root.is_symlink() or receipt.is_symlink():
        raise ValueError('Dependency cache must not be a symbolic link')
    record = json.loads(receipt.read_bytes())
    if record.get('schema') != 'openubmc.plugin-dependencies.v1' or record.get('files') != dependency_inventory(root):
        raise ValueError('Dependency cache drift; run pluginctl.py prepare --repair')
    return record


def prepare_dependencies(content: dict[str, bytes], repair: bool) -> Path:
    root = dependency_root(content)
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent/(root.name+'.lock')).open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        if (root/'receipt.json').exists() and not repair:
            check_dependencies(root)
            return root
        if root.exists():
            if root.is_symlink():
                raise ValueError('Dependency cache must not be a symbolic link')
            # Only this lock-addressed, plugin-owned directory can be repaired.
            shutil.rmtree(root)
        root.mkdir(mode=0o700)
        (root/'requirements.lock').write_bytes(content['requirements.lock'])
        knowledge = root/'knowledge'
        knowledge.mkdir()
        for name in ('package.json', 'package-lock.json'):
            (knowledge/name).write_bytes(content['openubmc-kb-mcp/'+name])
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        commands = [
            [sys.executable, '-I', '-B', '-m', 'venv', '--copies', str(root/'python')],
            [str(root/'python/bin/python'), '-I', '-B', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--only-binary=:all:', '--require-hashes', '-r', str(root/'requirements.lock')],
            ['npm', 'ci', '--ignore-scripts', '--omit=dev', '--no-audit', '--no-fund', '--prefix', str(knowledge)],
        ]
        for command in commands:
            result = subprocess.run(command, env=env, stdout=sys.stderr, stderr=sys.stderr, check=False)
            if result.returncode:
                raise ValueError('Dependency preparation failed; retry pluginctl.py prepare')
        # Dependency identity covers the complete installed content, including
        # Node production dependencies; no package writes occur during startup.
        record = {'schema': 'openubmc.plugin-dependencies.v1', 'files': dependency_inventory(root)}
        (root/'receipt.json').write_bytes(canonical(record))
        check_dependencies(root)
    return root


def launch_locked(command: str, content: dict[str, bytes], dependencies: Path) -> int:
    check_dependencies(dependencies)
    # Freeze the verified package bytes before executing a path-based entrypoint.
    # Runtime's existing launcher then verifies and freezes its composition.
    with tempfile.TemporaryDirectory(prefix='openubmc-plugin-execution-') as temporary:
        snapshot = Path(temporary)
        for name, data in content.items():
            path = snapshot/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o500 if name.endswith('.sh') else 0o400)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        if command == 'runtime':
            argv = [str(dependencies/'python/bin/python'), '-I', '-B', str(snapshot/'scripts/launch_runtime.py')]
        else:
            # Source and installed dependency content are individually verified.
            knowledge = snapshot/'openubmc-kb-mcp'
            shutil.copytree(dependencies/'knowledge/node_modules', knowledge/'node_modules', symlinks=True)
            expected = json.loads((dependencies/'receipt.json').read_bytes())['files']
            copied = {'knowledge/'+key: value for key, value in dependency_inventory(knowledge).items() if key.startswith('node_modules/')}
            if copied != {key: value for key, value in expected.items() if key.startswith('knowledge/node_modules/')}:
                raise ValueError('Dependency cache changed during startup')
            argv = ['node', str(knowledge/'src/server.js')]
        process = subprocess.Popen(argv, env=env)
        import signal
        previous = {}
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.signal(signum, lambda signum, frame: process.send_signal(signum))
        try:
            return process.wait()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def launch(command: str, content: dict[str, bytes], dependencies: Path) -> int:
    if not (dependencies/'receipt.json').is_file():
        raise ValueError('Dependencies are not prepared; run pluginctl.py prepare')
    with (dependencies.parent/(dependencies.name+'.lock')).open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_SH)
        return launch_locked(command, content, dependencies)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify', 'prepare', 'doctor', 'runtime', 'kb'])
    parser.add_argument('--repair', action='store_true', help='Recreate a damaged dependency cache')
    args = parser.parse_args()
    try:
        lock, content = verify()
        report = {'ok': True, 'source_commit': lock['source_commit'], 'version': lock['version'],
                  'content_digest': lock['content_digest'], 'skills': lock['skills']}
        if args.command == 'prepare':
            root = prepare_dependencies(content, args.repair)
            report['dependencies'] = str(root)
        elif args.command in ('runtime', 'kb'):
            return launch(args.command, content, dependency_root(content))
        elif args.command == 'doctor':
            report['package_integrity'] = True
            try:
                check_dependencies(dependency_root(content))
                report['dependencies_ready'] = True
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                report['dependencies_ready'] = False
                report['error'] = str(error)
            report['operational_ready'] = report['dependencies_ready']
            report['ok'] = report['operational_ready']
        print(json.dumps(report, sort_keys=True))
        return 0 if report['ok'] else 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
