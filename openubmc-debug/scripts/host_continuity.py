#!/usr/bin/env python3
"""Local host handoff/answer recovery; no target connection or Run execution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from _target_runtime_adapter import _load_runtime_module


def _default_state_dir() -> Path:
    if os.name == "nt":
        local = Path(os.environ.get("XDG_STATE_HOME") or os.environ.get("LOCALAPPDATA")
                     or Path.home() / "AppData" / "Local")
        return local / "openubmc" / "runtime-state"
    return Path.home() / ".local" / "state" / "openubmc-target-runtime"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("handoff", "notes", "answer", "audit", "hook"))
    parser.add_argument("--task-id")
    parser.add_argument("--run-id")
    parser.add_argument("--state-dir", default=os.environ.get(
        "OPENUBMC_TARGET_RUNTIME_STATE_DIR",
        str(_default_state_dir()),
    ))
    parser.add_argument("--notes-file", type=Path)
    parser.add_argument("--rollout", type=Path)
    args = parser.parse_args(argv)
    if args.command != "hook" and not args.task_id:
        parser.error("--task-id is required")
    if args.command in {"answer", "audit"} and not args.run_id:
        parser.error("--run-id is required for answer/audit")
    if args.command == "notes" and args.notes_file is None:
        parser.error("--notes-file is required for notes")
    if args.command == "audit" and args.rollout is None:
        parser.error("--rollout is required for audit")
    runtime = _load_runtime_module()

    root = Path(args.state_dir).expanduser().resolve()
    store = runtime.HostContinuity(root / "host-continuity")

    def read_run(run_id):
        return runtime.read_runtime_projection(root / "context-runtime.sqlite3", run_id)

    try:
        if args.command == "hook":
            body = sys.stdin.read(64 * 1024 + 1)
            if len(body.encode()) > 64 * 1024:
                raise ValueError("hook event exceeds 64 KiB")
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError("hook event must be an object")
            value = store.handle_hook(event, read_run=read_run)
        elif args.command == "notes":
            with args.notes_file.open(encoding="utf-8") as stream:
                body = stream.read(24 * 1024 + 1)
            if len(body.encode()) > 24 * 1024:
                raise ValueError("notes file exceeds 24 KiB")
            store.save_notes(args.task_id, json.loads(body))
            value = {"status": "saved", "task_id": args.task_id, "notes_authoritative": False}
        elif args.command == "audit":
            value = store.acknowledge_rollout(
                args.task_id, args.run_id, args.rollout, read_run=read_run,
            )
        else:
            value = store.handoff(args.task_id, read_run=read_run)
            if args.command == "answer":
                run = next((item for item in value["runs"] if item["run_id"] == args.run_id), {})
                answer = run.get("terminal_answer")
                if not answer:
                    raise ValueError("no Runtime-grounded prepared answer for this task/Run")
                # Printing recovers the text, but is NOT an observed host final acknowledgement.
                print(answer["text"])
                return 0
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        if args.command == "hook":
            # Hook failure never interrupts normal work or asks to repeat mutations.
            print("{}")
            return 0
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__,
                          "next": "Restore the ledger/host store; do not repeat device operations."}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
