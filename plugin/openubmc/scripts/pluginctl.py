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

sys.dont_write_bytecode = True
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
    if lock.get('manifest_digest') != hashlib.sha256(content['.codex-plugin/plugin.json']).hexdigest():
        raise ValueError('plugin manifest digest mismatch')
    return lock, content


def dependency_root(content: dict[str, bytes]) -> Path:
    node = subprocess.run(['node', '--version'], env=node_environment(), check=True, capture_output=True, text=True).stdout.strip()
    if int(node.removeprefix('v').split('.')[0]) < 20:
        raise ValueError('Node 20 or newer is required')
    identity = {'schema': 'isolated-target.v1', 'python': sys.version, 'machine': platform.machine(), 'platform': sys.platform, 'node': node,
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
        env = node_environment()
        commands = [
            [sys.executable, '-I', '-B', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--only-binary=:all:', '--require-hashes', '--target', str(root/'python-packages'), '-r', str(root/'requirements.lock')],
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


def execution_snapshot(content: dict[str, bytes], lock: dict, dependencies: Path) -> Path:
    record = check_dependencies(dependencies)
    expected = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'mode': 0o500 if name.endswith('.sh') else 0o400}
                for name, data in content.items()}
    dependency_paths = {}
    for name, identity in record['files'].items():
        if name.startswith('python-packages/'):
            destination = name
        elif name.startswith('knowledge/node_modules/'):
            destination = 'openubmc-kb-mcp/'+name.removeprefix('knowledge/')
        else:
            continue
        expected[destination] = identity
        dependency_paths[destination] = name
    key = hashlib.sha256(canonical({'plugin': lock['content_digest'], 'files': expected})).hexdigest()
    cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache')/'openubmc/plugin-executions'
    cache.mkdir(parents=True, exist_ok=True)
    snapshot = cache/key
    with (cache/(key+'.lock')).open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        if not snapshot.exists():
            with tempfile.TemporaryDirectory(prefix='.prepare-', dir=cache) as temporary:
                stage = Path(temporary)/'snapshot'
                stage.mkdir()
                for name, identity in expected.items():
                    path = stage/name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if 'link' in identity:
                        path.symlink_to(identity['link'])
                    else:
                        data = content[name] if name in content else (dependencies/dependency_paths[name]).read_bytes()
                        if hashlib.sha256(data).hexdigest() != identity['sha256']:
                            raise ValueError('Dependency cache changed during snapshot creation')
                        path.write_bytes(data)
                        path.chmod(identity['mode'])
                if dependency_inventory(stage) != expected:
                    raise ValueError('Execution snapshot inventory mismatch')
                stage.rename(snapshot)
        if snapshot.is_symlink() or dependency_inventory(snapshot) != expected:
            raise ValueError('Execution snapshot drift; remove the damaged execution cache and retry')
    return snapshot


def node_environment() -> dict[str, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for key in tuple(env):
        if key.upper() in {'NODE_OPTIONS', 'NODE_PATH', 'NPM_CONFIG_NODE_OPTIONS'}:
            env.pop(key)
    return env


def probe_server(command: str, content: dict[str, bytes], lock: dict) -> dict[str, object]:
    """Perform a bounded MCP initialize/tools/list probe through the public launcher."""
    request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                          'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                                     'clientInfo': {'name': 'openubmc-plugin-doctor', 'version': lock['version']}}}) + '\n'
    request += json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n'
    request += json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})+'\n'
    probe_env = node_environment()
    probe_env['OPENUBMC_MCP_FORMAL_RUN'] = '0'
    probe_env['OPENUBMC_MCP_PARENT_PID'] = str(os.getpid())
    process = subprocess.Popen([sys.executable, '-I', str(ROOT/'scripts/pluginctl.py'), command],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=probe_env)
    try:
        stdout, stderr = process.communicate(request, timeout=12)
    except subprocess.TimeoutExpired:
        process.kill(); stdout, stderr = process.communicate()
    messages = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict): messages.append(value)
    initialized = any(value.get('id') == 1 and isinstance(value.get('result'), dict) for value in messages)
    server = next((value['result'].get('serverInfo') for value in messages if value.get('id') == 1 and isinstance(value.get('result'), dict)), {})
    names = {tool.get('name') for value in messages if value.get('id') == 2
             for tool in value.get('result', {}).get('tools', [])}
    expected = {'observe', 'execute'} if command == 'runtime' else {'openubmc_kb_query', 'openubmc_kb_status', 'openubmc_kb_list'}
    ok = initialized and names == expected and process.returncode == 0
    return {'ok': ok, 'server': server, 'tools': sorted(names), 'stderr': stderr[-1000:] if not ok else ''}


def launch(command: str, content: dict[str, bytes], lock: dict) -> int:
    dependencies = dependency_root(content)
    if not (dependencies/'receipt.json').is_file():
        raise ValueError('Dependencies are not prepared; run pluginctl.py prepare')
    with (dependencies.parent/(dependencies.name+'.lock')).open('a') as mutex:
        fcntl.flock(mutex, fcntl.LOCK_SH)
        snapshot = execution_snapshot(content, lock, dependencies)
    env = node_environment()
    env['OPENUBMC_MCP_SOURCE_COMMIT'] = lock['source_commit']
    env['OPENUBMC_PLUGIN_CONTENT_DIGEST'] = lock['content_digest']
    if command == 'runtime':
        argv = [sys.executable, '-I', '-B', '-c',
                'import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_path(sys.argv[2],run_name="__main__")',
                str(snapshot/'python-packages'), str(snapshot/'scripts/launch_runtime.py')]
    else:
        argv = ['node', str(snapshot/'openubmc-kb-mcp/src/server.js')]
    # Preserve the direct Codex parent across launch. Execution caches contain
    # only verified release/dependency bytes and persist independently of state.
    os.execvpe(argv[0], argv, env)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['verify', 'prepare', 'doctor', 'runtime', 'kb', 'migrate', 'restore-legacy'])
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--codex-home', type=Path)
    parser.add_argument('--transaction', default='')
    parser.add_argument('--repair', action='store_true', help='Recreate a damaged dependency cache')
    args = parser.parse_args()
    try:
        lock, content = verify()
        report = {'ok': True, 'source_commit': lock['source_commit'], 'version': lock['version'],
                  'content_digest': lock['content_digest'], 'skills': lock['skills']}
        if args.command in ('migrate', 'restore-legacy'):
            import types
            module = types.ModuleType('openubmc_plugin_install')
            exec(compile(content['scripts/plugin_install.py'], '<verified-plugin-install>', 'exec'), module.__dict__)
            skill_paths = [item['path'] for item in json.loads(content['workflow.json'])['skills']]
            result = module.migrate(args.home, skill_paths, args.codex_home) if args.command == 'migrate' else module.restore(args.home, args.transaction, args.codex_home)
            print(json.dumps(result, sort_keys=True)); return 0
        if args.command == 'prepare':
            root = prepare_dependencies(content, args.repair)
            report['dependencies'] = str(root)
        elif args.command in ('runtime', 'kb'):
            return launch(args.command, content, lock)
        elif args.command == 'doctor':
            report['package_integrity'] = True
            try:
                check_dependencies(dependency_root(content))
                report['dependencies_ready'] = True
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                report['dependencies_ready'] = False
                report['error'] = str(error)
            if report['dependencies_ready']:
                report['mcp_health'] = {name: probe_server(name, content, lock) for name in ('runtime', 'kb')}
            else:
                report['mcp_health'] = {'runtime': {'ok': False, 'error': 'dependencies unavailable'}, 'kb': {'ok': False, 'error': 'dependencies unavailable'}}
            report['credentials_configured'] = bool(os.environ.get('OPENUBMC_CREDENTIALS_FILE') or (Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'openubmc/credentials.env').is_file())
            report['startup_ready'] = report['dependencies_ready'] and all(item.get('ok') for item in report['mcp_health'].values())
            report['ok'] = report['startup_ready']
        print(json.dumps(report, sort_keys=True))
        return 0 if report['ok'] else 2
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
