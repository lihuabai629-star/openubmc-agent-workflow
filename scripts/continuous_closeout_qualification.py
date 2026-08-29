#!/usr/bin/env python3
"""Run the hermetic product-closeout integration checkpoint."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
for search_root in (ROOT, RUNTIME_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

from scripts.evidence_report import (  # noqa: E402
    evidence_fingerprint,
    resolve_source_commit,
)
from scripts.product_closeout_qualification import (  # noqa: E402
    qualify as qualify_product_closeout,
)
from scripts.mcp_process_lifecycle import summarize as summarize_mcp_records  # noqa: E402
from scripts.runtime_stability import qualify_dual_projection  # noqa: E402
from openubmc_target_runtime import inspect_mcp_process_records  # noqa: E402


SCHEMA = "openubmc-agent-workflow.continuous-closeout-qualification.v1"
PRODUCT_CLIENTS = ("claude", "codex", "openclaw")
EVALUATION_HARNESSES = ("dsh",)
PRODUCT_CONTRACT_TESTS = (
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_complete_fresh_runtime_closeout_is_promotable",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_historical_product_evidence_is_qualified_but_not_fresh_runtime_promotable",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_evidence_digest_tamper_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_source_commit_mismatch_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_artifact_identity_mismatch_is_rejected",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_hash_valid_self_attested_empty_evidence",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_proofs_without_fixed_source_evidence",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_proofs_without_a_runtime_ledger",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_upgrade_proof_for_another_artifact",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_stale_or_misordered_target_evidence",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_fresh_closeout_rejects_any_contradictory_additional_timeline_proof",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_manifest_cannot_select_the_runtime_authority",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_runtime_evidence_must_be_bound_to_the_qualified_target",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_digest_bound_evidence_is_parsed_from_the_same_bytes",
    "scripts.tests.test_product_closeout_qualification.ProductCloseoutQualificationTests.test_malformed_runtime_repository_produces_a_json_report",
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
    ),
    "cleanup": (
        "tests.test_mcp_process_lifecycle.McpProcessLifecycleTests.test_cleanup_terminates_only_confirmed_orphaned_processes",
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
)


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
) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *tests],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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


def _product_evidence(
    path: Path | None,
    *,
    runtime_repository: Path | None,
) -> dict[str, object]:
    if path is None:
        return {
            "status": "not-supplied",
            "qualified": False,
            "promotable": False,
            "claim_level": "unavailable",
        }
    value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("product manifest must contain an object")
    report = qualify_product_closeout(
        value,
        runtime_repository=runtime_repository,
    )
    return {
        "status": "verified-manifest",
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


def _mcp_closeout_snapshot() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as raw:
        lifecycle_root = Path(raw) / "mcp-processes"
        child_source = "\n".join(
            (
                "import os",
                "import sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(RUNTIME_ROOT)!r})",
                "from openubmc_target_runtime.mcp_lifecycle import McpProcessLifecycle",
                f"root = Path({str(lifecycle_root)!r})",
                "lifecycle = McpProcessLifecycle(",
                "    component='continuous-closeout-mcp',",
                "    version='1',",
                "    client='qualification',",
                "    task_id='continuous-closeout-qualification',",
                "    session_id='continuous-closeout-session',",
                "    parent_pid=os.getppid(),",
                "    state_path=root / 'state',",
                "    lifecycle_root=root,",
                "    idle_timeout_seconds=300,",
                ")",
                "lifecycle.record_exit('qualification-complete')",
            )
        )
        completed = subprocess.run(
            [sys.executable, "-c", child_source],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        records = inspect_mcp_process_records(lifecycle_root)
    summary = summarize_mcp_records(records)
    return {
        "status": (
            "passed"
            if completed.returncode == 0
            and summary["live_processes"] == 0
            and summary["active_requests"] == 0
            else "failed"
        ),
        "returncode": completed.returncode,
        "failure_tail": completed.stderr.strip()[-2000:],
        "task_closeout_ready": (
            summary["live_processes"] == 0 and summary["active_requests"] == 0
        ),
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
        "summary": summary,
    }


def qualify(
    product_manifest: Path | None = None,
    *,
    runtime_repository: Path | None = None,
) -> dict[str, object]:
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
    evaluation_isolation = _run_tests(EVALUATION_ISOLATION_TESTS, cwd=ROOT)
    lifecycle_results = {
        name: _run_tests(tests, cwd=RUNTIME_ROOT)
        for name, tests in MCP_LIFECYCLE_TESTS.items()
    }
    lifecycle_closeout = _mcp_closeout_snapshot()
    projection_tests = _run_tests(PROJECTION_TESTS, cwd=RUNTIME_ROOT)
    projection = qualify_dual_projection()
    repeated = projection.get("representative_receipt", {}).get(
        "repeated_projection", {}
    )
    repeated_projection = repeated if isinstance(repeated, Mapping) else {}
    product_evidence = _product_evidence(
        product_manifest,
        runtime_repository=runtime_repository,
    )
    source_clean = _source_clean()

    client_matrix_passed = all(
        (
            product_clients == list(PRODUCT_CLIENTS),
            evaluation_harnesses == list(EVALUATION_HARNESSES),
            not overlap,
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
    qualified = all(
        (
            client_matrix_passed,
            product_contract.get("status") == "passed",
            evaluation_isolation.get("status") == "passed",
            lifecycle_passed,
            correctness_primary,
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
        "source_commit": resolve_source_commit(ROOT),
        "source_clean": source_clean,
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
            "tests": projection_tests,
        },
        "external_blockers": external_blockers,
    }
    report["qualification_digest"] = evidence_fingerprint(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-manifest", type=Path)
    parser.add_argument("--runtime-repository", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = qualify(
            args.product_manifest,
            runtime_repository=args.runtime_repository,
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
