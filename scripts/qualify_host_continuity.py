#!/usr/bin/env python3
"""Native Codex/Runtime handoff probe, using only a loopback model and fake target.

The disposable CODEX_HOME contains only this vetted test hook. No user config,
credentials, installed plugins, real provider, or BMC is used. This tests native
event semantics separately from immutable plugin-launcher qualification.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]


def runtime_imports():
    sys.path[:0] = [str(ROOT/'openubmc-target-runtime'), str(ROOT/'openubmc-target-runtime/tests')]
    from openubmc_target_runtime import JsonRpcMcpEndpoint, RuntimeMcpService, SQLiteRuntimeRepository, StdioMcpServer
    from openubmc_target_runtime.host_continuity import HostContinuity, read_runtime_projection
    from test_mcp_contracts import FakeDebugBackend
    return JsonRpcMcpEndpoint, RuntimeMcpService, SQLiteRuntimeRepository, StdioMcpServer, HostContinuity, read_runtime_projection, FakeDebugBackend


def append(root, name, value):
    with (root/name).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


def rows(root, name):
    path = root/name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def fixture(mode, root):
    Endpoint, Service, Repository, Server, Store, read_projection, Fake = runtime_imports()
    store = Store(root/'host-continuity')
    ledger = root/'context-runtime.sqlite3'
    if mode == 'hook':
        event = json.load(sys.stdin)
        result = store.handle_hook(event, read_run=lambda run: read_projection(ledger, run))
        append(root, 'hooks.jsonl', {'event': event, 'result': result})
        print(json.dumps(result, ensure_ascii=False))
        return

    class RecordingEndpoint(Endpoint):
        def handle(self, message):
            result = super().handle(message)
            if message.get('method') == 'tools/call':
                append(root, 'calls.jsonl', {'params': message['params'], 'response': result,
                                           'native_thread_env': os.environ.get('CODEX_THREAD_ID')})
            return result

    service = Service(Fake(), context_repository=Repository(ledger), host_continuity=store)
    Server(RecordingEndpoint(service, session_task_id=os.environ.get('CODEX_THREAD_ID'))).serve()


def probe(executable: Path, *, read_only_approval_probe: bool = False):
    runtime_imports()
    from openubmc_target_runtime.host_continuity import HostContinuity, read_runtime_projection

    with tempfile.TemporaryDirectory(prefix='openubmc-native-handoff-') as temporary:
        root = Path(temporary)
        home = root/'codex'
        home.mkdir()
        hook = shlex.join([sys.executable, str(Path(__file__).resolve()), 'hook', '--root', str(root)])
        (home/'hooks.json').write_text(json.dumps({'hooks': {
            name: [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 10}]}]
            for name in ('SessionStart', 'Stop')
        }}))

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                calls = rows(root, 'calls.jsonl')
                hooks = rows(root, 'hooks.jsonl')
                item = None
                if not calls:
                    action = {'kind': 'start', 'target': '192.0.2.1', 'intent': 'diagnosis-only'}
                elif len(calls) == 1:
                    turn = calls[0]['response']['result']['structuredContent']
                    gate = turn['gate']
                    action = {'kind': 'control', 'run_id': turn['run_id'], 'command': 'cancel',
                              'gate_id': gate['gate_id'], 'gate_version': gate['gate_version'],
                              'schema_digest': gate['schema_digest']}
                else:
                    action = None
                    stop = next((h for h in reversed(hooks) if h['event']['hook_event_name'] == 'Stop'), None)
                    if stop:
                        task = stop['event']['session_id']
                        handoff = HostContinuity(root/'host-continuity').handoff(task, read_run=lambda run: read_runtime_projection(root/'context-runtime.sqlite3', run))
                        answer = handoff['runs'][0]['terminal_answer']['text']
                        item = {'type': 'message', 'id': 'final-probe', 'role': 'assistant',
                                'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': answer}]}
                if action:
                    item = {'type': 'custom_tool_call', 'call_id': 'probe-'+str(len(calls)), 'name': 'exec',
                            'input': 'text(await tools.mcp__continuity_probe__execute('+json.dumps(action)+'));'}
                events = [{'type': 'response.created', 'response': {'id': 'local-continuity-probe'}}]
                if item:
                    events.append({'type': 'response.output_item.done', 'output_index': 0, 'item': item})
                events.append({'type': 'response.completed', 'response': {'id': 'local-continuity-probe',
                               'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})
                body = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('CODEX_', 'OPENAI_', 'OPENUBMC_', 'PYTHON', 'RUST_LOG'))}
        env.update(CODEX_HOME=str(home), OPENUBMC_LOCAL_PROBE_KEY='synthetic-loopback-only')
        settings = {
            'features.hooks': 'true', 'model_provider': '"continuity_probe"',
            'model_providers.continuity_probe.name': '"Loopback fixture"',
            'model_providers.continuity_probe.base_url': json.dumps(f'http://127.0.0.1:{server.server_port}/v1'),
            'model_providers.continuity_probe.env_key': '"OPENUBMC_LOCAL_PROBE_KEY"',
            'model_providers.continuity_probe.wire_api': '"responses"',
            'model_providers.continuity_probe.supports_websockets': 'false',
            'mcp_servers.continuity_probe.command': json.dumps(sys.executable),
            'mcp_servers.continuity_probe.args': json.dumps([str(Path(__file__).resolve()), 'mcp', '--root', str(root)]),
        }
        if read_only_approval_probe:
            settings['mcp_servers.continuity_probe.tools.execute.approval_mode'] = '"approve"'
        config = [part for key, value in settings.items() for part in ('-c', key+'='+value)]
        safety_flags = (['--sandbox', 'read-only', '-c', 'approval_policy="never"']
                        if read_only_approval_probe else
                        ['--dangerously-bypass-approvals-and-sandbox'])
        resume_safety_flags = (['-c', 'sandbox_mode="read-only"', '-c', 'approval_policy="never"']
                               if read_only_approval_probe else safety_flags)
        command = [str(executable), 'exec', '--json', '--skip-git-repo-check',
                   *safety_flags, '--dangerously-bypass-hook-trust',
                   '-C', str(root), '--model', 'gpt-5.6-sol', *config,
                   'Run only the synthetic continuity fixture. No real targets or network providers.']
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
            first_hooks = rows(root, 'hooks.jsonl')
            if result.returncode or not first_hooks:
                raise ValueError('native fixture failed: '+result.stderr[-1800:])
            task = first_hooks[-1]['event']['session_id']
            ledger_read = lambda run: read_runtime_projection(root/'context-runtime.sqlite3', run)
            store = HostContinuity(root/'host-continuity')
            handoff = store.handoff(task, read_run=ledger_read)
            if len(handoff['runs']) != 1 or not handoff['runs'][0].get('terminal_answer'):
                raise ValueError('native fixture has no prepared terminal answer')
            rollout_paths = list((home/'sessions').rglob('*'+task+'.jsonl'))
            if len(rollout_paths) != 1:
                raise ValueError('native fixture has no unique host rollout')
            store.acknowledge_rollout(
                task, handoff['runs'][0]['run_id'], rollout_paths[0], read_run=ledger_read,
            )
            resume_command = [str(executable), 'exec', 'resume', '--json', '--skip-git-repo-check',
                              *resume_safety_flags, '--dangerously-bypass-hook-trust',
                              '--model', 'gpt-5.6-sol', *config, task,
                              'Recover the existing result without any tool or target operation.']
            resumed = subprocess.run(resume_command, cwd=root, env=env, capture_output=True, text=True, timeout=45)
            if resumed.returncode:
                raise ValueError('native resume failed: '+resumed.stderr[-1800:])
        finally:
            server.shutdown()
            server.server_close()
        calls = rows(root, 'calls.jsonl')
        hooks = rows(root, 'hooks.jsonl')
        if result.returncode:
            raise ValueError('native fixture failed: '+result.stderr[-1800:])
        if len(calls) != 2:
            raise ValueError('expected start and cancel exactly once: '+result.stdout[-1800:])
        task = hooks[-1]['event']['session_id']
        handoff = HostContinuity(root/'host-continuity').handoff(task, read_run=lambda run: read_runtime_projection(root/'context-runtime.sqlite3', run))
        run = handoff['runs'][0] if handoff['runs'] else {}
        delivered = bool(run.get('terminal_answer', {}).get('delivery_confirmed'))
        resumed_hooks = [h for h in hooks if h['event'].get('source') == 'resume']
        restored = any('cancelled' in h['result'].get('hookSpecificOutput', {}).get('additionalContext', '')
                       for h in resumed_hooks)
        report = {'schema': 'openubmc.native-host-continuity.v1',
                  'codex': subprocess.check_output([str(executable), '--version'], text=True).strip(),
                  'mcp_call_count': len(calls), 'mcp_metadata_keys': sorted(calls[0]['params'].get('_meta', {})),
                  'native_thread_env_present': bool(calls[0]['native_thread_env']),
                  'native_thread_metadata_matches_hook': calls[0]['params'].get('_meta', {}).get('threadId') == task,
                  'terminal_state': run.get('turn', {}).get('state'),
                  'text_only_continuations': sum(h['result'].get('decision') == 'block' for h in hooks),
                  'delivery_confirmed': delivered, 'target_backend': 'synthetic',
                  'native_resume_restored_current_state': restored,
                  'plugin_launcher_tested': False, 'live_model_tested': False}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not delivered or not restored or report['text_only_continuations'] != 1:
            raise ValueError('native handoff qualification did not confirm one recovered final')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('probe', 'mcp', 'hook'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--codex', type=Path)
    parser.add_argument('--read-only-approval-probe', action='store_true')
    args = parser.parse_args()
    if args.mode == 'probe':
        if not args.codex:
            parser.error('--codex is required')
        probe(args.codex.resolve(), read_only_approval_probe=args.read_only_approval_probe)
    else:
        if not args.root:
            parser.error('--root is required')
        fixture(args.mode, args.root)
