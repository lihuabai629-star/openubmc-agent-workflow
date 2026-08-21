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
TRUSTED_CHECK_APP_ID = 15368
TRUSTED_CHECK_APP_SLUG = "github-actions"
TRUSTED_WORKFLOW_PATH = ".github/workflows/validate.yml"
TRUSTED_WORKFLOW_EVENTS = frozenset({"pull_request", "push"})


def _check_status(check: Mapping[str, object]) -> str:
    if str(check.get("status", "")) != "completed":
        return "pending"
    return "passed" if str(check.get("conclusion", "")) == "success" else "failed"


def _from_trusted_check_app(check: Mapping[str, object]) -> bool:
    app = check.get("app")
    if not isinstance(app, Mapping):
        return False
    try:
        app_id = int(app.get("id", 0))
    except (TypeError, ValueError):
        return False
    return (
        app_id == TRUSTED_CHECK_APP_ID
        and str(app.get("slug", "")) == TRUSTED_CHECK_APP_SLUG
    )


def _trusted_workflow_runs(
    payload: Mapping[str, object],
    *,
    commit: str,
) -> dict[int, Mapping[str, object]]:
    raw_runs = payload.get("workflow_runs", [])
    runs = (
        [item for item in raw_runs if isinstance(item, Mapping)]
        if isinstance(raw_runs, list)
        else []
    )
    trusted: dict[int, Mapping[str, object]] = {}
    for run in runs:
        try:
            check_suite_id = int(run.get("check_suite_id", 0))
        except (TypeError, ValueError):
            continue
        if (
            check_suite_id > 0
            and str(run.get("path", "")) == TRUSTED_WORKFLOW_PATH
            and str(run.get("event", "")) in TRUSTED_WORKFLOW_EVENTS
            and str(run.get("head_sha", "")) == commit
        ):
            trusted[check_suite_id] = run
    return trusted


def _check_suite_id(check: Mapping[str, object]) -> int:
    suite = check.get("check_suite")
    if not isinstance(suite, Mapping):
        return 0
    try:
        return int(suite.get("id", 0))
    except (TypeError, ValueError):
        return 0


def evaluate_check_runs(
    payload: Mapping[str, object],
    workflow_payload: Mapping[str, object],
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
    trusted_workflows = _trusted_workflow_runs(
        workflow_payload,
        commit=commit,
    )
    required: list[dict[str, object]] = []
    for name in REQUIRED_CHECKS:
        matches = [
            item
            for item in runs
            if str(item.get("name", "")) == name
            and str(item.get("head_sha", "")) == commit
            and _from_trusted_check_app(item)
            and _check_suite_id(item) in trusted_workflows
        ]
        selected = max(
            matches,
            key=lambda item: int(item.get("id", 0)),
            default=None,
        )
        if selected is None:
            required.append({"name": name, "status": "missing"})
            continue
        workflow = trusted_workflows[_check_suite_id(selected)]
        required.append(
            {
                "name": name,
                "status": _check_status(selected),
                "check_run_id": int(selected.get("id", 0)),
                "conclusion": str(selected.get("conclusion", "")),
                "url": str(selected.get("html_url", "")),
                "app_id": TRUSTED_CHECK_APP_ID,
                "app_slug": TRUSTED_CHECK_APP_SLUG,
                "workflow_run_id": int(workflow.get("id", 0)),
                "workflow_id": int(workflow.get("workflow_id", 0)),
                "workflow_path": str(workflow.get("path", "")),
                "workflow_event": str(workflow.get("event", "")),
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
    workflow_command = [
        "gh",
        "api",
        "--method",
        "GET",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        f"repos/{repository}/actions/runs",
        "-f",
        f"head_sha={commit}",
        "-f",
        "per_page=100",
    ]
    workflow_completed = runner(
        workflow_command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if workflow_completed.returncode:
        raise RuntimeError(
            "GitHub workflow runs query failed: "
            + (workflow_completed.stderr or "").strip()
        )
    workflow_payload = json.loads(workflow_completed.stdout)
    if not isinstance(workflow_payload, Mapping):
        raise RuntimeError("GitHub workflow runs response must be an object")
    return evaluate_check_runs(
        payload,
        workflow_payload,
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
