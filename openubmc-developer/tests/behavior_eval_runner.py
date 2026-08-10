#!/usr/bin/env python3
"""Repository-local observable behavior evaluator for ``openubmc-developer``."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any


SCHEMA_VERSION = 1
CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FRONTMATTER_NAME = re.compile(r"(?m)^name:\s*([a-z0-9-]+)\s*$")
SKILL_FILE_PATH = re.compile(
    r"(?P<path>/[^\s'\"`|;&]*?/SKILL\.md)(?=\s|['\"`]|$)"
)
READ_COMMAND = re.compile(r"\b(?:cat|sed|head|tail|less|more|rg|nl|awk|python3?)\b")
SHELL_EXECUTABLES = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
WORKSPACE_READ_EXECUTABLES = frozenset(
    {"awk", "cat", "head", "less", "more", "nl", "rg", "sed", "tail"}
)
DISCOVERY_EXECUTABLES = frozenset({"find", "ls", "tree", "whereis", "which"})
EVALUATION_MODES = frozenset({"behavior", "trigger"})
REFERENCE_FILE_PATH = re.compile(
    r"(?:^|/)references/(?P<filename>[a-z0-9-]+\.md)$"
)
EVAL_GUARD = (
    "Work on the following request as a normal user task. Use Skills only when they "
    "match. Work only inside the current workspace and copies created from it for the "
    "requested task; do not inspect unrelated files outside those roots except Skill "
    "package resources you actually need. Read each used Skill or Reference with a "
    "standalone command before relying on it. If the request genuinely requires a new "
    "user decision, explain that decision and stop without guessing; otherwise proceed "
    "with the task.\n\n"
)
TRANSPORT_FAILURE_MARKERS = (
    "idle timeout waiting for sse",
    "stream disconnected before completion",
    "reconnecting...",
)


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return list(dict.fromkeys(value))


def _safe_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or path in {PurePosixPath("."), PurePosixPath("")}:
        raise ValueError(f"unsafe {field}: {value}")
    if ".." in path.parts:
        raise ValueError(f"unsafe {field}: {value}")
    return path.as_posix()


def _ensure_within(root: Path, candidate: Path, field: str) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"{field} escapes its root: {candidate}") from error
    return resolved_candidate


def _compile_patterns(value: object, field: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for pattern in _string_list(value, field):
        try:
            compiled.append(
                (pattern, re.compile(pattern, flags=re.IGNORECASE | re.DOTALL))
            )
        except re.error as error:
            raise ValueError(f"invalid regex in {field}: {pattern}: {error}") from error
    return tuple(compiled)


def _validate_workspace_files(
    case_id: object, files: object, field: str
) -> None:
    if not isinstance(files, dict):
        raise ValueError(f"case {case_id}: {field} must be an object")
    for relative, specification in files.items():
        normalized = _safe_relative_path(relative, "workspace path")
        if ".git" in PurePosixPath(normalized).parts:
            raise ValueError(f"case {case_id}: {field} cannot write Git metadata")
        if isinstance(specification, str):
            continue
        if not isinstance(specification, dict) or not isinstance(
            specification.get("content"), str
        ):
            raise ValueError(
                f"case {case_id}: workspace file {relative} must be text "
                "or an object with text content"
            )
        mode = specification.get("mode")
        if mode is not None:
            if not isinstance(mode, str) or not re.fullmatch(r"0?[0-7]{3}", mode):
                raise ValueError(
                    f"case {case_id}: workspace file {relative} has invalid mode"
                )


def _validate_workspace(case: dict[str, Any]) -> None:
    workspace = case.get("workspace", {})
    if not isinstance(workspace, dict):
        raise ValueError(f"case {case.get('id')}: workspace must be an object")
    copy_from = workspace.get("copy_from")
    if copy_from is not None:
        _safe_relative_path(copy_from, "workspace path")
    _validate_workspace_files(
        case.get("id"), workspace.get("files", {}), "workspace.files"
    )

    path_prepend = _string_list(
        workspace.get("path_prepend"), "workspace.path_prepend"
    )
    for relative in path_prepend:
        _safe_relative_path(relative, "workspace.path_prepend entry")

    git = workspace.get("git")
    if git is not None:
        if not isinstance(git, dict):
            raise ValueError(f"case {case.get('id')}: workspace.git must be an object")
        unknown = set(git) - {"dirty_files"}
        if unknown:
            raise ValueError(
                f"case {case.get('id')}: unknown workspace.git field(s): "
                f"{', '.join(sorted(unknown))}"
            )
        _validate_workspace_files(
            case.get("id"), git.get("dirty_files", {}), "workspace.git.dirty_files"
        )


def _normalize_read_rules(expect: dict[str, Any], key: str) -> dict[str, Any]:
    reads = expect.get(key, {})
    if not isinstance(reads, dict):
        raise ValueError(f"expect.{key} must be an object")
    required = frozenset(
        _string_list(reads.get("required"), f"expect.{key}.required")
    )
    allowed_values = reads.get("allowed")
    allowed = (
        frozenset(_string_list(allowed_values, f"expect.{key}.allowed"))
        if allowed_values is not None
        else None
    )
    forbidden = frozenset(
        _string_list(reads.get("forbidden"), f"expect.{key}.forbidden")
    )
    if allowed is not None and not required <= allowed:
        raise ValueError(f"expect.{key}.allowed must include every required read")
    if forbidden & required:
        raise ValueError(f"expect.{key} cannot require and forbid the same read")
    return {"required": required, "allowed": allowed, "forbidden": forbidden}


def _validate_post_checks(case: dict[str, Any]) -> None:
    post_checks = case.get("post_checks", [])
    if not isinstance(post_checks, list):
        raise ValueError("post_checks must be a list")
    for index, check in enumerate(post_checks):
        if not isinstance(check, dict):
            raise ValueError(f"post_checks[{index}] must be an object")
        argv = check.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) and item for item in argv
        ):
            raise ValueError(f"post_checks[{index}].argv must be a non-empty string list")
        name = check.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError(f"post_checks[{index}].name must be non-empty text")
        timeout = check.get("timeout")
        if timeout is not None and (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ValueError(f"post_checks[{index}].timeout must be a positive integer")
        expected_returncode = check.get("expect_returncode")
        if expected_returncode is not None and (
            not isinstance(expected_returncode, int)
            or isinstance(expected_returncode, bool)
        ):
            raise ValueError(
                f"post_checks[{index}].expect_returncode must be an integer"
            )
        _string_list(
            check.get("stdout_contains"), f"post_checks[{index}].stdout_contains"
        )
        _string_list(
            check.get("stderr_contains"), f"post_checks[{index}].stderr_contains"
        )


def _normalize_expectations(case: dict[str, Any]) -> dict[str, Any]:
    expect = case.get("expect", {})
    if not isinstance(expect, dict):
        raise ValueError(f"case {case.get('id')}: expect must be an object")
    for key in (
        "returncode",
        "max_errors",
        "max_warnings",
        "max_commands",
        "max_agent_messages",
    ):
        value = expect.get(key)
        if value is not None and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (key != "returncode" and value < 0)
        ):
            raise ValueError(f"expect.{key} must be an integer")

    git_head_disclosed = expect.get("git_head_disclosed", False)
    if not isinstance(git_head_disclosed, bool):
        raise ValueError("expect.git_head_disclosed must be boolean")
    forbid_exact_command_repeats = expect.get(
        "forbid_exact_command_repeats", False
    )
    if not isinstance(forbid_exact_command_repeats, bool):
        raise ValueError("expect.forbid_exact_command_repeats must be boolean")
    forbid_redundant_unchanged_reads = expect.get(
        "forbid_redundant_unchanged_reads", False
    )
    if not isinstance(forbid_redundant_unchanged_reads, bool):
        raise ValueError(
            "expect.forbid_redundant_unchanged_reads must be boolean"
        )
    forbid_discovery_after_evidence = expect.get(
        "forbid_discovery_after_evidence", False
    )
    if not isinstance(forbid_discovery_after_evidence, bool):
        raise ValueError("expect.forbid_discovery_after_evidence must be boolean")

    changes_value = expect.get("workspace_changes", [])
    if isinstance(changes_value, list):
        required_changes = frozenset(
            _string_list(changes_value, "expect.workspace_changes")
        )
        allowed_changes = required_changes
        for relative in required_changes:
            _safe_relative_path(relative, "expected workspace path")
    elif isinstance(changes_value, dict):
        required_changes = frozenset(
            _string_list(
                changes_value.get("required"), "expect.workspace_changes.required"
            )
        )
        allowed_value = changes_value.get("allowed")
        allowed_changes = (
            frozenset(
                _string_list(allowed_value, "expect.workspace_changes.allowed")
            )
            if allowed_value is not None
            else required_changes
        ) | required_changes
        for relative in required_changes | allowed_changes:
            _safe_relative_path(relative, "expected workspace path")
    else:
        raise ValueError("expect.workspace_changes must be a list or object")

    file_values = expect.get("files", {})
    if not isinstance(file_values, dict):
        raise ValueError("expect.files must be an object")
    files: dict[str, dict[str, Any]] = {}
    for relative, assertions in file_values.items():
        _safe_relative_path(relative, "expected file path")
        if not isinstance(assertions, dict):
            raise ValueError(f"expect.files.{relative} must be an object")
        contains = tuple(
            _string_list(
                assertions.get("contains"), f"expect.files.{relative}.contains"
            )
        )
        not_contains = tuple(
            _string_list(
                assertions.get("not_contains"),
                f"expect.files.{relative}.not_contains",
            )
        )
        present = assertions.get("present")
        if present is not None and not isinstance(present, bool):
            raise ValueError(f"expect.files.{relative}.present must be boolean")
        equals = assertions.get("equals")
        if equals is not None and not isinstance(equals, str):
            raise ValueError(f"expect.files.{relative}.equals must be text")
        digest = assertions.get("sha256")
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError(f"expect.files.{relative}.sha256 must be a lowercase digest")
        files[relative] = {
            "present": True if present is None else present,
            "equals": equals,
            "contains": contains,
            "not_contains": not_contains,
            "sha256": digest,
        }

    skills = _normalize_read_rules(expect, "skills")
    references = _normalize_read_rules(expect, "references")

    commands = expect.get("commands", {})
    if not isinstance(commands, dict):
        raise ValueError("expect.commands must be an object")
    command_patterns = {
        "required": _compile_patterns(
            commands.get("required_patterns"), "expect.commands.required_patterns"
        ),
        "forbidden": _compile_patterns(
            commands.get("forbidden_patterns"), "expect.commands.forbidden_patterns"
        ),
    }

    messages = expect.get("messages", {})
    if not isinstance(messages, dict):
        raise ValueError("expect.messages must be an object")
    message_scope = messages.get("scope", "all")
    if message_scope not in {"all", "after_target_skill_read"}:
        raise ValueError(
            "expect.messages.scope must be 'all' or 'after_target_skill_read'"
        )
    message_patterns = {
        "scope": message_scope,
        "required": _compile_patterns(
            messages.get("required_patterns"), "expect.messages.required_patterns"
        ),
        "forbidden": _compile_patterns(
            messages.get("forbidden_patterns"), "expect.messages.forbidden_patterns"
        ),
    }

    evidence_values = expect.get("command_evidence", [])
    if not isinstance(evidence_values, list):
        raise ValueError("expect.command_evidence must be a list")
    command_evidence: list[dict[str, Any]] = []
    for index, evidence in enumerate(evidence_values):
        if not isinstance(evidence, dict):
            raise ValueError(f"expect.command_evidence[{index}] must be an object")
        unknown = set(evidence) - {
            "command_pattern",
            "output_pattern",
            "allow_empty_output",
        }
        if unknown:
            raise ValueError(
                f"expect.command_evidence[{index}] has unknown field(s): "
                f"{', '.join(sorted(unknown))}"
            )
        normalized_evidence: dict[str, Any] = {}
        for key in ("command_pattern", "output_pattern"):
            pattern = evidence.get(key)
            if not isinstance(pattern, str):
                raise ValueError(
                    f"expect.command_evidence[{index}].{key} must be text"
                )
            normalized_evidence[key] = _compile_patterns(
                [pattern], f"expect.command_evidence[{index}].{key}"
            )[0]
        allow_empty_output = evidence.get("allow_empty_output", False)
        if not isinstance(allow_empty_output, bool):
            raise ValueError(
                f"expect.command_evidence[{index}].allow_empty_output "
                "must be boolean"
            )
        normalized_evidence["allow_empty_output"] = allow_empty_output
        command_evidence.append(normalized_evidence)

    if forbid_discovery_after_evidence and not command_evidence:
        raise ValueError(
            "expect.forbid_discovery_after_evidence requires command_evidence"
        )

    disclosure_values = expect.get("disclosures", [])
    if not isinstance(disclosure_values, list):
        raise ValueError("expect.disclosures must be a list")
    disclosures: list[dict[str, Any]] = []
    for disclosure in disclosure_values:
        if not isinstance(disclosure, dict):
            raise ValueError("each disclosure must be an object")
        changed_paths = _string_list(
            disclosure.get("if_changed"), "expect.disclosures.if_changed"
        )
        if not changed_paths:
            raise ValueError("expect.disclosures.if_changed must not be empty")
        for relative in changed_paths:
            _safe_relative_path(relative, "disclosure workspace path")
        pattern = disclosure.get("message_pattern")
        if not isinstance(pattern, str):
            raise ValueError("expect.disclosures.message_pattern must be a string")
        disclosures.append(
            {
                "if_changed": frozenset(changed_paths),
                "message": _compile_patterns(
                    [pattern], "expect.disclosures.message_pattern"
                )[0],
            }
        )

    _validate_post_checks(case)
    return {
        "returncode": int(expect.get("returncode", 0)),
        "changes": {"required": required_changes, "allowed": allowed_changes},
        "skills": skills,
        "references": references,
        "commands": command_patterns,
        "messages": message_patterns,
        "command_evidence": tuple(command_evidence),
        "disclosures": tuple(disclosures),
        "files": files,
        "max_errors": int(expect.get("max_errors", 0)),
        "max_warnings": (
            int(expect["max_warnings"]) if "max_warnings" in expect else None
        ),
        "max_commands": (
            int(expect["max_commands"]) if "max_commands" in expect else None
        ),
        "max_agent_messages": (
            int(expect["max_agent_messages"])
            if "max_agent_messages" in expect
            else None
        ),
        "forbid_exact_command_repeats": forbid_exact_command_repeats,
        "forbid_redundant_unchanged_reads": forbid_redundant_unchanged_reads,
        "forbid_discovery_after_evidence": forbid_discovery_after_evidence,
        "git_head_disclosed": git_head_disclosed,
    }


def load_cases(path: Path, selected_ids: set[str] | None) -> list[dict[str, Any]]:
    """Load and validate behavior scenarios."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("eval file must contain an object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"eval schema_version must be {SCHEMA_VERSION}")
    if document.get("skill_name") != "openubmc-developer":
        raise ValueError("eval skill_name must be openubmc-developer")
    cases = document.get("evals")
    if not isinstance(cases, list) or not cases:
        raise ValueError("eval file must contain a non-empty 'evals' list")

    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"evals[{index}] must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id):
            raise ValueError(f"evals[{index}] has invalid id")
        if case_id in seen:
            raise ValueError(f"duplicate eval id: {case_id}")
        seen.add(case_id)
        prompt = case.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"case {case_id}: prompt must be non-empty text")
        case_timeout = case.get("timeout")
        if case_timeout is not None and (
            not isinstance(case_timeout, int)
            or isinstance(case_timeout, bool)
            or case_timeout <= 0
        ):
            raise ValueError(f"case {case_id}: timeout must be a positive integer")
        _validate_workspace(case)
        normalized_case = dict(case)
        normalized_case["_expectation_spec"] = _normalize_expectations(case)
        validated.append(normalized_case)

    if selected_ids is None:
        return validated
    selected = [case for case in validated if case["id"] in selected_ids]
    missing = selected_ids - {case["id"] for case in selected}
    if missing:
        raise ValueError(f"unknown eval id(s): {', '.join(sorted(missing))}")
    return selected


def _write_workspace_files(
    files: dict[str, Any], destination: Path, field: str
) -> None:
    for relative, specification in files.items():
        normalized = _safe_relative_path(relative, "workspace path")
        target = _ensure_within(destination, destination / normalized, field)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(specification, str):
            content = specification
            mode = None
        else:
            content = specification["content"]
            mode = specification.get("mode")
        target.write_text(content, encoding="utf-8")
        if mode is not None:
            target.chmod(int(mode, 8))


def _run_workspace_setup(argv: list[str], workspace: Path, label: str) -> None:
    completed = subprocess.run(
        argv,
        cwd=workspace,
        input="",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{label} failed ({completed.returncode}): {detail}")


def _initialize_git_workspace(workspace: Path) -> None:
    if (workspace / ".git").exists():
        raise ValueError(f"workspace fixture already contains Git metadata: {workspace}")
    _run_workspace_setup(["git", "init", "-q"], workspace, "git init")
    _run_workspace_setup(
        ["git", "-c", "core.autocrlf=false", "add", "--all"],
        workspace,
        "git add baseline",
    )
    _run_workspace_setup(
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=OpenUBMC Behavior Eval",
            "-c",
            "user.email=openubmc-eval@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "baseline",
        ],
        workspace,
        "git commit baseline",
    )


def materialize_workspace(
    case: dict[str, Any], destination: Path, fixture_root: Path
) -> None:
    """Copy a fixture and apply per-case overlays into a disposable workspace."""
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"workspace is not empty: {destination}")
    workspace = case.get("workspace") or {}
    copy_from = workspace.get("copy_from")
    if copy_from:
        relative = _safe_relative_path(copy_from, "workspace path")
        source = _ensure_within(fixture_root, fixture_root / relative, "workspace.copy_from")
        if not source.is_dir():
            raise FileNotFoundError(f"workspace fixture is not a directory: {source}")
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.mkdir(parents=True, exist_ok=True)

    _write_workspace_files(
        workspace.get("files") or {}, destination, "workspace file"
    )
    git = workspace.get("git")
    if git is not None:
        _initialize_git_workspace(destination)
        _write_workspace_files(
            git.get("dirty_files") or {}, destination, "workspace Git dirty file"
        )


def execution_environment(case: dict[str, Any], workspace: Path) -> dict[str, str]:
    """Return the per-case process environment without accepting arbitrary variables."""
    environment = os.environ.copy()
    prepend: list[str] = []
    workspace_spec = case.get("workspace") or {}
    for relative in _string_list(
        workspace_spec.get("path_prepend"), "workspace.path_prepend"
    ):
        normalized = _safe_relative_path(relative, "workspace.path_prepend entry")
        directory = _ensure_within(
            workspace, workspace / normalized, "workspace.path_prepend entry"
        )
        if not directory.is_dir():
            raise FileNotFoundError(f"PATH entry is not a directory: {directory}")
        prepend.append(str(directory))
    if prepend:
        current = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join(prepend + ([current] if current else []))
    return environment


def snapshot_tree(root: Path) -> dict[str, str]:
    """Return type, mode, and content hashes for observable files below *root*."""
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            snapshot[relative] = hashlib.sha256(
                b"symlink\0" + os.fsencode(target)
            ).hexdigest()
            continue
        if not path.is_file():
            continue
        mode = path.stat(follow_symlinks=False).st_mode & 0o7777
        snapshot[relative] = hashlib.sha256(
            f"file\0{mode:o}\0".encode("ascii") + path.read_bytes()
        ).hexdigest()
    return snapshot


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return created, deleted, or content-modified paths."""
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _git_read(workspace: Path, argv: list[str], label: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *argv],
        cwd=workspace,
        input="",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{label} failed ({completed.returncode}): {detail}")
    return completed


def snapshot_git_control_state(workspace: Path) -> dict[str, str] | None:
    """Capture semantic Git control state without depending on volatile index bytes."""
    if not (workspace / ".git").exists():
        return None
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=workspace,
        input="",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None

    head = _git_read(workspace, ["rev-parse", "--verify", "HEAD"], "git HEAD")
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=workspace,
        input="",
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    if symbolic.returncode not in {0, 1}:
        detail = symbolic.stderr.strip() or symbolic.stdout.strip()
        raise RuntimeError(
            f"git symbolic HEAD failed ({symbolic.returncode}): {detail}"
        )
    refs = _git_read(
        workspace,
        ["for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)"],
        "git refs",
    )
    staged = _git_read(
        workspace,
        ["diff", "--cached", "--binary", "--no-ext-diff"],
        "git staged diff",
    )
    worktrees = _git_read(
        workspace,
        ["worktree", "list", "--porcelain"],
        "git worktrees",
    )
    config_path_text = _git_read(
        workspace, ["rev-parse", "--git-path", "config"], "git config path"
    ).stdout.strip()
    config_path = Path(config_path_text)
    if not config_path.is_absolute():
        config_path = workspace / config_path
    config_hash = _sha256_file(config_path.resolve()) if config_path.is_file() else ""
    return {
        "head": head.stdout.strip(),
        "symbolic_head": symbolic.stdout.strip(),
        "refs_sha256": hashlib.sha256(refs.stdout.encode("utf-8")).hexdigest(),
        "staged_diff_sha256": hashlib.sha256(
            staged.stdout.encode("utf-8")
        ).hexdigest(),
        "worktrees_sha256": hashlib.sha256(
            worktrees.stdout.encode("utf-8")
        ).hexdigest(),
        "config_sha256": config_hash,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skill_package_fingerprint(skill_root: Path) -> dict[str, Any]:
    manifest_path = skill_root / "skill.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative_paths = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(relative_paths, list) or not all(
            isinstance(relative, str) for relative in relative_paths
        ):
            raise ValueError("skill.json files must be a list of strings")
        manifest_sha256: str | None = _sha256_file(manifest_path)
        fingerprint_source = "manifest"
    else:
        relative_paths = ["SKILL.md"]
        for directory_name in ("agents", "assets", "references", "scripts"):
            directory = skill_root / directory_name
            if not directory.is_dir():
                continue
            relative_paths.extend(
                path.relative_to(skill_root).as_posix()
                for path in directory.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            )
        manifest_sha256 = None
        fingerprint_source = "runtime-files"
    hashes: dict[str, str] = {}
    for relative in sorted(set(relative_paths)):
        normalized = _safe_relative_path(relative, "skill manifest file")
        path = _ensure_within(skill_root, skill_root / normalized, "skill manifest file")
        if not path.is_file():
            raise FileNotFoundError(f"packaged skill file is missing: {path}")
        hashes[normalized] = _sha256_file(path)
    aggregate = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "root": str(skill_root),
        "fingerprint_source": fingerprint_source,
        "manifest_sha256": manifest_sha256,
        "package_sha256": aggregate,
        "files": hashes,
    }


def _skill_name(skill_root: Path) -> str:
    manifest_path = skill_root / "skill.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = manifest.get("name") if isinstance(manifest, dict) else None
        source = manifest_path
    else:
        skill_path = skill_root / "SKILL.md"
        text = skill_path.read_text(encoding="utf-8")
        frontmatter = re.match(
            r"\A---\s*\n(?P<body>.*?)\n---(?:\s*\n|\Z)", text, re.DOTALL
        )
        match = (
            re.search(
                r"(?m)^name:\s*[\"']?([^\"'\s]+)[\"']?\s*$",
                frontmatter.group("body"),
            )
            if frontmatter is not None
            else None
        )
        name = match.group(1) if match is not None else None
        source = skill_path
    if not isinstance(name, str) or not name:
        raise ValueError(f"Skill metadata has no valid name: {source}")
    return name


def _sibling_skill_roots(skill_root: Path) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    candidates = {
        path.parent.resolve()
        for pattern in ("*/skill.json", "*/SKILL.md")
        for path in skill_root.parent.glob(pattern)
    }
    for candidate in sorted(candidates):
        try:
            name = _skill_name(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        existing = roots.get(name)
        if existing is not None and existing != candidate:
            raise ValueError(f"duplicate sibling Skill name: {name}")
        roots[name] = candidate
    return roots


def _executable_fingerprint(command: str) -> dict[str, Any]:
    """Return stable executable identity suitable for input-integrity comparison."""
    resolved = shutil.which(command)
    result: dict[str, Any] = {"requested": command, "resolved": resolved}
    if resolved is None:
        return result

    path = Path(resolved).resolve()
    result["resolved_realpath"] = str(path)
    if path.is_file():
        result["sha256"] = _sha256_file(path)
    return result


def _executable_version(command: str) -> dict[str, Any]:
    """Collect non-blocking diagnostic version output without affecting integrity."""
    resolved = shutil.which(command)
    result: dict[str, Any] = {"requested": command, "resolved": resolved}
    if resolved is None:
        return result
    try:
        completed = subprocess.run(
            [resolved, "--version"],
            input="",
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    else:
        result["returncode"] = completed.returncode
        result["stdout"] = completed.stdout.strip()
        result["stderr"] = completed.stderr.strip()
    return result


def evaluation_skill_names(cases: list[dict[str, Any]]) -> set[str]:
    """Return every Skill whose package can affect an asserted route."""
    names = {"openubmc-developer"}
    for case in cases:
        expectation = case.get("_expectation_spec")
        if not isinstance(expectation, dict):
            expectation = _normalize_expectations(case)
        rules = expectation["skills"]
        names.update(rules["required"])
        if rules["allowed"] is not None:
            names.update(rules["allowed"])
    return names


def input_provenance(
    skill_root: Path,
    runner_path: Path,
    cases_path: Path,
    *,
    skill_names: set[str] | None = None,
    codex_binary: str | None = None,
) -> dict[str, Any]:
    """Fingerprint every input that can change a behavior-evaluation conclusion."""
    target_name = _skill_name(skill_root)
    sibling_roots = _sibling_skill_roots(skill_root)
    dependencies: dict[str, Any] = {}
    for name in sorted((skill_names or set()) - {target_name}):
        dependency_root = sibling_roots.get(name)
        dependencies[name] = (
            _skill_package_fingerprint(dependency_root)
            if dependency_root is not None
            else {"missing": True}
        )

    provenance = {
        "skill_package": _skill_package_fingerprint(skill_root),
        "skill_dependencies": dependencies,
        "runner": {
            "path": str(runner_path),
            "sha256": _sha256_file(runner_path),
        },
        "cases": {
            "path": str(cases_path),
            "sha256": _sha256_file(cases_path),
        },
    }
    if codex_binary is not None:
        provenance["codex_runtime"] = _executable_fingerprint(codex_binary)
    return provenance


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def command_match_surfaces(command: str) -> tuple[str, ...]:
    """Expose a shell ``-c`` payload without treating nearby file reads as execution."""
    surfaces = [command]
    try:
        argv = shlex.split(command)
    except ValueError:
        return tuple(surfaces)
    for index, argument in enumerate(argv[:-1]):
        shell = Path(argv[index - 1]).name if index > 0 else ""
        if (
            argument in {"-c", "-lc"}
            and shell in SHELL_EXECUTABLES
            and argv[index + 1] not in surfaces
        ):
            surfaces.append(argv[index + 1])
    return tuple(surfaces)


def command_invocation_surfaces(command: str) -> tuple[str, ...]:
    """Return executed shell segments with the executable normalized to its basename."""
    matched = command_match_surfaces(command)
    sources = matched[1:] if len(matched) > 1 else matched
    invocations: list[str] = []
    for source in sources:
        try:
            lexer = shlex.shlex(source, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            tokens = [source]
        segment: list[str] = []
        for token in [*tokens, ";"]:
            if token and set(token) <= set(";&|"):
                if segment:
                    index = 0
                    while index < len(segment) and re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index]
                    ):
                        index += 1
                    if index < len(segment):
                        normalized = [Path(segment[index]).name, *segment[index + 1 :]]
                        invocations.append(" ".join(normalized))
                segment = []
            else:
                segment.append(token)
    return tuple(dict.fromkeys(invocations))


def workspace_file_read_paths(command: str, workspace: Path) -> tuple[str, ...]:
    """Return explicit workspace files read by common standalone reader commands."""
    paths: list[str] = []
    for invocation in command_invocation_surfaces(command):
        try:
            argv = shlex.split(invocation)
        except ValueError:
            continue
        if not argv or Path(argv[0]).name not in WORKSPACE_READ_EXECUTABLES:
            continue
        if Path(argv[0]).name == "rg" and "--files" in argv[1:]:
            continue
        for token in argv[1:]:
            if not token or token.startswith("-"):
                continue
            candidate = Path(token)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                resolved = _ensure_within(workspace, candidate, "workspace read")
            except ValueError:
                continue
            if not resolved.is_file():
                continue
            relative = resolved.relative_to(workspace.resolve()).as_posix()
            if relative not in paths:
                paths.append(relative)
    return tuple(paths)


def is_discovery_command(command: str) -> bool:
    """Return whether a command performs fresh tool, file, or tree discovery."""
    for invocation in command_invocation_surfaces(command):
        try:
            argv = shlex.split(invocation)
        except ValueError:
            continue
        if not argv:
            continue
        executable = Path(argv[0]).name
        if executable in DISCOVERY_EXECUTABLES:
            return True
        if executable == "command" and any(arg in {"-v", "-V"} for arg in argv[1:]):
            return True
        if executable == "type" and any("p" in arg for arg in argv[1:] if arg.startswith("-")):
            return True
        if executable == "rg" and "--files" in argv[1:]:
            return True
        if executable == "git" and len(argv) > 1 and argv[1] == "ls-files":
            return True
    return False


def skill_file_paths(command: str) -> tuple[str, ...]:
    """Extract absolute Skill paths, including shell-quoted paths with spaces."""
    paths: list[str] = []
    matched = command_match_surfaces(command)
    surfaces = matched[1:] if len(matched) > 1 else matched
    for surface in surfaces:
        try:
            tokens = shlex.split(surface)
        except ValueError:
            tokens = []
        token_paths: list[str] = []
        for token in tokens:
            normalized = token.replace("\\", "/")
            if normalized.startswith("/") and normalized.endswith("/SKILL.md"):
                token_paths.append(normalized)
        paths.extend(token_paths)
        for match in SKILL_FILE_PATH.finditer(surface.replace("\\", "/")):
            candidate = match.group("path")
            if any(
                token_path != candidate and token_path.endswith(candidate)
                for token_path in token_paths
            ):
                continue
            paths.append(candidate)
    return tuple(dict.fromkeys(paths))


def reference_file_paths(command: str) -> tuple[str, ...]:
    """Extract reference paths from successful standalone reader commands."""
    paths: list[str] = []
    matched = command_match_surfaces(command)
    surfaces = matched[1:] if len(matched) > 1 else matched
    for surface in surfaces:
        try:
            tokens = shlex.split(surface)
        except ValueError:
            continue
        for token in tokens:
            normalized = token.replace("\\", "/")
            if REFERENCE_FILE_PATH.search(normalized):
                paths.append(normalized)
    return tuple(dict.fromkeys(paths))


def reference_identity(path: str, skill_reads: list[dict[str, str]]) -> str:
    """Return a target-local or owner-qualified reference identity."""
    normalized = path.replace("\\", "/")
    filename_match = REFERENCE_FILE_PATH.search(normalized)
    if filename_match is None:
        raise ValueError(f"not a reference path: {path}")
    filename = filename_match.group("filename")

    if normalized.startswith("/"):
        reference_path = Path(normalized).resolve(strict=False)
        for read in skill_reads:
            skill_path = read.get("resolved_path") or read.get("path")
            if not skill_path:
                continue
            skill_root = Path(skill_path).resolve(strict=False).parent
            try:
                relative = reference_path.relative_to(skill_root)
            except ValueError:
                continue
            if relative.parts == ("references", filename):
                return f"references/{filename}"

    parts = PurePosixPath(normalized).parts
    reference_index = len(parts) - 2
    owner = parts[reference_index - 1] if reference_index > 0 else None
    if owner in {None, "/", "openubmc-developer"}:
        return f"references/{filename}"
    return f"{owner}:references/{filename}"


def _resolved_path(value: str | Path) -> str:
    return str(Path(value).resolve(strict=False))


def evaluation_prompt(prompt: str, skill_root: Path, mode: str) -> str:
    """Build a prompt that separates source behavior from installed triggering."""
    if mode not in EVALUATION_MODES:
        raise ValueError(f"unknown evaluation mode: {mode}")
    if mode == "behavior":
        resolved_root = skill_root.resolve()
        skill_path = resolved_root / "SKILL.md"
        reference_root = resolved_root / "references"
        mode_guard = (
            f"Use $openubmc-developer from {skill_path} for this request. Treat that "
            "checkout as the requested Skill version, load its references only from "
            f"{reference_root}, and ignore other openubmc-developer copies with the "
            "same name. Use another Skill only when the request itself genuinely "
            "requires it.\n\n"
        )
    else:
        mode_guard = (
            "Select Skills normally from the installed catalog. No source-checkout Skill "
            "path is supplied for this request.\n\n"
        )
    return EVAL_GUARD + mode_guard + prompt


def parse_event_stream(
    stdout: str, target_skill_path: Path | None = None
) -> dict[str, Any]:
    """Extract only observable reads, commands, errors, and messages from JSONL."""
    skills_read: list[str] = []
    skill_reads: list[dict[str, str]] = []
    references_read: list[str] = []
    commands: list[str] = []
    command_results: list[dict[str, Any]] = []
    command_failures: list[dict[str, Any]] = []
    agent_messages: list[str] = []
    agent_messages_after_target_skill_read: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    transport_events: list[str] = []
    usage: dict[str, Any] = {}
    target_skill_read = False
    expected_target_path = (
        _resolved_path(target_skill_path) if target_skill_path is not None else None
    )

    def record_error_message(message: str) -> None:
        normalized = message.lower()
        if any(marker in normalized for marker in TRANSPORT_FAILURE_MARKERS):
            transport_events.append(message)
        elif "warning" in normalized or "shortened to fit" in normalized:
            warnings.append(message)
        else:
            errors.append(message)

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            message = event.get("message")
            if isinstance(message, str):
                record_error_message(message)
            continue
        if event.get("type") == "turn.completed":
            candidate_usage = event.get("usage")
            if isinstance(candidate_usage, dict):
                usage = candidate_usage
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "agent_message":
            message = item.get("text")
            if isinstance(message, str):
                agent_messages.append(message)
                if target_skill_read:
                    agent_messages_after_target_skill_read.append(message)
            continue
        if item_type == "error":
            message = item.get("message")
            if isinstance(message, str):
                record_error_message(message)
            continue
        if item_type != "command_execution":
            continue
        status = item.get("status")
        if status not in {"completed", "failed"}:
            continue

        command = item.get("command") or ""
        output = item.get("aggregated_output") or ""
        if not isinstance(command, str):
            command = str(command)
        if not isinstance(output, str):
            output = str(output)
        commands.append(command)
        result = {
            "command": command,
            "status": status,
            "exit_code": item.get("exit_code"),
            "output": output,
        }
        command_results.append(result)
        command_failed = status == "failed" or item.get("exit_code") not in {None, 0}
        successful_command = not command_failed
        if command_failed:
            command_failures.append(result)

        if successful_command and "SKILL.md" in command and READ_COMMAND.search(command):
            observed_paths = list(skill_file_paths(command))
            observed_names = list(dict.fromkeys(FRONTMATTER_NAME.findall(output)))
            if not observed_names:
                observed_names = [Path(path).parent.name for path in observed_paths]
            if len(observed_names) == len(observed_paths):
                named_paths = zip(observed_names, observed_paths)
            elif len(observed_names) == 1:
                named_paths = ((observed_names[0], path) for path in observed_paths)
            else:
                named_paths = (
                    (Path(path).parent.name, path) for path in observed_paths
                )
            for name, path in named_paths:
                resolved_path = _resolved_path(path)
                if expected_target_path is not None and resolved_path == expected_target_path:
                    name = "openubmc-developer"
                if name not in skills_read:
                    skills_read.append(name)
                if name == "openubmc-developer":
                    target_skill_read = True
                read = {
                    "name": name,
                    "path": path,
                    "resolved_path": resolved_path,
                }
                if read not in skill_reads:
                    skill_reads.append(read)
        if successful_command and READ_COMMAND.search(command):
            for path in reference_file_paths(command):
                reference = reference_identity(path, skill_reads)
                if reference not in references_read:
                    references_read.append(reference)

    return {
        "skills_read": skills_read,
        "skill_reads": skill_reads,
        "skill_paths_read": list(
            dict.fromkeys(read["resolved_path"] for read in skill_reads)
        ),
        "references_read": references_read,
        "commands": commands,
        "command_results": command_results,
        "command_failures": command_failures,
        "agent_messages": agent_messages,
        "agent_messages_after_target_skill_read": (
            agent_messages_after_target_skill_read
        ),
        "errors": errors,
        "warnings": warnings,
        "transport_events": transport_events,
        "usage": usage,
    }


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_post_checks(
    checks: list[dict[str, Any]], workspace: Path, default_timeout: int
) -> list[dict[str, Any]]:
    """Run deterministic, argv-based checks after the Agent stops."""
    results: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        name = str(check.get("name") or f"check-{index + 1}")
        argv = list(check["argv"])
        timeout = int(check.get("timeout", default_timeout))
        expected_returncode = int(check.get("expect_returncode", 0))
        timed_out = False
        workspace_before = snapshot_tree(workspace)
        git_before = snapshot_git_control_state(workspace)
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                input="",
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True
            completed = subprocess.CompletedProcess(
                argv,
                124,
                stdout=_timeout_text(error.stdout),
                stderr=_timeout_text(error.stderr),
            )
        except OSError as error:
            completed = subprocess.CompletedProcess(
                argv,
                127,
                stdout="",
                stderr=str(error),
            )
        workspace_after = snapshot_tree(workspace)
        git_after = snapshot_git_control_state(workspace)
        workspace_changes = diff_snapshots(workspace_before, workspace_after)
        git_control_changed = git_before != git_after

        failures: list[str] = []
        if timed_out:
            failures.append("timeout")
        if completed.returncode != expected_returncode:
            failures.append(
                f"returncode:{completed.returncode}!={expected_returncode}"
            )
        for text in _string_list(check.get("stdout_contains"), "stdout_contains"):
            if text not in completed.stdout:
                failures.append(f"stdout_missing:{text}")
        for text in _string_list(check.get("stderr_contains"), "stderr_contains"):
            if text not in completed.stderr:
                failures.append(f"stderr_missing:{text}")
        if workspace_changes:
            failures.append(f"workspace_changed:{','.join(workspace_changes)}")
        if git_control_changed:
            failures.append("git_control_state_changed")
        results.append(
            {
                "name": name,
                "argv": argv,
                "returncode": completed.returncode,
                "timed_out": timed_out,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "workspace_changes": workspace_changes,
                "git_control_changed": git_control_changed,
                "failures": failures,
                "passed": not failures,
            }
        )
    return results


def _patterns_missing(
    patterns: tuple[tuple[str, re.Pattern[str]], ...], text: str
) -> list[str]:
    return [
        source for source, compiled in patterns if compiled.search(text) is None
    ]


def _file_failures(
    files: dict[str, dict[str, Any]], workspace: Path | None
) -> list[str]:
    if not files:
        return []
    if workspace is None:
        return ["workspace_unavailable_for_file_checks"]
    failures: list[str] = []
    for relative, rules in files.items():
        candidate = workspace / relative
        if candidate.is_symlink():
            failures.append(f"file_is_symlink:{relative}")
            continue
        try:
            path = _ensure_within(
                workspace, candidate, f"expected file {relative}"
            )
        except ValueError:
            failures.append(f"file_escapes_workspace:{relative}")
            continue
        if not rules["present"]:
            if path.exists():
                failures.append(f"file_should_be_absent:{relative}")
            continue
        if not path.is_file():
            failures.append(f"missing_file:{relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append(f"unreadable_text_file:{relative}")
            continue
        equals = rules["equals"]
        if equals is not None and text != equals:
            failures.append(f"file_content_mismatch:{relative}")
        for fragment in rules["contains"]:
            if fragment not in text:
                failures.append(f"file_missing_text:{relative}:{fragment}")
        for fragment in rules["not_contains"]:
            if fragment in text:
                failures.append(f"file_forbidden_text:{relative}:{fragment}")
        expected_hash = rules["sha256"]
        if expected_hash is not None:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                failures.append(f"file_hash_mismatch:{relative}")
    return failures


def assess_trace(
    case: dict[str, Any], trace: dict[str, Any], workspace: Path | None = None
) -> dict[str, Any]:
    """Assess observable outcomes without trusting the Agent's self-report."""
    expectation = case.get("_expectation_spec")
    if not isinstance(expectation, dict):
        expectation = _normalize_expectations(case)
    failures: list[str] = []

    timed_out = trace.get("timed_out") is True
    if timed_out:
        failures.append("codex_timeout")
    actual_returncode = int(trace.get("returncode", 0))
    expected_returncode = expectation["returncode"]
    if actual_returncode != expected_returncode:
        failures.append(f"codex_returncode:{actual_returncode}!={expected_returncode}")
    if trace.get("git_control_changed") is True:
        failures.append("git_control_state_changed")

    required_changes = expectation["changes"]["required"]
    allowed_changes = expectation["changes"]["allowed"]
    actual_changes = set(_string_list(trace.get("workspace_changes"), "workspace changes"))
    for path in sorted(required_changes - actual_changes):
        failures.append(f"missing_workspace_change:{path}")
    for path in sorted(actual_changes - allowed_changes):
        failures.append(f"unexpected_workspace_change:{path}")

    actual_skills = set(_string_list(trace.get("skills_read"), "skills_read"))
    skill_rules = expectation["skills"]
    required_skills = skill_rules["required"]
    allowed_skills = skill_rules["allowed"]
    forbidden_skills = skill_rules["forbidden"]
    for name in sorted(required_skills - actual_skills):
        failures.append(f"missing_skill_read:{name}")
    for name in sorted(forbidden_skills & actual_skills):
        failures.append(f"forbidden_skill_read:{name}")
    if allowed_skills is not None:
        for name in sorted(actual_skills - allowed_skills):
            failures.append(f"unexpected_skill_read:{name}")

    expected_skill_path = trace.get("expected_skill_path")
    if isinstance(expected_skill_path, str) and "openubmc-developer" in actual_skills:
        expected_resolved = _resolved_path(expected_skill_path)
        observed_target_paths = {
            str(read.get("resolved_path") or _resolved_path(str(read.get("path") or "")))
            for read in trace.get("skill_reads") or []
            if isinstance(read, dict) and read.get("name") == "openubmc-developer"
        }
        wrong_paths = sorted(observed_target_paths - {expected_resolved})
        if wrong_paths:
            failures.append(f"wrong_skill_path:{','.join(wrong_paths)}")
        elif expected_resolved not in observed_target_paths:
            failures.append(f"missing_target_skill_path:{expected_resolved}")

    actual_references = set(
        _string_list(trace.get("references_read"), "references_read")
    )
    reference_rules = expectation["references"]
    required_references = reference_rules["required"]
    allowed_references = reference_rules["allowed"]
    forbidden_references = reference_rules["forbidden"]
    for name in sorted(required_references - actual_references):
        failures.append(f"missing_reference_read:{name}")
    for name in sorted(forbidden_references & actual_references):
        failures.append(f"forbidden_reference_read:{name}")
    if allowed_references is not None:
        for name in sorted(actual_references - allowed_references):
            failures.append(f"unexpected_reference_read:{name}")

    command_list = _string_list(trace.get("commands"), "commands")
    max_commands = expectation["max_commands"]
    if max_commands is not None and len(command_list) > max_commands:
        failures.append(f"too_many_commands:{len(command_list)}>{max_commands}")
    if expectation["forbid_exact_command_repeats"]:
        seen_commands: set[str] = set()
        repeated_commands: set[str] = set()
        for command in command_list:
            normalized_command = re.sub(r"\s+", " ", command).strip()
            if normalized_command in seen_commands:
                repeated_commands.add(normalized_command)
            seen_commands.add(normalized_command)
        for command in sorted(repeated_commands):
            failures.append(f"repeated_command:{command[:240]}")
    if expectation["forbid_redundant_unchanged_reads"] and workspace is not None:
        observed_results = [
            result
            for result in trace.get("command_results") or []
            if isinstance(result, dict)
        ]
        successful_read_commands = [
            str(result.get("command") or "")
            for result in observed_results
            if result.get("status") == "completed"
            and result.get("exit_code") in {None, 0}
        ]
        commands_to_assess = (
            successful_read_commands if observed_results else command_list
        )
        reads: dict[str, list[str]] = {}
        for command in commands_to_assess:
            for relative in workspace_file_read_paths(command, workspace):
                reads.setdefault(relative, []).append(command)
        for relative, read_commands in sorted(reads.items()):
            if relative not in actual_changes and len(read_commands) > 1:
                failures.append(f"redundant_unchanged_file_read:{relative}")
    command_surfaces = tuple(
        dict.fromkeys(
            surface
            for command in command_list
            for surface in (
                *command_match_surfaces(command),
                *command_invocation_surfaces(command),
            )
        )
    )
    commands = "\n".join(command_surfaces)
    command_rules = expectation["commands"]
    for pattern in _patterns_missing(command_rules["required"], commands):
        failures.append(f"missing_command_pattern:{pattern}")
    for source, compiled in command_rules["forbidden"]:
        if any(
            compiled.match(surface)
            for command in command_list
            for surface in command_invocation_surfaces(command)
        ):
            failures.append(f"forbidden_command_pattern:{source}")

    command_results = [
        result
        for result in trace.get("command_results") or []
        if isinstance(result, dict)
    ]
    evidence_match_indices: list[int] = []
    for index, evidence in enumerate(expectation["command_evidence"]):
        _, command_pattern = evidence["command_pattern"]
        _, output_pattern = evidence["output_pattern"]
        found = False
        for result_index, result in enumerate(command_results):
            output = str(result.get("output") or "")
            if (
                result.get("status") == "completed"
                and result.get("exit_code") == 0
                and any(
                    command_pattern.search(surface)
                    for surface in (
                        *command_match_surfaces(
                            str(result.get("command") or "")
                        ),
                        *command_invocation_surfaces(
                            str(result.get("command") or "")
                        ),
                    )
                )
                and (
                    output_pattern.search(output)
                    or (evidence["allow_empty_output"] and not output.strip())
                )
            ):
                found = True
                evidence_match_indices.append(result_index)
                break
        if not found:
            failures.append(f"missing_command_evidence:{index}")
    if (
        expectation["forbid_discovery_after_evidence"]
        and len(evidence_match_indices) == len(expectation["command_evidence"])
    ):
        final_evidence_index = max(evidence_match_indices)
        for result in command_results[final_evidence_index + 1 :]:
            command = str(result.get("command") or "")
            if is_discovery_command(command):
                failures.append(f"discovery_after_evidence:{command[:240]}")

    agent_messages = _string_list(trace.get("agent_messages"), "agent_messages")
    max_agent_messages = expectation["max_agent_messages"]
    if max_agent_messages is not None and len(agent_messages) > max_agent_messages:
        failures.append(
            f"too_many_agent_messages:{len(agent_messages)}>{max_agent_messages}"
        )
    message_rules = expectation["messages"]
    message_scope = message_rules["scope"]
    if message_scope == "after_target_skill_read":
        pattern_messages = _string_list(
            trace.get("agent_messages_after_target_skill_read"),
            "agent_messages_after_target_skill_read",
        )
    else:
        pattern_messages = agent_messages
    messages = "\n".join(pattern_messages)
    for pattern in _patterns_missing(message_rules["required"], messages):
        failures.append(f"missing_message_pattern:{pattern}")
    for source, compiled in message_rules["forbidden"]:
        if compiled.search(messages):
            failures.append(f"forbidden_message_pattern:{source}")

    if expectation["git_head_disclosed"]:
        git_after = trace.get("git_control_after")
        head = git_after.get("head") if isinstance(git_after, dict) else None
        final_message = agent_messages[-1] if agent_messages else ""
        disclosed = False
        if isinstance(head, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", head):
            for candidate in re.findall(r"(?<![0-9a-fA-F])[0-9a-fA-F]{7,64}(?![0-9a-fA-F])", final_message):
                if head.lower().startswith(candidate.lower()):
                    disclosed = True
                    break
        if not disclosed:
            failures.append("missing_git_head_disclosure")

    for disclosure in expectation["disclosures"]:
        changed = sorted(set(disclosure["if_changed"]) & actual_changes)
        _, message_pattern = disclosure["message"]
        if changed and not message_pattern.search(messages):
            failures.append(f"missing_disclosure:{','.join(changed)}")

    failures.extend(_file_failures(expectation["files"], workspace))

    max_errors = expectation["max_errors"]
    errors = _string_list(trace.get("errors"), "errors")
    if len(errors) > max_errors:
        failures.append(f"too_many_errors:{len(errors)}>{max_errors}")
    max_warnings = expectation["max_warnings"]
    if max_warnings is not None:
        warnings = _string_list(trace.get("warnings"), "warnings")
        if len(warnings) > max_warnings:
            failures.append(f"too_many_warnings:{len(warnings)}>{max_warnings}")
    for check in trace.get("post_checks") or []:
        if not isinstance(check, dict) or check.get("passed") is not True:
            name = check.get("name", "unknown") if isinstance(check, dict) else "unknown"
            failures.append(f"post_check_failed:{name}")

    return {
        "case_pass": not failures,
        "failures": failures,
        "expected_workspace_changes": sorted(required_changes),
        "allowed_workspace_changes": sorted(allowed_changes),
        "actual_workspace_changes": sorted(actual_changes),
        "actual_skills_read": sorted(actual_skills),
        "actual_skill_paths_read": sorted(
            _string_list(trace.get("skill_paths_read"), "skill_paths_read")
        ),
        "actual_references_read": sorted(actual_references),
        "command_count": len(command_list),
        "agent_message_count": len(agent_messages),
    }


def run_case(
    case: dict[str, Any],
    *,
    attempt: int = 1,
    timeout: int,
    check_timeout: int,
    model: str | None,
    output_dir: Path,
    fixture_root: Path,
    codex_binary: str,
    skill_root: Path | None = None,
    evaluation_mode: str = "behavior",
) -> dict[str, Any]:
    """Run one case in its own disposable workspace and retain raw artifacts."""
    artifact_stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(case["id"]))
    attempt_suffix = "" if attempt == 1 else f".attempt-{attempt}"
    artifact_name = f"{artifact_stem}{attempt_suffix}"
    workspace = output_dir / "workspaces" / artifact_name
    events_dir = output_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    materialize_workspace(case, workspace, fixture_root)
    before = snapshot_tree(workspace)
    git_before = snapshot_git_control_state(workspace)
    environment = execution_environment(case, workspace)
    if skill_root is None:
        skill_root = Path(__file__).resolve().parents[1]
    target_skill_path = (skill_root.resolve() / "SKILL.md").resolve()

    command = [
        codex_binary,
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
    ]
    if model:
        command.extend(["--model", model])
    command.append(evaluation_prompt(str(case["prompt"]), skill_root, evaluation_mode))

    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            input="",
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stderr = _timeout_text(error.stderr)
        message = f"behavior evaluation timed out after {timeout} seconds"
        stderr = f"{stderr.rstrip()}\n{message}" if stderr else message
        completed = subprocess.CompletedProcess(
            command,
            124,
            stdout=_timeout_text(error.stdout),
            stderr=stderr,
        )
    except OSError as error:
        completed = subprocess.CompletedProcess(
            command,
            127,
            stdout="",
            stderr=str(error),
        )

    after = snapshot_tree(workspace)
    git_after = snapshot_git_control_state(workspace)
    event_log = events_dir / f"{artifact_name}.events.jsonl"
    stderr_log = events_dir / f"{artifact_name}.stderr.txt"
    event_log.write_text(completed.stdout, encoding="utf-8")
    stderr_log.write_text(completed.stderr, encoding="utf-8")

    parsed = parse_event_stream(
        completed.stdout,
        target_skill_path if evaluation_mode == "behavior" else None,
    )
    trace = {
        "id": case["id"],
        "attempt": attempt,
        "timeout_seconds": timeout,
        "evaluation_mode": evaluation_mode,
        "expected_skill_path": str(target_skill_path),
        "prompt": case["prompt"],
        "returncode": completed.returncode,
        "timed_out": timed_out,
        "workspace": str(workspace),
        "workspace_changes": diff_snapshots(before, after),
        "git_control_before": git_before,
        "git_control_after": git_after,
        "git_control_changed": git_before != git_after,
        "post_checks": run_post_checks(
            case.get("post_checks") or [], workspace, check_timeout
        ),
        "stderr": completed.stderr,
        "event_log": str(event_log),
        "stderr_log": str(stderr_log),
        **parsed,
    }
    return {**trace, **assess_trace(case, trace, workspace)}


def retryable_transport_failure(result: dict[str, Any]) -> bool:
    if result.get("timed_out") is not True and int(result.get("returncode", 0)) == 0:
        return False
    text = "\n".join(
        [
            str(result.get("stderr") or ""),
            *_string_list(result.get("errors"), "errors"),
            *_string_list(result.get("transport_events"), "transport events"),
        ]
    ).lower()
    return any(marker in text for marker in TRANSPORT_FAILURE_MARKERS)


def _attempt_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": result.get("attempt"),
        "timeout_seconds": result.get("timeout_seconds"),
        "returncode": result.get("returncode"),
        "timed_out": result.get("timed_out"),
        "retryable_transport_failure": retryable_transport_failure(result),
        "workspace_changes": result.get("workspace_changes"),
        "skills_read": result.get("skills_read"),
        "references_read": result.get("references_read"),
        "skill_paths_read": result.get("skill_paths_read"),
        "errors": result.get("errors"),
        "transport_events": result.get("transport_events"),
        "event_log": result.get("event_log"),
        "stderr_log": result.get("stderr_log"),
    }


def behavior_exit_code(results: list[dict[str, Any]]) -> int:
    return 0 if all(result.get("case_pass") is True for result in results) else 1


def _write_report(path: Path, report: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Run openubmc-developer behavior scenarios in disposable workspaces."
    )
    parser.add_argument("--cases", type=Path, default=root / "evals/evals.json")
    parser.add_argument("--ids", help="comma-separated eval ids; default is all")
    parser.add_argument(
        "--timeout", type=int, default=900, help="default timeout per case"
    )
    parser.add_argument("--check-timeout", type=int, default=30)
    parser.add_argument(
        "--transport-retries",
        type=int,
        default=1,
        help="fresh retries for recognized Codex transport failures",
    )
    parser.add_argument("--model")
    parser.add_argument(
        "--mode",
        choices=sorted(EVALUATION_MODES),
        default="behavior",
        help=(
            "behavior pins this source checkout; trigger uses the installed Skill catalog"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--codex", default="codex", help="Codex executable")
    parser.add_argument("--list", action="store_true", help="list selected case ids")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.check_timeout <= 0:
        raise ValueError("timeouts must be positive")
    if args.transport_retries < 0:
        raise ValueError("transport retries must not be negative")
    runner_path = Path(__file__).resolve()
    skill_root = runner_path.parents[1]
    evaluation_mode = args.mode
    case_path = args.cases.resolve()
    selected_ids = (
        {item.strip() for item in args.ids.split(",") if item.strip()}
        if args.ids
        else None
    )
    cases = load_cases(case_path, selected_ids)
    if args.list:
        for case in cases:
            print(case["id"])
        return 0

    started_at = utc_timestamp()
    codex_version = _executable_version(args.codex)
    asserted_skill_names = evaluation_skill_names(cases)
    inputs_at_start = input_provenance(
        skill_root,
        runner_path,
        case_path,
        skill_names=asserted_skill_names,
        codex_binary=args.codex,
    )

    output_dir = args.output or Path(
        tempfile.mkdtemp(prefix="openubmc-developer-behavior-eval-")
    )
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    run_status = "completed"
    runner_error: str | None = None
    current_case_id: str | None = None
    try:
        for index, case in enumerate(cases, start=1):
            current_case_id = str(case["id"])
            print(f"[{index}/{len(cases)}] running {current_case_id}", flush=True)
            attempts: list[dict[str, Any]] = []
            case_timeout = int(case.get("timeout", args.timeout))
            for attempt in range(1, args.transport_retries + 2):
                result = run_case(
                    case,
                    attempt=attempt,
                    timeout=case_timeout,
                    check_timeout=args.check_timeout,
                    model=args.model,
                    output_dir=output_dir,
                    fixture_root=case_path.parent,
                    codex_binary=args.codex,
                    skill_root=skill_root,
                    evaluation_mode=evaluation_mode,
                )
                attempts.append(result)
                if not retryable_transport_failure(result):
                    break
                if attempt <= args.transport_retries:
                    print(
                        f"retrying {current_case_id} after Codex transport failure "
                        f"({attempt}/{args.transport_retries})",
                        flush=True,
                    )
            if len(attempts) > 1:
                result = dict(result)
                result["transport_attempts"] = [
                    _attempt_summary(candidate) for candidate in attempts
                ]
            results.append(result)
            current_case_id = None
            print(
                json.dumps(
                    {
                        "id": result["id"],
                        "workspace_changes": result["workspace_changes"],
                        "skills_read": result["skills_read"],
                        "skill_paths_read": result.get("skill_paths_read", []),
                        "references_read": result["references_read"],
                        "returncode": result["returncode"],
                        "timed_out": result["timed_out"],
                        "case_pass": result["case_pass"],
                        "failures": result["failures"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    except KeyboardInterrupt:
        run_status = "interrupted"
        runner_error = "KeyboardInterrupt"
    except Exception as error:  # keep an auditable report for runner failures
        run_status = "runner_failed"
        runner_error = f"{type(error).__name__}: {error}"

    inputs_at_finish = input_provenance(
        skill_root,
        runner_path,
        case_path,
        skill_names=asserted_skill_names,
        codex_binary=args.codex,
    )
    inputs_unchanged = inputs_at_start == inputs_at_finish
    summary = {
        "total": len(cases),
        "passed": sum(result.get("case_pass") is True for result in results),
        "failed": sum(result.get("case_pass") is not True for result in results),
    }
    if run_status != "completed":
        summary["completed"] = len(results)
        summary["not_run"] = len(cases) - len(results)
    provenance: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": utc_timestamp(),
        "requested_model": args.model,
        "evaluation_mode": evaluation_mode,
        "target_skill_path": str((skill_root / "SKILL.md").resolve()),
        "codex_binary": args.codex,
        "codex_binary_resolved": shutil.which(args.codex),
        "codex_version": codex_version,
        "runner_options": {
            "timeout_seconds": args.timeout,
            "check_timeout_seconds": args.check_timeout,
            "transport_retries": args.transport_retries,
            "evaluation_mode": evaluation_mode,
        },
        "selected_case_ids": [str(case["id"]) for case in cases],
        "inputs": inputs_at_start,
        "inputs_unchanged": inputs_unchanged,
    }
    if not inputs_unchanged:
        provenance["inputs_at_finish"] = inputs_at_finish
    report = {
        "schema_version": SCHEMA_VERSION,
        "skill_name": "openubmc-developer",
        "evaluation_mode": evaluation_mode,
        "run_status": run_status,
        "cases_file": str(case_path),
        "summary": summary,
        "provenance": provenance,
        "cases": results,
    }
    if runner_error is not None:
        report["runner_error"] = runner_error
    if current_case_id is not None:
        report["incomplete_case_id"] = current_case_id
    report_path = output_dir / "behavior-results.json"
    _write_report(report_path, report)
    print(f"report={report_path}", flush=True)
    if run_status == "interrupted":
        return 130
    if run_status == "runner_failed":
        return 1
    return 0 if behavior_exit_code(results) == 0 and inputs_unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
