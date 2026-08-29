#!/usr/bin/env python3
"""Qualify Runtime safety invariants with hermetic behavioral tests."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import json
import platform
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))
sys.path.insert(0, str(ROOT))

from openubmc_target_runtime.agent_gateway import agent_projection_policy  # noqa: E402
from openubmc_target_runtime.run_store import persisted_run_support  # noqa: E402

from scripts.evidence_report import (  # noqa: E402
    evidence_fingerprint,
    source_commit as selected_source_commit,
)
from scripts.runtime_stability_contract import (  # noqa: E402
    verify_runtime_stability_report,
)

SCHEMA = "openubmc-agent-workflow.runtime-qualification.v2"

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
    (
        "semantic_projection_completion",
        (
            "tests.test_agent_gateway.AgentGatewayTests.test_observe_projection_target_preserves_complete_source_semantics",
            "tests.test_agent_gateway.AgentGatewayTests.test_observe_projection_target_does_not_rewrite_oversized_target_metadata",
            "tests.test_agent_gateway.AgentGatewayTests.test_observe_soft_target_survives_maximum_legal_scope_and_large_result",
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_query_is_partitioned_without_becoming_a_blocker",
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_assurance_cannot_mask_fast_target_epoch_drift",
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_keeps_selected_capability_partition_fact",
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_does_not_replace_a_missing_selected_capability_fact",
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_does_not_share_a_capability_fact_between_selectors",
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_rejects_missing_partition_timing",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_keeps_complete_source_content_complete_when_projection_compacts",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_target_preserves_runtime_control_semantics",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_gate_target_preserves_runtime_gate_semantics",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_target_preserves_runtime_incident_semantics",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_budget_never_rewrites_terminal_outcome",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_budget_preserves_diagnostic_receipt_semantics",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_soft_target_preserves_evaluable_result_values",
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_turn_exceeds_the_soft_target_instead_of_rewriting_completion",
            "tests.test_agent_gateway.AgentGatewayTests.test_gate_construction_preserves_schema_above_the_projection_target",
            "tests.test_agent_gateway.AgentGatewayTests.test_adapter_cannot_expand_diagnostic_scope_beyond_the_durable_contract",
            "tests.test_agent_gateway.AgentGatewayTests.test_multi_target_adapter_defaults_cannot_expand_the_durable_scope",
            "tests.test_agent_gateway.AgentGatewayTests.test_duplicate_special_file_requests_receive_unique_result_identities",
            "tests.test_mcp_contracts.JsonRpcEndpointTests.test_execute_text_does_not_treat_projection_compaction_as_incomplete_source",
        ),
    ),
    (
        "persisted_run_compatibility",
        (
            "tests.test_run_store.RunDecisionContractTests.test_supported_persisted_run_fixture_replays_through_current_readers",
            "tests.test_run_store.RunDecisionContractTests.test_legacy_run_events_are_explicitly_upcast_to_the_current_projection",
            "tests.test_run_store.RunDecisionContractTests.test_legacy_and_current_phase_facts_replay_to_the_same_projection",
            "tests.test_run_store.RunDecisionContractTests.test_every_legacy_workflow_definition_event_is_explicitly_upcast",
            "tests.test_run_store.RunDecisionContractTests.test_unknown_persisted_run_event_schema_is_rejected",
            "tests.test_run_store.RunDecisionContractTests.test_unknown_unversioned_persisted_event_kind_is_rejected",
            "tests.test_run_store.RunDecisionContractTests.test_incompatible_persisted_run_decision_version_is_rejected",
            "tests.test_compatibility.CompatibilityTelemetryTests.test_sqlite_repository_reads_retained_history_without_a_writer_api",
            "tests.test_mcp_contracts.RuntimeMcpServiceTests.test_agent_catalog_excludes_retired_compatibility_writers",
            "tests.test_mcp_contracts.RuntimeMcpServiceTests.test_retired_writer_names_cannot_be_dispatched",
        ),
    ),
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
    resolved_source_commit = selected_source_commit(
        source_commit,
        workspace=workspace,
    )
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
        resolved_source_commit,
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
    stability_verification_error = ""
    try:
        if raw_stability_report is None:
            raise ValueError("Runtime stability report is not a JSON object")
        verify_runtime_stability_report(
            raw_stability_report,
            expected_source_commit=resolved_source_commit,
            require_promotable=True,
        )
    except ValueError as exc:
        stability_verification_error = str(exc)
    stability_passed = (
        stability.returncode == 0 and not stability_verification_error
    )
    violations["runtime_stability"] = 0 if stability_passed else 1
    results.append(
        {
            "name": "runtime_stability",
            "status": "passed" if stability_passed else "failed",
            "command": list(stability_command),
            "verification_error": stability_verification_error,
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
    environment_record = dict(sorted((environment or _environment()).items()))
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": resolved_source_commit,
        "environment": environment_record,
        "environment_fingerprint": evidence_fingerprint(environment_record),
        "parameters": {
            "stability_profile": "ci",
            "qualification_groups": [name for name, _tests in QUALIFICATIONS],
            "stability_runner": "scripts/runtime_stability.py",
            "persisted_run_support": persisted_run_support(),
            "agent_projection_policy": agent_projection_policy(),
            "ordinary_partial_result_tests": list(PARTIAL_RESULT_TESTS),
            "real_backend_crash_tests": list(LIVE_PATCH_CRASH_TESTS),
        },
        "promotable": not any(violations.values()) and partial_accepted,
        "violations": violations,
        "ordinary_partial_result_accepted": partial_accepted,
        "qualifications": results,
    }
    report["evidence_digest"] = evidence_fingerprint(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-commit", default="")
    args = parser.parse_args(argv)
    report = qualify_runtime(
        args.workspace.expanduser().absolute(),
        source_commit=args.source_commit,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["promotable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
