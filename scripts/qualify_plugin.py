#!/usr/bin/env python3
"""Qualify an immutable archive through a clean native Codex installation."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

from package_plugin import build
from plugin_archive import canonical, verify_directory

ROOT = Path(__file__).resolve().parents[1]


def command(argv: list[str], env: dict[str, str]) -> dict | list:
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=360)
    if result.returncode:
        raise ValueError('qualification command failed: '+str(argv[:3])+': '+result.stderr[-3000:])
    return json.loads(result.stdout)


class _Responses(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self) -> None:
        self.requests: list[dict] = []
        super().__init__(('127.0.0.1', 0), _ResponsesHandler)


class _ResponsesHandler(http.server.BaseHTTPRequestHandler):
    server: _Responses
    def do_POST(self) -> None:  # noqa: N802
        try:
            size = int(self.headers.get('content-length', '0'))
            request = json.loads(self.rfile.read(size))
            if isinstance(request, dict):
                self.server.requests.append(request)
        except (ValueError, json.JSONDecodeError):
            pass
        events = [{'type':'response.created','response':{'id':'plugin-probe'}}]
        if len(self.server.requests) % 2:
            events.append({'type':'response.output_item.done', 'output_index':0,
                           'item':{'type':'custom_tool_call','call_id':'plugin-check','name':'exec',
                                   'input':"text({tool_names:ALL_TOOLS.filter(t => /openubmc/.test(t.name)).map(t => t.name)}); text({observe:await tools.mcp__openubmc_target_runtime__observe({})}); text({execute:await tools.mcp__openubmc_target_runtime__execute({})});"}})
        events.append({'type':'response.completed','response':{'id':'plugin-probe',
                       'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}}})
        body = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
        self.send_response(200)
        self.send_header('content-type', 'text/event-stream')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args: object) -> None:
        return


def native_exec_probe(env: dict[str, str], root: Path, source_commit: str) -> dict:
    machine = platform.machine().lower()
    arch, target = ('arm64', 'aarch64') if machine in {'arm64','aarch64'} else ('x64','x86_64')
    executable = ROOT/f'plugin/host/node_modules/@openai/codex-linux-{arch}/vendor/{target}-unknown-linux-musl/bin/codex'
    if not executable.is_file():
        raise ValueError('run npm ci --prefix plugin/host before plugin qualification')
    if subprocess.check_output([str(executable), '--version'], text=True).strip() != 'codex-cli 0.153.4':
        raise ValueError('native probe Codex version mismatch')
    server = _Responses()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = uuid.uuid4().hex
    env = dict(env, OPENUBMC_MCP_FORMAL_RUN='1', OPENUBMC_MCP_CLIENT='codex',
               OPENUBMC_MCP_TASK_ID='plugin-qualification', OPENUBMC_MCP_SESSION_ID=session,
               OPENUBMC_MCP_SOURCE_COMMIT=source_commit, OPENUBMC_MCP_LIFECYCLE_DIR=str(root/'lifecycle'),
               OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(root/'runtime-state'), OPENUBMC_CODEX_PROBE_API_KEY='local-probe',
               OPENUBMC_MCP_MODEL_IDENTITY=json.dumps({'model':'gpt-5.6-sol'}),
               OPENUBMC_MCP_CODEX_IDENTITY=json.dumps({'version':'codex-cli 0.153.4'}))
    argv = [str(executable), 'exec', '--ephemeral', '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox',
            '--color', 'never', '--model', 'gpt-5.6-sol', '-C', str(root),
            '-c', 'features.plugins=true', '-c', 'model_provider="openubmc_plugin_probe"',
            '-c', 'model_providers.openubmc_plugin_probe.name="OpenUBMC plugin probe"',
            '-c', f'model_providers.openubmc_plugin_probe.base_url="http://127.0.0.1:{server.server_port}/v1"',
            '-c', 'model_providers.openubmc_plugin_probe.env_key="OPENUBMC_CODEX_PROBE_API_KEY"',
            '-c', 'model_providers.openubmc_plugin_probe.wire_api="responses"',
            '-c', 'model_providers.openubmc_plugin_probe.supports_websockets=false',
            'Exercise the local Runtime request validation boundary without contacting a target.']
    runs = []
    try:
        for _ in range(2):
            start = len(server.requests)
            process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = process.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise ValueError('native Codex plugin exec timed out')
            if process.returncode:
                raise ValueError('native Codex plugin exec failed: '+(stderr or stdout)[-2000:])
            outputs = {}
            for request in server.requests[start:]:
                for item in request.get('input', []):
                    if item.get('type') != 'custom_tool_call_output':
                        continue
                    for part in item.get('output', []):
                        try:
                            value = json.loads(part.get('text', ''))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(value, dict):
                            outputs.update(value)
            names = outputs.get('tool_names', [])
            if not {'mcp__openubmc_target_runtime__observe', 'mcp__openubmc_target_runtime__execute'} <= set(names):
                raise ValueError('native Codex did not discover the packaged Runtime tools')
            for name in ('observe', 'execute'):
                result = outputs.get(name, {})
                if not result.get('isError') or not isinstance(result.get('structuredContent'), dict):
                    raise ValueError('native Codex did not receive Runtime validation for '+name+': '+str(result)[:1000])
            records = [json.loads(path.read_bytes()) for path in (root/'lifecycle').glob('*.json')]
            matched = [item for item in records if item.get('parent_pid') == process.pid and item.get('session_id') == session]
            if len(matched) != 1:
                raise ValueError('native Codex did not directly own exactly one Runtime process')
            lifecycle = matched[0]
            if lifecycle.get('source_commit') != source_commit or lifecycle.get('client') != 'codex' or not lifecycle.get('formal_run') or lifecycle.get('active_requests') != 0 or lifecycle.get('lifecycle_state') != 'stopped' or lifecycle.get('exit_reason') != 'client-terminated' or not lifecycle.get('parent_identity_verified'):
                raise ValueError('native Runtime lifecycle failed to close with the selected source identity')
            if Path('/proc').joinpath(str(lifecycle['process_id'])).exists():
                raise ValueError('Runtime process survived its Codex parent')
            runs.append({'codex_pid':process.pid, 'runtime_pid':lifecycle['process_id'],
                         'source_commit':source_commit, 'session_id':session, 'exit_reason':lifecycle['exit_reason'],
                         'active_requests':0, 'tool_names':names, 'validated_calls':['observe','execute']})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {'invocations':len(runs), 'restart_verified':len(runs) == 2, 'runs':runs,
            'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
            'transport':'local-hermetic-responses', 'network_scope':'loopback'}


def qualify(source: Path, ref: str, archive: Path) -> dict:
    report = build(source, ref, archive)
    codex_version = subprocess.check_output(['codex', '--version'], text=True).strip()
    if codex_version != 'codex-cli 0.153.4':
        raise ValueError('plugin qualification requires codex-cli 0.153.4')
    with tempfile.TemporaryDirectory(prefix='openubmc-plugin-qualification-') as temporary:
        root = Path(temporary)
        home, codex = root/'home', root/'custom-codex'
        codex.mkdir()
        config = codex/'config.toml'
        config.write_text('model = "plugin-qualification"\n')
        secret = home/'.config/openubmc/credentials.env'
        secret.parent.mkdir(parents=True)
        secret.write_text('# external credentials preserved\n')
        env = dict(os.environ, CODEX_HOME=str(codex), XDG_CONFIG_HOME=str(home/'.config'),
                   XDG_DATA_HOME=str(home/'.local/share'), XDG_CACHE_HOME=str(home/'.cache'),
                   OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(root/'runtime-state'),
                   OPENUBMC_MCP_LIFECYCLE_DIR=str(root/'lifecycle'), OPENUBMC_MCP_FORMAL_RUN='0')
        for key in ('OPENUBMC_CREDENTIALS_FILE', 'NODE_OPTIONS', 'NODE_PATH', 'NPM_CONFIG_NODE_OPTIONS'):
            env.pop(key, None)
        repeated = root/'repeated.tar.gz'
        build(source, ref, repeated)
        if repeated.read_bytes() != archive.read_bytes():
            raise ValueError('archive is not deterministic')
        install_cli = [sys.executable, '-I', str(source/'scripts/install_plugin.py'), str(archive), '--sha256', report['archive_sha256'], '--home', str(home), '--codex-home', str(codex)]
        installed = command(install_cli, env)
        plugin = Path(installed['codex_install']['installedPath'])
        lock = verify_directory(plugin)
        if lock['source_commit'] != report['source_commit'] or lock['content_digest'] != report['content_digest']:
            raise ValueError('installed identity does not match archive')
        doctor = command([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'doctor'], env)
        if not doctor['startup_ready']:
            raise ValueError('installed MCP startup failed')
        servers = command(['codex', 'mcp', 'list', '--json'], env)
        for name in ('openubmc-target-runtime', 'openubmc-kb'):
            server = next(row for row in servers if row['name'] == name)
            if Path(server['transport']['cwd']).resolve() != plugin:
                raise ValueError('MCP launcher does not resolve to the installed plugin')
        native_exec = native_exec_probe(env, root, lock['source_commit'])
        admin = [sys.executable, '-I', str(source/'scripts/plugin_admin.py')]
        command([*admin, 'uninstall', '--home', str(home), '--codex-home', str(codex)], env)
        listing = command(['codex', 'plugin', 'list', '--json'], env)
        if any(row['pluginId'].startswith('openubmc@') for row in listing['installed']):
            raise ValueError('Codex kept the plugin installed after uninstall')
        if secret.read_text() != '# external credentials preserved\n' or 'plugin-qualification' not in config.read_text():
            raise ValueError('lifecycle changed external configuration')
        # Reinstall must converge even though the owned distribution source
        # and archives intentionally survive native cache removal.
        command(install_cli, env)
        report.update(schema='openubmc.codex-plugin.qualification.v1', codex=codex_version,
                      deterministic_archive=True, native_install=True, native_uninstall=True,
                      reinstall=True, external_state_preserved=True, mcp_health=doctor['mcp_health'],
                      native_codex_exec=native_exec)
        report['ok'] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--source-ref', default='HEAD')
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = qualify(args.source.resolve(), args.source_ref, args.archive.resolve())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report = {'ok':False, 'error':str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(report))
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
