#!/usr/bin/env python3
"""Qualify Runtime safety invariants with hermetic behavioral tests."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
SCHEMA = "openubmc-agent-workflow.runtime-qualification.v2"

CONCURRENCY_TESTS = (
    "tests.test_agent_gateway.AgentGatewayTests.test_concurrent_read_only_settlement_commits_one_terminal_decision",
    "tests.test_agent_gateway.AgentGatewayTests.test_recovery_boundary_converges_after_a_concurrent_run_revision",
)

QUALIFICATIONS = (
    (
        "duplicate_dangerous_effects",
        (
            "tests.test_agent_gateway.AgentGatewayTests.test_effect_identity_is_unique_across_concurrent_runs",
            "tests.test_agent_gateway.AgentGatewayTests.test_stale_waiter_cannot_settle_as_a_new_evidence_retry_generation",
            "tests.test_agent_gateway.AgentGatewayTests.test_live_patch_submission_replays_after_internal_effect_decisions",
            "tests.test_agent_gateway.AgentGatewayTests.test_sqlite_restart_reconciles_a_persisted_mutation_without_reapply",
            "tests.test_mutation_recovery.MutationRecoveryTests.test_sigkill_crash_cuts_preserve_identity_and_never_repeat_the_mutation",
        ),
    ),
    (
        "false_successes",
        (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_failed_phase_never_produces_a_success_outcome",
            "tests.test_agent_gateway.AgentGatewayTests.test_incomplete_live_patch_acceptance_cannot_report_completed_success",
            "tests.test_agent_gateway.AgentGatewayTests.test_read_only_effect_retries_same_identity_after_evidence_failure",
        ),
    ),
    (
        "wrong_target_or_artifact_mutations",
        (
            "tests.test_agent_gateway.AgentGatewayTests.test_observation_ref_rejects_digest_tamper_and_target_mismatch",
            "tests.test_agent_gateway.AgentGatewayTests.test_artifact_ref_is_bound_to_content_kind_target_run_and_provenance",
            "tests.test_agent_gateway.AgentGatewayTests.test_live_patch_artifact_is_revalidated_immediately_before_effect_dispatch",
        ),
    ),
    (
        "unknown_new_identity_retries",
        (
            "tests.test_agent_gateway.AgentGatewayTests.test_automatic_reconcile_returns_running_at_the_caller_deadline",
            "tests.test_agent_gateway.AgentGatewayTests.test_explicit_reconcile_returns_running_at_the_caller_deadline",
            "tests.test_agent_gateway.AgentGatewayTests.test_live_patch_unknown_mutation_reconciles_after_process_restart",
            "tests.test_domain_pack_conformance.DomainPackConformanceTests.test_mutation_pack_never_retries_an_unknown_result",
        ),
    ),
    ("runtime_concurrency", CONCURRENCY_TESTS),
)

PARTIAL_RESULT_TESTS = (
    "tests.test_agent_gateway.AgentGatewayTests.test_auto_assurance_transport_failure_preserves_the_fast_observation",
)

LIVE_PATCH_CRASH_TESTS = (
    "tests.test_runtime_backend.LivePatchRuntimeBackendTests."
    "test_sigkill_at_real_backend_cuts_restarts_without_repeating_dangerous_steps",
)

def _tail(value: str, *, limit: int = 4000) -> str:
    text = value.strip()
    return text[-limit:] if len(text) > limit else text


def run_process(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _test_command(tests: Sequence[str]) -> tuple[str, ...]:
    return (sys.executable, "-m", "unittest", *tests)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _source_commit(workspace: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    value = completed.stdout.strip().lower()
    return value if completed.returncode == 0 and len(value) == 40 else "unknown"


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
    }


def qualify_runtime(
    workspace: Path,
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = run_process,
    source_commit: str = "",
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    runtime_root = workspace / "openubmc-target-runtime"
    results: list[dict[str, object]] = []
    violations: dict[str, int] = {}
    for violation, tests in QUALIFICATIONS:
        command = _test_command(tests)
        started = time.monotonic()
        completed = executor(command, cwd=runtime_root)
        passed = completed.returncode == 0
        violations[violation] = 0 if passed else 1
        results.append(
            {
                "name": violation,
                "status": "passed" if passed else "failed",
                "tests": list(tests),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "returncode": completed.returncode,
                "stdout_tail": _tail(completed.stdout or ""),
                "stderr_tail": _tail(completed.stderr or ""),
            }
        )

    live_patch_command = _test_command(LIVE_PATCH_CRASH_TESTS)
    started = time.monotonic()
    live_patch = executor(
        live_patch_command,
        cwd=workspace / "openubmc-live-patch",
    )
    live_patch_passed = live_patch.returncode == 0
    violations["real_backend_crash_cuts"] = 0 if live_patch_passed else 1
    results.append(
        {
            "name": "real_backend_crash_cuts",
            "status": "passed" if live_patch_passed else "failed",
            "tests": list(LIVE_PATCH_CRASH_TESTS),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "returncode": live_patch.returncode,
            "stdout_tail": _tail(live_patch.stdout or ""),
            "stderr_tail": _tail(live_patch.stderr or ""),
        }
    )

    stability_command = (
        sys.executable,
        str(workspace / "scripts" / "runtime_stability.py"),
        "--workspace",
        str(workspace),
        "--source-commit",
        source_commit or _source_commit(workspace),
    )
    started = time.monotonic()
    stability = executor(stability_command, cwd=workspace)
    raw_stability_report: Mapping[str, object] | None = None
    try:
        decoded = json.loads(stability.stdout or "")
        if isinstance(decoded, Mapping):
            raw_stability_report = decoded
    except json.JSONDecodeError:
        pass
    stability_passed = (
        stability.returncode == 0
        and raw_stability_report is not None
        and bool(raw_stability_report.get("promotable", False))
    )
    violations["runtime_stability"] = 0 if stability_passed else 1
    results.append(
        {
            "name": "runtime_stability",
            "status": "passed" if stability_passed else "failed",
            "command": list(stability_command),
            "concurrency_tests": list(CONCURRENCY_TESTS),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "returncode": stability.returncode,
            "report": dict(raw_stability_report or {}),
            "stdout_tail": _tail(stability.stdout or ""),
            "stderr_tail": _tail(stability.stderr or ""),
        }
    )

    partial_command = _test_command(PARTIAL_RESULT_TESTS)
    started = time.monotonic()
    partial = executor(partial_command, cwd=runtime_root)
    partial_accepted = partial.returncode == 0
    results.append(
        {
            "name": "ordinary_partial_result",
            "status": "passed" if partial_accepted else "failed",
            "tests": list(PARTIAL_RESULT_TESTS),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "returncode": partial.returncode,
            "stdout_tail": _tail(partial.stdout or ""),
            "stderr_tail": _tail(partial.stderr or ""),
        }
    )
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": source_commit or _source_commit(workspace),
        "environment": dict(sorted((environment or _environment()).items())),
        "parameters": {
            "stability_profile": "ci",
            "qualification_groups": [name for name, _tests in QUALIFICATIONS],
            "concurrency_tests": list(CONCURRENCY_TESTS),
            "stability_runner": "scripts/runtime_stability.py",
            "ordinary_partial_result_tests": list(PARTIAL_RESULT_TESTS),
            "real_backend_crash_tests": list(LIVE_PATCH_CRASH_TESTS),
        },
        "promotable": not any(violations.values()) and partial_accepted,
        "violations": violations,
        "ordinary_partial_result_accepted": partial_accepted,
        "qualifications": results,
    }
    report["evidence_digest"] = _fingerprint(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = qualify_runtime(args.workspace.expanduser().absolute())
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
