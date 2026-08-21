#!/usr/bin/env python3
"""Collect and verify required GitHub Check Runs for one candidate commit."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import json
from pathlib import Path
import subprocess


SCHEMA = "openubmc-agent-workflow.github-ci-evidence.v1"
REQUIRED_CHECKS = (
    "CI contract preflight",
    "Complete repository validation",
)


def _check_status(check: Mapping[str, object]) -> str:
    if str(check.get("status", "")) != "completed":
        return "pending"
    return "passed" if str(check.get("conclusion", "")) == "success" else "failed"


def evaluate_check_runs(
    payload: Mapping[str, object],
    *,
    repository: str,
    commit: str,
) -> dict[str, object]:
    raw_runs = payload.get("check_runs", [])
    runs = (
        [item for item in raw_runs if isinstance(item, Mapping)]
        if isinstance(raw_runs, list)
        else []
    )
    required: list[dict[str, object]] = []
    for name in REQUIRED_CHECKS:
        matches = [
            item
            for item in runs
            if str(item.get("name", "")) == name
            and str(item.get("head_sha", "")) == commit
        ]
        selected = max(
            matches,
            key=lambda item: int(item.get("id", 0)),
            default=None,
        )
        if selected is None:
            required.append({"name": name, "status": "missing"})
            continue
        required.append(
            {
                "name": name,
                "status": _check_status(selected),
                "check_run_id": int(selected.get("id", 0)),
                "conclusion": str(selected.get("conclusion", "")),
                "url": str(selected.get("html_url", "")),
            }
        )
    return {
        "schema": SCHEMA,
        "repository": repository,
        "source_commit": commit,
        "promotable": all(item["status"] == "passed" for item in required),
        "required_checks": required,
    }


def collect_evidence(
    *,
    repository: str,
    commit: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    command = [
        "gh",
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"repos/{repository}/commits/{commit}/check-runs",
        "-f",
        "per_page=100",
    ]
    completed = runner(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise RuntimeError(
            "GitHub Check Runs query failed: " + (completed.stderr or "").strip()
        )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, Mapping):
        raise RuntimeError("GitHub Check Runs response must be an object")
    return evaluate_check_runs(
        payload,
        repository=repository,
        commit=commit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = collect_evidence(
        repository=args.repository,
        commit=args.commit,
    )
    output = args.output.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if evidence["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
