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
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = args.root.expanduser().absolute()
    records = inspect_mcp_process_records(root)
    confirmed = sorted(
        int(record["process_id"])
        for record in records
        if record.get("lifecycle_state") == "orphaned"
        and record.get("identity_verified") is True
        and int(record.get("active_requests", 0)) == 0
    )
    cleaned: list[int] = []
    if args.operation == "cleanup" and not args.dry_run:
        cleaned = cleanup_confirmed_orphaned_mcp_processes(root)
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "operation": args.operation,
                "root": str(root),
                "records": records,
                "confirmed_orphaned_processes": confirmed,
                "cleaned_processes": cleaned,
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
