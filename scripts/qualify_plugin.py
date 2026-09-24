#!/usr/bin/env python3
"""Qualify an immutable archive through a clean native Codex installation."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import platform
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import uuid

from package_plugin import build
from plugin_archive import canonical, materialize, read_archive, verify_directory

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


def select_runtime_lifecycle(
    records: list[dict[str, object]],
    *,
    session_id: str,
    source_commit: str,
) -> dict[str, object]:
    """Select the Runtime created for one Codex-owned bootstrap invocation."""

    matched = [
        record
        for record in records
        if record.get("component") == "target-runtime"
        and record.get("client") == "codex"
        and record.get("task_id") == "plugin-qualification"
        and record.get("session_id") == session_id
        and record.get("source_commit") == source_commit
        and record.get("formal_run") is True
    ]
    if len(matched) != 1:
        raise ValueError(
            "native Codex invocation did not own exactly one supervised Runtime process"
        )
    return matched[0]


def native_exec_probe(env: dict[str, str], root: Path, source_commit: str, *, timeout: int = 180, persistent: bool = False) -> dict:
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
    argv = [str(executable), 'exec', '--json', *([] if persistent else ['--ephemeral']), '--skip-git-repo-check', '--dangerously-bypass-approvals-and-sandbox',
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
        for _ in range(1 if persistent else 2):
            start = len(server.requests)
            lifecycle_root = root / "lifecycle"
            existing_records = (
                {path.resolve() for path in lifecycle_root.glob("*.json")}
                if lifecycle_root.is_dir()
                else set()
            )
            process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                (root/f'native-{len(runs)+1}.stdout').write_text(stdout)
                (root/f'native-{len(runs)+1}.stderr').write_text(stderr)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise ValueError('native Codex plugin exec timed out')
            if process.returncode:
                raise ValueError('native Codex plugin exec failed: '+(stderr or stdout)[-2000:])
            catalog = "\n".join(part.get('text', '')
                                for item in server.requests[start].get('input', [])
                                if item.get('role') == 'developer'
                                for part in item.get('content', []) if isinstance(part, dict))
            expected_skills = ('openubmc-debug', 'openubmc-build', 'openubmc-upgrade',
                               'openubmc-environment-setup')
            if any(not re.search(r"(?m)^- openubmc:" + re.escape(name)
                                 + r": .*\(file: [^\n)]*/" + re.escape(name) + r"/SKILL\.md\)$", catalog)
                   for name in expected_skills):
                raise ValueError('packaged Skills are absent from the model catalog outside a source checkout')
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
            records = [
                json.loads(path.read_bytes())
                for path in lifecycle_root.glob("*.json")
                if path.resolve() not in existing_records
            ]
            lifecycle = select_runtime_lifecycle(
                records, session_id=session, source_commit=source_commit
            )
            if lifecycle.get('source_commit') != source_commit or lifecycle.get('client') != 'codex' or not lifecycle.get('formal_run') or lifecycle.get('active_requests') != 0 or lifecycle.get('lifecycle_state') != 'stopped' or lifecycle.get('exit_reason') != 'client-terminated' or not lifecycle.get('parent_identity_verified'):
                raise ValueError('native Runtime lifecycle failed to close: '+str({key:lifecycle.get(key) for key in ('source_commit','client','formal_run','active_requests','lifecycle_state','exit_reason','parent_identity_verified')}))
            if Path('/proc').joinpath(str(lifecycle['process_id'])).exists():
                raise ValueError('Runtime process survived its Codex parent')
            supervisor_pid = int(lifecycle['parent_pid'])
            if supervisor_pid != process.pid and Path('/proc').joinpath(str(supervisor_pid)).exists():
                raise ValueError('Runtime bootstrap supervisor survived its Codex parent')
            events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            thread_id = next(event['thread_id'] for event in events if event.get('type') == 'thread.started')
            runs.append({'thread_id': thread_id, 'codex_pid':process.pid, 'runtime_pid':lifecycle['process_id'],
                         'supervisor_pid':supervisor_pid,
                         'ownership_model':'direct' if supervisor_pid == process.pid else 'bootstrap-supervised',
                         'source_commit':source_commit, 'session_id':session, 'exit_reason':lifecycle['exit_reason'],
                         'active_requests':0, 'tool_names':names, 'validated_calls':['observe','execute'],
                         'skills_visible_outside_source': list(expected_skills)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {'invocations':len(runs), 'restart_verified':len(runs) == 2, 'runs':runs,
            'executable_sha256':hashlib.sha256(executable.read_bytes()).hexdigest(),
            'transport':'local-hermetic-responses', 'network_scope':'loopback'}


def native_resume_probe(env: dict[str, str], root: Path, plugin: Path, thread_id: str, old: Path, before: bytes) -> dict:
    """Exercise persisted thread/resume after obsolete manual launcher removal."""
    config = Path(env['CODEX_HOME'])/'config.toml'
    marketplace = plugin.parent.parent.name
    if old.exists():
        raise ValueError('old native cache must be absent before resume qualification')
    cli = [sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), '--target-plugin', 'openubmc@'+marketplace]
    check = subprocess.run([*cli, 'doctor'], env=env, capture_output=True, text=True, timeout=30)
    if check.returncode != 2 or not json.loads(check.stdout)['codex_configuration']['changes']['mcp_servers']:
        raise ValueError('obsolete override was not detected')
    preview = command([*cli, 'repair-overrides', '--preview'], env)
    repair = command([*cli, 'repair-overrides'], env)
    if not repair.get('changed') or tomllib.loads(config.read_text()) != tomllib.loads(before.decode()):
        raise ValueError('override repair did not preserve the original configuration')
    servers = command(['codex', 'mcp', 'list', '--json'], env)
    for name in ('openubmc-target-runtime', 'openubmc-kb'):
        current = next(row for row in servers if row['name'] == name)
        if Path(current['transport']['cwd']).resolve() != plugin:
            raise ValueError('repaired launcher does not use the current plugin')
    process = subprocess.Popen(['codex', 'app-server', '--stdio'], cwd=root, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    timer = threading.Timer(45, process.kill)
    timer.start()
    try:
        def call(number, method, params):
            process.stdin.write(json.dumps({'id':number, 'method':method, 'params':params})+'\n')
            process.stdin.flush()
            for line in process.stdout:
                result = json.loads(line)
                if result.get('id') == number:
                    if 'error' in result:
                        raise ValueError('native '+method+' failed: '+str(result['error']))
                    return result['result']
            raise ValueError('native app-server closed before '+method)
        call(1, 'initialize', {'clientInfo': {'name':'upgrade-qualification', 'version':'1'},
                              'capabilities': {'experimentalApi':True}})
        resumed = call(2, 'thread/resume', {'threadId':thread_id, 'cwd':str(root), 'modelProvider':'openai'})
        if resumed['thread']['id'] != thread_id:
            raise ValueError('resume returned another thread')
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        process.stdout.close()
        timer.cancel()
    return {'thread_id':thread_id, 'resume_error':None, 'transport':'native-app-server-stdio',
            'desktop_ui_verified':False, 'old_cache_absent':not old.exists(),
            'repaired_servers':preview['changes']['mcp_servers'], 'configuration_preserved':True}


BASELINE_VERSION = '2.0.17'
BASELINE_SHA256 = 'a273ec9cd09ac2fc36380d27ee421dff3ac386c8e953b86e1c0f516170e14642'


def native_upgrade_probe(root: Path, archive: Path, baseline: Path | None) -> dict:
    """Install a published baseline, persist a task, then upgrade its native cache."""
    import shutil
    import urllib.request
    workspace = root/'upgrade'
    workspace.mkdir()
    if baseline is None:
        baseline = workspace/'baseline.tar.gz'
        url = (f'https://github.com/lihuabai629-star/openubmc-codex-plugins/releases/download/'
               f'v{BASELINE_VERSION}/openubmc-v{BASELINE_VERSION}-codex.tar.gz')
        with urllib.request.urlopen(url, timeout=60) as response:
            baseline.write_bytes(response.read(4*1024*1024))
    old_lock, old_files = read_archive(baseline, BASELINE_SHA256)
    new_lock, new_files = read_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest())
    home = workspace/'home'; codex = workspace/'codex'; market = workspace/'market'
    codex.mkdir(); source = market/'plugins/openubmc'
    manifest = market/'.agents/plugins/marketplace.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(canonical({'name':'upgrade-test','plugins':[{'name':'openubmc','source':{'source':'local','path':'./plugins/openubmc'}}]}))
    materialize(source, old_files)
    env = {key:value for key,value in os.environ.items() if not key.startswith(('CODEX_', 'OPENAI_', 'OPENUBMC_', 'XDG_'))}
    env.update(HOME=str(home), CODEX_HOME=str(codex), XDG_CONFIG_HOME=str(home/'.config'),
               XDG_DATA_HOME=str(home/'data'), XDG_CACHE_HOME=str(home/'cache'))
    command(['codex','plugin','marketplace','add',str(market),'--json'],env)
    installed = command(['codex','plugin','add','openubmc@upgrade-test','--json'],env)
    old_plugin = Path(installed['installedPath'])
    command([sys.executable,'-I',str(old_plugin/'scripts/pluginctl.py'),'prepare'],env)
    persisted = native_exec_probe(env,workspace,old_lock['source_commit'],persistent=True)
    config = codex/'config.toml'; before = config.read_bytes()
    overrides = ''
    for name, capability in [('openubmc-target-runtime','runtime'),('openubmc-kb','kb')]:
        overrides += (f'\n[mcp_servers.{name}]\ncommand="python3"\n'
                      f'args={json.dumps(["-I",str(old_plugin/"scripts/pluginctl.py"),capability])}\n')
    config.write_bytes(before + overrides.encode())
    shutil.rmtree(source)
    materialize(source,new_files)
    updated = command(['codex','plugin','add','openubmc@upgrade-test','--json'],env)
    plugin = Path(updated['installedPath'])
    if plugin == old_plugin or updated['version'] != new_lock['version']:
        raise ValueError('native upgrade did not select a new version')
    native_removed = not old_plugin.exists()
    if old_plugin.exists():
        shutil.rmtree(old_plugin)  # Explicitly exercise cache garbage collection.
    command([sys.executable,'-I',str(plugin/'scripts/pluginctl.py'),'prepare'],env)
    result = native_resume_probe(env,workspace,plugin,persisted['runs'][0]['thread_id'],old_plugin,before)
    result.update(from_version=old_lock['version'],to_version=new_lock['version'],
                  baseline_archive_sha256=BASELINE_SHA256,native_upgrade=True,
                  old_cache_removed_by='native-client' if native_removed else 'qualification-gc')
    return result


def qualify(source: Path, ref: str, archive: Path, baseline: Path | None = None) -> dict:
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
        env = dict(os.environ, HOME=str(home), CODEX_HOME=str(codex), XDG_CONFIG_HOME=str(home/'.config'),
                   XDG_DATA_HOME=str(home/'.local/share'), XDG_CACHE_HOME=str(home/'.cache'),
                   OPENUBMC_TARGET_RUNTIME_STATE_DIR=str(root/'runtime-state'),
                   OPENUBMC_MCP_LIFECYCLE_DIR=str(root/'lifecycle'), OPENUBMC_MCP_FORMAL_RUN='0')
        for key in ('OPENUBMC_CREDENTIALS_FILE', 'NODE_OPTIONS', 'NODE_PATH', 'NPM_CONFIG_NODE_OPTIONS'):
            env.pop(key, None)
        repeated = root/'repeated.tar.gz'
        build(source, ref, repeated)
        if repeated.read_bytes() != archive.read_bytes():
            raise ValueError('archive is not deterministic')
        bootstrap = root/'bootstrap'/'openubmc'
        _, bootstrap_files = read_archive(archive, report['archive_sha256'])
        materialize(bootstrap, bootstrap_files)
        install_cli = [sys.executable, '-I', str(bootstrap/'scripts/install_plugin.py'), str(archive), '--sha256', report['archive_sha256'], '--home', str(home), '--codex-home', str(codex)]
        installed = command(install_cli, env)
        verify_directory(bootstrap)
        plugin = Path(installed['codex_install']['installedPath'])
        lock = verify_directory(plugin)
        if lock['source_commit'] != report['source_commit'] or lock['content_digest'] != report['content_digest']:
            raise ValueError('installed identity does not match archive')
        doctor = command([sys.executable, '-I', str(plugin/'scripts/pluginctl.py'), 'doctor'], env)
        if not doctor['startup_ready']:
            raise ValueError('installed MCP startup failed')
        # Exercise incidental Python caches in the installed product, then use
        # doctor to initialize and list tools from both real MCP servers again.
        import compileall
        if not compileall.compile_dir(str(plugin), quiet=2, force=True):
            raise ValueError('installed Python cache generation failed')
        generated_caches = list(plugin.rglob('__pycache__/*.pyc'))
        if not generated_caches:
            raise ValueError('cache restart qualification generated no bytecode')
        plugin_cli = [sys.executable, '-I', str(plugin/'scripts/pluginctl.py')]
        command([*plugin_cli, 'verify'], env)
        restarted = command([*plugin_cli, 'doctor'], env)
        if not restarted['startup_ready'] or not all(
                restarted['mcp_health'][name]['ok'] for name in ('runtime', 'kb')):
            raise ValueError('MCP restart after cache generation failed')
        command([*plugin_cli, 'verify'], env)
        report['bytecode_restart'] = {
            'generated_cache_count': len(generated_caches),
            'mcp_health': restarted['mcp_health'], 'verified_after_restart': True,
        }
        servers = command(['codex', 'mcp', 'list', '--json'], env)
        for name in ('openubmc-target-runtime', 'openubmc-kb'):
            server = next(row for row in servers if row['name'] == name)
            if Path(server['transport']['cwd']).resolve() != plugin:
                raise ValueError('MCP launcher does not resolve to the installed plugin')
        native_exec = native_exec_probe(env, root, lock['source_commit'])
        resume = native_upgrade_probe(root, archive, baseline)
        distribution = Path(installed['source'])
        admin = [sys.executable, '-I', str(distribution/'scripts/plugin_admin.py')]
        audit = command([*admin, 'audit', '--home', str(home), '--codex-home', str(codex)], env)
        if not audit['active']['consistent'] or audit['active']['source_commit'] != lock['source_commit']:
            raise ValueError('packaged administration reported an inconsistent installation')
        verify_directory(distribution)
        command([*admin, 'uninstall', '--home', str(home), '--codex-home', str(codex)], env)
        listing = command(['codex', 'plugin', 'list', '--json'], env)
        if any(row['pluginId'].startswith('openubmc@') for row in listing['installed']):
            raise ValueError('Codex kept the plugin installed after uninstall')
        if secret.read_text() != '# external credentials preserved\n' or 'plugin-qualification' not in config.read_text():
            raise ValueError('lifecycle changed external configuration')
        # Reinstall must converge even though the owned distribution source
        # and archives intentionally survive native cache removal.
        command(install_cli, env)
        verify_directory(bootstrap)
        verify_directory(distribution)
        report.update(schema='openubmc.codex-plugin.qualification.v1', codex=codex_version,
                      deterministic_archive=True, native_install=True, native_uninstall=True,
                      reinstall=True, external_state_preserved=True, mcp_health=doctor['mcp_health'],
                      native_codex_exec=native_exec, native_thread_resume=resume)
        report['ok'] = True
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT)
    parser.add_argument('--source-ref', default='HEAD')
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-archive', type=Path)
    args = parser.parse_args()
    try:
        report = qualify(args.source.resolve(), args.source_ref, args.archive.resolve(), args.baseline_archive)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report = {'ok':False, 'error':str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(report))
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
