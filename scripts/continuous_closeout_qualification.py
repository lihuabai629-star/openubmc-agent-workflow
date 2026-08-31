#!/usr/bin/env python3
"""Run the hermetic product-closeout integration checkpoint."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
for search_root in (ROOT, RUNTIME_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

from scripts.evidence_report import (  # noqa: E402
    evidence_fingerprint,
    resolve_source_commit,
)
from scripts.formal_identity import (  # noqa: E402
    identity_argument,
    normalize_identity,
)
from scripts.codex_adoption_contract import product_client_failures  # noqa: E402
from scripts.product_closeout_qualification import (  # noqa: E402
    qualify as qualify_product_closeout,
)
from scripts.product_closeout_ingestion import (  # noqa: E402
    assemble_manifest as assemble_product_manifest,
)
from scripts.runtime_stability import qualify_dual_projection  # noqa: E402
from openubmc_target_runtime import build_release_lock  # noqa: E402


SCHEMA = "openubmc-agent-workflow.continuous-closeout-qualification.v1"
PRODUCT_CLIENTS = ("codex",)
EVALUATION_HARNESSES = ("dsh",)
SUPPORTED_CLIENT_TESTS = {
    "codex": (
        "openubmc-environment-setup.tests.test_install_environment.EnvironmentSetupTests.test_install_qualifies_codex_product_client",
    ),
}


class CandidateRelease(NamedTuple):
    bundle: Path
    release_commit: str
    release: dict[str, object]


PRODUCT_CONTRACT_TESTS = (
    "scripts.tests.test_product_closeout_ingestion.ProductCloseoutIngestionTests.test_assembles_a_promotable_manifest_from_runtime_and_fixed_evidence",
    "scripts.tests.test_product_closeout_ingestion.ProductCloseoutIngestionTests.test_cli_writes_deterministic_manifest_and_qualification_report",
    "scripts.tests.test_product_closeout_ingestion.ProductCloseoutIngestionTests.test_rejects_supporting_evidence_that_is_not_bound_by_its_proof",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_complete_fresh_runtime_closeout_is_promotable",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_structured_proofs_may_be_projected_after_the_terminal_outcome",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_raw_supporting_evidence_must_be_attached_before_outcome",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_native_runtime_upgrade_evidence_verifies_artifact_version_and_epoch",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_native_runtime_debug_evidence_verifies_fresh_nvme_drive_state",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_historical_product_evidence_is_qualified_but_not_fresh_runtime_promotable",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_evidence_digest_tamper_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_source_commit_mismatch_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_artifact_identity_mismatch_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_hash_valid_self_attested_empty_evidence",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_proofs_without_fixed_source_evidence",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_proofs_without_a_runtime_ledger",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_upgrade_proof_for_another_artifact",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_stale_or_misordered_target_evidence",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_uses_ledger_timeline_over_projection_timestamps",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_operator_attached_native_json_cannot_impersonate_runtime_operations",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_manifest_cannot_select_the_runtime_authority",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_runtime_evidence_must_be_bound_to_the_qualified_target",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_digest_bound_evidence_is_parsed_from_the_same_bytes",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_malformed_runtime_repository_produces_a_json_report",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_missing_runtime_run_produces_a_json_report",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_unsupported_runtime_storage_version_produces_a_json_report",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_qualification_does_not_migrate_the_trusted_runtime_repository",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_read_only_snapshot_replays_committed_wal_events",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_artifact_read_failure_cannot_accept_the_artifact_dimension",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_boolean_artifact_size_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_retained_630_manifest_matches_the_machine_report",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_retained_630_evidence_replays_when_bundle_is_available",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_historical_evidence_rejects_manifest_authored_claims",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_historical_evidence_rejects_unknown_evidence_type",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_historical_evidence_type_must_match_its_dimension",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_sata_evidence_cannot_satisfy_nvme_scope",
    "openubmc-environment-setup.tests.test_install_environment.EnvironmentSetupTests.test_managed_immutable_identity_validation_error_is_top_level_unhealthy",
    "openubmc-environment-setup.tests.test_install_environment.EnvironmentSetupTests.test_legacy_managed_release_without_lock_is_top_level_unhealthy",
)
EVALUATION_ISOLATION_TESTS = (
    "scripts.tests.test_evaluation_harness.EvaluationHarnessMetadataTests.test_dsh_is_managed_as_an_evaluation_harness_not_a_product_client",
    "scripts.tests.test_evaluation_harness.EvaluationHarnessRunTests.test_formal_dsh_run_is_isolated_and_records_reproducible_identity",
)
MCP_LIFECYCLE_TESTS = {
    "parent_loss": (
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_unknown_owner_still_exits_after_confirmed_parent_loss",
    ),
    "active_request_drain": (
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_requested_shutdown_waits_for_the_active_request_to_finish",
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_stdio_sigterm_drains_the_active_response_before_exit",
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_stdio_task_closeout_drains_overlapping_responses_and_rejects_new_work",
    ),
    "cleanup": (
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_cleanup_terminates_only_confirmed_orphaned_processes",
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_cleanup_preserves_orphan_without_verified_ownership_binding",
    ),
    "zero_live_orphans": (
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_cleanup_signals_the_identity_bound_process_handle",
    ),
}
PROJECTION_TESTS = (
    "tests.test_agent_gateway.AgentGatewayTests.test_terminal_turn_references_an_unchanged_previously_presented_receipt",
    "tests.test_agent_gateway.AgentGatewayTests.test_terminal_turn_does_not_reference_receipt_across_task_ownership",
    "tests.test_agent_gateway.AgentGatewayTests.test_terminal_turn_preserves_a_changed_diagnostic_receipt",
    "tests.test_agent_gateway.AgentGatewayTests.test_retried_one_shot_terminal_turn_keeps_the_complete_receipt",
    "tests.test_runtime_stability.RuntimeStabilityTests.test_dual_projection_qualification_measures_gate_and_terminal_seams",
    "tests.test_mcp_contracts.RuntimeMcpServiceTests.test_operator_status_derives_bounded_current_run_evidence_from_the_ledger",
    "tests.test_mcp_contracts.RuntimeMcpServiceTests.test_operator_status_without_task_binding_has_no_current_run",
)
TASK_MATRIX_TESTS = {
    "source_only": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_terminal_response_is_one_complete_run_decision",
        ),
        "correctness": (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_failed_phase_never_produces_a_success_outcome",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_hides_runtime_mechanics_and_records_terminal_outcome",
        ),
    },
    "live_patch": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_live_patch_runs_diagnosis_mutation_and_fresh_verification",
        ),
        "correctness": (
            "tests.test_agent_gateway.AgentGatewayTests.test_incomplete_live_patch_acceptance_cannot_report_completed_success",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_conflicting_acceptance_evidence_fails_closed",
        ),
    },
    "build_upgrade": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_build_upgrade_runs_both_gates_and_fresh_verification",
        ),
        "correctness": (
            "tests.test_agent_gateway.AgentGatewayTests.test_build_upgrade_closes_from_runtime_adapter_receipts",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_completed_failed_build_does_not_advance_to_upgrade",
        ),
    },
    "wide_observe": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_query_is_partitioned_without_becoming_a_blocker",
        ),
        "correctness": (
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_assurance_cannot_mask_fast_target_epoch_drift",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_wide_observation_rejects_cross_partition_target_epoch_drift",
        ),
    },
    "restart_crash": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_execute_workflows_resume_after_process_restart",
        ),
        "correctness": (
            "tests.test_mutation_recovery.MutationRecoveryTests.test_sigkill_crash_cuts_preserve_identity_and_never_repeat_the_mutation",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_deferred_build_upgrade_verification_resumes_after_restart",
        ),
    },
    "dependency_blocked": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_reports_official_validation_and_build_classifications",
        ),
        "correctness": (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_keeps_dependency_and_nvme_coverage_gaps_visible",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_without_validation_fields_reports_not_run_gaps",
        ),
    },
    "hardware_blocked": {
        "completion": (
            "tests.test_agent_gateway.AgentGatewayTests.test_source_only_keeps_dependency_and_nvme_coverage_gaps_visible",
        ),
        "correctness": (
            "tests.test_agent_gateway.AgentGatewayTests.test_hardware_coverage_rejects_unrelated_current_evidence",
        ),
        "terminal_contract": (
            "tests.test_agent_gateway.AgentGatewayTests.test_validation_evidence_rejects_false_success_and_repeated_preflight",
        ),
    },
}


def _workflow_metadata() -> dict[str, object]:
    value = json.loads((ROOT / "workflow.json").read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("workflow.json must contain an object")
    return dict(value)


def _source_clean() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _run_tests(
    tests: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    process_environment = None
    if environment is not None:
        process_environment = {**dict(os.environ), **dict(environment)}
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *tests],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=process_environment,
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "tests": list(tests),
        "returncode": completed.returncode,
        "failure_tail": (
            (completed.stderr or completed.stdout or "").strip()[-2000:]
            if completed.returncode != 0
            else ""
        ),
    }


def _run_task_group(
    dimensions: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    results = {
        dimension: {
            **_run_tests(tests, cwd=RUNTIME_ROOT),
            "tests": list(tests),
        }
        for dimension, tests in dimensions.items()
    }
    tests = [
        test
        for dimension in ("completion", "correctness", "terminal_contract")
        for test in dimensions[dimension]
    ]
    return {
        "status": (
            "passed"
            if all(result.get("status") == "passed" for result in results.values())
            else "failed"
        ),
        "tests": tests,
        **results,
    }


def _checked(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _prepare_candidate_release(
    qualification_root: Path,
    source_commit: str,
) -> CandidateRelease:
    candidate_repository = qualification_root / "candidate-release"
    _checked(
        (
            "git",
            "clone",
            "--quiet",
            "--no-checkout",
            "--no-hardlinks",
            str(ROOT),
            str(candidate_repository),
        )
    )
    _checked(
        (
            "git",
            "fetch",
            "--quiet",
            str(ROOT),
            "+refs/remotes/*:refs/remotes/source/*",
            "+refs/tags/*:refs/tags/*",
        ),
        cwd=candidate_repository,
    )
    for command in (
        ("git", "checkout", "--quiet", "--detach", source_commit),
        ("git", "config", "user.name", "Workflow Qualification"),
        (
            "git",
            "config",
            "user.email",
            "workflow-qualification@example.invalid",
        ),
    ):
        _checked(command, cwd=candidate_repository)
    release = build_release_lock(
        candidate_repository,
        source_commit=source_commit,
    )
    (candidate_repository / "release-lock.json").write_text(
        json.dumps(release, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _checked(("git", "add", "release-lock.json"), cwd=candidate_repository)
    commit_environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    _checked(
        (
            "git",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "qualification release lock",
        ),
        cwd=candidate_repository,
        environment=commit_environment,
    )
    release_commit = _checked(
        ("git", "rev-parse", "HEAD"),
        cwd=candidate_repository,
    ).stdout.strip()
    _checked(
        (
            "git",
            "update-ref",
            "refs/heads/qualification-release",
            release_commit,
        ),
        cwd=candidate_repository,
    )
    candidate_bundle = qualification_root / "candidate-release.bundle"
    _checked(
        ("git", "bundle", "create", str(candidate_bundle), "--all"),
        cwd=candidate_repository,
    )
    return CandidateRelease(candidate_bundle, release_commit, release)


def _product_client_run(
    name: str,
    tests: Sequence[str],
    contract: Mapping[str, object],
    source_commit: str,
    *,
    model_identity: Mapping[str, object],
    codex_identity: Mapping[str, object],
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as raw:
        qualification_root = Path(raw)
        evidence_path = qualification_root / f"{name}-product-client.json"
        try:
            candidate = _prepare_candidate_release(
                qualification_root,
                source_commit,
            )
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            return {
                "status": "failed",
                "client": name,
                "tests": list(tests),
                "failure_tail": f"candidate release is unavailable: {error}",
            }
        result = _run_tests(
            tests,
            cwd=ROOT,
            environment={
                "OPENUBMC_PRODUCT_CLIENT_EVIDENCE": str(evidence_path),
                "OPENUBMC_PRODUCT_CLIENT_REPO_URL": str(candidate.bundle),
                "OPENUBMC_PRODUCT_CLIENT_RELEASE_COMMIT": candidate.release_commit,
                "OPENUBMC_PRODUCT_CLIENT_MODEL_IDENTITY": json.dumps(
                    dict(model_identity), ensure_ascii=True, sort_keys=True
                ),
                "OPENUBMC_PRODUCT_CLIENT_CODEX_IDENTITY": json.dumps(
                    dict(codex_identity), ensure_ascii=True, sort_keys=True
                ),
            },
        )
        if result.get("status") != "passed":
            return {**result, "client": name, "tests": list(tests)}
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return {
                **result,
                "status": "failed",
                "client": name,
                "tests": list(tests),
                "failure_tail": f"product client evidence is unavailable: {error}",
            }
    if not isinstance(evidence, Mapping) or evidence.get("client") != name:
        return {
            **result,
            "status": "failed",
            "client": name,
            "tests": list(tests),
            "failure_tail": "product client evidence identity is invalid",
        }
    failures = product_client_failures(
        evidence,
        contract=contract,
        expected_source_commit=source_commit,
        expected_release=candidate.release,
    )
    lifecycle_records = evidence.get("mcp_lifecycle_records", [])
    records = lifecycle_records if isinstance(lifecycle_records, list) else []
    if any(
        not isinstance(record, Mapping)
        or record.get("model_identity") != dict(model_identity)
        for record in records
    ):
        failures.append("model_identity_mismatch")
    if any(
        not isinstance(record, Mapping)
        or record.get("codex_identity") != dict(codex_identity)
        for record in records
    ):
        failures.append("codex_identity_mismatch")
    return {
        **result,
        **dict(evidence),
        "status": "passed" if not failures else "failed",
        "tests": list(tests),
        "declared_mcp": contract.get("mcp") is True,
        "failure_codes": failures,
        "failure_tail": (
            ""
            if not failures
            else "product client evidence contradicts its contract"
        ),
    }


def load_product_ingestion(path: Path) -> dict[str, object]:
    return _load_json_object(path, "product ingestion input")


def _load_json_object(path: Path, label: str) -> dict[str, object]:
    value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must contain an object")
    return dict(value)


def _product_evidence(
    path: Path | None,
    *,
    ingestion_path: Path | None,
    runtime_repository: Path | None,
) -> dict[str, object]:
    if path is not None and ingestion_path is not None:
        raise ValueError(
            "product manifest and product ingestion input are mutually exclusive"
        )
    if path is None and ingestion_path is None:
        return {
            "status": "not-supplied",
            "qualified": False,
            "promotable": False,
            "claim_level": "unavailable",
        }
    if ingestion_path is not None:
        if runtime_repository is None:
            raise ValueError("product ingestion requires --runtime-repository")
        value = assemble_product_manifest(
            load_product_ingestion(ingestion_path),
            runtime_repository=runtime_repository,
        )
        status = "verified-ingestion"
    else:
        assert path is not None
        value = _load_json_object(path, "product manifest")
        status = "verified-manifest"
    report = qualify_product_closeout(
        value,
        runtime_repository=runtime_repository,
    )
    return {
        "status": status,
        "qualified": report.get("qualified") is True,
        "promotable": report.get("promotable") is True,
        "claim_level": str(report.get("claim_level", "unqualified")),
        "manifest_digest": str(report.get("manifest_digest", "")),
        "evidence_digest": str(report.get("evidence_digest", "")),
        "gaps": list(report.get("gaps", []))
        if isinstance(report.get("gaps"), list)
        else [],
        "violations": list(report.get("violations", []))
        if isinstance(report.get("violations"), list)
        else [],
    }


def _mcp_closeout_snapshot(
    product_run: Mapping[str, object],
    *,
    source_commit: str,
    model_identity: Mapping[str, object],
    codex_identity: Mapping[str, object],
) -> dict[str, object]:
    raw_records = product_run.get("mcp_lifecycle_records", [])
    records = [
        dict(record) for record in raw_records if isinstance(record, Mapping)
    ] if isinstance(raw_records, list) else []
    raw_closeout = product_run.get("mcp_closeout", {})
    closeout = dict(raw_closeout) if isinstance(raw_closeout, Mapping) else {}
    summary = closeout.get("summary", {})
    normalized_summary = dict(summary) if isinstance(summary, Mapping) else {}
    checks = closeout.get("closeout_checks", {})
    closeout_checks = dict(checks) if isinstance(checks, Mapping) else {}
    isolation = closeout.get("isolation", {})
    normalized_isolation = (
        dict(isolation) if isinstance(isolation, Mapping) else {}
    )
    identity_records_valid = bool(records) and all(
        record.get("client") == "codex"
        and not str(record.get("task_id", "")).startswith("unknown-")
        and not str(record.get("session_id", "")).startswith("unknown-")
        and record.get("source_commit") == source_commit
        and record.get("formal_run") is True
        and record.get("model_identity") == dict(model_identity)
        and record.get("codex_identity") == dict(codex_identity)
        and record.get("parent_identity_verified") is True
        and isinstance(record.get("parent_identity_currently_verified"), bool)
        and record.get("exit_reason") == "client-terminated"
        for record in records
    )
    isolation_verified = (
        closeout.get("isolation_verified") is True
        and normalized_isolation.get("global_codex_state_used") is False
        and normalized_isolation.get("installed_launcher_invocation") is True
    )
    return {
        "status": (
            "passed"
            if product_run.get("status") == "passed"
            and product_run.get("restart_verified") is True
            and closeout.get("status") == "passed"
            and identity_records_valid
            and isolation_verified
            and all(closeout_checks.values())
            else "failed"
        ),
        "returncode": int(product_run.get("returncode", 1)),
        "failure_tail": str(product_run.get("failure_tail", ""))[-2000:],
        "task_closeout_ready": identity_records_valid
        and all(closeout_checks.values()),
        "identity_records_valid": identity_records_valid,
        "isolation_verified": isolation_verified,
        "restart_verified": product_run.get("restart_verified") is True,
        "task_ids": sorted(
            {
                str(record.get("task_id", ""))
                for record in records
                if record.get("task_id")
            }
        ),
        "session_ids": sorted(
            {
                str(record.get("session_id", ""))
                for record in records
                if record.get("session_id")
            }
        ),
        "records": records,
        "summary": normalized_summary,
        "closeout_checks": closeout_checks,
        "isolation": normalized_isolation,
    }


def qualify(
    product_manifest: Path | None = None,
    *,
    product_ingestion: Path | None = None,
    runtime_repository: Path | None = None,
    source_commit: str | None = None,
    model_identity: Mapping[str, object] | None = None,
    codex_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    selected_source_commit = source_commit or resolve_source_commit(ROOT)
    required_identity = "formal model and Codex identity are required"
    selected_model_identity = normalize_identity(
        model_identity,
        label="model identity",
        required_message=required_identity,
    )
    selected_codex_identity = normalize_identity(
        codex_identity,
        label="Codex identity",
        required_message=required_identity,
    )
    workflow = _workflow_metadata()
    raw_clients = workflow.get("clients", {})
    clients = raw_clients if isinstance(raw_clients, Mapping) else {}
    raw_harnesses = workflow.get("evaluation_harnesses", {})
    harnesses = raw_harnesses if isinstance(raw_harnesses, Mapping) else {}
    product_clients = sorted(
        str(name)
        for name, contract in clients.items()
        if isinstance(contract, Mapping)
        and contract.get("role") == "supported-product-client"
    )
    evaluation_harnesses = sorted(
        str(name)
        for name, contract in harnesses.items()
        if isinstance(contract, Mapping)
        and contract.get("role") == "evaluation-harness"
    )
    overlap = sorted(set(product_clients) & set(evaluation_harnesses))

    product_contract = _run_tests(PRODUCT_CONTRACT_TESTS, cwd=ROOT)
    client_runs = {
        name: _product_client_run(
            name,
            tests,
            clients.get(name, {})
            if isinstance(clients.get(name), Mapping)
            else {},
            selected_source_commit,
            model_identity=selected_model_identity,
            codex_identity=selected_codex_identity,
        )
        for name, tests in SUPPORTED_CLIENT_TESTS.items()
    }
    evaluation_isolation = _run_tests(EVALUATION_ISOLATION_TESTS, cwd=ROOT)
    lifecycle_results = {
        name: _run_tests(tests, cwd=RUNTIME_ROOT)
        for name, tests in MCP_LIFECYCLE_TESTS.items()
    }
    lifecycle_closeout = _mcp_closeout_snapshot(
        client_runs.get("codex", {}),
        source_commit=selected_source_commit,
        model_identity=selected_model_identity,
        codex_identity=selected_codex_identity,
    )
    projection_tests = _run_tests(PROJECTION_TESTS, cwd=RUNTIME_ROOT)
    task_matrix = {
        name: _run_task_group(dimensions)
        for name, dimensions in TASK_MATRIX_TESTS.items()
    }
    projection = qualify_dual_projection()
    repeated = projection.get("representative_receipt", {}).get(
        "repeated_projection", {}
    )
    repeated_projection = repeated if isinstance(repeated, Mapping) else {}
    product_evidence = _product_evidence(
        product_manifest,
        ingestion_path=product_ingestion,
        runtime_repository=runtime_repository,
    )
    source_clean = _source_clean()

    client_matrix_passed = all(
        (
            product_clients == list(PRODUCT_CLIENTS),
            not overlap,
            all(result.get("status") == "passed" for result in client_runs.values()),
        )
    )
    lifecycle_passed = all(
        result.get("status") == "passed" for result in lifecycle_results.values()
    ) and lifecycle_closeout.get("status") == "passed"
    correctness_primary = (
        projection.get("status") == "passed"
        and projection.get("correctness", {}).get("passed") is True
        and projection_tests.get("status") == "passed"
    )
    task_matrix_completion = all(
        result["completion"].get("status") == "passed"
        for result in task_matrix.values()
    )
    task_matrix_correctness = all(
        result["correctness"].get("status") == "passed"
        for result in task_matrix.values()
    )
    task_matrix_terminal = all(
        result["terminal_contract"].get("status") == "passed"
        for result in task_matrix.values()
    )
    task_matrix_passed = all(
        (task_matrix_completion, task_matrix_correctness, task_matrix_terminal)
    )
    qualified = all(
        (
            client_matrix_passed,
            product_contract.get("status") == "passed",
            evaluation_isolation.get("status") == "passed",
            lifecycle_passed,
            correctness_primary,
            task_matrix_passed,
            source_clean,
        )
    )
    external_blockers = []
    if product_evidence.get("promotable") is not True:
        external_blockers.extend(
            (
                "fresh_runtime_product_evidence_required",
                "independent_upgrade_authorization_required",
                "target_and_rollback_package_required",
            )
        )
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": selected_source_commit,
        "source_clean": source_clean,
        "formal_identity": {
            "model": selected_model_identity,
            "codex": selected_codex_identity,
        },
        "qualified": qualified,
        "maintenance_checkpoint_ready": qualified,
        "fresh_product_promotable": product_evidence.get("promotable") is True,
        "product_contract": product_contract,
        "product_evidence": product_evidence,
        "client_matrix": {
            "status": "passed" if client_matrix_passed else "failed",
            "product_clients": product_clients,
            "evaluation_harnesses": evaluation_harnesses,
            "overlap": overlap,
            "runs": client_runs,
        },
        "evaluation_isolation": {
            **evaluation_isolation,
            "global_state_blocked": evaluation_isolation.get("status") == "passed",
            "task_owned": evaluation_isolation.get("status") == "passed",
        },
        "mcp_lifecycle": {
            "status": "passed" if lifecycle_passed else "failed",
            "parent_loss_covered": lifecycle_results["parent_loss"]["status"]
            == "passed",
            "active_request_drain_covered": lifecycle_results[
                "active_request_drain"
            ]["status"]
            == "passed",
            "cleanup_covered": lifecycle_results["cleanup"]["status"] == "passed",
            "zero_live_orphans_covered": lifecycle_results["zero_live_orphans"][
                "status"
            ]
            == "passed",
            "restart_closeout_covered": lifecycle_closeout.get(
                "restart_verified"
            )
            is True,
            "groups": lifecycle_results,
            "closeout": lifecycle_closeout,
        },
        "execute_projection": {
            "status": "passed" if correctness_primary else "failed",
            "correctness_primary": correctness_primary,
            "repeated_reference": repeated_projection.get("repeated_reference")
            is True,
            "full_bytes": int(repeated_projection.get("full_bytes", 0)),
            "reference_bytes": int(repeated_projection.get("reference_bytes", 0)),
            "saved_bytes": int(repeated_projection.get("saved_bytes", 0)),
            "blocks_promotability": False,
            "operator_projection_covered": projection_tests.get("status")
            == "passed",
            "tests": projection_tests,
        },
        "task_matrix": {
            "status": "passed" if task_matrix_passed else "failed",
            "correctness_primary": task_matrix_correctness,
            "completion_primary": task_matrix_completion,
            "terminal_contract_primary": task_matrix_terminal,
            "token_bytes_secondary": True,
            "groups": task_matrix,
            "product_clients": product_clients,
            "evaluation_harnesses": evaluation_harnesses,
        },
        "external_blockers": external_blockers,
    }
    report["qualification_digest"] = evidence_fingerprint(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--product-manifest", type=Path)
    parser.add_argument("--product-ingestion", type=Path)
    parser.add_argument("--runtime-repository", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = qualify(
            args.product_manifest,
            product_ingestion=args.product_ingestion,
            runtime_repository=args.runtime_repository,
            source_commit=args.source_commit,
            model_identity=args.model_identity,
            codex_identity=args.codex_identity,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
