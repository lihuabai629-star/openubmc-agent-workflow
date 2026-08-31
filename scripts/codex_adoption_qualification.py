#!/usr/bin/env python3
"""Produce the canonical hermetic Codex Adoption Qualification report."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
for search_root in (ROOT, RUNTIME_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

from openubmc_target_runtime import build_release_lock  # noqa: E402
from scripts.codex_adoption_contract import (  # noqa: E402
    DIMENSION_ORDER,
    SCHEMA,
    codex_mcp_dimension_failures,
    codex_mcp_failures,
    installation_dimension_failures,
    installation_identity_failures,
    verify_codex_adoption_report,
)
from scripts.continuous_closeout_qualification import (  # noqa: E402
    qualify as qualify_closeout,
)
from scripts.evidence_report import (  # noqa: E402
    evidence_fingerprint,
    source_commit as bind_source_commit,
)
from scripts.formal_identity import (  # noqa: E402
    identity_argument,
    normalize_identity,
)


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _passed(value: Mapping[str, object]) -> bool:
    return value.get("status") == "passed"


def qualify(
    *,
    source_commit: str | None = None,
    model_identity: Mapping[str, object] | None = None,
    codex_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
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
    selected_source_commit = bind_source_commit(
        source_commit or "",
        workspace=ROOT,
    )
    closeout = qualify_closeout(
        source_commit=selected_source_commit,
        model_identity=selected_model_identity,
        codex_identity=selected_codex_identity,
    )
    qualification_commit = str(closeout.get("source_commit", ""))
    release = build_release_lock(ROOT, source_commit=selected_source_commit)

    compatibility = _mapping(release.get("compatibility"))
    release_clients = _mapping(compatibility.get("clients"))
    runtime = _mapping(release.get("runtime"))
    source_clean = closeout.get("source_clean") is True

    client_matrix = _mapping(closeout.get("client_matrix"))
    runs = _mapping(client_matrix.get("runs"))
    codex_run = _mapping(runs.get("codex"))
    workflow_exchange = _mapping(codex_run.get("workflow_exchange"))
    installed = _mapping(codex_run.get("installation"))
    installed_source = _mapping(installed.get("source"))
    installed_release = _mapping(installed.get("release"))
    installation_failures = installation_identity_failures(
        codex_run,
        expected_source_commit=selected_source_commit,
        expected_release=release,
    )
    if not source_clean:
        installation_failures.insert(0, "qualification_source_dirty")
    if list(release_clients) != ["codex"]:
        installation_failures.append("product_client_matrix_invalid")
    installation_identity = {
        "status": "passed" if not installation_failures else "failed",
        "failure_codes": installation_failures,
        "source_clean": source_clean,
        "release_version": str(installed_release.get("release_version", "")),
        "source_commit": str(installed_release.get("source_commit", "")),
        "release_commit": str(installed_source.get("current_commit", "")),
        "lock_digest": str(installed_release.get("lock_digest", "")),
        "source_tree_digest": str(
            installed_release.get("source_tree_digest", "")
        ),
        "workflow_digest": str(installed_release.get("workflow_digest", "")),
        "clients": list(release_clients),
        "source_mode": str(installed_source.get("mode", "")),
        "trust_mode": str(installed_release.get("trust_mode", "")),
        "operational_ready": installed.get("operational_ready") is True,
        "release_identity_verified": installed.get("release_identity_verified")
        is True,
        "evaluation_ready": installed.get("evaluation_ready") is True,
        "skill_digests": _mapping(installed_release.get("skill_digests")),
        "runtime_api": str(runtime.get("api_version", "")),
        "runtime_content_digest": str(runtime.get("content_digest", "")),
    }
    installation_failures.extend(
        failure
        for failure in installation_dimension_failures(
            installation_identity,
            expected_source_commit=selected_source_commit,
        )
        if failure not in installation_failures
    )
    installation_identity["failure_codes"] = installation_failures
    installation_identity["status"] = (
        "passed" if not installation_failures else "failed"
    )

    launcher_identity = _mapping(codex_run.get("launcher_identity"))
    launcher_identity_digest = str(
        codex_run.get("launcher_identity_digest", "")
    )
    installed_source_commit = str(codex_run.get("source_commit", ""))
    raw_mcp_lifecycle_records = codex_run.get("mcp_lifecycle_records")
    mcp_lifecycle_records = (
        list(raw_mcp_lifecycle_records)
        if isinstance(raw_mcp_lifecycle_records, list)
        else []
    )
    mcp_failures = codex_mcp_failures(
        codex_run,
        contract={"mcp": True},
        expected_source_commit=selected_source_commit,
        expected_runtime=runtime,
    )
    if any(
        not isinstance(record, Mapping)
        or record.get("model_identity") != selected_model_identity
        for record in mcp_lifecycle_records
    ):
        mcp_failures.append("model_identity_mismatch")
    if any(
        not isinstance(record, Mapping)
        or record.get("codex_identity") != selected_codex_identity
        for record in mcp_lifecycle_records
    ):
        mcp_failures.append("codex_identity_mismatch")
    identity_bound = all(
        (
            installed_source_commit == selected_source_commit,
            codex_run.get("runtime_api") == runtime.get("api_version"),
            codex_run.get("runtime_content_digest") == runtime.get("content_digest"),
            codex_run.get("launcher_state_verified") is True,
            launcher_identity.get("source_commit") == selected_source_commit,
            launcher_identity_digest.startswith("sha256:"),
        )
    )
    if client_matrix.get("status") != "passed":
        mcp_failures.append("client_matrix_failed")
    if client_matrix.get("product_clients") != ["codex"]:
        mcp_failures.append("product_clients_invalid")
    if client_matrix.get("overlap") != []:
        mcp_failures.append("client_harness_overlap")
    if codex_run.get("status") != "passed":
        mcp_failures.append("product_client_run_failed")
    codex_mcp = {
        "status": "passed" if not mcp_failures else "failed",
        "failure_codes": mcp_failures,
        "configured": codex_run.get("declared_mcp") is True,
        "registration_verified": codex_run.get("mcp_registration_verified")
        is True,
        "runtime_launcher_verified": codex_run.get("runtime_launcher_verified")
        is True,
        "runtime_invocation": str(codex_run.get("runtime_invocation", "")),
        "protocol_exchange": list(codex_run.get("protocol_exchange", [])),
        "tools": list(codex_run.get("tools", [])),
        "identity_bound": identity_bound,
        "installed_source_commit": installed_source_commit,
        "runtime_api": str(codex_run.get("runtime_api", "")),
        "runtime_content_digest": str(
            codex_run.get("runtime_content_digest", "")
        ),
        "launcher_state_verified": codex_run.get("launcher_state_verified")
        is True,
        "launcher_identity": launcher_identity,
        "launcher_identity_digest": launcher_identity_digest,
        "workflow_exchange": workflow_exchange,
        "codex_process_invocation": codex_run.get(
            "codex_process_invocation"
        )
        is True,
        "codex_process_runs": list(codex_run.get("codex_process_runs", [])),
        "captured_model_tools": list(
            codex_run.get("captured_model_tools", [])
        ),
        "captured_runtime_tool_contracts": list(
            codex_run.get("captured_runtime_tool_contracts", [])
        ),
        "mcp_lifecycle_records": mcp_lifecycle_records,
        "restart_verified": codex_run.get("restart_verified") is True,
        "mcp_closeout": _mapping(codex_run.get("mcp_closeout")),
    }
    mcp_failures.extend(
        failure
        for failure in codex_mcp_dimension_failures(
            codex_mcp,
            expected_source_commit=selected_source_commit,
            expected_runtime=runtime,
        )
        if failure not in mcp_failures
    )
    codex_mcp["failure_codes"] = mcp_failures
    codex_mcp["status"] = "passed" if not mcp_failures else "failed"

    product_contract = _mapping(closeout.get("product_contract"))
    task_matrix = _mapping(closeout.get("task_matrix"))
    projection = _mapping(closeout.get("execute_projection"))
    lifecycle = _mapping(closeout.get("mcp_lifecycle"))
    lifecycle_closeout = _mapping(lifecycle.get("closeout"))
    dimensions: dict[str, dict[str, object]] = {
        "installation_identity": installation_identity,
        "codex_mcp": codex_mcp,
        "product_contract": {
            **product_contract,
            "status": "passed" if _passed(product_contract) else "failed",
        },
        "task_matrix": {
            **task_matrix,
            "status": (
                "passed"
                if all(
                    (
                        _passed(task_matrix),
                        task_matrix.get("correctness_primary") is True,
                        task_matrix.get("completion_primary") is True,
                        task_matrix.get("terminal_contract_primary") is True,
                    )
                )
                else "failed"
            ),
        },
        "projection": {
            **projection,
            "status": (
                "passed"
                if all(
                    (
                        _passed(projection),
                        projection.get("correctness_primary") is True,
                        projection.get("repeated_reference") is True,
                        projection.get("operator_projection_covered") is True,
                    )
                )
                else "failed"
            ),
        },
        "lifecycle": {
            **lifecycle,
            "status": (
                "passed"
                if all(
                    (
                        _passed(lifecycle),
                        _passed(lifecycle_closeout),
                        lifecycle_closeout.get("task_closeout_ready") is True,
                        lifecycle_closeout.get("identity_records_valid") is True,
                        lifecycle_closeout.get("isolation_verified") is True,
                        _mapping(lifecycle_closeout.get("summary")).get(
                            "owned_live_processes"
                        )
                        == 0,
                        all(
                            _mapping(
                                lifecycle_closeout.get("closeout_checks")
                            ).get(name)
                            is True
                            for name in (
                                "active_requests_zero",
                                "confirmed_live_orphans_zero",
                                "unattributed_live_processes_zero",
                                "owned_live_processes_zero",
                            )
                        ),
                    )
                )
                else "failed"
            ),
        },
    }
    failed_dimensions = [
        name for name in DIMENSION_ORDER if dimensions[name]["status"] != "passed"
    ]
    qualified = not failed_dimensions
    evaluation_isolation = _mapping(closeout.get("evaluation_isolation"))
    maintenance_checkpoint_blockers = list(failed_dimensions)
    maintenance_checkpoint_ready = not maintenance_checkpoint_blockers
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": selected_source_commit,
        "qualified": qualified,
        "maintenance_checkpoint_ready": maintenance_checkpoint_ready,
        "maintenance_checkpoint_blockers": maintenance_checkpoint_blockers,
        "failed_dimensions": failed_dimensions,
        "dimensions": dimensions,
        "provenance": {
            "source": {
                "commit": selected_source_commit,
                "qualification_commit": qualification_commit,
                "continuous_closeout_digest": str(
                    closeout.get("qualification_digest", "")
                ),
            },
            "model": selected_model_identity,
            "codex": selected_codex_identity,
        },
        "external_evaluation": {
            "blocking": False,
            "harnesses": list(client_matrix.get("evaluation_harnesses", [])),
            "isolation": evaluation_isolation,
            "required_for_maintenance_checkpoint": False,
        },
        "release_gate": {
            "evidence_type": "codex-adoption-qualification",
            "eligible": maintenance_checkpoint_ready,
        },
    }
    report["evidence_digest"] = evidence_fingerprint(report)
    verify_codex_adoption_report(report)
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
    parser.add_argument("--source-commit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = qualify(
            source_commit=args.source_commit,
            model_identity=args.model_identity,
            codex_identity=args.codex_identity,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["maintenance_checkpoint_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
