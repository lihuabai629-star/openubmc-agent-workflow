#!/usr/bin/env python3
"""Inspect or retire attributable task-scoped MCP processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    cleanup_confirmed_orphaned_mcp_processes,
    inspect_mcp_process_records,
)


SCHEMA = "openubmc-agent-workflow.mcp-process-status.v1"


def default_lifecycle_root() -> Path:
    configured = os.environ.get("OPENUBMC_MCP_LIFECYCLE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    return (
        Path.home()
        / ".local"
        / "state"
        / "openubmc-agent-workflow"
        / "mcp-processes"
    ).absolute()


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("operation", choices=("status", "cleanup"))
    command.add_argument("--root", type=Path, default=default_lifecycle_root())
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--task-id")
    command.add_argument("--session-id")
    return command


def summarize(records: list[dict[str, object]]) -> dict[str, int]:
    live = [record for record in records if record.get("process_running") is True]
    return {
        "record_count": len(records),
        "live_processes": len(live),
        "active_requests": sum(
            int(record.get("active_requests", 0)) for record in live
        ),
        "confirmed_live_orphans": sum(
            1
            for record in live
            if record.get("lifecycle_state") == "orphaned"
            and record.get("identity_verified") is True
            and record.get("ownership_identity_bound") is True
        ),
        "unattributed_live_processes": sum(
            1
            for record in live
            if record.get("lifecycle_state") == "unknown-owner"
            or record.get("ownership_identity_bound") is not True
        ),
        "owned_live_processes": sum(
            1
            for record in live
            if record.get("ownership_identity_bound") is True
        ),
        "stopped_processes": sum(
            1 for record in records if record.get("lifecycle_state") == "stopped"
        ),
    }


def records_for_scope(
    records: list[dict[str, object]],
    *,
    task_id: str | None,
    session_id: str | None,
) -> list[dict[str, object]]:
    if task_id is None and session_id is None:
        return records
    return [
        record
        for record in records
        if (
            record.get("task_id") == task_id
            and record.get("session_id") == session_id
        )
        or (
            record.get("process_running") is True
            and record.get("ownership_identity_bound") is not True
        )
    ]


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.operation == "cleanup" and (
        not args.task_id or not args.session_id
    ):
        argument_parser.error("cleanup requires --task-id and --session-id")
    root = args.root.expanduser().absolute()
    records = records_for_scope(
        inspect_mcp_process_records(root),
        task_id=args.task_id,
        session_id=args.session_id,
    )
    records_before_cleanup = list(records)
    confirmed = sorted(
        int(record["process_id"])
        for record in records
        if record.get("lifecycle_state") == "orphaned"
        and record.get("identity_verified") is True
        and record.get("ownership_identity_bound") is True
        and int(record.get("active_requests", 0)) == 0
    )
    cleaned: list[int] = []
    if args.operation == "cleanup" and not args.dry_run:
        cleaned = cleanup_confirmed_orphaned_mcp_processes(
            root,
            task_id=args.task_id,
            session_id=args.session_id,
        )
        records = records_for_scope(
            inspect_mcp_process_records(root),
            task_id=args.task_id,
            session_id=args.session_id,
        )
    summary = summarize(records)
    closeout_checks = {
        "active_requests_zero": summary["active_requests"] == 0,
        "confirmed_live_orphans_zero": summary["confirmed_live_orphans"] == 0,
        "unattributed_live_processes_zero": (
            summary["unattributed_live_processes"] == 0
        ),
        "owned_live_processes_zero": summary["owned_live_processes"] == 0,
    }
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "operation": args.operation,
                "root": str(root),
                "task_id": args.task_id,
                "session_id": args.session_id,
                "records": records,
                "records_before_cleanup": (
                    records_before_cleanup if args.operation == "cleanup" else []
                ),
                "confirmed_orphaned_processes": confirmed,
                "cleaned_processes": cleaned,
                "dry_run": bool(args.dry_run),
                "summary": summary,
                "closeout_checks": closeout_checks,
                "task_closeout_ready": all(closeout_checks.values()),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
