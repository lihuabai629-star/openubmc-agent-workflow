#!/usr/bin/env python3
"""Prepare and run isolated evaluation harness qualifications."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.release import build_release_lock  # noqa: E402
from scripts.evidence_report import (  # noqa: E402
    evidence_fingerprint,
    source_commit as selected_source_commit,
)


SCHEMA = "openubmc-agent-workflow.evaluation-harness.v1"
_SEMVER = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)


def _mapping(value: object, *, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} must be an object")
    return value


def workflow_metadata(workspace: Path) -> dict[str, object]:
    path = workspace / "workflow.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"workflow metadata is unavailable: {path}") from exc
    return dict(_mapping(value, description="workflow metadata"))


def harness_contract(workspace: Path, name: str) -> dict[str, object]:
    workflow = workflow_metadata(workspace)
    harnesses = _mapping(
        workflow.get("evaluation_harnesses"),
        description="evaluation_harnesses",
    )
    contract = harnesses.get(name)
    if not isinstance(contract, Mapping):
        raise ValueError(f"unknown evaluation harness: {name}")
    if contract.get("role") != "evaluation-harness":
        raise ValueError(f"{name} is not declared as an evaluation harness")
    if name in _mapping(workflow.get("clients"), description="clients"):
        raise ValueError(f"{name} cannot also be a supported product client")
    return dict(contract)


def _parse_semver(value: str) -> tuple[int, int, int, tuple[int, object]]:
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid semantic version: {value!r}")
    prerelease = match.group("prerelease")
    if prerelease is None:
        suffix: tuple[int, object] = (1, 0)
    else:
        rc = re.fullmatch(r"rc\.([0-9]+)", prerelease)
        if rc is None:
            raise ValueError(f"unsupported semantic prerelease: {value!r}")
        suffix = (0, int(rc.group(1)))
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        suffix,
    )


def _version_in_range(actual: str, minimum: str, maximum_exclusive: str) -> bool:
    selected = _parse_semver(actual)
    return _parse_semver(minimum) <= selected < _parse_semver(maximum_exclusive)


def _run_version(
    command: Sequence[str],
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str]:
    completed = executor(
        list(command),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return (output[-1].strip() if output else "", "" if completed.returncode == 0 else "command failed")


def preflight_harness(
    workspace: Path,
    name: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    executable_path: str | None = None,
) -> dict[str, object]:
    contract = harness_contract(workspace, name)
    executable = _mapping(contract.get("executable"), description=f"{name} executable")
    command = str(executable.get("command", "")).strip()
    package = str(executable.get("package", "")).strip()
    minimum = str(executable.get("minimum_version", "")).strip()
    maximum = str(executable.get("maximum_version_exclusive", "")).strip()
    version_args = executable.get("version_args", ["--version"])
    if not isinstance(version_args, list) or not all(
        isinstance(item, str) and item for item in version_args
    ):
        raise ValueError(f"{name} executable version_args must be strings")
    issues: list[str] = []
    executable_path = executable_path or (which(command) if command else None)
    actual_version = ""
    if executable_path is None:
        issues.append(f"{command or name} executable is unavailable")
    else:
        actual_version, version_error = _run_version(
            [executable_path, *version_args],
            executor=executor,
        )
        if version_error:
            issues.append(f"{name} version check failed")
        else:
            try:
                compatible = _version_in_range(actual_version, minimum, maximum)
            except ValueError:
                compatible = False
            if not compatible:
                issues.append(
                    f"{name} version {actual_version or 'unknown'} is incompatible; "
                    f"require >= {minimum} and < {maximum}"
                )

    node_path = which("node")
    node_version = ""
    if node_path is None:
        issues.append("Node.js executable is unavailable")
    else:
        raw_node, node_error = _run_version([node_path, "--version"], executor=executor)
        node_version = raw_node.removeprefix("v")
        if node_error:
            issues.append("Node.js version check failed")
        else:
            try:
                major, minor, _patch, _suffix = _parse_semver(node_version)
                node_compatible = major >= 24 or (major == 22 and minor >= 19)
            except ValueError:
                node_compatible = False
            if not node_compatible:
                requirement = _mapping(
                    contract.get("runtime_requirements"),
                    description=f"{name} runtime_requirements",
                ).get("node", "^22.19.0 || >=24.0.0")
                issues.append(
                    f"Node.js version {node_version or 'unknown'} is incompatible; "
                    f"require {requirement}"
                )

    install_range = f">={minimum} <{maximum}"
    return {
        "schema": f"{SCHEMA}/preflight-v1",
        "harness": name,
        "role": contract.get("role"),
        "adapter": contract.get("adapter"),
        "ready": not issues,
        "issues": issues,
        "install_command": [
            "npm",
            "install",
            "--global",
            f"{package}@{install_range}",
        ],
        "executable": {
            "command": command,
            "path": executable_path or "",
            "version": actual_version,
            "minimum_version": minimum,
            "maximum_version_exclusive": maximum,
        },
        "node": {
            "path": node_path or "",
            "version": node_version,
            "requirement": _mapping(
                contract.get("runtime_requirements"),
                description=f"{name} runtime_requirements",
            ).get("node", ""),
        },
        "model_identity_fields": list(contract.get("model_identity_fields", [])),
        "scenarios": list(contract.get("scenarios", [])),
    }


def install_harness(
    workspace: Path,
    name: str,
    *,
    install_root: Path,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    workspace = workspace.expanduser().absolute()
    install_root = install_root.expanduser().absolute()
    contract = harness_contract(workspace, name)
    executable = _mapping(contract.get("executable"), description=f"{name} executable")
    package = str(executable.get("package", "")).strip()
    minimum = str(executable.get("minimum_version", "")).strip()
    maximum = str(executable.get("maximum_version_exclusive", "")).strip()
    install_root.mkdir(parents=True, exist_ok=True)
    install_command = [
        "npm",
        "install",
        "--prefix",
        str(install_root),
        "--no-save",
        f"{package}@>={minimum} <{maximum}",
    ]
    completed = executor(
        install_command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return {
            "schema": f"{SCHEMA}/install-v1",
            "harness": name,
            "ready": False,
            "install_root": str(install_root),
            "install_command": install_command,
            "issues": [
                (completed.stderr or completed.stdout or "harness installation failed").strip()
            ],
        }
    command_name = str(executable.get("command", name)).strip()
    installed_executable = install_root / "node_modules" / ".bin" / command_name
    if not installed_executable.is_file():
        return {
            "schema": f"{SCHEMA}/install-v1",
            "harness": name,
            "ready": False,
            "install_root": str(install_root),
            "install_command": install_command,
            "issues": [f"installed {command_name} executable is unavailable"],
        }
    report = preflight_harness(
        workspace,
        name,
        which=which,
        executor=executor,
        executable_path=str(installed_executable),
    )
    report.update(
        {
            "schema": f"{SCHEMA}/install-v1",
            "install_root": str(install_root),
            "install_command": install_command,
        }
    )
    return report


def _isolated_environment(
    base_environment: Mapping[str, str],
    *,
    run_root: Path,
    source_commit: str,
    task_id: str,
    execution_id: str,
    credentials_file: Path | None,
) -> dict[str, str]:
    blocked_prefixes = ("CODEX_", "CLAUDE_", "DSH_", "OPENUBMC_")
    environment = {
        name: value
        for name, value in base_environment.items()
        if name not in {"HOME", "XDG_CONFIG_HOME"}
        and not name.startswith(blocked_prefixes)
    }
    environment.update(
        {
            "HOME": str(run_root / "home"),
            "XDG_CONFIG_HOME": str(run_root / "config"),
            "DSH_HOME": str(run_root / "dsh-home"),
            "DSH_SESSION_ROOT": str(run_root / "sessions"),
            "DSH_TELEMETRY_DISABLED": "1",
            "OPENUBMC_TARGET_RUNTIME_STATE_DIR": str(run_root / "runtime-state"),
            "OPENUBMC_MCP_CLIENT": "dsh",
            "OPENUBMC_MCP_TASK_ID": task_id,
            "OPENUBMC_MCP_SESSION_ID": execution_id,
            "OPENUBMC_MCP_LIFECYCLE_DIR": str(run_root / "mcp-processes"),
            "OPENUBMC_EVALUATION_SOURCE_COMMIT": source_commit,
            "OPENUBMC_EVALUATION_TASK_ID": task_id,
            "OPENUBMC_EVALUATION_EXECUTION_ID": execution_id,
        }
    )
    if credentials_file is not None:
        environment["OPENUBMC_CREDENTIALS_FILE"] = str(credentials_file)
        environment["OPENUBMC_DEBUG_CREDENTIALS_FILE"] = str(credentials_file)
    return environment


def _dsh_mcp_patch(workspace: Path, *, include_credentials: bool) -> str:
    runtime_entry = (
        workspace
        / "openubmc-debug"
        / "scripts"
        / "target_runtime_mcp.py"
    ).resolve()
    if not runtime_entry.is_file():
        raise ValueError(f"target Runtime MCP entry is unavailable: {runtime_entry}")
    lines = [
        "- id: session-persistence-jsonl",
        "  config:",
        "    root: !!js process.env.DSH_SESSION_ROOT",
        "    compression: none",
        "    packChunks: false",
        "",
        "- id: session-title-llm",
        "  disabled: true",
        "",
        "- insert:",
        "    - id: mcp-openubmc-target-runtime",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        "        serverName: openubmc-target-runtime",
        "        transport: stdio",
        f"        command: {Path(sys.executable).resolve()}",
        "        args:",
        f"          - {runtime_entry}",
        "        env:",
        "          OPENUBMC_TARGET_RUNTIME_STATE_DIR: !!js process.env.OPENUBMC_TARGET_RUNTIME_STATE_DIR",
        "          OPENUBMC_MCP_CLIENT: !!js process.env.OPENUBMC_MCP_CLIENT",
        "          OPENUBMC_MCP_TASK_ID: !!js process.env.OPENUBMC_MCP_TASK_ID",
        "          OPENUBMC_MCP_SESSION_ID: !!js process.env.OPENUBMC_MCP_SESSION_ID",
        "          OPENUBMC_MCP_LIFECYCLE_DIR: !!js process.env.OPENUBMC_MCP_LIFECYCLE_DIR",
        "          CODEX_TASK_ID: !!js process.env.OPENUBMC_EVALUATION_TASK_ID",
        "          OPENUBMC_EVALUATION_SOURCE_COMMIT: !!js process.env.OPENUBMC_EVALUATION_SOURCE_COMMIT",
        "          OPENUBMC_EVALUATION_EXECUTION_ID: !!js process.env.OPENUBMC_EVALUATION_EXECUTION_ID",
    ]
    if include_credentials:
        lines.extend(
            (
                "          OPENUBMC_CREDENTIALS_FILE: !!js process.env.OPENUBMC_CREDENTIALS_FILE",
                "          OPENUBMC_DEBUG_CREDENTIALS_FILE: !!js process.env.OPENUBMC_DEBUG_CREDENTIALS_FILE",
            )
        )
    lines.extend(
        (
            "        toolCallTimeoutMs: 900000",
            "        failOnStartupError: true",
            "        reconnect:",
            "          enabled: false",
            "",
        )
    )
    return "\n".join(lines)


def _dsh_model_identity(settings: Path) -> dict[str, str]:
    selected: dict[str, str] = {}
    in_default_model = False
    for raw_line in settings.read_text(encoding="utf-8").splitlines():
        if raw_line.strip() == "agent-default-model:":
            in_default_model = True
            continue
        if not in_default_model:
            continue
        if raw_line and not raw_line[0].isspace():
            break
        match = re.fullmatch(r"\s+([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*", raw_line)
        if match is None:
            continue
        value = match.group(2).strip().strip("'\"")
        if value:
            selected[match.group(1)] = value
    return {
        "provider": selected.get("provider", ""),
        "model": selected.get("model", ""),
        "reasoning_effort": selected.get("reasoningEffort", ""),
    }


def _evaluation_readiness_identity(
    receipt: Mapping[str, object],
    *,
    source_commit: str,
) -> dict[str, object]:
    required = (
        "ok",
        "operational_ready",
        "release_identity_verified",
        "evaluation_ready",
    )
    if any(receipt.get(field) is not True for field in required):
        raise ValueError("formal harness runs require verified Evaluation readiness")
    source = _mapping(receipt.get("source"), description="readiness source")
    release = _mapping(receipt.get("release"), description="readiness release")
    if (
        source.get("mode") != "managed"
        or source.get("dirty") is not False
        or release.get("verified") is not True
        or release.get("trust_mode") != "verified-immutable-source"
        or str(release.get("source_commit", "")).lower() != source_commit
    ):
        raise ValueError(
            "Evaluation readiness must bind a clean verified immutable source"
        )
    return {
        "digest": evidence_fingerprint(receipt),
        "source_mode": source.get("mode"),
        "trust_mode": release.get("trust_mode"),
        "release_version": release.get("release_version"),
        "source_commit": release.get("source_commit"),
    }


def prepare_dsh_run(
    workspace: Path,
    name: str,
    *,
    run_root: Path,
    source_commit: str,
    scenario: Mapping[str, object],
    model_identity: Mapping[str, object],
    settings_source: Path,
    credentials_file: Path | None,
    evaluation_readiness: Mapping[str, object],
    preflight: Mapping[str, object] | None = None,
    source_selector: Callable[..., str] = selected_source_commit,
    base_environment: Mapping[str, str] | None = None,
    task_id: str = "",
    executable_path: str | None = None,
) -> dict[str, object]:
    workspace = workspace.expanduser().absolute()
    run_root = run_root.expanduser().absolute()
    if run_root.exists() and any(run_root.iterdir()):
        raise ValueError(f"evaluation run root must be empty: {run_root}")
    contract = harness_contract(workspace, name)
    selected_preflight = dict(
        preflight
        or preflight_harness(
            workspace,
            name,
            executable_path=executable_path,
        )
    )
    if not selected_preflight.get("ready"):
        issues = selected_preflight.get("issues", [])
        raise ValueError(f"{name} preflight failed: {issues}")
    resolved_source_commit = source_selector(source_commit, workspace=workspace)

    supported_scenarios = {
        (str(item.get("name", "")), str(item.get("version", ""))): dict(item)
        for item in contract.get("scenarios", [])
        if isinstance(item, Mapping)
    }
    selected_scenario = (
        str(scenario.get("name", "")).strip(),
        str(scenario.get("version", "")).strip(),
    )
    if selected_scenario not in supported_scenarios:
        raise ValueError(
            f"{name} does not support qualification scenario "
            f"{selected_scenario[0]} {selected_scenario[1]}"
        )
    scenario_contract = supported_scenarios[selected_scenario]
    identity_fields = contract.get("model_identity_fields", [])
    if not isinstance(identity_fields, list) or not all(
        isinstance(item, str) and item for item in identity_fields
    ):
        raise ValueError(f"{name} model_identity_fields are invalid")
    missing_identity = [
        field
        for field in identity_fields
        if not str(model_identity.get(field, "")).strip()
    ]
    if missing_identity:
        raise ValueError(
            "model identity requires fields: " + ", ".join(missing_identity)
        )
    if not settings_source.is_file():
        raise ValueError(f"isolated DSH settings are unavailable: {settings_source}")
    selected_model = {
        field: str(model_identity[field]).strip() for field in identity_fields
    }
    settings_model = _dsh_model_identity(settings_source)
    if selected_model != settings_model:
        raise ValueError(
            "declared model identity does not match isolated DSH settings"
        )
    credentials = credentials_file.expanduser().absolute() if credentials_file else None
    if selected_scenario[0] != "mcp-structured-content" and (
        credentials is None or not credentials.is_file()
    ):
        raise ValueError(
            f"{selected_scenario[0]} qualification requires a credentials file"
        )
    readiness_identity = _evaluation_readiness_identity(
        evaluation_readiness,
        source_commit=resolved_source_commit,
    )

    for directory in (
        run_root / "home",
        run_root / "config",
        run_root / "dsh-home",
        run_root / "sessions",
        run_root / "runtime-state",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(settings_source, run_root / "dsh-home" / "settings.yaml")
    mcp_config = run_root / "mcp.patch.yml"
    mcp_config.write_text(
        _dsh_mcp_patch(workspace, include_credentials=credentials is not None),
        encoding="utf-8",
    )

    workflow = workflow_metadata(workspace)
    task = task_id.strip() or f"qualification-{resolved_source_commit[:12]}"
    execution_id = evidence_fingerprint(
        {
            "harness": name,
            "source_commit": resolved_source_commit,
            "scenario": selected_scenario,
            "task_id": task,
            "run_root": str(run_root),
        }
    )
    environment = _isolated_environment(
        base_environment or os.environ,
        run_root=run_root,
        source_commit=resolved_source_commit,
        task_id=task,
        execution_id=execution_id,
        credentials_file=credentials,
    )
    executable = _mapping(
        selected_preflight.get("executable"),
        description="preflight executable",
    )
    executable_path = str(executable.get("path", "")).strip()
    if not executable_path:
        raise ValueError(f"{name} preflight did not resolve an executable")
    install_root = Path(str(selected_preflight.get("install_root", ""))).absolute()
    managed_bin = install_root / "node_modules" / ".bin"
    try:
        Path(executable_path).absolute().relative_to(managed_bin)
    except ValueError as exc:
        raise ValueError(
            "formal DSH runs require the managed isolated installation"
        ) from exc
    source_identity = build_release_lock(
        workspace,
        source_commit=resolved_source_commit,
    )
    identity = {
        "source_commit": resolved_source_commit,
        "source_tree_digest": source_identity["source_tree_digest"],
        "harness": {
            "name": name,
            "role": contract.get("role"),
            "adapter": contract.get("adapter"),
            "version": executable.get("version"),
        },
        "model": selected_model,
        "runtime": dict(source_identity["runtime"]),
        "skills": {
            "workflow_version": workflow.get("version"),
            "digests": {
                str(item["name"]): str(item["digest"])
                for item in source_identity["skills"]
            },
        },
        "scenario": {
            "name": selected_scenario[0],
            "version": selected_scenario[1],
            "acceptance": scenario_contract.get("acceptance"),
        },
        "evaluation_readiness": readiness_identity,
    }
    return {
        "schema": f"{SCHEMA}/run-plan-v1",
        "workspace": str(workspace),
        "run_root": str(run_root),
        "execution_id": execution_id,
        "command": [
            executable_path,
            "--profile",
            str(contract.get("profile", "headless")),
            "--patch",
            str(mcp_config),
        ],
        "environment": environment,
        "isolation": {
            "home": str(run_root / "home"),
            "config": str(run_root / "config"),
            "harness_home": str(run_root / "dsh-home"),
            "sessions": str(run_root / "sessions"),
            "runtime_state": str(run_root / "runtime-state"),
            "mcp_lifecycle": str(run_root / "mcp-processes"),
            "mcp_config": str(mcp_config),
        },
        "identity": identity,
    }


def _scenario_acceptance_issues(
    plan: Mapping[str, object],
    receipt: Mapping[str, object],
) -> list[str]:
    identity = _mapping(plan.get("identity"), description="harness identity")
    scenario = _mapping(identity.get("scenario"), description="harness scenario")
    issues: list[str] = []
    if receipt.get("schema") != "openubmc-agent-workflow.scenario-acceptance.v1":
        issues.append("scenario acceptance receipt schema is invalid")
    if receipt.get("execution_id") != plan.get("execution_id"):
        issues.append("scenario acceptance receipt targets another execution")
    if receipt.get("source_commit") != identity.get("source_commit"):
        issues.append("scenario acceptance receipt targets another source")
    if receipt.get("scenario") != {
        "name": scenario.get("name"),
        "version": scenario.get("version"),
    }:
        issues.append("scenario acceptance receipt targets another scenario")
    if receipt.get("accepted") is not True:
        issues.append("scenario verifier did not accept the run")
    if scenario.get("acceptance") == "terminal-outcome-v1":
        outcome = _mapping(
            receipt.get("terminal_outcome"),
            description="terminal Outcome",
        )
        if outcome.get("status") != "completed":
            issues.append("scenario requires a completed terminal Outcome")
    if scenario.get("final_answer_required") is True or scenario.get("acceptance") == "terminal-answer-v1":
        final_answer = receipt.get("final_answer")
        if not isinstance(final_answer, Mapping):
            issues.append("terminal evidence exists but final answer is missing")
        else:
            if final_answer.get("task_id") != plan.get("execution_id"):
                issues.append("final answer belongs to another task")
            if not str(final_answer.get("text", "")).strip():
                issues.append("final answer is empty")
            if not str(final_answer.get("run_id", "")).strip():
                issues.append("final answer is not bound to a terminal Run")
            if not str(final_answer.get("delivery_stage", "")).strip():
                issues.append("final answer has no delivery stage")
            outcome = receipt.get("terminal_outcome")
            if isinstance(outcome, Mapping):
                if final_answer.get("status") != outcome.get("status"):
                    issues.append("final answer status does not match terminal Outcome")
            fingerprint = str(final_answer.get("outcome_fingerprint", ""))
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
                issues.append("final answer is missing an Outcome fingerprint")
    return issues


def execute_dsh_run(
    plan: Mapping[str, object],
    prompt: str,
    *,
    acceptance_receipt: Mapping[str, object] | Callable[[], Mapping[str, object]],
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 1800,
) -> dict[str, object]:
    if not prompt.strip():
        raise ValueError("qualification prompt must not be empty")
    run_root = Path(str(plan.get("run_root", "")))
    command = plan.get("command")
    environment = plan.get("environment")
    identity = plan.get("identity")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("harness run plan command is invalid")
    if not isinstance(environment, Mapping) or not isinstance(identity, Mapping):
        raise ValueError("harness run plan is incomplete")
    prompt_path = run_root / "prompt.md"
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    prompt_path.write_text(prompt, encoding="utf-8")
    execution_environment = dict(environment)
    execution_environment["OPENUBMC_EVALUATION_PROMPT_DIGEST"] = evidence_fingerprint(
        prompt
    )
    completed = executor(
        [*command, prompt],
        cwd=Path(str(plan.get("workspace", ""))),
        env=execution_environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    raw_acceptance = (
        acceptance_receipt()
        if callable(acceptance_receipt)
        else acceptance_receipt
    )
    acceptance = dict(
        _mapping(raw_acceptance, description="scenario acceptance receipt")
    )
    issues = _scenario_acceptance_issues(plan, acceptance)
    harness_status = "completed" if completed.returncode == 0 else "failed"
    if completed.returncode != 0:
        issues.insert(0, f"DSH exited with status {completed.returncode}")
    result: dict[str, object] = {
        "schema": f"{SCHEMA}/result-v1",
        "identity": dict(identity),
        "exit_code": completed.returncode,
        "harness_status": harness_status,
        "status": "passed" if not issues else "failed",
        "issues": issues,
        "scenario_acceptance": {
            "accepted": acceptance.get("accepted") is True,
            "digest": evidence_fingerprint(acceptance),
        },
        "stdout": stdout,
        "stderr": stderr,
        "artifacts": {
            "prompt": str(prompt_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "sessions": _mapping(
                plan.get("isolation"),
                description="harness isolation",
            ).get("sessions"),
        },
    }
    result["evidence_digest"] = evidence_fingerprint(result)
    (run_root / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--workspace", type=Path, default=ROOT)
    preflight_parser.add_argument("--harness", default="dsh")
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--workspace", type=Path, default=ROOT)
    install_parser.add_argument("--harness", default="dsh")
    install_parser.add_argument("--install-root", type=Path, required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--workspace", type=Path, default=ROOT)
    run_parser.add_argument("--harness", default="dsh")
    run_parser.add_argument("--run-root", type=Path, required=True)
    run_parser.add_argument("--source-commit", required=True)
    run_parser.add_argument("--scenario", required=True)
    run_parser.add_argument("--scenario-version", default="v1")
    run_parser.add_argument("--provider", required=True)
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--reasoning-effort", required=True)
    run_parser.add_argument("--install-root", type=Path, required=True)
    run_parser.add_argument("--settings", type=Path, required=True)
    run_parser.add_argument("--credentials", type=Path)
    run_parser.add_argument("--evaluation-readiness", type=Path, required=True)
    run_parser.add_argument("--acceptance-receipt", type=Path, required=True)
    run_parser.add_argument("--prompt-file", type=Path, required=True)
    run_parser.add_argument("--task-id", default="")
    run_parser.add_argument("--timeout-seconds", type=float, default=1800)
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().absolute()
    if args.command == "preflight":
        report = preflight_harness(workspace, args.harness)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ready"] else 1
    if args.command == "install":
        report = install_harness(
            workspace,
            args.harness,
            install_root=args.install_root,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ready"] else 1
    prompt = args.prompt_file.expanduser().read_text(encoding="utf-8")
    install_root = args.install_root.expanduser().absolute()
    executable_path = install_root / "node_modules" / ".bin" / "dsh"
    selected_preflight = preflight_harness(
        workspace,
        args.harness,
        executable_path=str(executable_path),
    )
    selected_preflight["install_root"] = str(install_root)
    readiness = json.loads(
        args.evaluation_readiness.expanduser().read_text(encoding="utf-8")
    )
    if not isinstance(readiness, Mapping):
        parser.error("evaluation readiness must contain an object")
    acceptance_path = args.acceptance_receipt.expanduser().absolute()
    report = execute_dsh_run(
        prepare_dsh_run(
            workspace,
            args.harness,
            run_root=args.run_root,
            source_commit=args.source_commit,
            scenario={"name": args.scenario, "version": args.scenario_version},
            model_identity={
                "provider": args.provider,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
            },
            settings_source=args.settings.expanduser().absolute(),
            credentials_file=(
                args.credentials.expanduser().absolute()
                if args.credentials is not None
                else None
            ),
            evaluation_readiness=readiness,
            preflight=selected_preflight,
            task_id=args.task_id,
        ),
        prompt,
        acceptance_receipt=lambda: json.loads(
            acceptance_path.read_text(encoding="utf-8")
        ),
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
