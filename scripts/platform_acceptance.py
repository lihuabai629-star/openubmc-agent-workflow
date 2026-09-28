#!/usr/bin/env python3
"""Capture platform command evidence and assess the #284 source acceptance matrix.

This is an evidence index, not a replacement for the existing validation,
plugin qualification, Runtime safety, or release gates.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any


SCHEMA = "openubmc-agent-workflow.platform-acceptance.v1"
REPOSITORY = "lihuabai629-star/openubmc-agent-workflow"
WORKFLOW_PATH = ".github/workflows/validate.yml"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
ROWS: dict[str, tuple[str, ...]] = {
    "linux-x86_64": ("complete_repository_validation", "immutable_plugin_qualification"),
    "windows-bootstrap": ("native_plugin_activation", "unavailable_mcp_setup"),
    "windows-wsl-runtime": (
        "healthy_mcp", "unavailable_mcp", "shell_fallback_receipt",
        "credential_reuse", "one_effect_after_interruption",
    ),
    "desktop-synthetic": ("same_run_outcome",),
    "hosted-ci": ("ci_preflight", "ci_complete_validation", "ci_windows_bootstrap"),
}
OPTIONAL_ROWS = {"macos-arm64-local": ("local_regressions",)}
CI_JOBS = {
    "CI contract preflight": "ci_preflight",
    "Complete repository validation": "ci_complete_validation",
    "Windows marketplace bootstrap": "ci_windows_bootstrap",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _full_commit(value: object) -> bool:
    return isinstance(value, str) and COMMIT.fullmatch(value) is not None


def _hash(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def initial_report(source_commit: str) -> dict[str, object]:
    if not _full_commit(source_commit):
        raise ValueError("source commit must be a full lowercase Git SHA")
    return {
        "schema": SCHEMA,
        "source_commit": source_commit,
        "rows": [
            {"id": row_id, "status": "untested", "reason": "No native evidence for this source commit"}
            for row_id in (*ROWS, *OPTIONAL_ROWS)
        ],
    }


def _host(row: dict[str, Any], role: str, os_name: str, arch: str, environment: str) -> bool:
    hosts = row.get("hosts")
    if not isinstance(hosts, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("role") == role
        and item.get("os") == os_name
        and item.get("architecture") == arch
        and item.get("environment") == environment
        and _nonempty(item.get("identity"))
        for item in hosts
    )


def _desktop_host(row: dict[str, Any]) -> bool:
    return any(
        _host(row, "client", os_name, architecture, "native-desktop")
        for os_name, architecture in (
            ("Darwin", "arm64"), ("Darwin", "x86_64"),
            ("Windows", "x86_64"), ("Linux", "x86_64"),
        )
    )


def _commands_valid(row: dict[str, Any], *, require_counts: bool = False) -> bool:
    commands = row.get("commands")
    if not isinstance(commands, list) or not commands:
        return False
    for command in commands:
        if not isinstance(command, dict):
            return False
        argv = command.get("argv")
        if (
            not isinstance(argv, list) or not argv
            or not all(_nonempty(part) for part in argv)
            or type(command.get("exit_code")) is not int
            or command["exit_code"] != 0
            or not _hash(command.get("stdout_sha256"))
            or not _hash(command.get("stderr_sha256"))
        ):
            return False
    if require_counts:
        totals = row.get("test_counts")
        if (
            not isinstance(totals, dict)
            or type(totals.get("python")) is not int or totals["python"] < 1
            or type(totals.get("node")) is not int or totals["node"] < 1
        ):
            return False
    return True


def _checks_valid(row: dict[str, Any], required: tuple[str, ...]) -> bool:
    checks = row.get("checks")
    return isinstance(checks, dict) and all(
        isinstance(checks.get(name), dict)
        and checks[name].get("passed") is True
        and _hash(checks[name].get("evidence_sha256"))
        for name in required
    )


def _ran_script(row: dict[str, Any], name: str, *, full: bool = False) -> bool:
    commands = row.get("commands")
    if not isinstance(commands, list):
        return False
    for command in commands:
        if not isinstance(command, dict):
            continue
        argv = command.get("argv", [])
        if not isinstance(argv, list):
            continue
        if any(isinstance(part, str) and part.endswith(name) for part in argv):
            if full and any(part in ("--quick", "--release-contract-only") for part in argv):
                continue
            return True
    return False


def _ci_valid(row: dict[str, Any], source_commit: str) -> bool:
    run = row.get("ci_run")
    if not isinstance(run, dict):
        return False
    if (
        type(run.get("id")) is not int or run["id"] <= 0
        or run.get("repository") != REPOSITORY
        or run.get("path") != WORKFLOW_PATH
        or run.get("head_sha") != source_commit
        or run.get("event") not in ("push", "pull_request")
        or run.get("conclusion") != "success"
    ):
        return False
    jobs = run.get("jobs")
    if not isinstance(jobs, list):
        return False
    for name in CI_JOBS:
        matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == name]
        if (
            len(matches) != 1 or matches[0].get("conclusion") != "success"
            or type(matches[0].get("steps_count")) is not int
            or matches[0]["steps_count"] < 1
        ):
            return False
    return True


def _row_errors(row: dict[str, Any], source_commit: str) -> list[str]:
    row_id = row["id"]
    status = row.get("status")
    if status not in ("passed", "failed", "untested"):
        return ["status must be passed, failed, or untested"]
    if status != "passed":
        return [] if _nonempty(row.get("reason")) else ["non-passing row needs a reason"]
    errors: list[str] = []
    if row.get("source_commit") != source_commit:
        errors.append("source commit differs from matrix")
    if row.get("source_clean") is not True:
        errors.append("source checkout was not clean")
    if not _timestamp(row.get("observed_at")):
        errors.append("observation time is missing")
    if not _nonempty(row.get("client_version")) or not _nonempty(row.get("runtime_version")):
        errors.append("client or Runtime version is missing")
    if not _hash(row.get("package_sha256")):
        errors.append("package SHA-256 is missing")
    toolchain = row.get("toolchain")
    required_tools = ("installer",) if row_id == "desktop-synthetic" else ("python", "node", "codex")
    if not isinstance(toolchain, dict) or not all(_nonempty(toolchain.get(name)) for name in required_tools):
        errors.append("toolchain identity is incomplete")
    artifacts = row.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts or not all(_hash(value) for value in artifacts.values()):
        errors.append("artifact SHA-256 evidence is missing")
    if not _commands_valid(row, require_counts=row_id == "linux-x86_64"):
        errors.append("commands, exit codes, log hashes or test counts are incomplete")
    if not _checks_valid(row, (ROWS | OPTIONAL_ROWS)[row_id]):
        errors.append("required checks lack passing evidence digests")

    if row_id == "linux-x86_64":
        if not _ran_script(row, "scripts/validate_workflow.py", full=True) or not _ran_script(
            row, "scripts/qualify_plugin.py"
        ):
            errors.append("existing complete validation and plugin qualification commands are required")
        if not _host(row, "client", "Linux", "x86_64", "native-linux") or not _host(
            row, "runtime", "Linux", "x86_64", "native-linux"
        ):
            errors.append("native Linux x86_64 client and Runtime hosts are required")
        if row.get("emulated") is not False:
            errors.append("emulated Linux cannot certify native x86_64")
    elif row_id == "windows-bootstrap":
        if not _ran_script(row, "scripts/qualify_windows_plugin.ps1"):
            errors.append("existing native Windows qualification command is required")
        if not _host(row, "client", "Windows", "x86_64", "native-windows"):
            errors.append("native Windows x86_64 client is required")
    elif row_id == "windows-wsl-runtime":
        if not _host(row, "client", "Windows", "x86_64", "native-windows") or not _host(
            row, "runtime", "Linux", "x86_64", "wsl2"
        ) or not _host(row, "target", "Synthetic", "none", "fixture"):
            errors.append("Windows client, selected WSL Runtime and synthetic target must be separate")
        routing = row.get("routing")
        if not isinstance(routing, dict) or (
            not _nonempty(routing.get("selected_wsl"))
            or not _nonempty(routing.get("credential_revision_before"))
            or routing.get("credential_revision_before") != routing.get("credential_revision_after")
            or not _nonempty(routing.get("run_id_before"))
            or routing.get("run_id_before") != routing.get("run_id_after")
            or routing.get("effect_count_before") != 1
            or routing.get("effect_count_after") != 1
        ):
            errors.append("WSL selection, credential reuse or single-Effect continuity is unproven")
    elif row_id == "desktop-synthetic":
        if not _desktop_host(row) or not _host(
            row, "target", "Synthetic", "none", "fixture"
        ):
            errors.append("Desktop installer and synthetic target hosts are required")
        same = row.get("same_run_outcome")
        if not isinstance(same, dict) or (
            not _nonempty(same.get("plugin_run_id"))
            or same.get("plugin_run_id") != same.get("desktop_run_id")
            or not _hash(same.get("plugin_outcome_sha256"))
            or same.get("plugin_outcome_sha256") != same.get("desktop_outcome_sha256")
            or not _full_commit(row.get("desktop_source_commit"))
        ):
            errors.append("same Run/Outcome or Desktop source identity is unproven")
    elif row_id == "hosted-ci":
        if not _host(row, "linux-runner", "Linux", "x86_64", "github-hosted") or not _host(
            row, "windows-runner", "Windows", "x86_64", "github-hosted"
        ) or not _ci_valid(row, source_commit):
            errors.append("exact-commit hosted CI jobs did not all execute successfully")
    elif row_id == "macos-arm64-local":
        if not _host(row, "client", "Darwin", "arm64", "native-macos"):
            errors.append("local Mac host identity is missing")
    if row_id in ("linux-x86_64", "windows-bootstrap", "windows-wsl-runtime"):
        if isinstance(artifacts, dict) and artifacts.get("plugin_archive") != row.get("package_sha256"):
            errors.append("plugin archive artifact does not match package digest")
    if row_id == "desktop-synthetic":
        if isinstance(artifacts, dict) and artifacts.get("desktop_installer") != row.get("package_sha256"):
            errors.append("Desktop installer artifact does not match package digest")
    return errors


def assess(
    report: dict[str, Any], *, candidate_archive: Path | None = None
) -> dict[str, Any]:
    if report.get("schema") != SCHEMA or not _full_commit(report.get("source_commit")):
        raise ValueError("invalid platform matrix schema or source commit")
    validation_mode = report.get("validation_mode", "hosted-ci")
    if validation_mode not in ("hosted-ci", "installed-candidate"):
        raise ValueError("unsupported platform validation mode")
    rows = report.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("platform matrix rows must be objects")
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = row.get("id")
        if row_id not in (ROWS | OPTIONAL_ROWS) or row_id in selected:
            raise ValueError("platform matrix has an unknown or duplicate row")
        selected[row_id] = row
    if set(selected) != set(ROWS | OPTIONAL_ROWS):
        raise ValueError("platform matrix must contain every required and optional row")
    blockers: list[str] = []
    required_rows = set(ROWS)
    if validation_mode == "installed-candidate":
        required_rows.remove("hosted-ci")
        if selected["hosted-ci"].get("status") != "untested":
            blockers.append(
                "installed candidate validation requires an untested hosted-CI row"
            )
        if not _hash(report.get("candidate_archive_sha256")):
            blockers.append("installed candidate archive SHA-256 is missing")
        if candidate_archive is None or not candidate_archive.is_file():
            blockers.append("installed candidate archive is missing")
        elif file_digest(candidate_archive) != report.get("candidate_archive_sha256"):
            blockers.append("installed candidate archive bytes differ from the matrix")
        ci_run = selected["hosted-ci"].get("ci_run")
        if ci_run is not None:
            jobs = ci_run.get("jobs") if isinstance(ci_run, dict) else None
            if not isinstance(jobs, list) or any(
                not isinstance(job, dict) or job.get("steps_count") != 0
                for job in jobs
            ):
                blockers.append(
                    "executed hosted CI cannot be waived by installed candidate validation"
                )
    for row_id, row in selected.items():
        errors = _row_errors(row, report["source_commit"])
        if errors:
            blockers.extend(f"{row_id}: {error}" for error in errors)
        if row_id in required_rows and row.get("status") != "passed":
            blockers.append(f"{row_id}: {row.get('status')} - {row.get('reason', 'reason missing')}")
    passed = [selected[row_id] for row_id in ("linux-x86_64", "windows-bootstrap", "windows-wsl-runtime", "hosted-ci") if selected[row_id].get("status") == "passed"]
    digests = {row["package_sha256"] for row in passed if _hash(row.get("package_sha256"))}
    if len(digests) > 1:
        blockers.append("Linux, Windows and hosted CI plugin package digests differ")
    if validation_mode == "installed-candidate" and digests != {
        report.get("candidate_archive_sha256")
    }:
        blockers.append("installed candidate package digest differs from native platform rows")
    evaluated = {key: value for key, value in report.items() if key not in ("release_ready", "blockers", "evidence_digest")}
    evaluated["release_ready"] = not blockers
    evaluated["blockers"] = blockers
    evaluated["evidence_digest"] = _digest(_canonical(evaluated))
    return evaluated


def parse_test_counts(stdout: bytes, stderr: bytes) -> dict[str, int]:
    output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    return {
        "python": sum(int(value) for value in re.findall(r"\bRan (\d+) tests?\b", output)),
        "node": sum(int(value) for value in re.findall(r"(?m)^# tests (\d+)\s*$", output)),
    }


def _version(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def capture_command(argv: list[str], *, cwd: Path, log_dir: Path, artifacts: dict[str, Path]) -> dict[str, Any]:
    if not argv:
        raise ValueError("capture requires a command after --")
    if log_dir.exists():
        raise FileExistsError(f"capture log directory already exists: {log_dir}")
    log_dir.mkdir(parents=True, mode=0o700)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    with os.fdopen(os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stdout, os.fdopen(
        os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as stderr:
        exit_code = subprocess.run(argv, cwd=cwd, stdout=stdout, stderr=stderr, check=False).returncode
    out = stdout_path.read_bytes()
    err = stderr_path.read_bytes()
    missing_artifacts = [name for name, path in artifacts.items() if not path.is_file()]
    if missing_artifacts:
        raise ValueError("capture artifacts are missing: " + ", ".join(missing_artifacts))
    source_commit = _version(["git", "-C", str(cwd), "rev-parse", "HEAD"])
    git_status = _version(["git", "-C", str(cwd), "status", "--porcelain=v1"])
    invoked_python = (
        _version([argv[0], "--version"])
        if Path(argv[0]).name.lower().startswith("python") else None
    )
    return {
        "argv": argv,
        "cwd": str(cwd.resolve()),
        "exit_code": exit_code,
        "stdout_sha256": _digest(out),
        "stderr_sha256": _digest(err),
        "test_counts": parse_test_counts(out, err),
        "artifacts": {name: file_digest(path) for name, path in artifacts.items()},
        "source_commit": source_commit,
        "source_clean": git_status == "",
        "toolchain": {
            "python": invoked_python,
            "node": _version(["node", "--version"]),
            "npm": _version(["npm", "--version"]),
            "codex": _version(["codex", "--version"]),
        },
        "host": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "platform": platform.platform(),
            "collector_python_version": platform.python_version(),
        },
        "log_paths": {"stdout": str(stdout_path), "stderr": str(stderr_path)},
    }


def evaluate_ci_run(run: dict[str, Any], jobs: dict[str, Any], *, source_commit: str) -> dict[str, Any]:
    listed_jobs = jobs.get("jobs")
    if not isinstance(listed_jobs, list):
        raise ValueError("GitHub jobs response is incomplete")
    summary = {
        "id": run.get("id"),
        "repository": REPOSITORY,
        "path": run.get("path"),
        "head_sha": run.get("head_sha"),
        "event": run.get("event"),
        "conclusion": run.get("conclusion"),
        "url": run.get("html_url"),
        "jobs": [
            {"name": job.get("name"), "conclusion": job.get("conclusion"),
             "steps_count": len(job.get("steps") or []), "url": job.get("html_url")}
            for job in listed_jobs if isinstance(job, dict) and job.get("name") in CI_JOBS
        ],
    }
    if run.get("head_sha") != source_commit:
        raise ValueError("CI run is for a different source commit")
    return {"ci_run": summary, "passed": _ci_valid({"ci_run": summary}, source_commit)}


def _gh_json(endpoint: str) -> dict[str, Any]:
    result = subprocess.run(["gh", "api", endpoint], check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"GitHub API query failed with exit code {result.returncode}")
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ValueError("GitHub API returned a non-object")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="create an untested matrix for one immutable source commit")
    init.add_argument("--source-commit", required=True)
    init.add_argument("--output", type=Path, required=True)
    init.add_argument(
        "--validation-mode", choices=("hosted-ci", "installed-candidate"),
        default="hosted-ci",
    )
    init.add_argument("--candidate-archive", type=Path)
    verify = commands.add_parser("verify", help="assess a matrix without changing existing release gates")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    verify.add_argument("--candidate-archive", type=Path)
    verify.add_argument("--expected-source-commit")
    capture = commands.add_parser("capture", help="record command status, test counts and log/artifact hashes")
    capture.add_argument("--cwd", type=Path, default=Path.cwd())
    capture.add_argument("--log-dir", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--artifact", action="append", default=[], metavar="NAME=PATH")
    capture.add_argument("command", nargs=argparse.REMAINDER)
    ci = commands.add_parser("collect-ci", help="read exact-commit hosted CI run and job status")
    ci.add_argument("--source-commit", required=True)
    ci.add_argument("--run-id", type=int, required=True)
    ci.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            result = initial_report(args.source_commit)
            if args.validation_mode == "installed-candidate":
                if args.candidate_archive is None or not args.candidate_archive.is_file():
                    raise ValueError("installed candidate initialization requires an archive")
                result["validation_mode"] = "installed-candidate"
                result["candidate_archive_sha256"] = file_digest(args.candidate_archive)
            elif args.candidate_archive is not None:
                raise ValueError("candidate archive requires installed-candidate mode")
            code = 0
        elif args.action == "verify":
            raw = json.loads(args.input.read_text(encoding="utf-8"))
            if (
                args.expected_source_commit is not None
                and raw.get("source_commit") != args.expected_source_commit
            ):
                raise ValueError("platform matrix source commit differs from expected release source")
            result = assess(raw, candidate_archive=args.candidate_archive)
            code = 0 if result["release_ready"] else 1
        elif args.action == "capture":
            artifacts: dict[str, Path] = {}
            for item in args.artifact:
                name, separator, path = item.partition("=")
                if not separator or not name or not path or name in artifacts:
                    raise ValueError("--artifact must be a unique NAME=PATH")
                artifacts[name] = Path(path)
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            result = capture_command(command, cwd=args.cwd, log_dir=args.log_dir, artifacts=artifacts)
            code = result["exit_code"]
        else:
            if not _full_commit(args.source_commit) or args.run_id <= 0:
                raise ValueError("collect-ci requires a full commit and positive run ID")
            base = f"repos/{REPOSITORY}/actions/runs/{args.run_id}"
            run = _gh_json(base)
            jobs = _gh_json(base + "/jobs?per_page=100")
            if jobs.get("total_count") != len(jobs.get("jobs", [])):
                raise ValueError("CI run has more than one page of jobs")
            result = evaluate_ci_run(run, jobs, source_commit=args.source_commit)
            code = 0 if result["passed"] else 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(_canonical(result))
        print(json.dumps({"output": str(args.output), "exit_code": code,
                          "release_ready": result.get("release_ready")}, sort_keys=True))
        return code
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"platform acceptance: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
