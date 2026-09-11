#!/usr/bin/env python3
"""Run and summarize native Codex natural-language Skill-routing samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence


MATRIX_SCHEMA = "openubmc.skill-routing-matrix.v1"
REVIEW_CONTRACT_SCHEMA = "openubmc.skill-routing-review-contract.v1"
RUN_SCHEMA = "openubmc.skill-routing-run.v2"
ARM_SCHEMA = "openubmc.skill-routing-arm.v1"
EVIDENCE_SCHEMA = "openubmc.skill-routing-evidence.v1"
COMPARISON_SCHEMA = "openubmc.skill-routing-comparison.v1"
CLASSIFICATIONS = {
    "passed",
    "skill-not-loaded",
    "mcp-not-loaded",
    "skill-not-triggered",
    "wrong-route",
    "capability-missing",
    "environment-or-auth",
    "unclassified",
}
WORKSPACE_MODES = {"ordinary", "source", "explicit-source"}
SECRET_ENV_NAME = re.compile(
    r"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY|COOKIE)",
    re.IGNORECASE,
)


def _credential_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in environ.items()
        if value and (name == "CLI_PROXY_API_KEY" or SECRET_ENV_NAME.search(name))
    }


def scan_secret_files(
    paths: Sequence[Path], environ: Mapping[str, str]
) -> dict[str, object]:
    """Scan retained evidence without returning or logging secret values."""
    credentials = _credential_environment(environ)
    matches: list[dict[str, str]] = []
    for path in paths:
        content = path.read_bytes()
        for name, value in credentials.items():
            if value.encode() in content:
                matches.append({"path": path.name, "variable": name})
    return {
        "status": "clean" if not matches else "blocked",
        "policy": "CLI_PROXY_API_KEY-and-credential-name-pattern",
        "matches": matches,
    }


def _sanitize_value(value: object, environ: Mapping[str, str]) -> object:
    """Recursively redact credential environment values from retained evidence."""
    credentials = sorted(
        _credential_environment(environ).items(), key=lambda item: len(item[1]), reverse=True
    )

    def sanitize(current: object) -> object:
        if isinstance(current, str):
            for name, secret in credentials:
                current = current.replace(secret, f"<redacted:{name}>")
            return current
        if isinstance(current, Mapping):
            return {str(key): sanitize(item) for key, item in current.items()}
        if isinstance(current, list):
            return [sanitize(item) for item in current]
        if isinstance(current, tuple):
            return [sanitize(item) for item in current]
        return current

    return sanitize(value)


def resolve_executable(value: str) -> Path:
    """Resolve a command name via PATH while preserving explicit-path failures."""
    if os.sep not in value:
        resolved = shutil.which(value)
        if resolved is None:
            raise FileNotFoundError(f"executable was not found on PATH: {value}")
        return Path(resolved).resolve()
    return Path(value).expanduser().resolve(strict=True)


def _git_value(workspace: Path, *arguments: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.strip()


def verify_workspace_layout(
    ordinary: Path,
    source: Path,
    explicit_source: Path,
    expected_source_commit: str,
) -> dict[str, object]:
    """Verify the three directory modes before native requests are executed."""
    ordinary = ordinary.resolve(strict=True)
    source = source.resolve(strict=True)
    explicit_source = explicit_source.resolve(strict=True)
    mismatches: list[str] = []
    ordinary_empty = ordinary.is_dir() and not any(ordinary.iterdir())
    ordinary_git_code, _ = _git_value(ordinary, "rev-parse", "--show-toplevel")
    ordinary_inside_git = ordinary_git_code == 0
    if not ordinary_empty:
        mismatches.append("ordinary.empty")
    if ordinary_inside_git:
        mismatches.append("ordinary.non_git")

    repositories: dict[str, dict[str, object]] = {}
    for label, path in (("source", source), ("explicit_source", explicit_source)):
        root_code, root = _git_value(path, "rev-parse", "--show-toplevel")
        commit_code, commit = _git_value(path, "rev-parse", "HEAD")
        status_code, status = _git_value(path, "status", "--porcelain")
        exact_root = root_code == 0 and Path(root).resolve() == path
        clean = status_code == 0 and not status
        repositories[label] = {
            "path": str(path),
            "repository_root": root if root_code == 0 else None,
            "commit": commit if commit_code == 0 else None,
            "clean": clean,
        }
        if not exact_root:
            mismatches.append(f"{label}.repository_root")
        if commit_code != 0 or commit != expected_source_commit:
            mismatches.append(f"{label}.commit")
        if not clean:
            mismatches.append(f"{label}.clean")
    if ordinary == explicit_source or ordinary in explicit_source.parents:
        mismatches.append("explicit_source.external")
    return {
        "status": "verified" if not mismatches else "unverified",
        "mismatches": mismatches,
        "ordinary": {
            "path": str(ordinary),
            "empty": ordinary_empty,
            "inside_git": ordinary_inside_git,
        },
        **repositories,
    }


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def document_digest(document: Mapping[str, object]) -> str:
    unsigned = {
        key: value
        for key, value in document.items()
        if key not in {"digest", "evidence_digest", "bundle_digest"}
    }
    payload = json.dumps(
        unsigned, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def verify_evaluator_identity(
    workspace: Path,
    evaluator_commit: str,
    runner_path: Path,
    matrix_path: Path,
    review_contract_path: Path,
) -> dict[str, object]:
    """Bind evaluator inputs to exact bytes present in one Git commit."""
    workspace = workspace.resolve(strict=True)
    root_code, root_value = _git_value(workspace, "rev-parse", "--show-toplevel")
    commit_code, commit = _git_value(
        workspace, "rev-parse", f"{evaluator_commit}^{{commit}}"
    )
    tree_code, tree = _git_value(
        workspace, "rev-parse", f"{evaluator_commit}^{{tree}}"
    )
    mismatches: list[str] = []
    if root_code != 0 or Path(root_value).resolve() != workspace:
        mismatches.append("repository.root")
    if commit_code != 0:
        mismatches.append("repository.commit")
    if tree_code != 0:
        mismatches.append("repository.tree")

    files: dict[str, dict[str, object]] = {}
    for label, path in (
        ("runner", runner_path),
        ("matrix", matrix_path),
        ("review_contract", review_contract_path),
    ):
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(workspace).as_posix()
        except ValueError:
            relative = str(resolved)
            mismatches.append(f"{label}.repository")
            committed = None
        else:
            shown = subprocess.run(
                ["git", "-C", str(workspace), "show", f"{commit}:{relative}"],
                capture_output=True,
                check=False,
            )
            committed = shown.stdout if shown.returncode == 0 else None
            if committed is None:
                mismatches.append(f"{label}.tracked")
        current_sha = _sha256(resolved)
        committed_sha = (
            "sha256:" + hashlib.sha256(committed).hexdigest()
            if committed is not None
            else None
        )
        if committed_sha != current_sha:
            mismatches.append(f"{label}.bytes")
        files[label] = {
            "path": relative,
            "sha256": current_sha,
            "committed_sha256": committed_sha,
            "commit": commit if commit_code == 0 else None,
        }
    return {
        "schema": "openubmc.skill-routing-evaluator.v1",
        "status": "verified" if not mismatches else "unverified",
        "mismatches": mismatches,
        "commit": commit if commit_code == 0 else None,
        "tree": tree if tree_code == 0 else None,
        "files": files,
    }


def inventory_content_digest(inventory: Sequence[Mapping[str, object]]) -> str:
    """Digest loaded Skill metadata without machine-specific absolute paths."""
    rows = [
        {key: value for key, value in row.items() if key != "path"}
        for row in inventory
    ]
    rows.sort(
        key=lambda row: (
            str(row.get("name", "")),
            str(row.get("pluginId", "")),
            str(row.get("scope", "")),
        )
    )
    return document_digest({"rows": rows})


def file_content_inventory(root: Path) -> dict[str, object]:
    """Create a path-relative per-file inventory for a loose Skill snapshot."""
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.split("/", 1)[0].startswith("."):
            continue
        rows.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
    document: dict[str, object] = {
        "schema": "openubmc.skill-routing-file-inventory.v1",
        "files": rows,
    }
    document["digest"] = document_digest(document)
    return document


def verify_loose_content_inventory(
    skill_root: Path,
    retained_path: Path,
    expected_file: Mapping[str, object],
) -> dict[str, object]:
    """Bind the retained loose file list to its arm record and live snapshot."""
    retained = _read_digested_document(retained_path)
    recomputed = file_content_inventory(skill_root)
    mismatches: list[str] = []
    if retained_path.name != expected_file.get("path"):
        mismatches.append("path")
    if _sha256(retained_path) != expected_file.get("sha256"):
        mismatches.append("sha256")
    retained_files = retained.get("files")
    if (
        not isinstance(retained_files, list)
        or len(retained_files) != expected_file.get("file_count")
    ):
        mismatches.append("file_count")
    if retained != recomputed:
        mismatches.append("content")
    return {
        "status": "verified" if not mismatches else "unverified",
        "mismatches": mismatches,
        "sha256": _sha256(retained_path),
        "digest": retained.get("digest"),
        "file_count": len(retained_files) if isinstance(retained_files, list) else None,
    }


def _read_digested_document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("digest") != document_digest(value):
        raise ValueError(f"artifact document digest is invalid: {path.name}")
    return value


def verify_arm_artifacts(
    identity: Mapping[str, object],
    *,
    inventory_path: Path,
    source_workspace: Path,
    artifact_paths: Mapping[str, Path],
) -> dict[str, object]:
    """Verify the concrete files and checkout bound by an arm identity."""
    mismatches: list[str] = []
    observed: dict[str, object] = {}
    inventory_value = json.loads(inventory_path.read_text())
    if not isinstance(inventory_value, list) or not all(
        isinstance(row, Mapping) for row in inventory_value
    ):
        raise ValueError("Skill inventory is invalid")
    inventory_rows = [dict(row) for row in inventory_value]
    inventory_observed = {
        "sha256": _sha256(inventory_path),
        "content_digest": inventory_content_digest(inventory_rows),
    }
    observed["inventory"] = inventory_observed
    expected_inventory = identity.get("inventory")
    if not isinstance(expected_inventory, Mapping):
        raise ValueError("routing arm inventory identity is invalid")
    for key, value in inventory_observed.items():
        if expected_inventory.get(key) != value:
            mismatches.append(f"inventory.{key}")

    commit_code, commit = _git_value(source_workspace, "rev-parse", "HEAD")
    tree_code, tree = _git_value(source_workspace, "rev-parse", "HEAD^{tree}")
    status_code, status = _git_value(source_workspace, "status", "--porcelain")
    source_observed = {
        "commit": commit if commit_code == 0 else None,
        "tree": tree if tree_code == 0 else None,
        "clean": status_code == 0 and not status,
    }
    observed["source"] = source_observed
    expected_source = identity.get("source")
    if not isinstance(expected_source, Mapping):
        raise ValueError("routing arm source identity is invalid")
    for key in ("commit", "tree"):
        if expected_source.get(key) != source_observed[key]:
            mismatches.append(f"source.{key}")
    if not source_observed["clean"]:
        mismatches.append("source.clean")

    if identity.get("kind") == "loose-skills" or "loose_skills" in identity:
        loose = identity.get("loose_skills")
        if not isinstance(loose, Mapping):
            raise ValueError("loose Skills identity is invalid")
        skill_root = artifact_paths.get("skill_root")
        config = artifact_paths.get("configuration")
        retained_inventory = artifact_paths.get("content_inventory")
        if skill_root is None or config is None or retained_inventory is None:
            raise ValueError(
                "loose Skills artifacts require skill_root, configuration, and content_inventory"
            )
        content_inventory = file_content_inventory(skill_root)
        loose_observed = {
            "content_inventory_digest": content_inventory["digest"],
            "configuration_sha256": _sha256(config),
            "file_count": len(content_inventory["files"]),
        }
        observed["loose_skills"] = loose_observed
        for key in ("content_inventory_digest", "configuration_sha256"):
            if loose.get(key) != loose_observed[key]:
                mismatches.append(f"loose_skills.{key}")
        expected_inventory_file = loose.get("content_inventory_file")
        if not isinstance(expected_inventory_file, Mapping):
            raise ValueError("loose Skills content inventory identity is invalid")
        inventory_verification = verify_loose_content_inventory(
            skill_root,
            retained_inventory,
            expected_inventory_file,
        )
        observed["loose_content_inventory"] = inventory_verification
        mismatches.extend(
            f"loose_skills.content_inventory_file.{value}"
            for value in inventory_verification["mismatches"]
        )
    else:
        plugin = identity.get("plugin")
        if not isinstance(plugin, Mapping):
            raise ValueError("plugin identity is invalid")
        missing = [
            name for name in ("archive", "subject", "runtime") if name not in artifact_paths
        ]
        if missing:
            raise ValueError("plugin artifacts require " + ", ".join(missing))
        subject = _read_digested_document(artifact_paths["subject"])
        runtime = _read_digested_document(artifact_paths["runtime"])
        payload = subject.get("payload")
        content_digest = payload.get("content_digest") if isinstance(payload, Mapping) else None
        plugin_observed = {
            "archive_sha256": _sha256(artifact_paths["archive"]),
            "content_digest": content_digest,
            "subject_digest": subject.get("digest"),
            "runtime_digest": runtime.get("digest"),
        }
        observed["plugin"] = plugin_observed
        for key, value in plugin_observed.items():
            if plugin.get(key) != value:
                mismatches.append(f"plugin.{key}")
        if runtime.get("subject_digest") != subject.get("digest"):
            mismatches.append("plugin.runtime_subject_digest")

        def digest_hex(value: object) -> str:
            return str(value).removeprefix("sha256:")

        subject_distribution = subject.get("distribution")
        subject_payload = subject.get("payload")
        subject_plugin = subject.get("plugin")
        subject_archive = (
            subject_distribution.get("archive")
            if isinstance(subject_distribution, Mapping)
            else None
        )
        execution = identity.get("execution")
        expected_commit = expected_source.get("commit")
        expected_repository = expected_source.get("repository")
        archive_hex = digest_hex(plugin_observed["archive_sha256"])
        codex_hex = (
            digest_hex(execution.get("codex_executable_sha256"))
            if isinstance(execution, Mapping)
            else ""
        )
        expected_plugin = {
            key: plugin.get(key) for key in ("marketplace", "name", "version")
        }
        expected_mcp = [str(value) for value in plugin.get("mcp_servers", [])]
        expected_plugin_id = f"{expected_plugin['name']}@{expected_plugin['marketplace']}"
        plugin_inventory_rows = [
            row
            for row in inventory_rows
            if row.get("enabled") is True and row.get("pluginId") == expected_plugin_id
        ]
        inventory_skill_names = sorted(
            str(row.get("name")) for row in plugin_inventory_rows
        )

        def inventory_skill_file(row: Mapping[str, object]) -> str:
            path = str(row.get("path", "")).replace("\\", "/")
            marker = "/skills/"
            return "skills/" + path.rsplit(marker, 1)[-1] if marker in path else path

        inventory_skill_files = sorted(
            inventory_skill_file(row) for row in plugin_inventory_rows
        )
        subject_skill_files = (
            sorted(str(value) for value in subject_payload.get("skill_files", {}))
            if isinstance(subject_payload, Mapping)
            and isinstance(subject_payload.get("skill_files"), Mapping)
            else []
        )
        expected_skill_count = plugin.get("plugin_skill_count")
        cross_checks = {
            "subject_distribution_commit": (
                subject_distribution.get("commit")
                if isinstance(subject_distribution, Mapping)
                else None,
                expected_commit,
            ),
            "subject_distribution_repository": (
                subject_distribution.get("repository")
                if isinstance(subject_distribution, Mapping)
                else None,
                expected_repository,
            ),
            "subject_archive_sha256": (
                digest_hex(subject_archive.get("sha256"))
                if isinstance(subject_archive, Mapping)
                else "",
                archive_hex,
            ),
            "subject_payload_source_commit": (
                subject_payload.get("source_commit")
                if isinstance(subject_payload, Mapping)
                else None,
                expected_commit,
            ),
            "subject_payload_content_digest": (
                subject_payload.get("content_digest")
                if isinstance(subject_payload, Mapping)
                else None,
                plugin_observed["content_digest"],
            ),
            "subject_plugin": (subject_plugin, expected_plugin),
            "subject_mcp_servers": (
                subject_payload.get("mcp_servers")
                if isinstance(subject_payload, Mapping)
                else None,
                expected_mcp,
            ),
            "subject_skill_file_count": (
                len(subject_skill_files),
                expected_skill_count,
            ),
            "subject_inventory_skill_files": (
                subject_skill_files,
                inventory_skill_files,
            ),
            "runtime_distribution_commit": (
                runtime.get("distribution_commit"),
                expected_commit,
            ),
            "runtime_payload_source_commit": (
                runtime.get("payload_source_commit"),
                expected_commit,
            ),
            "runtime_archive_sha256": (
                digest_hex(runtime.get("archive_sha256")),
                archive_hex,
            ),
            "runtime_content_digest": (
                runtime.get("content_digest"),
                plugin_observed["content_digest"],
            ),
            "runtime_codex_executable_sha256": (
                digest_hex(runtime.get("codex_executable_sha256")),
                codex_hex,
            ),
            "runtime_plugin": (runtime.get("plugin"), expected_plugin),
            "runtime_mcp_servers": (runtime.get("mcp_servers"), expected_mcp),
            "runtime_loaded_mcp_servers": (
                runtime.get("loaded_mcp_servers"),
                expected_mcp,
            ),
            "runtime_loaded_skill_files": (
                sorted(str(value) for value in runtime.get("loaded_skill_files", []))
                if isinstance(runtime.get("loaded_skill_files"), list)
                else None,
                subject_skill_files,
            ),
            "runtime_loaded_skill_count": (
                len(runtime.get("loaded_skills", []))
                if isinstance(runtime.get("loaded_skills"), list)
                else None,
                expected_skill_count,
            ),
            "runtime_inventory_skill_names": (
                sorted(str(value) for value in runtime.get("loaded_skills", []))
                if isinstance(runtime.get("loaded_skills"), list)
                else None,
                inventory_skill_names,
            ),
            "runtime_inventory_skill_files": (
                sorted(str(value) for value in runtime.get("loaded_skill_files", []))
                if isinstance(runtime.get("loaded_skill_files"), list)
                else None,
                inventory_skill_files,
            ),
            "plugin_inventory_skill_count": (
                len(plugin_inventory_rows),
                expected_skill_count,
            ),
            "runtime_skills_verified": (
                runtime.get("skills_verified"),
                expected_skill_count,
            ),
            "runtime_archive_verified": (runtime.get("archive_verified"), True),
            "runtime_native_plugin_loaded": (
                runtime.get("native_plugin_loaded"),
                True,
            ),
        }
        binding_mismatches = [
            name for name, (actual, expected) in cross_checks.items() if actual != expected
        ]
        mismatches.extend(f"plugin.{name}" for name in binding_mismatches)
        observed["plugin_binding"] = {
            "status": "verified" if not binding_mismatches else "unverified",
            "mismatches": binding_mismatches,
        }
    return {
        "status": "verified" if not mismatches else "unverified",
        "mismatches": mismatches,
        "observed": observed,
    }


def load_arm_identity(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("schema") != ARM_SCHEMA:
        raise ValueError("routing arm identity schema is invalid")
    if document.get("digest") != document_digest(document):
        raise ValueError("routing arm identity digest is invalid")
    for key in ("arm_id", "kind", "source", "inventory", "execution", "environment"):
        if key not in document:
            raise ValueError(f"routing arm identity requires {key}")
    if document["kind"] not in {"plugin", "loose-skills"}:
        raise ValueError("routing arm identity kind is invalid")
    for section in ("source", "inventory", "execution", "environment"):
        if not isinstance(document[section], Mapping) or not document[section]:
            raise ValueError(f"routing arm identity {section} is invalid")
    required_execution = {
        "codex_version",
        "codex_executable_sha256",
        "model",
        "effort",
        "model_provider",
        "originator",
        "approval_policy",
        "sandbox_policy",
        "plugins",
    }
    missing_execution = sorted(required_execution - set(document["execution"]))
    if missing_execution:
        raise ValueError(
            "routing arm execution identity requires " + ", ".join(missing_execution)
        )
    if document["kind"] == "plugin":
        plugin = document.get("plugin")
        if not isinstance(plugin, Mapping):
            raise ValueError("plugin routing arm identity is required")
        for key in ("archive_sha256", "content_digest", "subject_digest", "runtime_digest"):
            if not plugin.get(key):
                raise ValueError(f"plugin routing arm identity requires {key}")
    else:
        loose = document.get("loose_skills")
        if not isinstance(loose, Mapping):
            raise ValueError("loose Skills routing arm identity is required")
        for key in ("content_inventory_digest", "configuration_sha256", "source_state"):
            if not loose.get(key):
                raise ValueError(f"loose Skills routing arm identity requires {key}")
    return document


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def load_matrix(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("schema") != MATRIX_SCHEMA:
        raise ValueError("routing matrix schema is invalid")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("routing matrix cases are required")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("routing matrix case is invalid")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError("routing matrix case identity is invalid")
        seen.add(case_id)
        if case.get("workspace_mode") not in WORKSPACE_MODES:
            raise ValueError(f"routing matrix workspace mode is invalid for {case_id}")
        if not isinstance(case.get("expected_routes"), list) or not all(
            isinstance(value, str) and value for value in case["expected_routes"]
        ):
            raise ValueError(f"routing matrix expected routes are invalid for {case_id}")
        turns = case.get("turns")
        if not isinstance(turns, list) or not turns or not all(
            isinstance(value, str) and value.strip() for value in turns
        ):
            raise ValueError(f"routing matrix turns are invalid for {case_id}")
        if case.get("intent") == "multi-turn" and len(turns) < 2:
            raise ValueError("multi-turn routing case requires at least two turns")
        turn_routes = case.get("expected_routes_by_turn")
        if turn_routes is not None and (
            not isinstance(turn_routes, list)
            or len(turn_routes) != len(turns)
            or not all(
                isinstance(routes, list)
                and all(isinstance(value, str) and value for value in routes)
                for routes in turn_routes
            )
        ):
            raise ValueError(f"routing matrix turn routes are invalid for {case_id}")
    return document


def apply_review_contract(
    matrix: Mapping[str, object], matrix_path: Path, contract_path: Path
) -> dict[str, object]:
    """Overlay predeclared review-only routes without changing executed prompts."""
    contract = json.loads(contract_path.read_text())
    if (
        not isinstance(contract, Mapping)
        or contract.get("schema") != REVIEW_CONTRACT_SCHEMA
        or contract.get("execution_matrix_sha256") != _sha256(matrix_path)
    ):
        raise ValueError("routing review contract identity is invalid")
    rules = contract.get("cases")
    if not isinstance(rules, Mapping):
        raise ValueError("routing review contract cases are invalid")
    matrix_cases = matrix.get("cases")
    if not isinstance(matrix_cases, list):
        raise ValueError("routing matrix cases are invalid")
    known = {
        str(case.get("case_id")): case
        for case in matrix_cases
        if isinstance(case, Mapping)
    }
    unknown = sorted(str(case_id) for case_id in rules if str(case_id) not in known)
    if unknown:
        raise ValueError("routing review contract has unknown cases: " + ", ".join(unknown))
    merged_cases = []
    for original in matrix_cases:
        case = dict(original)
        rule = rules.get(str(case["case_id"]), {})
        if not isinstance(rule, Mapping) or set(rule) - {
            "allowed_routes",
            "expected_routes_by_turn",
        }:
            raise ValueError(f"routing review contract rule is invalid for {case['case_id']}")
        allowed = rule.get("allowed_routes", [])
        if not isinstance(allowed, list) or not all(
            isinstance(value, str) and value for value in allowed
        ):
            raise ValueError(f"routing review allowed routes are invalid for {case['case_id']}")
        if allowed:
            case["allowed_routes"] = list(allowed)
        turn_routes = rule.get("expected_routes_by_turn")
        if turn_routes is not None:
            if (
                not isinstance(turn_routes, list)
                or len(turn_routes) != len(case["turns"])
                or not all(
                    isinstance(routes, list)
                    and all(isinstance(value, str) and value for value in routes)
                    for routes in turn_routes
                )
            ):
                raise ValueError(
                    f"routing review turn routes are invalid for {case['case_id']}"
                )
            case["expected_routes_by_turn"] = [list(routes) for routes in turn_routes]
        merged_cases.append(case)
    return {**matrix, "cases": merged_cases, "review_contract": dict(contract)}


def validate_review_classification(value: str) -> str:
    if value not in CLASSIFICATIONS:
        raise ValueError("routing review classification is invalid")
    return value


def _canonical_skill_name(value: object) -> str:
    return str(value).rsplit(":", 1)[-1]


def evaluate_route(
    case: Mapping[str, object],
    observation: Mapping[str, object],
    inventory: Sequence[Mapping[str, object]],
    *,
    turn_observations: Sequence[Mapping[str, object]] | None = None,
    available_mcp: Sequence[str] | None = None,
) -> dict[str, object]:
    """Evaluate observed native reads/calls against the matrix routing contract."""
    expected_routes = [_canonical_skill_name(value) for value in case.get("expected_routes", [])]
    allowed_routes = [_canonical_skill_name(value) for value in case.get("allowed_routes", [])]
    observed_routes = [
        _canonical_skill_name(value) for value in observation.get("skill_reads", [])
    ]
    expected_mcp = [str(value) for value in case.get("expected_mcp", [])]
    observed_mcp = [
        str(call.get("server", ""))
        for call in observation.get("mcp_calls", [])
        if isinstance(call, Mapping)
    ]
    availability: dict[str, str] = {}
    for expected in expected_routes:
        matches = [
            row
            for row in inventory
            if _canonical_skill_name(row.get("name", "")) == expected
        ]
        availability[expected] = (
            "enabled"
            if any(row.get("enabled") is True for row in matches)
            else "disabled"
            if matches
            else "absent"
        )
    fallback_routes = [route for route in observed_routes if route not in expected_routes]
    unexpected_routes = [
        route for route in observed_routes if route not in expected_routes + allowed_routes
    ]
    missing_routes = [route for route in expected_routes if route not in observed_routes]
    missing_mcp = [server for server in expected_mcp if server not in observed_mcp]
    mcp_availability = (
        {
            server: "available" if server in available_mcp else "unavailable"
            for server in expected_mcp
        }
        if available_mcp is not None
        else {}
    )

    classification = "passed"
    if any(value != "enabled" for value in availability.values()):
        classification = "skill-not-loaded"
    elif any(value == "unavailable" for value in mcp_availability.values()):
        classification = "mcp-not-loaded"
    elif missing_routes:
        classification = "wrong-route" if observed_routes else "skill-not-triggered"
    elif unexpected_routes:
        classification = "wrong-route"
    elif missing_mcp:
        classification = "wrong-route" if observed_mcp else "skill-not-triggered"
    elif not expected_routes and not expected_mcp and (observed_routes or observed_mcp):
        classification = "wrong-route"
    result = {
        "status": "passed" if classification == "passed" else "failed",
        "classification": classification,
        "expected_availability": availability,
        "observed_routes": observed_routes,
        "allowed_routes": allowed_routes,
        "fallback_routes": fallback_routes,
        "unexpected_routes": unexpected_routes,
        "missing_routes": missing_routes,
        "observed_mcp": observed_mcp,
        "missing_mcp": missing_mcp,
        "expected_mcp_availability": mcp_availability,
    }
    turn_expectations = case.get("expected_routes_by_turn")
    if (
        turn_expectations is None
        and case.get("intent") == "multi-turn"
        and turn_observations is not None
    ):
        turn_expectations = [
            *([[]] * max(0, len(turn_observations) - 1)),
            list(case.get("expected_routes", [])),
        ]
    if isinstance(turn_expectations, list):
        turns: list[dict[str, object]] = []
        if turn_observations is None or len(turn_observations) != len(turn_expectations):
            result["status"] = "failed"
            result["classification"] = "unclassified"
        else:
            for expected, turn_observation in zip(turn_expectations, turn_observations, strict=True):
                turn_case = {
                    "expected_routes": expected,
                    "expected_mcp": [],
                    "intent": case.get("intent"),
                }
                turns.append(
                    evaluate_route(
                        turn_case,
                        turn_observation,
                        inventory,
                        available_mcp=available_mcp,
                    )
                )
            first_failure = next(
                (turn for turn in turns if turn["status"] != "passed"), None
            )
            if first_failure is not None:
                result["status"] = "failed"
                result["classification"] = first_failure["classification"]
        result["turns"] = turns
    return result


def _arm_mcp_servers(arm: Mapping[str, object]) -> list[str]:
    plugin = arm.get("plugin")
    if not isinstance(plugin, Mapping) and arm.get("kind") == "plugin":
        plugin = arm.get("distribution")
    if not isinstance(plugin, Mapping) or not isinstance(plugin.get("mcp_servers"), list):
        return []
    return [str(value) for value in plugin["mcp_servers"]]


def build_codex_command(
    executable: Path,
    *,
    model: str,
    effort: str,
    cwd: Path,
    prompt: str,
    plugins: bool,
) -> list[str]:
    return [
        str(executable),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        model,
        "-c",
        "model_reasoning_effort=" + json.dumps(effort),
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        "features.plugins=" + str(plugins).lower(),
        "--cd",
        str(cwd),
        prompt,
    ]


def build_resume_command(
    executable: Path,
    *,
    thread_id: str,
    model: str,
    effort: str,
    prompt: str,
    plugins: bool,
) -> list[str]:
    return [
        str(executable),
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
        "--model",
        model,
        "-c",
        "model_reasoning_effort=" + json.dumps(effort),
        "-c",
        'sandbox_mode="read-only"',
        "-c",
        "features.plugins=" + str(plugins).lower(),
        thread_id,
        prompt,
    ]


def _event_lines(text: str) -> tuple[list[dict[str, object]], list[str]]:
    events: list[dict[str, object]] = []
    invalid: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid.append(line)
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            invalid.append(line)
    return events, invalid


def _bounded_evidence(
    value: object,
    *,
    max_bytes: int = 8192,
    environ: Mapping[str, str] | None = None,
) -> object:
    if environ is not None:
        value = _sanitize_value(value, environ)
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(payload) <= max_bytes:
        return value
    preview = (
        value[:4096]
        if isinstance(value, str)
        else payload[:4096].decode(errors="replace")
    )
    return {
        "truncated": True,
        "original_bytes": len(payload),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "preview": preview,
    }


def summarize_events(
    events: Sequence[Mapping[str, object]],
    inventory: Sequence[Mapping[str, object]],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    environment = os.environ if environ is None else environ
    thread_id = ""
    commands: list[dict[str, object]] = []
    mcp_calls: list[dict[str, object]] = []
    final_answer = ""
    errors: list[str] = []
    usage: dict[str, int | float] = {}
    completed_turns = 0
    skill_reads: list[str] = []
    skill_read_evidence: list[dict[str, str]] = []
    inventory_paths = [
        (str(row.get("name", "")), str(row.get("path", "")))
        for row in inventory
        if row.get("enabled") is True
        and isinstance(row.get("name"), str)
        and isinstance(row.get("path"), str)
    ]

    def output_proves_skill_read(name: str, path: str, item: Mapping[str, object]) -> bool:
        canonical_name = name.rsplit(":", 1)[-1]
        output = item.get("aggregated_output")
        return (
            item.get("status") == "completed"
            and item.get("exit_code") == 0
            and path in str(item.get("command", ""))
            and isinstance(output, str)
            and re.search(
                rf"(?m)^name:\s*['\"]?{re.escape(canonical_name)}['\"]?\s*$",
                output,
            )
            is not None
        )
    for event in events:
        event_type = event.get("type")
        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = str(event["thread_id"])
        if event_type == "error" and isinstance(event.get("message"), str):
            errors.append(str(_sanitize_value(event["message"], environment)))
        if event_type == "turn.completed" and isinstance(event.get("usage"), Mapping):
            completed_turns += 1
            for key, value in event["usage"].items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[str(key)] = usage.get(str(key), 0) + value
        if event_type != "item.completed" or not isinstance(event.get("item"), Mapping):
            continue
        item = event["item"]
        item_type = item.get("type")
        if item_type == "agent_message" and isinstance(item.get("text"), str):
            final_answer = str(_sanitize_value(item["text"], environment))
        elif item_type == "command_execution":
            command = str(item.get("command", ""))
            commands.append(
                {
                    "command": str(_sanitize_value(command, environment)),
                    "status": str(item.get("status", "unknown")),
                    **({"exit_code": item["exit_code"]} if isinstance(item.get("exit_code"), int) else {}),
                }
            )
            for name, path in inventory_paths:
                if path and output_proves_skill_read(name, path, item) and name not in skill_reads:
                    skill_reads.append(name)
                    output = str(item.get("aggregated_output", ""))
                    skill_read_evidence.append(
                        {
                            "name": name,
                            "path": path,
                            "command": str(_sanitize_value(command, environment)),
                            "output_sha256": "sha256:"
                            + hashlib.sha256(output.encode()).hexdigest(),
                        }
                    )
        elif item_type == "mcp_tool_call":
            mcp_calls.append(
                {
                    "server": str(item.get("server", "")),
                    "tool": str(item.get("tool", "")),
                    "status": str(item.get("status", "unknown")),
                    "arguments": _bounded_evidence(
                        item.get("arguments"), environ=environment
                    ),
                    "result": _bounded_evidence(item.get("result"), environ=environment),
                    "error": _bounded_evidence(item.get("error"), environ=environment),
                }
            )
        elif item_type == "error" and isinstance(item.get("message"), str):
            errors.append(str(_sanitize_value(item["message"], environment)))
    if "total_tokens" not in usage:
        usage["total_tokens"] = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    return {
        "thread_id": thread_id,
        "skill_reads": skill_reads,
        "skill_read_evidence": skill_read_evidence,
        "mcp_calls": mcp_calls,
        "commands": commands,
        "usage": usage,
        "completed_turns": completed_turns,
        "errors": errors,
        "final_answer": final_answer,
    }


def probe_skills(executable: Path, workspace: Path) -> list[dict[str, object]]:
    process = subprocess.Popen(
        [str(executable), "app-server", "--stdio"],
        cwd=workspace,
        env=os.environ,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    responses: queue.Queue[object] = queue.Queue()

    def read_responses() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                responses.put(json.loads(line))
            except json.JSONDecodeError:
                responses.put({"invalid_json": True})

    threading.Thread(target=read_responses, daemon=True).start()

    def call(identifier: int, method: str, params: Mapping[str, object]) -> object:
        assert process.stdin is not None
        process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                response = responses.get(timeout=1)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if not isinstance(response, Mapping) or response.get("id") != identifier:
                continue
            if "error" in response:
                raise RuntimeError(f"Codex app-server {method} failed")
            return response.get("result")
        raise RuntimeError(f"Codex app-server {method} timed out")

    try:
        call(1, "initialize", {"clientInfo": {"name": "openubmc-routing-eval", "version": "1"}, "capabilities": {"experimentalApi": True}})
        assert process.stdin is not None
        process.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
        process.stdin.flush()
        result = call(2, "skills/list", {"cwds": [str(workspace)], "forceReload": True})
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if not isinstance(result, Mapping) or not isinstance(result.get("data"), list):
        raise RuntimeError("Codex app-server returned an invalid Skill inventory")
    errors = [
        error
        for group in result["data"]
        if isinstance(group, Mapping) and isinstance(group.get("errors"), list)
        for error in group["errors"]
    ]
    if errors:
        raise RuntimeError("Codex app-server reported Skill loading errors")
    return [
        dict(skill)
        for group in result["data"]
        if isinstance(group, Mapping) and isinstance(group.get("skills"), list)
        for skill in group["skills"]
        if isinstance(skill, Mapping)
    ]


def _rollout_identity(codex_home: Path, thread_id: str) -> dict[str, object]:
    matches = [
        path
        for path in (codex_home / "sessions").rglob("*.jsonl")
        if thread_id in path.name
    ]
    if len(matches) != 1:
        return {"thread_id": thread_id, "status": "unverified", "rollout_matches": len(matches)}
    path = matches[0]
    identity: dict[str, object] = {
        "thread_id": thread_id,
        "status": "recorded",
        "rollout_sha256": _sha256(path),
    }
    contexts: list[dict[str, object]] = []
    user_prompts: list[str] = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if row.get("type") == "session_meta":
            identity.update(
                {
                    "codex_version": payload.get("cli_version"),
                    "originator": payload.get("originator"),
                    "model_provider": payload.get("model_provider"),
                    "cwd": payload.get("cwd"),
                }
            )
        elif row.get("type") == "turn_context":
            contexts.append(
                {
                    "model": payload.get("model"),
                    "effort": payload.get("effort"),
                    "cwd": payload.get("cwd"),
                    "approval_policy": payload.get("approval_policy"),
                    "sandbox_policy": payload.get("sandbox_policy"),
                }
            )
        elif (
            row.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
            and isinstance(payload.get("content"), list)
        ):
            text_parts = [
                str(part.get("text"))
                for part in payload["content"]
                if isinstance(part, Mapping)
                and part.get("type") == "input_text"
                and isinstance(part.get("text"), str)
            ]
            prompt = "".join(text_parts)
            if prompt and "<environment_context>" not in prompt:
                user_prompts.append(prompt)
    identity["turn_contexts"] = contexts
    identity["user_prompts"] = user_prompts
    return identity


def verify_rollout_identity(
    identity: Mapping[str, object], expected: Mapping[str, object]
) -> dict[str, object]:
    """Compare rollout metadata with the exact execution contract for every turn."""
    mismatches: list[str] = []
    for key in ("codex_version", "model_provider", "originator", "cwd"):
        if key in expected and identity.get(key) != expected[key]:
            mismatches.append(key)
    contexts = identity.get("turn_contexts")
    if not isinstance(contexts, list):
        contexts = []
        mismatches.append("turn_contexts")
    expected_turns = expected.get("turn_count")
    if isinstance(expected_turns, int) and len(contexts) != expected_turns:
        mismatches.append("turn_contexts.count")
    for index, context in enumerate(contexts):
        if not isinstance(context, Mapping):
            mismatches.append(f"turn_contexts[{index}]")
            continue
        for key in ("model", "effort", "cwd", "approval_policy", "sandbox_policy"):
            if key in expected and context.get(key) != expected[key]:
                mismatches.append(f"turn_contexts[{index}].{key}")
    if "prompts" in expected and identity.get("user_prompts") != expected["prompts"]:
        mismatches.append("user_prompts")
    if identity.get("status") not in {"recorded", "verified"}:
        mismatches.append("rollout_record")
    return {"status": "verified" if not mismatches else "unverified", "mismatches": mismatches}


def _case_workspace(
    case: Mapping[str, object], ordinary: Path, source: Path
) -> Path:
    return source if case.get("workspace_mode") == "source" else ordinary


def _execution_failure_layer(
    *,
    returncodes: Sequence[object],
    timed_out: bool,
    invalid_json_lines: int,
    observation: Mapping[str, object],
    expected_turns: int,
) -> dict[str, object]:
    reasons: list[str] = []
    if timed_out:
        reasons.append("timeout")
    if len(returncodes) != expected_turns:
        reasons.append("returncode-count")
    if any(not isinstance(code, int) or code != 0 for code in returncodes):
        reasons.append("nonzero-returncode")
    if invalid_json_lines:
        reasons.append("invalid-jsonl")
    if observation.get("completed_turns") != expected_turns:
        reasons.append("incomplete-turns")
    if not str(observation.get("final_answer", "")).strip():
        reasons.append("missing-final-answer")
    errors = observation.get("errors")
    if not isinstance(errors, list):
        errors = []
    transport_pattern = re.compile(
        r"(?:Reconnecting|stream disconnected|Transport error|idle timeout waiting for SSE)",
        re.IGNORECASE,
    )
    recovered_errors = bool(errors) and not reasons and all(
        isinstance(value, str) and transport_pattern.search(value) for value in errors
    )
    if errors and not recovered_errors:
        reasons.append("model-event-error")
    classifications = list(reasons)
    if recovered_errors:
        classifications.append("model-transport-recovered")
    return {
        "status": "failed" if reasons else "passed",
        "classifications": classifications,
        "returncodes": list(returncodes),
        "timed_out": timed_out,
        "invalid_json_lines": invalid_json_lines,
    }


def _operational_failure_layer(
    observation: Mapping[str, object],
) -> dict[str, object]:
    environment_codes: set[str] = set()
    capability_codes: set[str] = set()
    failed_calls = []
    environment_pattern = re.compile(
        r"(?:AUTH|CREDENTIAL|NOT_CONFIGURED|CONFIGURATION|API_KEY)", re.IGNORECASE
    )
    capability_pattern = re.compile(
        r"(?:CAPABILITY|UNAVAILABLE|UNSUPPORTED|NOT_IMPLEMENTED|TOOL_MISSING)",
        re.IGNORECASE,
    )
    code_pattern = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
    calls = observation.get("mcp_calls", [])
    if not isinstance(calls, list):
        calls = []

    def domain_error(value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        if value.get("isError") is True or value.get("ok") is False:
            return True
        error = value.get("error")
        if error not in (None, False, "", {}):
            return True
        return any(
            domain_error(value.get(key))
            for key in ("structured_content", "structuredContent")
        )

    for call in calls:
        if not isinstance(call, Mapping):
            continue
        transport_failed = call.get("status") in {"failed", "error"}
        result_failed = domain_error(call.get("result"))
        if not transport_failed and not result_failed:
            continue
        failed_calls.append(
            {
                "server": str(call.get("server", "")),
                "tool": str(call.get("tool", "")),
                "status": str(call.get("status", "")),
                "failure_kind": "transport" if transport_failed else "domain",
            }
        )
        rendered = json.dumps(call, ensure_ascii=True, sort_keys=True)
        codes = code_pattern.findall(rendered)
        environment_codes.update(code for code in codes if environment_pattern.search(code))
        capability_codes.update(code for code in codes if capability_pattern.search(code))
    classifications = []
    if environment_codes:
        classifications.append("environment-or-auth")
    if capability_codes:
        classifications.append("capability-missing")
    if failed_calls and not classifications:
        classifications.append("unclassified")
    return {
        "status": "failed" if failed_calls else "passed",
        "classifications": classifications,
        "environment_or_auth_codes": sorted(environment_codes),
        "capability_codes": sorted(capability_codes),
        "failed_mcp_calls": failed_calls,
    }


def routing_exit_code(
    samples: Sequence[Mapping[str, object]], secret_scan: Mapping[str, object]
) -> int:
    """Return failure when execution, identity, routing, or secret checks fail."""
    complete = all(
        not sample.get("timed_out")
        and isinstance(sample.get("expected_turns"), int)
        and len(sample.get("returncodes", [])) == sample["expected_turns"]
        and all(isinstance(code, int) and code == 0 for code in sample["returncodes"])
        and isinstance(sample.get("observation"), Mapping)
        and sample["observation"].get("completed_turns") == sample["expected_turns"]
        and bool(str(sample["observation"].get("final_answer", "")).strip())
        and isinstance(sample.get("identity"), Mapping)
        and sample["identity"].get("verification", {}).get("status") == "verified"
        and sample.get("route", {}).get("status") == "passed"
        for sample in samples
    )
    return 0 if complete and secret_scan.get("status") == "clean" else 2


def _legacy_binding(
    legacy_run: Mapping[str, object],
    *,
    arm: Mapping[str, object],
    matrix_path: Path,
    inventory_path: Path,
) -> dict[str, object]:
    mismatches: list[str] = []
    execution = arm.get("execution")
    if not isinstance(execution, Mapping):
        raise ValueError("routing arm execution identity is invalid")
    expected = {
        "arm_id": arm.get("arm_id"),
        "model": execution.get("model"),
        "effort": execution.get("effort"),
        "plugins": execution.get("plugins"),
    }
    for key, value in expected.items():
        if legacy_run.get(key) != value:
            mismatches.append(key)
    matrix = legacy_run.get("matrix")
    inventory = legacy_run.get("inventory")
    retained_matrix_sha = matrix.get("sha256") if isinstance(matrix, Mapping) else None
    current_matrix_sha = _sha256(matrix_path)
    if not isinstance(inventory, Mapping) or inventory.get("sha256") != _sha256(inventory_path):
        mismatches.append("inventory.sha256")
    codex = legacy_run.get("codex")
    if not isinstance(codex, Mapping):
        mismatches.append("codex")
    else:
        actual_version = str(codex.get("version", "")).rsplit(" ", 1)[-1]
        if actual_version != execution.get("codex_version"):
            mismatches.append("codex.version")
        if codex.get("executable_sha256") != execution.get("codex_executable_sha256"):
            mismatches.append("codex.executable_sha256")
    return {
        "status": "verified" if not mismatches else "unverified",
        "mismatches": mismatches,
        "matrix_file": {
            "status": "verified" if retained_matrix_sha == current_matrix_sha else "changed",
            "retained_sha256": retained_matrix_sha,
            "current_sha256": current_matrix_sha,
            "semantic_binding": "verified-by-rollout-user-prompts",
        },
    }


def reconstruct_arm_evidence(
    *,
    evaluator: Mapping[str, object],
    arm: Mapping[str, object],
    arm_verification: Mapping[str, object],
    run_directory: Path,
    matrix_path: Path,
    review_contract_path: Path,
    inventory_path: Path,
    codex_home: Path,
    ordinary_workspace: Path,
    source_workspace: Path,
    explicit_source_workspace: Path,
    workspace_verification: Mapping[str, object],
    environ: Mapping[str, str],
) -> dict[str, object]:
    """Rebuild sanitized evidence from retained raw JSONL without model execution."""
    matrix = apply_review_contract(
        load_matrix(matrix_path), matrix_path, review_contract_path
    )
    inventory_value = json.loads(inventory_path.read_text())
    if not isinstance(inventory_value, list) or not all(
        isinstance(row, Mapping) for row in inventory_value
    ):
        raise ValueError("Skill inventory is invalid")
    inventory = [dict(row) for row in inventory_value]
    legacy_run_path = run_directory / "run.json"
    legacy_run = json.loads(legacy_run_path.read_text())
    if not isinstance(legacy_run, Mapping):
        raise ValueError("retained run metadata is invalid")
    binding = _legacy_binding(
        legacy_run,
        arm=arm,
        matrix_path=matrix_path,
        inventory_path=inventory_path,
    )
    execution = arm["execution"]
    assert isinstance(execution, Mapping)
    source_files = sorted(path for path in (run_directory / "raw").rglob("*") if path.is_file())
    source_secret_scan = scan_secret_files(source_files, environ)
    legacy_samples = {
        path.stem: json.loads(path.read_text())
        for path in (run_directory / "samples").glob("*.json")
    }
    rebuilt_samples: list[dict[str, object]] = []
    for raw_case in matrix["cases"]:
        case = dict(raw_case)
        case_id = str(case["case_id"])
        legacy = legacy_samples.get(case_id)
        if not isinstance(legacy, Mapping):
            raise ValueError(f"retained sample metadata is missing for {case_id}")
        raw_metadata = legacy.get("raw_files")
        if not isinstance(raw_metadata, list) or not raw_metadata:
            raise ValueError(f"retained raw file metadata is missing for {case_id}")
        combined_events: list[dict[str, object]] = []
        turn_summaries: list[dict[str, object]] = []
        raw_manifest: list[dict[str, object]] = []
        raw_mismatches: list[str] = []
        if len(raw_metadata) != len(case["turns"]):
            raw_mismatches.append("raw.turn_count")
        for key, expected_value in (
            ("intent", case["intent"]),
            ("language", case["language"]),
            ("workspace_mode", case["workspace_mode"]),
            ("expected_routes", case["expected_routes"]),
            ("expected_mcp", case.get("expected_mcp", [])),
        ):
            if legacy.get(key, [] if key == "expected_mcp" else None) != expected_value:
                raw_mismatches.append(f"legacy.{key}")
        invalid_count = 0
        for index, entry in enumerate(raw_metadata, 1):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
                raise ValueError(f"retained raw file metadata is invalid for {case_id}")
            raw_path = run_directory / str(entry["path"])
            stderr_path = run_directory / str(entry.get("stderr_path", ""))
            expected_raw_path = f"raw/{case_id}/turn-{index}.jsonl"
            expected_stderr_path = f"raw/{case_id}/turn-{index}.stderr"
            if entry["path"] != expected_raw_path:
                raw_mismatches.append(f"turn-{index}.jsonl.path")
            if entry.get("stderr_path") != expected_stderr_path:
                raw_mismatches.append(f"turn-{index}.stderr.path")
            raw_sha = _sha256(raw_path)
            stderr_sha = _sha256(stderr_path)
            if raw_sha != entry.get("sha256"):
                raw_mismatches.append(f"turn-{index}.jsonl.sha256")
            if stderr_sha != entry.get("stderr_sha256"):
                raw_mismatches.append(f"turn-{index}.stderr.sha256")
            events, invalid = _event_lines(raw_path.read_text())
            invalid_count += len(invalid)
            combined_events.extend(events)
            turn_summaries.append(summarize_events(events, inventory, environ=environ))
            raw_manifest.append(
                {
                    "turn": index,
                    "jsonl": {
                        "path": str(entry["path"]),
                        "sha256": raw_sha,
                        "size_bytes": raw_path.stat().st_size,
                    },
                    "stderr": {
                        "path": str(entry.get("stderr_path", "")),
                        "sha256": stderr_sha,
                        "size_bytes": stderr_path.stat().st_size,
                    },
                }
            )
        observation = summarize_events(combined_events, inventory, environ=environ)
        cwd = _case_workspace(case, ordinary_workspace, source_workspace)
        rollout = (
            _rollout_identity(codex_home, str(observation["thread_id"]))
            if observation["thread_id"]
            else {"status": "unverified", "thread_id": ""}
        )
        rollout_verification = verify_rollout_identity(
            rollout,
            {
                "codex_version": execution["codex_version"],
                "model_provider": execution["model_provider"],
                "originator": execution["originator"],
                "model": execution["model"],
                "effort": execution["effort"],
                "cwd": str(cwd),
                "approval_policy": execution["approval_policy"],
                "sandbox_policy": execution["sandbox_policy"],
                "turn_count": len(case["turns"]),
                "prompts": [
                    str(template).replace(
                        "{source_path}", str(explicit_source_workspace)
                    )
                    for template in case["turns"]
                ],
            },
        )
        legacy_identity = legacy.get("identity")
        if not isinstance(legacy_identity, Mapping):
            raw_mismatches.append("legacy.identity")
        else:
            for key in ("thread_id", "rollout_sha256"):
                if legacy_identity.get(key) != rollout.get(key):
                    raw_mismatches.append(f"legacy.identity.{key}")
        rollout["verification"] = rollout_verification
        route = evaluate_route(
            case,
            observation,
            inventory,
            turn_observations=turn_summaries,
            available_mcp=_arm_mcp_servers(arm),
        )
        returncodes = legacy.get("returncodes", [])
        if not isinstance(returncodes, list):
            returncodes = []
        if len(returncodes) != len(case["turns"]):
            raw_mismatches.append("legacy.returncodes.count")
        timed_out = legacy.get("timed_out") is True
        execution_layer = _execution_failure_layer(
            returncodes=returncodes,
            timed_out=timed_out,
            invalid_json_lines=invalid_count,
            observation=observation,
            expected_turns=len(case["turns"]),
        )
        raw_integrity = {
            "status": "verified" if not raw_mismatches else "unverified",
            "mismatches": raw_mismatches,
        }
        rebuilt_samples.append(
            {
                "case_id": case_id,
                "intent": case["intent"],
                "language": case["language"],
                "workspace_mode": case["workspace_mode"],
                "expected_routes": list(case["expected_routes"]),
                "expected_mcp": list(case.get("expected_mcp", [])),
                "wall_seconds": legacy.get("wall_seconds"),
                "observation": observation,
                "raw_manifest": raw_manifest,
                "raw_integrity": raw_integrity,
                "rollout_identity": rollout,
                "route": route,
                "failure_layers": {
                    "execution": execution_layer,
                    "identity": rollout_verification,
                    "routing": route,
                    "operational": _operational_failure_layer(observation),
                },
            }
        )
    classification_counts = {
        classification: sum(
            sample["route"]["classification"] == classification
            for sample in rebuilt_samples
        )
        for classification in sorted(CLASSIFICATIONS)
    }
    total_usage: dict[str, int | float] = {}
    for sample in rebuilt_samples:
        for key, value in sample["observation"]["usage"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total_usage[key] = total_usage.get(key, 0) + value
    integrity_ok = (
        binding["status"] == "verified"
        and binding["matrix_file"]["status"] == "verified"
        and arm_verification.get("status") == "verified"
        and workspace_verification.get("status") == "verified"
        and source_secret_scan["status"] == "clean"
        and all(sample["raw_integrity"]["status"] == "verified" for sample in rebuilt_samples)
        and all(
            sample["rollout_identity"]["verification"]["status"] == "verified"
            for sample in rebuilt_samples
        )
        and all(
            sample["failure_layers"]["execution"]["status"] == "passed"
            for sample in rebuilt_samples
        )
    )
    evidence: dict[str, object] = {
        "schema": "openubmc.skill-routing-evidence.v1",
        "evaluator": dict(evaluator),
        "arm_identity": {
            "arm_id": arm["arm_id"],
            "kind": arm["kind"],
            "digest": arm["digest"],
            "source": arm["source"],
            "inventory": arm["inventory"],
            "execution": arm["execution"],
            "environment": arm["environment"],
            "distribution": arm.get("plugin", arm.get("loose_skills")),
            "artifact_verification": arm_verification,
        },
        "source_binding": {
            "retained_run_schema": legacy_run.get("schema"),
            "retained_run_sha256": _sha256(legacy_run_path),
            "legacy_binding": binding,
            "matrix_sha256": _sha256(matrix_path),
            "review_contract_sha256": _sha256(review_contract_path),
            "inventory_sha256": _sha256(inventory_path),
            "workspace_verification": workspace_verification,
        },
        "raw_evidence": {
            "boundary": "local-only",
            "embedded": False,
            "file_count": len(source_files),
            "secret_scan": source_secret_scan,
        },
        "integrity": {"status": "verified" if integrity_ok else "unverified"},
        "summary": {
            "sample_count": len(rebuilt_samples),
            "routing_passed": sum(
                sample["route"]["status"] == "passed" for sample in rebuilt_samples
            ),
            "routing_failed": sum(
                sample["route"]["status"] != "passed" for sample in rebuilt_samples
            ),
            "classifications": classification_counts,
            "wall_seconds": legacy_run.get("wall_seconds"),
            "usage": total_usage,
        },
        "samples": rebuilt_samples,
    }
    evidence["digest"] = document_digest(evidence)
    return evidence


def _require_qualified_evidence(label: str, evidence: Mapping[str, object]) -> None:
    """Fail closed before publishing a paired routing conclusion."""
    integrity = evidence.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("status") != "verified":
        raise ValueError(f"{label} evidence integrity is unverified")
    evaluator = evidence.get("evaluator")
    evaluator_files = evaluator.get("files") if isinstance(evaluator, Mapping) else None
    if (
        not isinstance(evaluator, Mapping)
        or evaluator.get("schema") != "openubmc.skill-routing-evaluator.v1"
        or evaluator.get("status") != "verified"
        or evaluator.get("mismatches") != []
        or not re.fullmatch(r"[0-9a-f]{40}", str(evaluator.get("commit", "")))
        or not re.fullmatch(r"[0-9a-f]{40}", str(evaluator.get("tree", "")))
        or not isinstance(evaluator_files, Mapping)
        or set(evaluator_files) != {"runner", "matrix", "review_contract"}
        or any(
            not isinstance(value, Mapping)
            or value.get("commit") != evaluator.get("commit")
            or value.get("sha256") != value.get("committed_sha256")
            for value in evaluator_files.values()
        )
    ):
        raise ValueError(f"{label} evaluator identity is unverified")

    arm = evidence.get("arm_identity")
    artifact = arm.get("artifact_verification") if isinstance(arm, Mapping) else None
    if not isinstance(artifact, Mapping) or artifact.get("status") != "verified":
        raise ValueError(f"{label} evidence artifact identity is unverified")

    source_binding = evidence.get("source_binding")
    if not isinstance(source_binding, Mapping):
        raise ValueError(f"{label} evidence source binding is unverified")
    legacy = source_binding.get("legacy_binding")
    workspace = source_binding.get("workspace_verification")
    matrix_file = legacy.get("matrix_file") if isinstance(legacy, Mapping) else None
    if (
        not isinstance(legacy, Mapping)
        or legacy.get("status") != "verified"
        or not isinstance(matrix_file, Mapping)
        or matrix_file.get("status") != "verified"
    ):
        raise ValueError(f"{label} evidence retained matrix binding is unverified")
    if not isinstance(workspace, Mapping) or workspace.get("status") != "verified":
        raise ValueError(f"{label} evidence workspace identity is unverified")

    raw_evidence = evidence.get("raw_evidence")
    secret_scan = raw_evidence.get("secret_scan") if isinstance(raw_evidence, Mapping) else None
    if (
        not isinstance(raw_evidence, Mapping)
        or raw_evidence.get("boundary") != "local-only"
        or raw_evidence.get("embedded") is not False
        or not isinstance(secret_scan, Mapping)
        or secret_scan.get("status") != "clean"
    ):
        raise ValueError(f"{label} evidence raw boundary is unverified")

    samples = evidence.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError(f"{label} evidence samples are missing")
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise ValueError(f"{label} evidence sample is invalid")
        case_id = str(sample.get("case_id", "unknown"))
        raw_integrity = sample.get("raw_integrity")
        rollout = sample.get("rollout_identity")
        rollout_verification = (
            rollout.get("verification") if isinstance(rollout, Mapping) else None
        )
        layers = sample.get("failure_layers")
        execution_layer = layers.get("execution") if isinstance(layers, Mapping) else None
        identity_layer = layers.get("identity") if isinstance(layers, Mapping) else None
        if not isinstance(raw_integrity, Mapping) or raw_integrity.get("status") != "verified":
            raise ValueError(f"{label} evidence raw sample is unverified: {case_id}")
        if (
            not isinstance(rollout_verification, Mapping)
            or rollout_verification.get("status") != "verified"
            or not isinstance(identity_layer, Mapping)
            or identity_layer.get("status") != "verified"
        ):
            raise ValueError(f"{label} evidence rollout identity is unverified: {case_id}")
        if (
            not isinstance(execution_layer, Mapping)
            or execution_layer.get("status") != "passed"
        ):
            raise ValueError(f"{label} evidence execution failed: {case_id}")

    if evidence.get("digest") != document_digest(evidence):
        raise ValueError(f"{label} evidence digest is invalid")


def compare_arm_evidence(
    baseline: Mapping[str, object], candidate: Mapping[str, object]
) -> dict[str, object]:
    _require_qualified_evidence("baseline", baseline)
    _require_qualified_evidence("candidate", candidate)
    baseline_identity = baseline.get("arm_identity")
    candidate_identity = candidate.get("arm_identity")
    if not isinstance(baseline_identity, Mapping) or not isinstance(
        candidate_identity, Mapping
    ):
        raise ValueError("paired arm identities are missing")
    if baseline_identity.get("source") != candidate_identity.get("source"):
        raise ValueError("paired source identity does not match")
    if baseline.get("evaluator") != candidate.get("evaluator"):
        raise ValueError("paired evaluator identity does not match")
    before_execution = baseline_identity.get("execution")
    after_execution = candidate_identity.get("execution")
    if not isinstance(before_execution, Mapping) or not isinstance(
        after_execution, Mapping
    ):
        raise ValueError("paired execution identity is missing")
    shared_execution_keys = (
        "codex_version",
        "codex_executable_sha256",
        "model",
        "effort",
        "model_provider",
        "originator",
        "approval_policy",
        "sandbox_policy",
    )
    if any(
        before_execution.get(key) != after_execution.get(key)
        for key in shared_execution_keys
    ):
        raise ValueError("paired execution identity does not match")
    before_environment = baseline_identity.get("environment")
    after_environment = candidate_identity.get("environment")
    if not isinstance(before_environment, Mapping) or not isinstance(
        after_environment, Mapping
    ):
        raise ValueError("paired environment identity is missing")
    shared_environment_keys = ("profile", "target", "credentials", "operating_system")
    if any(
        before_environment.get(key) != after_environment.get(key)
        for key in shared_environment_keys
    ):
        raise ValueError("paired environment identity does not match")
    baseline_samples = {
        sample["case_id"]: sample
        for sample in baseline.get("samples", [])
        if isinstance(sample, Mapping) and isinstance(sample.get("case_id"), str)
    }
    candidate_samples = {
        sample["case_id"]: sample
        for sample in candidate.get("samples", [])
        if isinstance(sample, Mapping) and isinstance(sample.get("case_id"), str)
    }
    if baseline_samples.keys() != candidate_samples.keys():
        raise ValueError("paired evidence case identities do not match")
    rows = []
    for case_id in baseline_samples:
        before = baseline_samples[case_id]
        after = candidate_samples[case_id]
        before_passed = before["route"]["status"] == "passed"
        after_passed = after["route"]["status"] == "passed"
        treatment_changed = (
            bool(before.get("expected_mcp"))
            and before_environment.get("knowledge_base")
            != after_environment.get("knowledge_base")
        )
        change = (
            "not-comparable-treatment"
            if treatment_changed
            else "improved"
            if not before_passed and after_passed
            else "regressed"
            if before_passed and not after_passed
            else "unchanged-pass"
            if before_passed
            else "unchanged-fail"
        )
        rows.append(
            {
                "case_id": case_id,
                "intent": before["intent"],
                "language": before["language"],
                "workspace_mode": before["workspace_mode"],
                "baseline": {
                    "classification": before["route"]["classification"],
                    "observed_routes": before["route"]["observed_routes"],
                    "observed_mcp": before["route"]["observed_mcp"],
                    "total_tokens": before["observation"]["usage"].get("total_tokens", 0),
                    "wall_seconds": before.get("wall_seconds"),
                    "operational": before["failure_layers"]["operational"],
                    "execution": before["failure_layers"]["execution"],
                    "mcp_calls": [
                        {
                            "server": call["server"],
                            "tool": call["tool"],
                            "status": call["status"],
                        }
                        for call in before["observation"]["mcp_calls"]
                    ],
                },
                "candidate": {
                    "classification": after["route"]["classification"],
                    "observed_routes": after["route"]["observed_routes"],
                    "observed_mcp": after["route"]["observed_mcp"],
                    "total_tokens": after["observation"]["usage"].get("total_tokens", 0),
                    "wall_seconds": after.get("wall_seconds"),
                    "operational": after["failure_layers"]["operational"],
                    "execution": after["failure_layers"]["execution"],
                    "mcp_calls": [
                        {
                            "server": call["server"],
                            "tool": call["tool"],
                            "status": call["status"],
                        }
                        for call in after["observation"]["mcp_calls"]
                    ],
                },
                "change": change,
            }
        )
    comparison: dict[str, object] = {
        "schema": "openubmc.skill-routing-comparison.v1",
        "baseline": {
            "arm_id": baseline["arm_identity"]["arm_id"],
            "identity_digest": baseline["arm_identity"]["digest"],
            "evidence_digest": baseline["digest"],
            "summary": baseline["summary"],
            "inventory": baseline_identity["inventory"],
            "distribution": baseline_identity["distribution"],
        },
        "candidate": {
            "arm_id": candidate["arm_identity"]["arm_id"],
            "identity_digest": candidate["arm_identity"]["digest"],
            "evidence_digest": candidate["digest"],
            "summary": candidate["summary"],
            "inventory": candidate_identity["inventory"],
            "distribution": candidate_identity["distribution"],
        },
        "summary": {
            "cases": len(rows),
            "improved": sum(row["change"] == "improved" for row in rows),
            "regressed": sum(row["change"] == "regressed" for row in rows),
            "unchanged_pass": sum(row["change"] == "unchanged-pass" for row in rows),
            "unchanged_fail": sum(row["change"] == "unchanged-fail" for row in rows),
            "discoverability_improved": sum(
                row["change"] == "improved" for row in rows
            ),
            "not_comparable_treatment": sum(
                row["change"] == "not-comparable-treatment" for row in rows
            ),
            "routing_pass_delta": candidate["summary"]["routing_passed"]
            - baseline["summary"]["routing_passed"],
        },
        "paired_identity": {
            "evaluator": baseline["evaluator"],
            "source": baseline_identity["source"],
            "execution": {
                key: before_execution[key] for key in shared_execution_keys
            },
            "external_environment": {
                key: before_environment[key] for key in shared_environment_keys
            },
            "treatment": {
                "baseline": {
                    "kind": baseline_identity["kind"],
                    "knowledge_base": before_environment.get("knowledge_base"),
                    "mcp_servers": _arm_mcp_servers(baseline_identity),
                },
                "candidate": {
                    "kind": candidate_identity["kind"],
                    "knowledge_base": after_environment.get("knowledge_base"),
                    "mcp_servers": _arm_mcp_servers(candidate_identity),
                },
            },
            "status": "verified",
        },
        "cases": rows,
    }
    comparison["digest"] = document_digest(comparison)
    return comparison


def render_comparison_report(comparison: Mapping[str, object]) -> str:
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    summary = comparison["summary"]
    paired = comparison["paired_identity"]
    evaluator = paired["evaluator"]
    execution = paired["execution"]
    source = paired["source"]
    treatment = paired["treatment"]
    before = baseline["summary"]
    after = candidate["summary"]

    cases = comparison["cases"]
    workspace_counts = {
        mode: sum(row["workspace_mode"] == mode for row in cases)
        for mode in WORKSPACE_MODES
    }

    def observed(arm: Mapping[str, object]) -> str:
        values = [f"`{name}`" for name in arm["observed_routes"]]
        values.extend(
            f"`{call['tool']}` {call['status']}"
            for call in arm["mcp_calls"]
        )
        return ", ".join(values) or "—"

    def case(case_id: str) -> Mapping[str, object]:
        return next(row for row in cases if row["case_id"] == case_id)

    wall_delta = after["wall_seconds"] - before["wall_seconds"]
    token_delta = after["usage"]["total_tokens"] - before["usage"]["total_tokens"]
    baseline_distribution = baseline["distribution"]
    candidate_distribution = candidate["distribution"]
    disabled_count = before["classifications"]["skill-not-loaded"]
    unchanged_pass = summary["unchanged_pass"]
    explicit_build = case("build-zh-explicit-source")
    source_build = case("build-en-source-cwd")
    lines = [
        "# openUBMC 原生 Skill 路由对比",
        "",
        (
            f"插件通过 {after['routing_passed']}/{summary['cases']} 个路由判据用例，"
            f"loose Skills 基线通过 {before['routing_passed']}/{summary['cases']} 个，"
            f"产品级路由结果增加 {summary['routing_pass_delta']} 个。"
            f"其中 {summary['discoverability_improved']} 个可配对用例改善，"
            f"{summary['not_comparable_treatment']} 个 KB 用例因安装能力不同不计入发现性增量，"
            f"{unchanged_pass} 个负向用例两组均未误触发。"
        ),
        "",
        "## 执行与输入身份",
        "",
        f"- Codex CLI：`{execution['codex_version']}`；模型：`{execution['model']}`；effort：`{execution['effort']}`；provider：`{execution['model_provider']}`。",
        f"- 原生入口：`originator={execution['originator']}`；安全策略：`approval_policy={execution['approval_policy']}`，`sandbox={execution['sandbox_policy']['type']}`。每个 rollout 和多轮 resume 都从原生 session 记录反验。",
        f"- 评估器：提交 `{evaluator['commit']}`，tree `{evaluator['tree']}`；runner `{evaluator['files']['runner']['sha256']}`，matrix `{evaluator['files']['matrix']['sha256']}`，review contract `{evaluator['files']['review_contract']['sha256']}`。三个输入均已与该提交中的精确字节核验。",
        f"- 外部环境：两臂均未提供目标或凭据；KB MCP 可用性属于安装形态这一处理变量。loose 臂为 `{treatment['baseline']['knowledge_base']}`，插件臂为 `{treatment['candidate']['knowledge_base']}`。",
        f"- 源码：`{source['repository']}@{source['commit']}`，tree `{source['tree']}`。",
        f"- 插件：`{candidate_distribution['name']} {candidate_distribution['version']}`；archive `{candidate_distribution['archive_sha256']}`；content `{candidate_distribution['content_digest']}`；subject `{candidate_distribution['subject_digest']}`；Runtime `{candidate_distribution['runtime_digest']}`。原生 inventory 含 {candidate_distribution['plugin_skill_count']} 个插件 Skill 和 {len(candidate_distribution['mcp_servers'])} 个 MCP。",
        f"- loose Skills：原生 inventory 含 {baseline['inventory']['openubmc_entry_count']} 个 openUBMC 条目；{baseline_distribution['content_inventory_file']['file_count']} 文件内容清单 `{baseline_distribution['content_inventory_digest']}`；配置 `{baseline_distribution['configuration_sha256']}`。其上游仓库为 dirty 状态，内容清单是权威身份。",
        f"- 目录覆盖：{workspace_counts['ordinary']} 个普通空目录用例、{workspace_counts['explicit-source']} 个从普通目录引用外部源码的用例、{workspace_counts['source']} 个源码 cwd 用例。普通目录已验证为空且不属于 Git，源码目录已验证为上述干净提交。",
        "",
        "## 实际读取与调用",
        "",
        "| 用例 | 目录 | loose Skills 实际读取/调用 | 插件实际读取/调用 | 路由变化 |",
        "|---|---|---|---|---|",
    ]
    for row in cases:
        lines.append(
            f"| `{row['case_id']}` | {row['workspace_mode']} | "
            f"{observed(row['baseline'])} | {observed(row['candidate'])} | "
            f"`{row['baseline']['classification']}` → `{row['candidate']['classification']}` |"
        )
    lines.extend(
        [
            "",
            "多轮用例逐 turn 核验：第一轮没有提前读取 openUBMC Skill，补充 openUBMC 上下文后第二轮才读取 Debug。Skill 读取只有在命令完成、退出码为零且输出含对应 `SKILL.md` frontmatter 名称时才成立。",
            "",
            "## 耗时与 token",
            "",
            "| 评测臂 | 总耗时 | 输入 token | 输出 token | 总 token |",
            "|---|---:|---:|---:|---:|",
            f"| Loose Skills | {before['wall_seconds']:.2f}s | {before['usage']['input_tokens']:,} | {before['usage']['output_tokens']:,} | {before['usage']['total_tokens']:,} |",
            f"| 插件 | {after['wall_seconds']:.2f}s | {after['usage']['input_tokens']:,} | {after['usage']['output_tokens']:,} | {after['usage']['total_tokens']:,} |",
            "",
            f"单次样本合计差值为 {wall_delta:+.2f}s、{token_delta:+,} token。没有可信价格依据，USD 成本未测量；每个用例只有一次 rollout，这些数据不支持统计性能结论。",
            "",
            f"代表性构建用例：显式外部源码从 {explicit_build['baseline']['wall_seconds']:.2f}s / {explicit_build['baseline']['total_tokens']:,} token 降至 {explicit_build['candidate']['wall_seconds']:.2f}s / {explicit_build['candidate']['total_tokens']:,} token；源码 cwd 从 {source_build['baseline']['wall_seconds']:.2f}s / {source_build['baseline']['total_tokens']:,} token 降至 {source_build['candidate']['wall_seconds']:.2f}s / {source_build['candidate']['total_tokens']:,} token。插件诊断 Skill 仍会读取较多参考资料，存在进一步压缩输入 token 的空间。",
            "",
            "## 错误归因",
            "",
            f"- loose 基线的 {disabled_count} 个正向失败首先归因为 `skill-not-loaded`：原生 inventory 将预期的 7 个 canonical Skills 标为 disabled。记录同时保留随后读取的 fallback Skill，没有把加载问题误写成模型未触发。",
            "- loose KB 用例归因为 `mcp-not-loaded`：该安装形态没有 `openubmc-kb` MCP，不能据此判断模型是否会触发一个并不存在的工具。插件 KB 已读取相关 Debug Skill 并实际调用 query/status；query 返回 `KB_CREDENTIALS_MISSING`，属于 `environment-or-auth`，不属于路由失败。",
            "- loose 的两个 rollout 出现可恢复的模型流超时并继续到 `turn.completed`，记录为 `model-transport-recovered`，未误判为终态执行失败。",
            "- 当前矩阵没有出现预期 Skill 已启用并读取、随后因内部实现能力缺失而失败的样本。已有 `skill-positive` 的 systemd/journal 采集失败属于 #228 的 Runtime 能力缺口，不计入 #233 路由结果。",
            "- 未提供目标、设备凭据或真实包；没有联系、重启、刷写或修改 BMC。设备任务未被宣称完成。",
            "",
            "## 最小改进",
            "",
            "1. 保留已合并的插件中英文描述；本轮 9 个可配对正向发现性样本均改善，没有证据支持继续扩大默认 prompt。",
            "2. loose 安装先校正目录 `enabled=true` 与直接 `SKILL.md enabled=false` 的重复配置，再重新探测 canonical Skill inventory。本轮证明了 canonical Skill 被禁用，没有单独证明 Codex 的路径解析因果。",
            "3. 后续单独精简 Debug/Build 的参考资料读取；该问题影响 token 和延迟，不改变本轮路由结论。",
            "4. systemd/journal 当前状态采集继续由 #228 修复，不并入 Skill 发现性改动。",
            "",
            "## 证据边界",
            "",
            f"原始 JSONL 与 stderr 仅保留在本地；提交的是脱敏摘要、逐文件 SHA-256 和读取/调用记录。loose evidence：`{baseline['evidence_digest']}`；plugin evidence：`{candidate['evidence_digest']}`。原始文件已针对当前凭据类环境变量扫描，未发现匹配。",
            "",
            "本记录只验证 Linux WSL2 中的原生 Codex CLI 路由；未验证原生 Windows、真实 BMC、Conan 发布或升级完成度。",
            "这是 routing-only 原生 Codex 记录，不替代完整 Evaluation Lab Bundle 和 independent task review。",
            "",
        ]
    )
    return "\n".join(lines)


def rebuild_pair(args: argparse.Namespace) -> int:
    matrix_path = Path(args.matrix).resolve(strict=True)
    review_contract_path = Path(args.review_contract).resolve(strict=True)
    evaluator_workspace = Path(__file__).resolve().parents[1]
    evaluator = verify_evaluator_identity(
        evaluator_workspace,
        args.evaluator_commit,
        Path(__file__).resolve(),
        matrix_path,
        review_contract_path,
    )
    if evaluator["status"] != "verified":
        raise ValueError(
            "evaluator identity verification failed: "
            + ", ".join(evaluator["mismatches"])
        )
    ordinary = Path(args.ordinary_workspace).resolve(strict=True)
    source = Path(args.source_workspace).resolve(strict=True)
    explicit = Path(args.explicit_source).resolve(strict=True)
    baseline_arm = load_arm_identity(Path(args.baseline_arm_identity).resolve(strict=True))
    candidate_arm = load_arm_identity(Path(args.candidate_arm_identity).resolve(strict=True))
    expected_commit = str(baseline_arm["source"]["commit"])
    if candidate_arm["source"].get("commit") != expected_commit:
        raise ValueError("paired arm source commits do not match")
    workspace_verification = verify_workspace_layout(
        ordinary, source, explicit, expected_commit
    )
    if workspace_verification["status"] != "verified":
        raise ValueError(
            "workspace identity verification failed: "
            + ", ".join(workspace_verification["mismatches"])
        )
    baseline_inventory = Path(args.baseline_inventory).resolve(strict=True)
    candidate_inventory = Path(args.candidate_inventory).resolve(strict=True)
    baseline_verification = verify_arm_artifacts(
        baseline_arm,
        inventory_path=baseline_inventory,
        source_workspace=source,
        artifact_paths={
            "skill_root": Path(args.baseline_skill_root).resolve(strict=True),
            "configuration": Path(args.baseline_configuration).resolve(strict=True),
            "content_inventory": Path(args.baseline_content_inventory).resolve(
                strict=True
            ),
        },
    )
    candidate_verification = verify_arm_artifacts(
        candidate_arm,
        inventory_path=candidate_inventory,
        source_workspace=source,
        artifact_paths={
            "archive": Path(args.candidate_archive).resolve(strict=True),
            "subject": Path(args.candidate_subject).resolve(strict=True),
            "runtime": Path(args.candidate_runtime).resolve(strict=True),
        },
    )
    for label, verification in (
        ("baseline", baseline_verification),
        ("candidate", candidate_verification),
    ):
        if verification["status"] != "verified":
            raise ValueError(
                f"{label} arm artifact verification failed: "
                + ", ".join(verification["mismatches"])
            )
    baseline = reconstruct_arm_evidence(
        evaluator=evaluator,
        arm=baseline_arm,
        arm_verification=baseline_verification,
        run_directory=Path(args.baseline_run).resolve(strict=True),
        matrix_path=matrix_path,
        review_contract_path=review_contract_path,
        inventory_path=baseline_inventory,
        codex_home=Path(args.baseline_codex_home).resolve(strict=True),
        ordinary_workspace=ordinary,
        source_workspace=source,
        explicit_source_workspace=explicit,
        workspace_verification=workspace_verification,
        environ=os.environ,
    )
    candidate = reconstruct_arm_evidence(
        evaluator=evaluator,
        arm=candidate_arm,
        arm_verification=candidate_verification,
        run_directory=Path(args.candidate_run).resolve(strict=True),
        matrix_path=matrix_path,
        review_contract_path=review_contract_path,
        inventory_path=candidate_inventory,
        codex_home=Path(args.candidate_codex_home).resolve(strict=True),
        ordinary_workspace=ordinary,
        source_workspace=source,
        explicit_source_workspace=explicit,
        workspace_verification=workspace_verification,
        environ=os.environ,
    )
    comparison = compare_arm_evidence(baseline, candidate)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    baseline_path = output / "routing-evidence-loose.json"
    candidate_path = output / "routing-evidence-plugin.json"
    comparison_path = output / "routing-comparison.json"
    report_path = output / "routing-report.md"
    _write_json(baseline_path, baseline)
    _write_json(candidate_path, candidate)
    _write_json(comparison_path, comparison)
    report_path.write_text(render_comparison_report(comparison))
    generated_scan = scan_secret_files(
        [baseline_path, candidate_path, comparison_path, report_path], os.environ
    )
    if generated_scan["status"] != "clean":
        raise RuntimeError(
            "generated sanitized evidence contains credential environment values: "
            + ", ".join(match["variable"] for match in generated_scan["matches"])
        )
    if baseline["integrity"]["status"] != "verified" or candidate["integrity"]["status"] != "verified":
        return 2
    return 0


def run_matrix(args: argparse.Namespace) -> int:
    matrix_path = Path(args.matrix).resolve()
    review_contract_path = Path(args.review_contract).resolve(strict=True)
    evaluator = verify_evaluator_identity(
        Path(__file__).resolve().parents[1],
        args.evaluator_commit,
        Path(__file__).resolve(),
        matrix_path,
        review_contract_path,
    )
    if evaluator["status"] != "verified":
        raise ValueError(
            "evaluator identity verification failed: "
            + ", ".join(evaluator["mismatches"])
        )
    matrix = apply_review_contract(
        load_matrix(matrix_path), matrix_path, review_contract_path
    )
    arm_path = Path(args.arm_identity).resolve(strict=True)
    arm = load_arm_identity(arm_path)
    execution = arm["execution"]
    assert isinstance(execution, Mapping)
    inventory_path = Path(args.inventory).resolve()
    inventory = json.loads(inventory_path.read_text())
    if not isinstance(inventory, list):
        raise ValueError("Skill inventory is invalid")
    executable = resolve_executable(args.codex)
    codex_home = Path(os.environ["CODEX_HOME"]).resolve(strict=True)
    ordinary = Path(args.ordinary_workspace).resolve(strict=True)
    source = Path(args.source_workspace).resolve(strict=True)
    explicit_source = Path(args.explicit_source).resolve(strict=True)
    source_identity = arm["source"]
    assert isinstance(source_identity, Mapping)
    workspace_verification = verify_workspace_layout(
        ordinary, source, explicit_source, str(source_identity["commit"])
    )
    artifact_paths = {
        name: Path(value).resolve(strict=True)
        for name, value in {
            "archive": args.archive,
            "subject": args.subject,
            "runtime": args.runtime,
            "skill_root": args.skill_root,
            "configuration": args.configuration,
            "content_inventory": args.content_inventory,
        }.items()
        if value
    }
    arm_verification = verify_arm_artifacts(
        arm,
        inventory_path=inventory_path,
        source_workspace=source,
        artifact_paths=artifact_paths,
    )
    version = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, check=True, env=os.environ
    ).stdout.strip()
    codex_version = version.rsplit(" ", 1)[-1]
    executable_sha256 = _sha256(executable)
    execution_mismatches = []
    if codex_version != execution["codex_version"]:
        execution_mismatches.append("codex_version")
    if executable_sha256 != execution["codex_executable_sha256"]:
        execution_mismatches.append("codex_executable_sha256")
    if workspace_verification["status"] != "verified":
        raise ValueError(
            "workspace identity verification failed: "
            + ", ".join(workspace_verification["mismatches"])
        )
    if arm_verification["status"] != "verified":
        raise ValueError(
            "arm artifact verification failed: " + ", ".join(arm_verification["mismatches"])
        )
    if execution_mismatches:
        raise ValueError("Codex execution identity failed: " + ", ".join(execution_mismatches))
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "LOCAL_ONLY_EVIDENCE").write_text(
        "Raw model output may contain private inputs. Export only through the sanitized evidence command.\n"
    )
    model = str(execution["model"])
    effort = str(execution["effort"])
    plugins = bool(execution["plugins"])
    started = time.monotonic()
    sample_records: list[dict[str, object]] = []
    for raw_case in matrix["cases"]:
        case = dict(raw_case)
        case_id = str(case["case_id"])
        case_root = output / "raw" / case_id
        case_root.mkdir(parents=True)
        cwd = source if case["workspace_mode"] == "source" else ordinary
        combined_events: list[dict[str, object]] = []
        turn_summaries: list[dict[str, object]] = []
        raw_files: list[dict[str, str]] = []
        thread_id = ""
        returncodes: list[int] = []
        timed_out = False
        case_started = time.monotonic()
        for turn_number, template in enumerate(case["turns"], 1):
            prompt = str(template).replace("{source_path}", str(explicit_source))
            command = (
                build_codex_command(
                    executable,
                    model=model,
                    effort=effort,
                    cwd=cwd,
                    prompt=prompt,
                    plugins=plugins,
                )
                if turn_number == 1
                else build_resume_command(
                    executable,
                    thread_id=thread_id,
                    model=model,
                    effort=effort,
                    prompt=prompt,
                    plugins=plugins,
                )
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=os.environ,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=args.timeout,
                    check=False,
                )
                stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                returncode = 124
                timed_out = True
            raw_path = case_root / f"turn-{turn_number}.jsonl"
            raw_path.write_text(stdout)
            stderr_path = case_root / f"turn-{turn_number}.stderr"
            stderr_path.write_text(stderr)
            raw_files.append(
                {
                    "path": raw_path.relative_to(output).as_posix(),
                    "sha256": _sha256(raw_path),
                    "stderr_path": stderr_path.relative_to(output).as_posix(),
                    "stderr_sha256": _sha256(stderr_path),
                }
            )
            events, invalid = _event_lines(stdout)
            if invalid:
                _write_json(case_root / f"turn-{turn_number}-invalid.json", invalid)
            combined_events.extend(events)
            turn_summary = summarize_events(events, inventory)
            turn_summaries.append(turn_summary)
            if turn_number == 1:
                thread_id = str(turn_summary["thread_id"])
            returncodes.append(returncode)
            if returncode or not thread_id:
                break
        summary = summarize_events(combined_events, inventory)
        identity = _rollout_identity(codex_home, thread_id) if thread_id else {"status": "unverified"}
        identity_verification = verify_rollout_identity(
            identity,
            {
                "codex_version": execution["codex_version"],
                "model_provider": execution["model_provider"],
                "originator": execution["originator"],
                "model": model,
                "effort": effort,
                "cwd": str(cwd),
                "approval_policy": execution["approval_policy"],
                "sandbox_policy": execution["sandbox_policy"],
                "turn_count": len(case["turns"]),
            },
        )
        identity["verification"] = identity_verification
        route = evaluate_route(
            case,
            summary,
            inventory,
            turn_observations=turn_summaries,
            available_mcp=_arm_mcp_servers(arm),
        )
        sample = {
            "case_id": case_id,
            "expected_turns": len(case["turns"]),
            "intent": case["intent"],
            "language": case["language"],
            "workspace_mode": case["workspace_mode"],
            "expected_routes": list(case["expected_routes"]),
            "expected_mcp": list(case.get("expected_mcp", [])),
            "raw_files": raw_files,
            "returncodes": returncodes,
            "timed_out": timed_out,
            "wall_seconds": round(time.monotonic() - case_started, 6),
            "observation": summary,
            "identity": identity,
            "route": route,
        }
        _write_json(output / "samples" / f"{case_id}.json", sample)
        sample_records.append(sample)
        print(
            json.dumps(
                {
                    "case_id": case_id,
                    "returncodes": returncodes,
                    "timed_out": timed_out,
                    "wall_seconds": sample["wall_seconds"],
                    "skill_reads": summary["skill_reads"],
                    "mcp_calls": [
                        {
                            "server": call["server"],
                            "tool": call["tool"],
                            "status": call["status"],
                        }
                        for call in summary["mcp_calls"]
                    ],
                    "route": route["classification"],
                    "total_tokens": summary["usage"].get("total_tokens", 0),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    evidence_paths = sorted(
        path for path in (output / "raw").rglob("*") if path.is_file()
    )
    secret_scan = scan_secret_files(evidence_paths, os.environ)
    route_passes = sum(sample["route"]["status"] == "passed" for sample in sample_records)
    identities_verified = sum(
        sample["identity"].get("verification", {}).get("status") == "verified"
        for sample in sample_records
    )
    record = {
        "schema": RUN_SCHEMA,
        "evaluator": evaluator,
        "arm_identity": {
            "arm_id": arm["arm_id"],
            "digest": arm["digest"],
            "verification": arm_verification,
        },
        "matrix": {"path": matrix_path.name, "sha256": _sha256(matrix_path)},
        "review_contract": {
            "path": review_contract_path.name,
            "sha256": _sha256(review_contract_path),
        },
        "inventory": {"path": inventory_path.name, "sha256": _sha256(inventory_path)},
        "workspace": workspace_verification,
        "environment": arm["environment"],
        "codex": {"version": version, "executable_sha256": executable_sha256},
        "model": model,
        "effort": effort,
        "plugins": plugins,
        "sample_count": len(sample_records),
        "routing": {"passed": route_passes, "failed": len(sample_records) - route_passes},
        "rollout_identities": {
            "verified": identities_verified,
            "unverified": len(sample_records) - identities_verified,
        },
        "raw_evidence": {
            "boundary": "local-only",
            "secret_scan": secret_scan,
        },
        "wall_seconds": round(time.monotonic() - started, 6),
        "samples": [str(Path("samples") / f"{sample['case_id']}.json") for sample in sample_records],
    }
    record["digest"] = document_digest(record)
    _write_json(output / "run.json", record)
    return routing_exit_code(sample_records, secret_scan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--codex", default="codex")
    inventory.add_argument("--workspace", required=True)
    inventory.add_argument("--output", required=True)
    run = commands.add_parser("run")
    run.add_argument("--matrix", required=True)
    run.add_argument("--review-contract", required=True)
    run.add_argument("--evaluator-commit", required=True)
    run.add_argument("--inventory", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--arm-identity", required=True)
    run.add_argument("--codex", default="codex")
    run.add_argument("--ordinary-workspace", required=True)
    run.add_argument("--source-workspace", required=True)
    run.add_argument("--explicit-source", required=True)
    run.add_argument("--archive")
    run.add_argument("--subject")
    run.add_argument("--runtime")
    run.add_argument("--skill-root")
    run.add_argument("--configuration")
    run.add_argument("--content-inventory")
    run.add_argument("--timeout", type=float, default=600)
    rebuild = commands.add_parser(
        "rebuild-pair",
        help="rebuild sanitized paired evidence from retained raw JSONL",
    )
    rebuild.add_argument("--matrix", required=True)
    rebuild.add_argument("--review-contract", required=True)
    rebuild.add_argument("--evaluator-commit", required=True)
    rebuild.add_argument("--baseline-run", required=True)
    rebuild.add_argument("--baseline-inventory", required=True)
    rebuild.add_argument("--baseline-arm-identity", required=True)
    rebuild.add_argument("--baseline-codex-home", required=True)
    rebuild.add_argument("--baseline-skill-root", required=True)
    rebuild.add_argument("--baseline-configuration", required=True)
    rebuild.add_argument("--baseline-content-inventory", required=True)
    rebuild.add_argument("--candidate-run", required=True)
    rebuild.add_argument("--candidate-inventory", required=True)
    rebuild.add_argument("--candidate-arm-identity", required=True)
    rebuild.add_argument("--candidate-codex-home", required=True)
    rebuild.add_argument("--candidate-archive", required=True)
    rebuild.add_argument("--candidate-subject", required=True)
    rebuild.add_argument("--candidate-runtime", required=True)
    rebuild.add_argument("--ordinary-workspace", required=True)
    rebuild.add_argument("--source-workspace", required=True)
    rebuild.add_argument("--explicit-source", required=True)
    rebuild.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "inventory":
        executable = resolve_executable(args.codex)
        rows = probe_skills(executable, Path(args.workspace).resolve(strict=True))
        _write_json(Path(args.output).resolve(), rows)
        return 0
    if args.command == "rebuild-pair":
        return rebuild_pair(args)
    return run_matrix(args)


if __name__ == "__main__":
    raise SystemExit(main())
