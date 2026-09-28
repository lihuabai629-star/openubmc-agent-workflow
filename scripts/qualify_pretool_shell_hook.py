#!/usr/bin/env python3
"""Probe native Codex PreToolUse shell interception in a disposable fixture.

The model is a loopback scripted Responses server. The only shell commands
touch two temporary files. This does not load the user's hooks or credentials.
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


def hook(root: Path) -> None:
    event = json.load(sys.stdin)
    command = event.get("tool_input", {}).get("command")
    blocked_command = "touch " + shlex.quote(str(root / "blocked"))
    allowed_command = "touch " + shlex.quote(str(root / "allowed"))
    with (root / "hook-events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "event": event.get("hook_event_name"),
            "tool": event.get("tool_name"),
            "event_keys": sorted(event),
            "tool_input_keys": sorted(event.get("tool_input", {})),
            "session_id": event.get("session_id"),
            "blocked_match": command == blocked_command,
            "allowed_match": command == allowed_command,
        }) + "\n")
    if command == blocked_command:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Synthetic fixture blocks this command.",
        }}))
    else:
        print("{}")


def probe(executable: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="openubmc-pretool-fixture-") as temporary:
        root = Path(temporary)
        home = root / "codex-home"
        home.mkdir()
        command = shlex.join([sys.executable, str(Path(__file__).resolve()),
                              "hook", "--root", str(root)])
        (home / "hooks.json").write_text(json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "^Bash$", "hooks": [{
                "type": "command", "command": command, "timeout": 5,
            }]}],
        }}), encoding="utf-8")

        class Handler(http.server.BaseHTTPRequestHandler):
            calls = 0

            def log_message(self, *_args: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                Handler.calls += 1
                if Handler.calls == 1:
                    blocked = "touch " + shlex.quote(str(root / "blocked"))
                    allowed = "touch " + shlex.quote(str(root / "allowed"))
                    script = (
                        "try { await tools.exec_command({cmd:" + json.dumps(blocked)
                        + ",workdir:" + json.dumps(str(root)) + "}); } catch (_) {}\n"
                        + "await tools.exec_command({cmd:" + json.dumps(allowed)
                        + ",workdir:" + json.dumps(str(root)) + "});\ntext('fixture-done');"
                    )
                    item = {"type": "custom_tool_call", "call_id": "shell-fixture",
                            "name": "exec", "input": script}
                else:
                    item = {"type": "message", "id": "fixture-final", "role": "assistant",
                            "phase": "final_answer", "content": [{"type": "output_text",
                            "text": "Synthetic shell hook probe complete."}]}
                events = [
                    {"type": "response.created", "response": {"id": "shell-fixture"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "shell-fixture",
                     "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}},
                ]
                body = "".join("event: " + event["type"] + "\ndata: "
                               + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CODEX_", "OPENAI_", "OPENUBMC_", "PYTHON"))}
        env.update(CODEX_HOME=str(home), OPENUBMC_LOCAL_PROBE_KEY="loopback-fixture")
        settings = {
            "features.hooks": "true", "model_provider": '"shell_fixture"',
            "model_providers.shell_fixture.name": '"Loopback shell fixture"',
            "model_providers.shell_fixture.base_url": json.dumps(
                f"http://127.0.0.1:{server.server_port}/v1"),
            "model_providers.shell_fixture.env_key": '"OPENUBMC_LOCAL_PROBE_KEY"',
            "model_providers.shell_fixture.wire_api": '"responses"',
            "model_providers.shell_fixture.supports_websockets": "false",
        }
        config = [part for key, value in settings.items()
                  for part in ("-c", key + "=" + value)]
        argv = [str(executable), "exec", "--json", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust", "-C", str(root),
                "--model", "gpt-5.6-sol", *config,
                "Run only the synthetic shell fixture."]
        try:
            result = subprocess.run(argv, env=env, capture_output=True, text=True,
                                    timeout=40)
        finally:
            server.shutdown()
            server.server_close()
        events_path = root / "hook-events.jsonl"
        events = ([json.loads(line) for line in events_path.read_text().splitlines()]
                  if events_path.exists() else [])
        report = {"schema": "openubmc.pretool-shell-hook-fixture.v1",
                  "codex": subprocess.check_output([str(executable), "--version"],
                                                   text=True).strip(),
                  "hook_event_count": len(events),
                  "session_id_present": all(bool(item.get("session_id")) for item in events),
                  "session_count": len({item.get("session_id") for item in events}),
                  "event_keys": sorted({key for item in events for key in item["event_keys"]}),
                  "tool_input_keys": sorted({key for item in events
                                             for key in item["tool_input_keys"]}),
                  "matched_blocked": sum(bool(item["blocked_match"]) for item in events),
                  "matched_allowed": sum(bool(item["allowed_match"]) for item in events),
                  "blocked_file_exists": (root / "blocked").exists(),
                  "allowed_file_exists": (root / "allowed").exists(),
                  "codex_exit": result.returncode}
        print(json.dumps(report, indent=2))
        if (result.returncode != 0 or not report["session_id_present"]
                or report["session_count"] != 1 or report["matched_blocked"] != 1
                or report["matched_allowed"] != 1 or report["blocked_file_exists"]
                or not report["allowed_file_exists"]):
            raise RuntimeError("native synthetic shell hook fixture failed: "
                               + result.stderr[-1200:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "hook"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--codex", type=Path)
    args = parser.parse_args()
    if args.mode == "hook":
        if args.root is None:
            parser.error("--root is required")
        hook(args.root)
    else:
        if args.codex is None:
            parser.error("--codex is required")
        probe(args.codex.resolve())
