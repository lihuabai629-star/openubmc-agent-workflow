#!/usr/bin/env python3
"""One native Codex pilot against a persisted, target-free Runtime MCP server.

This adapter supports diagnosis-complete and diagnosis-resume. The latter
interrupts the native Host at the diagnosis Gate and resumes the same session.
The other 18 scenarios remain unsupported; this is not a 60-slot adapter.
"""

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
from collections.abc import Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "openubmc-target-runtime"),
                str(ROOT / "openubmc-target-runtime" / "tests")]

from openubmc_target_runtime import (  # noqa: E402
    FilesystemBlobRepository, JsonRpcMcpEndpoint, RuntimeMcpService,
    SQLiteRuntimeRepository, StdioMcpServer,
)
from openubmc_target_runtime.terminal_delivery import (  # noqa: E402
    TerminalAnswerStore, audit_rollout_final,
)
from scripts.stateful_agent_evaluation import (  # noqa: E402
    SCHEMA, ReadOnlyTrialRepository, _digest, load_manifest,
)
from test_mcp_contracts import FakeDebugBackend  # noqa: E402


class _ProviderRelay(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, base_url: str, credential: str) -> None:
        upstream = urlsplit(base_url)
        if (upstream.scheme not in {"http", "https"} or not upstream.hostname
                or upstream.path.rstrip("/") != "/v1" or upstream.query
                or upstream.fragment or upstream.username or upstream.password):
            raise ValueError("provider URL must end at /v1")
        if (upstream.scheme == "http"
                and upstream.hostname not in {"localhost", "127.0.0.1", "::1"}):
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
                 if item["id"] == (value.get("scenario_id") if isinstance(value, dict) else "")), None)
    if (not isinstance(value, dict) or case is None
            or case["id"] not in {"diagnosis-complete", "diagnosis-resume"}
            or value.get("execution_mode", "agent") not in {"agent", "controlled-scripted-responses"}
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
    if case["id"] == "diagnosis-resume" and (
            value.get("execution_mode") not in {"agent", "controlled-scripted-responses"}
            or value.get("model_invoked") is not (value.get("execution_mode") == "agent")):
        raise ValueError("resume execution mode and model invocation must be explicit")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                     text=True).strip()
    if commit != value["source_commit"]:
        raise ValueError("pilot source commit differs from request")
    return value


def serve(request_path: Path) -> None:
    request = _request(request_path)
    directory = request_path.parent
    task_id = str(request["task_id"])

    class BoundEndpoint(JsonRpcMcpEndpoint):
        # Runtime itself creates the task-to-Run binding. The trial slot is the
        # Runtime task; the native Host session is separately recorded below.
        def task_id_for_params(self, _params: object) -> str:
            return task_id

        def operation_id_for_params(self, params: object, request_id: object) -> str:
            # Native exec resume starts a new MCP transport with RPC ids reused.
            # Keep retries stable, while start/resume remain different commands.
            if request["scenario_id"] == "diagnosis-resume" and isinstance(params, Mapping):
                arguments = params.get("arguments")
                kind = arguments.get("kind") if isinstance(arguments, Mapping) else None
                request_id = {"rpc_id": request_id, "command_kind": kind}
            return super().operation_id_for_params(params, request_id)

        def handle(self, message: Mapping[str, object]) -> dict[str, object]:
            response = super().handle(message)
            if message.get("method") == "tools/call":
                params = message.get("params")
                params = params if isinstance(params, Mapping) else {}
                metadata = params.get("_meta")
                metadata = metadata if isinstance(metadata, Mapping) else {}
                thread_id = metadata.get("threadId")
                record = {
                    "task_id": task_id,
                    "host_session_id": thread_id if isinstance(thread_id, str) else "",
                    "tool": params.get("name", ""),
                    "response_received": "result" in response,
                    "metadata_keys": sorted(str(key) for key in metadata),
                    "host_call_id": metadata.get("callId", ""),
                    "request_id": message.get("id"),
                    "command_id": self.operation_id_for_params(params, message.get("id")),
                    "request_kind": (params.get("arguments") or {}).get("kind"),
                    "request_run_id": (params.get("arguments") or {}).get("run_id", ""),
                    "response_run_id": (response.get("result", {}).get("structuredContent") or {}).get("run_id", ""),
                }
                with (directory / "host-trace.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, sort_keys=True) + "\n")
            return response

    class CountedBackend(FakeDebugBackend):
        @staticmethod
        def debug_run(task, arguments, context):
            with (directory / "backend-invocations.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"task_id": task_id, "operation": "debug_run"}) + "\n")
            return FakeDebugBackend.debug_run(task, arguments, context)

    service = RuntimeMcpService(
        CountedBackend(),
        context_repository=SQLiteRuntimeRepository(directory / "runtime.sqlite"),
        blob_repository=FilesystemBlobRepository(directory / "blobs"),
    )
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
    config = {
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
            "--ignore-user-config", "--sandbox", "read-only", "-C", str(directory),
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
            *, credential: str, timeout: int = 300) -> int:
    with output.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, cwd=output.parent, env=dict(env),
                                   stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError("native Codex turn timed out") from None
        finally:
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
    first = json.loads(destination.open(encoding="utf-8").readline())
    if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != session_id:
        raise ValueError("native rollout identity differs from Host session")


def _cancel_at_diagnosis_gate(command: list[str], env: Mapping[str, str],
                              directory: Path, task_id: str, credential: str) -> tuple[str, str]:
    """Interrupt only our native child, after its first nonterminal MCP reply."""
    output = directory / "codex-initial.jsonl"
    deadline = time.monotonic() + 60
    checkpoint = None
    with output.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, cwd=directory, env=dict(env),
                                   stdin=subprocess.DEVNULL, stdout=stream,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            while process.poll() is None and time.monotonic() < deadline:
                # The CLI record is emitted only after Runtime returned its Turn.
                for line in output.read_text(encoding="utf-8").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # the writer may be finishing the last line
                    item = event.get("item", {})
                    if (event.get("type") != "item.completed" or item.get("type") != "mcp_tool_call"
                            or item.get("server") != "runtime_fake" or item.get("tool") != "execute"
                            or item.get("arguments", {}).get("kind") != "start"):
                        continue
                    turn = (item.get("result") or {}).get("structured_content", {})
                    if (turn.get("state") != "waiting_response" or turn.get("outcome")
                            or (turn.get("gate") or {}).get("name") != "diagnosis.acceptance"):
                        raise ValueError("initial diagnosis did not stop at its nonterminal Gate")
                    session_id = _session_id(output)
                    repository = ReadOnlyTrialRepository(directory / "runtime.sqlite")
                    run_id = repository.case_for_task(task_id)
                    projection = repository.load(str(run_id))
                    if run_id != turn.get("run_id") or not projection or projection.get("run_outcome"):
                        raise ValueError("cancellation checkpoint is not the bound nonterminal Run")
                    events = repository.events(str(run_id))
                    checkpoint = {"host_session_id": session_id, "run_id": run_id,
                                  "state": turn["state"], "gate_id": turn["gate"]["gate_id"],
                                  "revision": projection["revision"], "run_outcome": None}
                    _write_json(directory / "cancel-checkpoint.json", checkpoint)
                    _write_json(directory / "cancel-runtime-events.json", {"events": list(events)})
                    process.send_signal(signal.SIGINT)
                    process.wait(timeout=10)
                    break
                if checkpoint is not None:
                    break
                time.sleep(0.02)
            if checkpoint is None:
                raise ValueError("native Host ended or timed out before the diagnosis cancellation checkpoint")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            _scrub_secret_artifacts(directory, credential)
    session_id, run_id = str(checkpoint["host_session_id"]), str(checkpoint["run_id"])
    _native_rollout(directory / "home", session_id, directory / "cancel-rollout.jsonl")
    active, aborted = "", []
    for line in (directory / "cancel-rollout.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        payload = event.get("payload", {})
        if event.get("type") == "event_msg":
            if payload.get("type") == "task_started":
                active = payload.get("turn_id", "")
            elif payload.get("type") == "task_complete":
                active = ""
            elif (payload.get("type") == "turn_aborted" and active
                  and payload.get("turn_id") == active and payload.get("reason") == "interrupted"):
                aborted.append(active)
                active = ""
    if len(aborted) != 1:
        raise ValueError("native Host did not persist a matching interrupted turn")
    projection = ReadOnlyTrialRepository(directory / "runtime.sqlite").load(run_id)
    if not projection or projection.get("run_outcome"):
        raise ValueError("Run became terminal before Host cancellation")
    return session_id, run_id


def _run_connected(request: Mapping[str, object], directory: Path,
                   executable: str, relay_url: str, credential: str,
                   marker: str) -> None:
    command, env = _cli(relay_url, request, directory, executable, marker)
    _verify_shell_is_disabled(executable, env)
    target = request["fixture_target"]
    prompt = (
        f"This is one isolated synthetic Runtime trial. The target {target} is a documentation "
        "address and the MCP server uses a fake backend. Use only runtime_fake execute. "
        "Start with kind=start, intent=diagnosis-only, and the specified target. "
        "Wait for the diagnosis.acceptance Gate, then respond with status=completed, "
        "summary='synthetic diagnosis', and payload containing root_cause='synthetic mismatch', "
        "all evidence_ids from diagnostic_receipt, causal_chain=['synthetic evidence supports cause'], "
        "code_owner='src/fake.lua', contradictions=[], remaining_gaps=[], and "
        "verification_status='verified'. Copy the returned Gate binding exactly. "
        "Do not use shell, edit files, contact any target, or claim a later delivery stage. "
        f"Scenario: {request['prompt']}"
    )
    if request["scenario_id"] == "diagnosis-resume":
        initial_prompt = (
            f"Start one isolated synthetic diagnosis-only Run for {target} using runtime_fake execute. "
            "The backend is fake. Do not answer its diagnosis Gate or claim completion. "
            "The Host controller will interrupt this turn after the first tool result."
        )
        session_id, interrupted_run = _cancel_at_diagnosis_gate(
            [*command, initial_prompt], env, directory, str(request["task_id"]), credential)
        resumed = [executable, "exec", "resume", "--json", "--skip-git-repo-check",
                   "--ignore-user-config", "--model", str(request["model"]),
                   "-c", 'sandbox_mode="read-only"', *command[command.index("-c"):], session_id]
        recovery_prompt = (
            f"Continue the interrupted diagnosis. First call execute with kind=resume and run_id={interrupted_run}. "
            "Do not start a new Run. Then answer the returned Gate using its exact binding. "
            "Use status=completed, summary='synthetic diagnosis', payload root_cause='synthetic mismatch', "
            "evidence_ids from diagnostic_receipt, causal_chain=['synthetic evidence supports cause'], "
            "code_owner='src/fake.lua', contradictions=[], remaining_gaps=[], verification_status='verified'."
        )
        if _invoke([*resumed, recovery_prompt], env, directory / "codex-resumed.jsonl",
                   credential=credential) != 0:
            raise ValueError("native Codex recovery turn failed")
        if _session_id(directory / "codex-resumed.jsonl") != session_id:
            raise ValueError("native Codex recovery changed Host session")
    else:
        if _invoke([*command, prompt], env, directory / "codex-initial.jsonl",
                   credential=credential) != 0:
            raise ValueError("native Codex first turn failed")
        session_id = _session_id(directory / "codex-initial.jsonl")
    repository = ReadOnlyTrialRepository(directory / "runtime.sqlite")
    run_id = repository.case_for_task(str(request["task_id"]))
    if not run_id:
        raise ValueError("Runtime task binding was not persisted")
    projection = repository.load(run_id)
    if not projection or not isinstance(projection.get("run_outcome"), Mapping):
        raise ValueError("Runtime terminal Outcome was not persisted")
    outcome = projection["run_outcome"]
    if outcome.get("status") != "completed":
        raise ValueError("Runtime Outcome is not completed")
    closeout = projection.get("closeout", {})
    closeout = closeout if isinstance(closeout, Mapping) else {}
    stage = closeout.get("delivery_stage", {})
    stage = stage if isinstance(stage, Mapping) else {}
    highest = str(stage.get("highest") or "unverified")
    final_text = (f"Synthetic diagnosis completed. Run: {run_id}. "
                  f"Delivery stage: {highest}.")
    store = TerminalAnswerStore(directory / "terminal.json")
    prepared = store.prepare(task_id=str(request["task_id"]), run_id=run_id,
                             outcome=outcome, delivery_stage=highest, text=final_text)
    resume = [executable, "exec", "resume", "--json", "--skip-git-repo-check",
              "--ignore-user-config", "--model", str(request["model"]),
              "-c", 'sandbox_mode="read-only"', *command[command.index("-c"):],
              session_id, "Reply with exactly this single line and no other text: " + final_text]
    if _invoke(resume, env, directory / "codex-final.jsonl",
               credential=credential) != 0:
        raise ValueError("native Codex final turn failed")
    _native_rollout(directory / "home", session_id, directory / "rollout.jsonl")
    event_id, observed_text, observed_at = audit_rollout_final(
        directory / "rollout.jsonl", task_id=session_id,
        prepared_at=prepared.prepared_at, expected_text=final_text,
    )
    store.acknowledge(task_id=str(request["task_id"]), run_id=run_id,
                      outcome=outcome, delivery_stage=highest,
                      text=observed_text, host_event_id=event_id,
                      observed_at=observed_at, delivery_source="codex-rollout-v1")
    _write_json(directory / "trial.json", {
        "scenario_id": request["scenario_id"], "scenario_version": request["scenario_version"],
        "trial": request["trial"], "task_id": request["task_id"], "run_id": run_id,
        "host_session_id": session_id, "backend": "runtime-fake",
        "execution_mode": request.get("execution_mode", "agent"),
        "model_invoked": request.get("execution_mode", "agent") == "agent",
        **({"model_invoked": False, "actual_agent_trials": 0, "live_acceptance": "unverified"}
           if request.get("execution_mode") == "controlled-scripted-responses" else {}),
        "plan_digest": request["plan_digest"],
        "identity": {key: request[key] for key in (
            "source_commit", "model", "client_version", "reasoning_effort",
            "prompt_digest", "schedule_digest")},
    })


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
    base_url = os.environ.get("OPENUBMC_EVAL_BASE_URL", "")
    if request.get("execution_mode") == "controlled-scripted-responses":
        if urlsplit(base_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("controlled Responses must use a loopback fixture")
        credential = "controlled-local-fixture"
    else:
        credential = _credential()
    relay = _ProviderRelay(base_url, credential)
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
