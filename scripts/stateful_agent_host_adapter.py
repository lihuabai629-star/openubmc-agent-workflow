#!/usr/bin/env python3
"""Native Codex trials against persisted, target-free Runtime fixtures."""

from __future__ import annotations

import argparse
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
from collections.abc import Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOST_INSTRUCTIONS = (
    "You are an Agent in a pinned target-free Runtime evaluation. Follow the scenario "
    "using only the provided MCP tool. Preserve Run, Gate and Effect identities. "
    "Report only Runtime-verified status and explicit gaps. Never claim a delivery "
    "stage beyond Runtime evidence. Every completed turn must end with a single JSON object "
    "with exactly run_id, status and delivery_stage. Use Outcome status when recorded, otherwise "
    "the returned Turn state; delivery_stage must be closeout.delivery_stage.highest or unverified. "
    "No prose or markdown in final answers."
)
_OWNED_GROUPS: set[int] = set()
_CANCELLED = False


def _terminate_owned_groups() -> None:
    for pid in tuple(_OWNED_GROUPS):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _cancel_adapter(_signal: int, _frame: object) -> None:
    global _CANCELLED
    _CANCELLED = True
    _terminate_owned_groups()
sys.path[:0] = [str(ROOT), str(ROOT / "openubmc-target-runtime"),
                str(ROOT / "openubmc-target-runtime" / "tests")]

from openubmc_target_runtime import (  # noqa: E402
    FilesystemBlobRepository, JsonRpcMcpEndpoint, RuntimeMcpService,
    SQLiteRuntimeRepository, StdioMcpServer,
)
from openubmc_target_runtime.terminal_delivery import (  # noqa: E402
    TerminalAnswerStore, TerminalAnswerError, audit_rollout_final, qualify_terminal_answer,
)
from scripts.stateful_agent_evaluation import (  # noqa: E402
    SCHEMA, ReadOnlyTrialRepository, _digest, load_manifest,
)
from test_mcp_contracts import FakeDebugBackend  # noqa: E402
from scripts.stateful_agent_scenarios import (  # noqa: E402
    ScenarioBackend, source_options, prompt_for, fixture_turn, DIAGNOSIS_RESPONSE,
)


class _ProviderRelay(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, base_url: str, credential: str, *, configured_url: str = "") -> None:
        upstream = urlsplit(base_url)
        if (upstream.scheme not in {"http", "https"} or not upstream.hostname
                or upstream.path.rstrip("/") != "/v1" or upstream.query
                or upstream.fragment or upstream.username or upstream.password):
            raise ValueError("provider URL must end at /v1")
        if (upstream.scheme == "http"
                and upstream.hostname not in {"localhost", "127.0.0.1", "::1"}
                and configured_url != base_url):
            raise ValueError("remote provider requires HTTPS")
        self.upstream = upstream
        self.credential = credential
        self.marker = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", 0), _RelayHandler)


class _RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + self.server.marker):
            self.send_error(403)
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 4 * 1024 * 1024:
                self.send_error(413)
                return
            upstream = self.server.upstream
            connection_type = (http.client.HTTPSConnection if upstream.scheme == "https"
                               else http.client.HTTPConnection)
            connection = connection_type(upstream.hostname, upstream.port, timeout=180)
            try:
                connection.request(
                    "POST", upstream.path.rstrip("/") + "/responses",
                    self.rfile.read(length),
                    {"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.server.credential,
                     "Accept": "text/event-stream"},
                )
                response = connection.getresponse()
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader(
                    "Content-Type", "text/event-stream"))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                connection.close()
        except (OSError, ValueError, http.client.HTTPException):
            self.close_connection = True


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def _request(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    manifest = load_manifest()
    case = next((item for item in manifest["scenarios"]
                 if item["id"] == value.get("scenario_id")), None)
    if case is None:
        raise ValueError("unsupported scenario")
    if (not isinstance(value, dict)
            or value.get("schema") != f"{SCHEMA}/adapter-request"
            or value.get("scenario_id") != case["id"]
            or value.get("scenario_version") != case["version"]
            or value.get("trial") not in (1, 2, 3)
            or value.get("task_id") != f"eval-{case['id']}-v{case['version']}-t{value.get('trial')}"
            or value.get("prompt") != case["prompt"]
            or value.get("prompt_digest") != _digest(case["prompt"])
            or value.get("fixture_target") != manifest["fixture_target"]
            or value.get("backend") != "runtime-fake"
            or Path(str(value.get("output_directory", ""))).resolve() != path.parent.resolve()
            or not re.fullmatch(r"[0-9a-f]{40}", str(value.get("source_commit", "")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("plan_digest", "")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("schedule_digest", "")))):
        raise ValueError("unsupported or unbound pilot request")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                     text=True).strip()
    if commit != value["source_commit"]:
        raise ValueError("pilot source commit differs from request")
    return value


def serve(request_path: Path) -> None:
    request = _request(request_path)
    directory = request_path.parent
    task_id = str(request["task_id"])
    scenario = str(request['scenario_id'])

    class BoundEndpoint(JsonRpcMcpEndpoint):
        # Runtime itself creates the task-to-Run binding. The trial slot is the
        # Runtime task; the native Host session is separately recorded below.
        def task_id_for_params(self, _params: object) -> str:
            return task_id

        def handle(self, message: Mapping[str, object]) -> dict[str, object]:
            if message.get("method") == "tools/list":
                return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"tools": [{
                    "name": "execute", "description": "Execute the pinned target-free Runtime scenario; copy returned Gate bindings.",
                    "inputSchema": {"type": "object", "required": ["kind"], "properties": {
                        "kind": {"type": "string"},
                        "target": {"type": "string"}, "intent": {"type": "string"},
                        "run_id": {"type": "string"}, "deadline": {"type": "number"},
                        "gate_id": {"type": "string"}, "gate_version": {"type": "integer"},
                        "schema_digest": {"type": "string"}, "submission_id": {"type": "string"},
                        "command": {"type": "string"}, "response": {"type": "object"},
                        "delivery_strategy": {"type": "string"}
                    }}, "annotations": {"readOnlyHint": False}}]}}
            params = message.get('params') or {}
            arguments = params.get('arguments') or {}
            if (message.get('method') == 'tools/call' and arguments.get('kind') == 'start'
                    and arguments.get('target') != request['fixture_target']):
                response = {'jsonrpc':'2.0','id':message.get('id'),'result':{
                    'isError':True,'content':[{'type':'text','text':'fixture_scope_rejected: use the pinned synthetic target'}]}}
            else:
                response = super().handle(message)
            if message.get("method") == "tools/call":
                params = message.get("params")
                params = params if isinstance(params, Mapping) else {}
                metadata = params.get("_meta")
                metadata = metadata if isinstance(metadata, Mapping) else {}
                thread_id = metadata.get("threadId")
                record = {
                    "created_at": time.time(),
                    "task_id": task_id,
                    "host_session_id": thread_id if isinstance(thread_id, str) else "",
                    "tool": params.get("name", ""),
                    "response_received": "result" in response,
                    "arguments": arguments,
                    "is_error": bool((response.get('result') or {}).get('isError')) or 'error' in response,
                    "runtime_result": (response.get('result') or {}).get('structuredContent', {}),
                }
                with (directory / "host-trace.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
                result = response.get('result') or {}
                turn = result.get('structuredContent') or {}
                if turn.get('run_id'):
                    projected = fixture_turn(turn, directory, scenario, str(request['fixture_target']))
                    if projected != turn:
                        result['structuredContent'] = projected
                        result['content'] = [{'type':'text','text':json.dumps(projected,ensure_ascii=False)}]
                    should_interrupt = (
                        scenario == 'diagnosis-resume' and arguments.get('kind') == 'start'
                        or scenario == 'effect-reconcile' and turn.get('state') == 'incident'
                    )
                    if should_interrupt and not (directory/'host-interruption.json').exists():
                        events = ReadOnlyTrialRepository(directory/'runtime.sqlite').events(turn['run_id'])
                        operation = 'live_patch_run' if scenario == 'effect-reconcile' else 'debug_run'
                        accepted = [e for e in events if e.get('kind') == 'OperationAccepted'
                                    and e.get('payload', {}).get('operation') == operation]
                        _write_json(directory/'interrupt-ready', {
                            'run_id':turn['run_id'], 'task_id':task_id, 'host_session_id':record['host_session_id'],
                            'operation_id':accepted[-1]['operation_id'] if accepted else '',
                            'armed_at':time.time()})
                    if (scenario == 'gate-replay-idempotent' and arguments.get('kind') == 'respond'
                            and not result.get('isError') and not (directory/'lost-response.json').exists()):
                        _write_json(directory/'lost-response.json', {
                            'run_id':turn['run_id'],'submission_id':arguments.get('submission_id'),
                            'gate_id':arguments.get('gate_id'), 'gate_version':arguments.get('gate_version'),
                            'schema_digest':arguments.get('schema_digest'), 'response':arguments.get('response'),
                            'task_id':task_id,'host_session_id':record['host_session_id'],
                            'after_runtime_commit':True})
                        response = {'jsonrpc':'2.0','id':message.get('id'),'result':{
                            'isError':True,'content':[{'type':'text','text':
                                'Synthetic response lost after commit. Replay the identical Gate reply with its original submission_id.'}]}}
            return response

    service = RuntimeMcpService(
        ScenarioBackend(directory, scenario),
        context_repository=SQLiteRuntimeRepository(directory / "runtime.sqlite"),
        blob_repository=FilesystemBlobRepository(directory / "blobs"),
        **(source_options(directory, task_id) if scenario == 'source-identity-drift' else {}),
    )
    if scenario == 'wrong-target':
        foreign_path = directory/'foreign-evidence.json'
        if not foreign_path.exists():
            foreign = service.call_exposed_tool('execute',
                {'kind':'start','target':'192.0.2.11','intent':'diagnosis-only'},
                task_id=task_id+'-foreign', operation_id=task_id+'-foreign-start')
            _write_json(foreign_path, {'run_id':foreign['run_id'],'target':'192.0.2.11',
                'evidence_id':foreign['diagnostic_receipt']['evidence'][0]['evidence_id']})
    try:
        StdioMcpServer(BoundEndpoint(service, session_task_id=task_id)).serve()
    finally:
        service.close()


def _credential() -> str:
    value = os.environ.get("OPENUBMC_EVAL_API_KEY", "")
    if value:
        return value
    if sys.platform != "darwin":
        raise ValueError("OPENUBMC_EVAL_API_KEY is unavailable")
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "codex.user-api.cliproxy", "-w"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise ValueError("Keychain evaluation credential is unavailable")
    return result.stdout.strip()


def _cli(base_url: str, request: Mapping[str, object], directory: Path,
         executable: str, marker: str) -> tuple[list[str], dict[str, str]]:
    if not re.fullmatch(r"https?://[^\s]+/v1/?", base_url):
        raise ValueError("provider URL must end at /v1")
    home = directory / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {key: value for key, value in os.environ.items()
           if key in {"PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "TMPDIR",
                      "SSL_CERT_FILE"}}
    env.update(HOME=str(home), CODEX_HOME=str(codex_home),
               OPENUBMC_EVAL_API_KEY=marker,
               PYTHONDONTWRITEBYTECODE="1")
    instruction_file = directory / "host-instructions.md"
    instruction_file.write_text(HOST_INSTRUCTIONS, encoding="utf-8")
    config = {
        "model_instructions_file": json.dumps(str(instruction_file)),
        "features.plugins": "false", "features.shell_tool": "false",
        "features.shell_snapshot": "false",
        "approval_policy": '"never"',
        "model_reasoning_effort": json.dumps(request["reasoning_effort"]),
        "model_provider": '"stateful_pilot"',
        "model_providers.stateful_pilot.name": '"Stateful pilot provider"',
        "model_providers.stateful_pilot.base_url": json.dumps(base_url.rstrip("/")),
        "model_providers.stateful_pilot.env_key": '"OPENUBMC_EVAL_API_KEY"',
        "model_providers.stateful_pilot.wire_api": '"responses"',
        "model_providers.stateful_pilot.supports_websockets": "false",
        "mcp_servers.runtime_fake.command": json.dumps(sys.executable),
        "mcp_servers.runtime_fake.args": json.dumps(
            [str(Path(__file__).resolve()), "serve", str(directory / "request.json")]),
        "mcp_servers.runtime_fake.required": "true",
        "mcp_servers.runtime_fake.tool_timeout_sec": "60",
        "mcp_servers.runtime_fake.enabled_tools": '["execute"]',
        "mcp_servers.runtime_fake.tools.execute.approval_mode": '"approve"',
    }
    flags = [part for key, value in config.items() for part in ("-c", f"{key}={value}")]
    return [executable, "exec", "--json", "--skip-git-repo-check",
            "--ignore-user-config", "--dangerously-bypass-approvals-and-sandbox", "-C", str(directory),
            "--model", str(request["model"]), *flags], env


def _verify_shell_is_disabled(executable: str, env: Mapping[str, str]) -> None:
    check = subprocess.run(
        [executable, "features", "list", "-c", "features.shell_tool=false",
         "-c", "features.shell_snapshot=false"],
        env=dict(env), capture_output=True, text=True, check=False,
    )
    if check.returncode or not all(re.search(rf"(?m)^{feature}\s+\S+\s+false$", check.stdout)
                                   for feature in ("shell_tool", "shell_snapshot")):
        raise ValueError("native CLI cannot disable shell tools and snapshots")


def _invoke(command: list[str], env: Mapping[str, str], output: Path,
            *, credential: str, timeout: float = 300, deadline_at: float | None = None,
            interrupt_after: Path | None = None) -> int:
    if deadline_at is not None:
        timeout = min(timeout, deadline_at - time.time())
    if _CANCELLED:
        raise SystemExit(124)
    if timeout <= 0:
        raise ValueError('native trial deadline expired')
    with output.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, cwd=output.parent, env=dict(env),
                                   stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        _OWNED_GROUPS.add(process.pid)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if _CANCELLED:
                    raise SystemExit(124)
                if interrupt_after is not None and interrupt_after.exists():
                    # Let the bounded fake read settle durably before stopping
                    # the Host turn. The model's next provider request remains
                    # interrupted; the persisted Run can attach after restart.
                    time.sleep(0.35)
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    _write_json(output.parent/'host-interruption.json', {
                        **json.loads(interrupt_after.read_text()),
                        'signal':'SIGINT','exit_code':process.returncode,'after_mcp_result':True,
                        'interrupted_at':time.time()})
                    return process.returncode
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.02)
            return process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError("native Codex turn timed out") from None
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            _OWNED_GROUPS.discard(process.pid)
            _scrub_secret_artifacts(output.parent, credential)


def _scrub_secret_artifacts(directory: Path, credential: str) -> None:
    """Fail closed if native Codex wrote the provider key into trial files."""
    needle = credential.encode("utf-8")
    if not needle:
        raise ValueError("evaluation credential is unavailable")
    leaked = False
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        tail = b""
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                combined = tail + chunk
                if needle in combined:
                    path.unlink()
                    leaked = True
                    break
                tail = combined[-len(needle) + 1:] if len(needle) > 1 else b""
    if leaked:
        raise ValueError("native Codex persisted an evaluation credential")


def _session_id(events: Path) -> str:
    sessions = set()
    for line in events.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") == "thread.started":
            sessions.add(event.get("thread_id"))
    if len(sessions) != 1:
        raise ValueError("native Codex session identity is missing or ambiguous")
    session = sessions.pop()
    if not isinstance(session, str) or not re.fullmatch(
            r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", session):
        raise ValueError("native Codex session identity is invalid")
    return session


def _native_rollout(home: Path, session_id: str, destination: Path) -> None:
    paths = list((home / ".codex" / "sessions").rglob(f"*{session_id}.jsonl"))
    if len(paths) != 1:
        raise ValueError("unique native Codex rollout is unavailable")
    shutil.copyfile(paths[0], destination)
    with destination.open(encoding="utf-8") as stream:
        first = json.loads(stream.readline())
    if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != session_id:
        raise ValueError("native rollout identity differs from Host session")
    if first.get("payload", {}).get("base_instructions", {}).get("text", "").strip() != HOST_INSTRUCTIONS:
        raise ValueError("native session did not load the pinned Host instructions")
    contexts = [json.loads(line)["payload"] for line in destination.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("type") == "turn_context"]
    if not contexts or any(context.get("sandbox_policy", {}).get("type") != "danger-full-access"
                           or context.get("approval_policy") != "never" for context in contexts):
        raise ValueError("native session did not receive the requested full access")
    _write_json(destination.with_name("permissions.json"), {
        "sandbox": "danger-full-access", "approval_policy": "never", "verified_turns": len(contexts)})


def _run_connected(request: Mapping[str, object], directory: Path,
                   executable: str, relay_url: str, credential: str,
                   marker: str) -> None:
    command, env = _cli(relay_url, request, directory, executable, marker)
    _verify_shell_is_disabled(executable, env)
    target = str(request["fixture_target"])
    scenario = str(request['scenario_id'])
    deadline_at = float(request.get('deadline_at', time.time() + 600))
    if scenario == 'source-identity-drift':
        candidate = {**request, 'source_commit':'0'*40}
        candidate_path = directory/'source-drift-request.json'
        _write_json(candidate_path, candidate)
        try:
            _request(candidate_path)
            rejection = ''
        except ValueError as error:
            rejection = str(error)
        _write_json(directory/'source-identity-probe.json', {
            'task_id':request['task_id'], 'plan_digest':request['plan_digest'],
            'plan_source_commit':request['source_commit'], 'trial_source_commit':candidate['source_commit'],
            'rejection':rejection})
    prompt = prompt_for(scenario, target) + f"Scenario: {request['prompt']}"
    interruption = directory / 'interrupt-ready' if scenario in {'diagnosis-resume','effect-reconcile'} else None
    code = _invoke([*command, prompt], env, directory / "codex-initial.jsonl",
                   credential=credential, deadline_at=deadline_at, interrupt_after=interruption)
    session_id = _session_id(directory / "codex-initial.jsonl")
    _native_rollout(directory / "home", session_id, directory / "rollout.jsonl")
    repository = ReadOnlyTrialRepository(directory / "runtime.sqlite")
    run_id = repository.case_for_task(str(request["task_id"]))
    if not run_id:
        raise ValueError("Runtime task binding was not persisted")
    # A failed confirmation must remain scoreable from its original Host claims.
    _write_json(directory / "trial.json", {
        "scenario_id": request["scenario_id"], "scenario_version": request["scenario_version"],
        "trial": request["trial"], "task_id": request["task_id"], "run_id": run_id,
        "host_session_id": session_id, "backend": "runtime-fake",
        "plan_digest": request["plan_digest"],
        "identity": {key: request[key] for key in (
            "source_commit", "model", "client_version", "reasoning_effort",
            "prompt_digest", "schedule_digest")},
    })
    if code != 0 and not (directory/'host-interruption.json').exists():
        raise ValueError("native Codex first turn failed")
    resume = [executable, "exec", "resume", "--json", "--skip-git-repo-check",
              "--ignore-user-config", "--model", str(request["model"]),
              "-c", 'sandbox_mode="danger-full-access"', *command[command.index("-c"):], session_id]
    if (directory/'host-interruption.json').exists():
        recovery_prompt = (
            f"The Host interrupted the previous turn. Continue the original Run {run_id}; never Start a new Run. "
            + ("Use kind=control, command=reconcile on that run_id to inspect the original mutation journal. "
               if scenario == 'effect-reconcile' else
               "Use kind=resume on that run_id and wait for diagnosis.acceptance. " + DIAGNOSIS_RESPONSE)
        )
        recovery_code = _invoke([*resume, recovery_prompt], env, directory/'codex-recovery.jsonl', credential=credential,
                                deadline_at=deadline_at)
        _native_rollout(directory/'home', session_id, directory/'rollout.jsonl')
        if recovery_code != 0:
            raise ValueError('native recovery turn failed')
    projection = repository.load(run_id)
    if not projection:
        raise ValueError('Runtime projection is unavailable')
    outcome = projection.get('run_outcome') or {}
    closeout = projection.get('closeout') or {}
    highest = str((closeout.get('delivery_stage') or {}).get('highest') or 'unverified')
    from openubmc_target_runtime.context_runtime import operator_run_projection
    status = str(outcome.get('status') or operator_run_projection(projection)['turn_state'])
    # Confirm the Agent's original completed final verbatim. Never replace an
    # earlier claim with a fixture-generated corrected status or delivery stage.
    from scripts.stateful_agent_live_evidence import native_turns
    completed_finals, _ = native_turns(directory/'rollout.jsonl', session_id)
    if not completed_finals:
        raise ValueError('native final completion is missing')
    final_text = completed_finals[-1]['text']
    claim = json.loads(final_text)
    if (not isinstance(claim, dict) or set(claim) != {'run_id','status','delivery_stage'}
            or claim['run_id'] != run_id or claim['status'] != status):
        raise ValueError('native final claim differs from Runtime facts')
    highest = str(claim['delivery_stage'])
    store = TerminalAnswerStore(directory/'terminal.json')
    prepared = None
    prepared_at = datetime.now(timezone.utc).isoformat()
    if outcome:
        prepared = store.prepare(task_id=str(request['task_id']), run_id=run_id,
                                 outcome=outcome, delivery_stage=highest, text=final_text)
        prepared_at = prepared.prepared_at
    if scenario == 'terminal-unconfirmed' and prepared is not None:
        probe = qualify_terminal_answer(task_id=str(request['task_id']),run_id=run_id,
                                        outcome=outcome,delivery_stage=highest,record=prepared)
        _write_json(directory/'terminal-probe.json',{'challenge':'prepared-only','rejected':probe['status'] != 'passed'})
    elif scenario == 'terminal-false-success':
        false_outcome = {**outcome,'status':'completed'}
        candidate = TerminalAnswerStore(directory/'false-claim.json').prepare(
            task_id=str(request['task_id']),run_id=run_id,outcome=false_outcome,delivery_stage=highest,text='Synthetic completed claim')
        probe = qualify_terminal_answer(task_id=str(request['task_id']),run_id=run_id,
                                        outcome=outcome,delivery_stage=highest,record=candidate)
        _write_json(directory/'terminal-probe.json',{'challenge':'completed-on-partial','rejected':probe['status'] != 'passed',
                                                  'failures':probe['failures'], 'run_id':run_id,
                                                  'runtime_status':outcome.get('status')})
    elif scenario == 'terminal-outcome-missing':
        rejected = False
        try:
            store.prepare(task_id=str(request['task_id']),run_id=run_id,outcome=outcome,delivery_stage=highest,text='Synthetic completed claim')
        except TerminalAnswerError:
            rejected = True
        _write_json(directory/'terminal-probe.json',{'challenge':'missing-outcome','rejected':rejected})
    final_code = _invoke([*resume, "Reply with exactly this single line and no other text: " + final_text],
                         env, directory/'codex-final.jsonl', credential=credential, deadline_at=deadline_at)
    _native_rollout(directory/'home',session_id,directory/'rollout.jsonl')
    if final_code != 0:
        raise ValueError('native Codex final turn failed')
    event_id, observed_text, observed_at = audit_rollout_final(
        directory/'rollout.jsonl',task_id=session_id,prepared_at=prepared_at,expected_text=final_text)
    if prepared is not None:
        store.acknowledge(task_id=str(request['task_id']),run_id=run_id,outcome=outcome,
                          delivery_stage=highest,text=observed_text,host_event_id=event_id,
                          observed_at=observed_at,delivery_source='codex-rollout-v1')


def run(request_path: Path) -> None:
    request = _request(request_path)
    directory = request_path.parent
    executable = os.environ.get("OPENUBMC_EVAL_CODEX_BIN") or shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI is unavailable")
    executable_path = Path(executable)
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise ValueError("Codex CLI is not an executable file")
    version = subprocess.check_output([executable, "--version"], text=True).strip()
    if version != str(request["client_version"]).replace("/", " "):
        raise ValueError("Codex CLI version differs from pinned plan")
    if version == "codex-cli 0.144.6":
        raise ValueError("pinned CLI cancels fake Runtime MCP execute under read-only sandbox")
    configured = os.environ.get("OPENUBMC_EVAL_CODEX_CONFIG", "")
    configured_url = ""
    if configured:
        config = tomllib.loads(Path(configured).read_text(encoding="utf-8"))
        provider = config["model_providers"][config["model_provider"]]
        if provider.get("wire_api") != "responses" or not provider.get("env_key"):
            raise ValueError("configured provider requires Responses and an environment credential")
        configured_url = provider["base_url"]
        credential = os.environ.get(provider["env_key"], "")
        if not credential:
            raise ValueError("configured provider credential is unavailable")
        descriptor = {"provider": config["model_provider"], "base_url_digest": _digest(configured_url),
                      "transport": urlsplit(configured_url).scheme, "wire_api": "responses",
                      "credential_source": "configured environment variable"}
        if request.get("provider_descriptor") != descriptor:
            raise ValueError("configured provider differs from the pinned plan")
        _write_json(directory / "provider.json", descriptor)
    else:
        if request.get("provider_descriptor"):
            raise ValueError("pinned provider configuration is unavailable")
        credential = _credential()
    relay = _ProviderRelay(configured_url or os.environ.get("OPENUBMC_EVAL_BASE_URL", ""), credential,
                           configured_url=configured_url)
    relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
    relay_thread.start()
    try:
        _run_connected(request, directory, executable,
                       f"http://127.0.0.1:{relay.server_port}/v1",
                       credential, relay.marker)
    finally:
        relay.shutdown()
        relay.server_close()
        relay_thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first")
    parser.add_argument("second", nargs="?")
    args = parser.parse_args()
    mode = args.first if args.second else "run"
    if mode not in {"run", "serve"}:
        parser.error("mode must be run or serve")
    request_path = Path(args.second if args.second else args.first).resolve()
    try:
        if mode == "serve":
            serve(request_path)
        else:
            signal.signal(signal.SIGTERM, _cancel_adapter)
            signal.signal(signal.SIGINT, _cancel_adapter)
            started = time.monotonic()
            try:
                run(request_path)
                status = "completed"
            except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error) as error:
                status = type(error).__name__ + ": " + str(error)
            _write_json(request_path.parent / "pilot-status.json", {
                "status": status, "elapsed_seconds": round(time.monotonic() - started, 3),
            })
            if status != "completed":
                return 1
    except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
