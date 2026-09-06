#!/usr/bin/env python3
"""Prepare/check a stable Skill candidate without activating or publishing it."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "openubmc-agent-workflow.skill-promotion-check.v1"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load authority module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _module("promotion_installer", ROOT / "openubmc-environment-setup/scripts/install_environment.py")
release = _module("promotion_release", ROOT / "openubmc-target-runtime/openubmc_target_runtime/release.py")


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _root(path: Path) -> Path:
    path = path.expanduser().resolve()
    actual = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
    if actual != path:
        raise ValueError(f"path must be the Git worktree root: {path} (root: {actual})")
    return path


def _commit(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


def _clean(root: Path) -> bool:
    return not _git(root, "status", "--porcelain=v1", "--untracked-files=all")


def _merged(center: Path, commit: str, main_ref: str) -> tuple[bool, str]:
    try:
        main = _commit(center, main_ref)
    except ValueError as exc:
        raise ValueError(f"main ref cannot be resolved in center: {main_ref}") from exc
    result = subprocess.run(["git", "merge-base", "--is-ancestor", commit, main], cwd=center, capture_output=True)
    if result.returncode not in (0, 1):
        raise ValueError("candidate commit is unavailable in center Git objects")
    return result.returncode == 0, main


def _skills(source: Path, bundle) -> list[dict[str, object]]:
    # Use exactly the installer profile and release-lock package digest rules.
    for _name, relative in bundle:
        if (source / relative).is_symlink():
            raise ValueError(f"Skill package root is a symlink: {relative}")
    return release._skill_records(source, {"skills": [{"name": name, "path": path} for name, path in bundle]})


def _active(home: Path) -> tuple[dict[str, Any], Any, list[str]]:
    report: dict[str, Any] = {"state_path": str(installer.state_path(home)), "verified": False}
    blockers: list[str] = []
    state = installer.load_state(home)
    recorded = installer.decode_recorded_install(state)
    report.update(installer.workflow_json_summary(state, installed=True))
    source = _root(recorded.source_root)
    actual_commit = _commit(source, "HEAD")
    source_clean = _clean(source)
    report["actual_source_commit"] = actual_commit
    report["source_clean"] = source_clean
    if not installer.FULL_COMMIT.fullmatch(recorded.source_commit):
        blockers.append("active source commit is not a full commit")
    if actual_commit != recorded.source_commit or actual_commit != recorded.resolved_commit:
        blockers.append("active source commit does not match installer state")
    if not source_clean:
        blockers.append("active source worktree is dirty")
    runtime = installer.inspect_runtime_installation(state)
    report["runtime_inspection"] = runtime
    if runtime.get("matches_installed_state") is not True:
        blockers.append("active Runtime does not match installer digest, manifest, launcher, or composition")
    if recorded.runtime.get("source_commit") != actual_commit:
        blockers.append("active Runtime source commit does not match installer source")
    skill_records = _skills(source, recorded.profile.bundle)
    report["skills"] = skill_records
    if recorded.release or recorded.source_mode == "managed":
        identity = installer.release_identity(source, source_mode=recorded.source_mode, dry_run=False)
        report["actual_release"] = identity
        if identity != recorded.release or identity.get("validation_error"):
            blockers.append("active release identity does not match installer state")
    # Installer-owned links are checked against their recorded targets; no
    # parallel activation registry is maintained by this helper.
    for link, target in recorded.links.items():
        path = Path(link)
        if not path.is_symlink() or path.resolve() != Path(target).resolve():
            blockers.append(f"active Skill link does not match installer state: {link}")
    report["verified"] = not blockers
    return report, recorded, blockers


def check_promotion(*, center: Path, candidate: Path, candidate_commit: str = "HEAD", main_ref: str = "main", installer_home: Path | None = None) -> dict[str, Any]:
    center = _root(center)
    candidate = _root(candidate)
    home = (installer_home or Path.home()).expanduser().resolve()
    blockers: list[str] = []
    head = _commit(candidate, "HEAD")
    selected = _commit(candidate, candidate_commit)
    if head != selected:
        blockers.append("candidate HEAD does not equal requested commit")
    if _git(candidate, "config", "--type=bool", "--default=false", "--get", "core.sparseCheckout") == "true":
        blockers.append("candidate must not use sparse checkout")
    if any(line[:1].islower() or line.startswith("S ") for line in _git(candidate, "ls-files", "-v").splitlines()):
        blockers.append("candidate index must not hide worktree changes")
    clean = _clean(candidate)
    if not clean:
        blockers.append("candidate worktree is dirty")
    try:
        center_commit = _commit(center, selected)
        source_matches = center_commit == selected and _git(center, "rev-parse", f"{selected}^{{tree}}") == _git(candidate, "rev-parse", f"{head}^{{tree}}")
    except ValueError:
        source_matches = False
    if not source_matches:
        blockers.append("candidate commit/tree is not available from center Git objects")
    try:
        merged, main = _merged(center, selected, main_ref)
        if not merged:
            blockers.append(f"candidate commit is not merged into center {main_ref}")
    except ValueError as exc:
        merged, main = False, ""
        blockers.append(str(exc))
    active: dict[str, Any] = {"state_path": str(installer.state_path(home)), "verified": False}
    recorded = None
    try:
        active, recorded, active_blockers = _active(home)
        blockers.extend(active_blockers)
    except (OSError, ValueError, installer.SetupError) as exc:
        blockers.append(f"active installer identity cannot be verified: {exc}")
    skill_records: list[dict[str, object]] = []
    candidate_release: dict[str, object] = {}
    runtime_digest = ""
    composition_digest = ""
    if recorded is not None:
        try:
            installer.validate_source(candidate, recorded.profile.bundle)
            skill_records = _skills(candidate, recorded.profile.bundle)
            runtime_plan = installer.build_runtime_plan(home, candidate, source_commit=head)
            runtime_digest = runtime_plan["content_digest"]
            composition_digest = release._fingerprint(runtime_plan["composition_files"])
            # A standalone Skill center may have no workflow release-lock.
            # If a lock is present, its existing verifier remains mandatory.
            if (candidate / "release-lock.json").exists():
                candidate_release = installer.release_identity(candidate, source_mode="managed", dry_run=False)
        except (OSError, ValueError, installer.SetupError) as exc:
            blockers.append(f"candidate identity cannot be verified: {exc}")
    if not skill_records or not runtime_digest:
        blockers.append("candidate Skill and Runtime digests are incomplete")
    command = [sys.executable, str(ROOT / "openubmc-environment-setup/scripts/install_environment.py"), "install", "--source", str(candidate), "--source-mode", "linked", "--home", str(home), "--non-interactive", "--json"]
    if recorded is not None:
        command.extend(["--skill-profile", recorded.profile.name, "--clients", "codex"])
        command.extend(["--preserve-skills", ",".join(recorded.preserved_skills) or "none"])
    return {
        "schema": SCHEMA,
        "center": {"path": str(center), "head": _commit(center, "HEAD"), "clean": _clean(center)},
        "candidate": {"path": str(candidate), "commit": selected, "head": head, "tree": _git(candidate, "rev-parse", f"{head}^{{tree}}"), "clean": clean, "source_matches_center": source_matches, "merged_into": main_ref, "resolved_main_commit": main, "merged": merged, "skills": skill_records, "runtime_digest": runtime_digest, "composition_digest": composition_digest, "release": candidate_release},
        "active_install": active,
        "decision": "ready" if not blockers else "blocked",
        "blockers": list(dict.fromkeys(blockers)),
        "installer_proposal": {"executed": False, "argv": command, "shell": shlex.join(command), "clients": ["codex"]},
    }


def prepare_candidate(*, center: Path, candidate: Path, commit: str = "HEAD", main_ref: str = "main") -> dict[str, Any]:
    center = _root(center)
    candidate = candidate.expanduser().resolve()
    if candidate.exists() or candidate.is_relative_to(center):
        raise ValueError("candidate must be a new directory outside center")
    selected = _commit(center, commit)
    merged, main = _merged(center, selected, main_ref)
    if not merged:
        raise ValueError(f"candidate commit is not merged into center {main_ref}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    # Copy Git objects into the candidate repository; do not register a worktree
    # in the Windows center or copy any dirty worktree files.
    subprocess.run(["git", "clone", "--no-local", "--no-checkout", "--", str(center), str(candidate)], check=True, capture_output=True)
    _git(candidate, "checkout", "--detach", selected)
    return {"schema": SCHEMA, "prepared": True, "activated": False, "candidate": str(candidate), "commit": selected, "resolved_main_commit": main}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check"))
    parser.add_argument("--center", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--main-ref", default="main")
    parser.add_argument("--installer-home", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            report = prepare_candidate(center=args.center, candidate=args.candidate, commit=args.commit, main_ref=args.main_ref)
        else:
            report = check_promotion(center=args.center, candidate=args.candidate, candidate_commit=args.commit, main_ref=args.main_ref, installer_home=args.installer_home)
    except (OSError, ValueError, installer.SetupError, subprocess.CalledProcessError) as exc:
        report = {"schema": SCHEMA, "decision": "blocked", "blockers": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("prepared") or report.get("decision") == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
