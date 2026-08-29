"""Shared schema and validation for Release Gate evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import sys

from scripts.evidence_report import evidence_fingerprint


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime.release import is_full_commit  # noqa: E402


LEGACY_RELEASE_GATE_SCHEMA = "openubmc-agent-workflow.release-gate.v2"
RELEASE_GATE_SCHEMA = "openubmc-agent-workflow.release-gate.v3"
REQUIRED_RELEASE_GATES = (
    "github_ci",
    "clean_install",
    "upgrade",
    "rollback",
    "agent_interface",
    "source_only",
    "live_patch",
    "build_upgrade",
    "replay_smoke",
    "old_schema_compatibility",
    "domain_pack_conformance",
    "runtime_safety_qualification",
    "agent_gateway_ab_evidence",
)
RETIREMENT_RELEASE_ARTIFACTS = (
    "runtime_qualification",
    "github_ci",
    "agent_gateway_ab",
)

def _verify_artifact(name: str, value: object) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"Release Gate lacks {name} evidence")
    path = str(value.get("path", "")).strip()
    sha256 = str(value.get("sha256", "")).strip().lower()
    size_bytes = value.get("size_bytes")
    if (
        not path
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
    ):
        raise ValueError(f"Release Gate {name} evidence is incomplete")


def _verify_command(name: str, value: object, *, passed: bool) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"Release Gate {name} command evidence is invalid")
    argv = value.get("argv")
    returncode = value.get("returncode")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(argument, str) for argument in argv)
        or isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or (passed and returncode != 0)
        or not isinstance(value.get("stdout_tail"), str)
        or not isinstance(value.get("stderr_tail"), str)
    ):
        raise ValueError(f"Release Gate {name} command evidence is invalid")


def verify_release_gate_report(
    report: Mapping[str, object],
    *,
    expected_source_commit: str | None = None,
    require_promotable: bool = False,
    required_gates: Sequence[str] = REQUIRED_RELEASE_GATES,
    required_artifacts: Sequence[str] = (),
) -> None:
    schema = report.get("schema")
    if schema not in {LEGACY_RELEASE_GATE_SCHEMA, RELEASE_GATE_SCHEMA}:
        raise ValueError("Release Gate schema is unsupported")
    expected_digest = report.get("evidence_digest")
    unsigned = dict(report)
    unsigned.pop("evidence_digest", None)
    if expected_digest != evidence_fingerprint(unsigned):
        raise ValueError("Release Gate evidence digest is invalid")
    if (
        expected_source_commit is not None
        and str(report.get("source_commit", "")).lower()
        != expected_source_commit.lower()
    ):
        raise ValueError("Release Gate source commit does not match retirement evidence")
    if require_promotable and report.get("promotable") is not True:
        raise ValueError("Release Gate is not promotable")
    current_ref = str(report.get("current_ref", "")).strip()
    previous_ref = str(report.get("previous_ref", "")).strip()
    if not current_ref or not previous_ref or current_ref == previous_ref:
        raise ValueError("Release Gate refs are incomplete")
    requested_ref = str(report.get("requested_ref", "")).strip()
    release_commit = str(report.get("release_commit", "")).lower()
    source_commit = str(report.get("source_commit", "")).lower()
    if schema == RELEASE_GATE_SCHEMA:
        if (
            requested_ref != current_ref
            or not is_full_commit(release_commit)
            or not is_full_commit(source_commit)
            or release_commit == source_commit
        ):
            raise ValueError("Release Gate candidate identity is invalid")
    environment = report.get("environment")
    if not isinstance(environment, Mapping) or not environment:
        raise ValueError("Release Gate lacks complete environment evidence")
    if report.get("environment_fingerprint") != evidence_fingerprint(environment):
        raise ValueError("Release Gate environment fingerprint is invalid")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("Release Gate artifacts are unavailable")
    for artifact_name, artifact in artifacts.items():
        _verify_artifact(str(artifact_name), artifact)
    for artifact_name in required_artifacts:
        if artifact_name not in artifacts:
            raise ValueError(f"Release Gate lacks {artifact_name} evidence")
    raw_gates = report.get("gates")
    if not isinstance(raw_gates, list):
        raise ValueError("Release Gate results are unavailable")
    gates: dict[str, str] = {}
    for item in raw_gates:
        if not isinstance(item, Mapping):
            raise ValueError("Release Gate result must be an object")
        name = str(item.get("name", ""))
        if not name or name in gates:
            raise ValueError("Release Gate result names must be unique")
        status = str(item.get("status", ""))
        elapsed_seconds = item.get("elapsed_seconds")
        commands = item.get("commands")
        if (
            isinstance(elapsed_seconds, bool)
            or not isinstance(elapsed_seconds, (int, float))
            or elapsed_seconds < 0
            or not isinstance(commands, list)
        ):
            raise ValueError(f"Release Gate {name} command evidence is incomplete")
        if status == "skipped":
            if commands:
                raise ValueError(f"Release Gate {name} skipped evidence is invalid")
        else:
            if not commands:
                raise ValueError(f"Release Gate {name} command evidence is incomplete")
            for command in commands:
                _verify_command(name, command, passed=status == "passed")
        gates[name] = status
    missing = [name for name in required_gates if name not in gates]
    if missing:
        raise ValueError("Release Gate is incomplete: " + ", ".join(missing))
    if require_promotable:
        failed = [name for name, status in gates.items() if status != "passed"]
        if failed:
            raise ValueError("Release Gate contains non-passing results")
