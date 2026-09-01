#!/usr/bin/env python3
"""Run the mandatory install, lifecycle, and replay gates for a release."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "openubmc-target-runtime"))

from scripts.release_gate_contract import (  # noqa: E402
    RELEASE_GATE_SCHEMA,
    evidence_fingerprint,
    verify_release_gate_report,
)
from scripts.codex_adoption_contract import (  # noqa: E402
    verify_codex_adoption_report,
)
from scripts.formal_identity import (  # noqa: E402
    identity_argument,
    normalize_codex_identity,
    normalize_model_identity,
)

from openubmc_target_runtime.release import (  # noqa: E402
    ReleaseLockError,
    is_full_commit,
    verify_release_lock,
)


def _tail(value: str, *, limit: int = 4000) -> str:
    text = value.strip()
    return text[-limit:] if len(text) > limit else text


def run_process(command: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _install_command(ref: str, home: Path) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "bootstrap.py"),
        "--ref",
        ref,
        "--home",
        str(home),
        "--clients",
        "codex",
        "--skill-profile",
        "target-runtime",
        "--skip-credentials",
        "--skip-tool-install",
        "--non-interactive",
    ]


def _installed_installer(home: Path) -> Path:
    return (
        home
        / ".local"
        / "share"
        / "openubmc"
        / "skills"
        / "openubmc-environment-setup"
        / "scripts"
        / "install_environment.py"
    )


def _test_discovery(pattern: str, *, name_filter: str = "") -> tuple[str, ...]:
    command = (
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "openubmc-target-runtime/tests",
        "-p",
        pattern,
    )
    return (*command, "-k", name_filter) if name_filter else command


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


def _artifact(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def _load_codex_adoption_evidence(
    path: Path,
    *,
    expected_source_commit: str,
    expected_model_identity: Mapping[str, object],
    expected_codex_identity: Mapping[str, object],
) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Codex Adoption Qualification evidence is unavailable: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ValueError("Codex Adoption Qualification evidence must be an object")
    verify_codex_adoption_report(
        document,
        expected_source_commit=expected_source_commit,
        require_ready=True,
    )
    provenance = document.get("provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("Codex Adoption Qualification provenance is unavailable")
    if provenance.get("model") != dict(expected_model_identity):
        raise ValueError("Codex Adoption Qualification model identity mismatch")
    if provenance.get("codex") != dict(expected_codex_identity):
        raise ValueError("Codex Adoption Qualification Codex identity mismatch")
    return document


def _resolve_commit(workspace: Path, ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}"],
        cwd=workspace,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"unable to resolve release candidate ref {ref}{suffix}")
    return completed.stdout.strip().lower()


class ReleaseCandidate(NamedTuple):
    requested_ref: str
    release_commit: str
    source_commit: str
    release_version: str = ""


class ReleaseVersion(NamedTuple):
    major: int
    minor: int
    patch: int


_RELEASE_VERSION = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
)


def _parsed_release_version(value: str, *, label: str) -> ReleaseVersion:
    matched = _RELEASE_VERSION.fullmatch(value)
    if matched is None:
        raise ValueError(f"{label} must be a strict MAJOR.MINOR.PATCH version")
    return ReleaseVersion(
        *(int(matched.group(name)) for name in ("major", "minor", "patch"))
    )


def _parsed_release_tag(value: str, *, label: str) -> ReleaseVersion:
    if not value.startswith("v"):
        raise ValueError(f"{label} must be a strict vMAJOR.MINOR.PATCH tag")
    try:
        return _parsed_release_version(value[1:], label=label)
    except ValueError as exc:
        raise ValueError(
            f"{label} must be a strict vMAJOR.MINOR.PATCH tag"
        ) from exc


def require_latest_published_release(
    previous_ref: str,
    github_repository: str,
) -> None:
    """Require the baseline to be the repository's latest non-draft release."""

    repository = github_repository.strip()
    if not repository:
        raise ValueError("GitHub repository is required for previous release lookup")
    completed = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repository}/releases/latest",
            "--jq",
            ".tag_name",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        suffix = f": {_tail(detail)}" if detail else ""
        raise ValueError(
            "unable to determine the latest published release "
            f"from GitHub repository {github_repository}{suffix}"
        )
    latest_tag = completed.stdout.strip()
    if latest_tag != previous_ref:
        raise ValueError(
            "previous release ref must be the latest published release "
            f"({latest_tag or 'unknown'})"
        )


def require_published_candidate(
    release_commit: str,
    github_repository: str,
) -> None:
    """Fail fast when GitHub cannot serve the immutable candidate commit."""

    repository = github_repository.strip()
    if not repository:
        raise ValueError("GitHub repository is required for candidate reachability")
    try:
        completed = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/commits/{release_commit}",
                "--silent",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ValueError(
            f"unable to verify release candidate reachability on GitHub: {exc}"
        ) from exc
    if completed.returncode == 0:
        return
    detail = completed.stderr.strip() or completed.stdout.strip()
    lowered_detail = detail.lower()
    if (
        "404" in detail
        or "422" in detail
        or "not found" in lowered_detail
        or "no commit found" in lowered_detail
    ):
        raise ValueError(
            f"release candidate {release_commit} is not published or reachable "
            f"from GitHub repository {github_repository}; push the lock-only "
            "commit or tag before running Release Gate"
        )
    suffix = f": {_tail(detail)}" if detail else ""
    raise ValueError(
        f"unable to verify release candidate reachability on GitHub{suffix}"
    )


def _resolve_release_candidate(workspace: Path, ref: str) -> ReleaseCandidate:
    """Resolve one requested ref to the immutable lock-only candidate identity."""

    release_commit = _resolve_commit(workspace, ref)
    workspace_commit = _resolve_commit(workspace, "HEAD")
    if release_commit != workspace_commit:
        raise ValueError("release gate workspace HEAD must match --current-ref")
    try:
        identity = verify_release_lock(workspace)
    except ReleaseLockError as exc:
        raise ValueError(f"invalid immutable release ref: {exc}") from exc
    source_commit = str(identity.get("source_commit", "")).strip().lower()
    release_version = str(identity.get("release_version", "")).strip()
    if not is_full_commit(source_commit):
        raise ValueError("immutable release ref records an invalid source_commit")
    if source_commit == release_commit:
        raise ValueError(
            "immutable release ref must be a lock-only child of source_commit"
        )
    return ReleaseCandidate(
        requested_ref=ref,
        release_commit=release_commit,
        source_commit=source_commit,
        release_version=release_version,
    )


def gate_commands(
    *,
    current_ref: str,
    previous_ref: str,
    clean_home: Path,
    lifecycle_home: Path,
    ab_evidence: Path | None = None,
    source_commit: str = "",
    model_identity: Mapping[str, object],
    codex_identity: Mapping[str, object],
    github_repository: str = "lihuabai629-star/openubmc-agent-workflow",
    ab_attestation_public_key: Path | None = None,
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    clean_install = tuple(_install_command(current_ref, clean_home))
    previous_install = tuple(_install_command(previous_ref, lifecycle_home))
    current_upgrade = tuple(_install_command(current_ref, lifecycle_home))
    installer = str(_installed_installer(lifecycle_home))
    qualification_output = lifecycle_home.parent / "runtime-qualification.json"
    adoption_output = lifecycle_home.parent / "codex-adoption-qualification.json"
    selected_ab_evidence = (
        ab_evidence
        if ab_evidence is not None
        else lifecycle_home.parent / "agent-gateway-ab-summary.json"
    )
    selected_attestation_public_key = (
        ab_attestation_public_key
        if ab_attestation_public_key is not None
        else lifecycle_home.parent / "agent-gateway-ab-attestation.pub"
    )
    return (
        (
            "github_ci",
            (
                (
                    sys.executable,
                    str(ROOT / "scripts" / "github_ci_evidence.py"),
                    "--repository",
                    github_repository,
                    "--commit",
                    source_commit,
                    "--output",
                    str(lifecycle_home.parent / "github-ci-evidence.json"),
                ),
            ),
        ),
        ("clean_install", (clean_install,)),
        ("upgrade", (previous_install, current_upgrade)),
        (
            "rollback",
            (
                (
                    sys.executable,
                    installer,
                    "rollback",
                    "--home",
                    str(lifecycle_home),
                    "--non-interactive",
                    "--skip-tool-install",
                ),
            ),
        ),
        (
            "agent_interface",
            ((_test_discovery("test_agent_gateway.py")),),
        ),
        (
            "source_only",
            ((_test_discovery("test_agent_gateway.py", name_filter="source_only")),),
        ),
        (
            "live_patch",
            ((_test_discovery("test_agent_gateway.py", name_filter="live_patch")),),
        ),
        (
            "build_upgrade",
            ((_test_discovery("test_agent_gateway.py", name_filter="build_upgrade")),),
        ),
        (
            "replay_smoke",
            (
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "openubmc-target-runtime/tests",
                    "-p",
                    "test_case_replay.py",
                ),
            ),
        ),
        (
            "old_schema_compatibility",
            ((_test_discovery("test_run_store.py")),),
        ),
        (
            "domain_pack_conformance",
            ((_test_discovery("test_domain_pack_conformance.py")),),
        ),
        (
            "runtime_safety_qualification",
            (
                (
                    sys.executable,
                    str(ROOT / "scripts" / "runtime_qualification.py"),
                    "--workspace",
                    str(ROOT),
                    "--output",
                    str(qualification_output),
                    "--source-commit",
                    source_commit,
                ),
            ),
        ),
        (
            "codex_adoption_qualification",
            (
                (
                    sys.executable,
                    str(ROOT / "scripts" / "codex_adoption_qualification.py"),
                    "--source-commit",
                    source_commit,
                    "--model-identity",
                    json.dumps(
                        dict(model_identity), ensure_ascii=True, sort_keys=True
                    ),
                    "--codex-identity",
                    json.dumps(
                        dict(codex_identity), ensure_ascii=True, sort_keys=True
                    ),
                    "--output",
                    str(adoption_output),
                ),
            ),
        ),
        (
            "agent_gateway_ab_evidence",
            (
                (
                    sys.executable,
                    str(ROOT / "scripts" / "agent_gateway_ab.py"),
                    "verify",
                    str(selected_ab_evidence),
                    "--source-ref",
                    source_commit,
                    "--repo",
                    str(ROOT),
                    "--attestation-public-key",
                    str(selected_attestation_public_key),
                ),
            ),
        ),
    )


def execute_release_gate(
    *,
    current_ref: str,
    previous_ref: str,
    workspace: Path,
    work_root: Path,
    model_identity: Mapping[str, object],
    codex_identity: Mapping[str, object],
    executor: Callable[..., subprocess.CompletedProcess[str]] = run_process,
    ab_evidence: Path | None = None,
    github_repository: str = "lihuabai629-star/openubmc-agent-workflow",
    ab_attestation_public_key: Path | None = None,
    release_tag: str | None = None,
) -> dict[str, object]:
    selected_model_identity = normalize_model_identity(model_identity)
    selected_codex_identity = normalize_codex_identity(codex_identity)
    clean_home = work_root / "clean-install-home"
    lifecycle_home = work_root / "lifecycle-home"
    results: list[dict[str, object]] = []
    blocked = False
    adoption_document: dict[str, object] | None = None
    candidate = _resolve_release_candidate(workspace, current_ref)
    resolved_source_commit = candidate.source_commit
    resolved_release_commit = candidate.release_commit
    current_version = _parsed_release_version(
        candidate.release_version,
        label="release lock version",
    )
    previous_version = _parsed_release_tag(
        previous_ref,
        label="previous release ref",
    )
    selected_release_tag = str(release_tag or "").strip()
    if (
        not selected_release_tag
        and candidate.requested_ref != "HEAD"
        and not is_full_commit(candidate.requested_ref)
    ):
        selected_release_tag = candidate.requested_ref
    if selected_release_tag:
        requested_version = _parsed_release_tag(
            selected_release_tag,
            label="current release tag",
        )
        if requested_version != current_version:
            raise ValueError(
                "current release tag does not match release-lock.json version"
            )
    if current_version <= previous_version:
        raise ValueError("current release version must be newer than previous release")
    if (
        current_version.major != previous_version.major
        or current_version.minor != previous_version.minor
    ):
        raise ValueError(
            "maintenance release must use the same major and minor as previous release"
        )
    require_latest_published_release(previous_ref, github_repository)
    require_published_candidate(resolved_release_commit, github_repository)
    for name, commands in gate_commands(
        current_ref=resolved_release_commit,
        previous_ref=previous_ref,
        clean_home=clean_home,
        lifecycle_home=lifecycle_home,
        ab_evidence=ab_evidence,
        source_commit=resolved_source_commit,
        model_identity=selected_model_identity,
        codex_identity=selected_codex_identity,
        github_repository=github_repository,
        ab_attestation_public_key=ab_attestation_public_key,
    ):
        if blocked:
            results.append(
                {
                    "name": name,
                    "status": "skipped",
                    "elapsed_seconds": 0.0,
                    "commands": [],
                }
            )
            continue
        command_results: list[dict[str, object]] = []
        started = time.monotonic()
        for command in commands:
            completed = executor(command, cwd=workspace)
            command_results.append(
                {
                    "argv": list(command),
                    "returncode": completed.returncode,
                    "stdout_tail": _tail(completed.stdout or ""),
                    "stderr_tail": _tail(completed.stderr or ""),
                }
            )
            if completed.returncode:
                blocked = True
                break
        if not blocked and name == "codex_adoption_qualification":
            try:
                adoption_document = _load_codex_adoption_evidence(
                    work_root / "codex-adoption-qualification.json",
                    expected_source_commit=resolved_source_commit,
                    expected_model_identity=selected_model_identity,
                    expected_codex_identity=selected_codex_identity,
                )
            except ValueError as error:
                blocked = True
                if command_results:
                    command_results[-1]["stderr_tail"] = _tail(str(error))
        results.append(
            {
                "name": name,
                "status": "passed" if not blocked else "failed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "commands": command_results,
            }
        )
    promotable = all(item["status"] == "passed" for item in results)
    environment = _environment()
    qualification_path = work_root / "runtime-qualification.json"
    artifacts = {}
    qualification_artifact = _artifact(qualification_path)
    if qualification_artifact is not None:
        artifacts["runtime_qualification"] = qualification_artifact
    adoption_artifact = _artifact(work_root / "codex-adoption-qualification.json")
    if adoption_artifact is not None and adoption_document is not None:
        adoption_artifact.update(
            {
                "schema": adoption_document["schema"],
                "source_commit": adoption_document["source_commit"],
                "evidence_digest": adoption_document["evidence_digest"],
                "qualified": adoption_document["qualified"],
                "maintenance_checkpoint_ready": adoption_document[
                    "maintenance_checkpoint_ready"
                ],
            }
        )
        artifacts["codex_adoption_qualification"] = adoption_artifact
    github_ci_artifact = _artifact(work_root / "github-ci-evidence.json")
    if github_ci_artifact is not None:
        artifacts["github_ci"] = github_ci_artifact
    if ab_evidence is not None:
        ab_artifact = _artifact(ab_evidence)
        if ab_artifact is not None:
            artifacts["agent_gateway_ab"] = ab_artifact
    report = {
        "schema": RELEASE_GATE_SCHEMA,
        "current_ref": current_ref,
        "requested_ref": candidate.requested_ref,
        "release_tag": selected_release_tag,
        "release_commit": candidate.release_commit,
        "previous_ref": previous_ref,
        "source_commit": resolved_source_commit,
        "formal_identity": {
            "model": selected_model_identity,
            "codex": selected_codex_identity,
        },
        "environment": environment,
        "environment_fingerprint": evidence_fingerprint(environment),
        "promotable": promotable,
        "gates": results,
        "artifacts": artifacts,
    }
    report["evidence_digest"] = evidence_fingerprint(report)
    verify_release_gate_report(
        report,
        required_artifacts=("codex_adoption_qualification",)
        if promotable
        else (),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-ref", required=True)
    parser.add_argument("--previous-ref", required=True)
    parser.add_argument(
        "--model-identity",
        type=identity_argument,
        required=True,
    )
    parser.add_argument(
        "--codex-identity",
        type=identity_argument,
        required=True,
    )
    parser.add_argument("--release-tag")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ab-evidence", type=Path, required=True)
    parser.add_argument(
        "--ab-attestation-public-key",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--github-repository",
        default="lihuabai629-star/openubmc-agent-workflow",
    )
    args = parser.parse_args(argv)

    if args.current_ref == args.previous_ref:
        parser.error("--current-ref and --previous-ref must differ")
    if args.work_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="openubmc-release-gate-")
        work_root = Path(temporary.name)
    else:
        temporary = None
        work_root = args.work_root.expanduser().absolute()
        work_root.mkdir(parents=True, exist_ok=True)
    try:
        try:
            report = execute_release_gate(
                current_ref=args.current_ref,
                previous_ref=args.previous_ref,
                workspace=args.workspace.expanduser().absolute(),
                work_root=work_root,
                model_identity=args.model_identity,
                codex_identity=args.codex_identity,
                ab_evidence=args.ab_evidence.expanduser().absolute(),
                github_repository=args.github_repository,
                release_tag=args.release_tag,
                ab_attestation_public_key=(
                    args.ab_attestation_public_key.expanduser().absolute()
                ),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            output = args.output.expanduser().absolute()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0 if report["promotable"] else 1
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
