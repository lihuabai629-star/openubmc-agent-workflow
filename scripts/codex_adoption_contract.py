"""Shared validation for Codex product-client and adoption evidence."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from scripts.evidence_report import evidence_fingerprint
from scripts.formal_identity import PINNED_CODEX_VERSION


SCHEMA = "openubmc-agent-workflow.codex-adoption-qualification.v1"
DIMENSION_ORDER = (
    "installation_identity",
    "codex_mcp",
    "product_contract",
    "task_matrix",
    "projection",
    "lifecycle",
)
RISK_CONTROL_NAMES = (
    "false_successes",
    "duplicate_dangerous_effects",
    "unknown_new_identity_retries",
    "wrong_target_or_artifact_mutations",
    "lifecycle_leaks",
)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sha256(value: object) -> bool:
    text = str(value)
    return (
        len(text) == 71
        and text.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in text[7:])
    )


def _full_commit(value: object) -> bool:
    text = str(value).lower()
    return len(text) in {40, 64} and all(
        character in "0123456789abcdef" for character in text
    )


def risk_controls_valid(value: object) -> bool:
    controls = _mapping(value)
    violations = _mapping(controls.get("violations"))
    tests = _mapping(controls.get("tests"))
    expected = set(RISK_CONTROL_NAMES)
    return all(
        (
            controls.get("passed") is True,
            set(violations) == expected,
            all(
                isinstance(violations.get(name), int)
                and not isinstance(violations.get(name), bool)
                and violations.get(name) == 0
                for name in RISK_CONTROL_NAMES
            ),
            set(tests) == expected,
            all(
                _mapping(tests.get(name)).get("status") == "passed"
                and isinstance(
                    _mapping(tests.get(name)).get("returncode"), int
                )
                and not isinstance(
                    _mapping(tests.get(name)).get("returncode"), bool
                )
                and _mapping(tests.get(name)).get("returncode") == 0
                and isinstance(_mapping(tests.get(name)).get("tests"), list)
                and bool(_mapping(tests.get(name)).get("tests"))
                for name in RISK_CONTROL_NAMES
            ),
        )
    )


def _launcher_identity_failures(
    launcher_identity: Mapping[str, object],
    launcher_identity_digest: object,
    *,
    expected_source_commit: str,
    expected_runtime: Mapping[str, object],
) -> list[str]:
    valid = all(
        (
            launcher_identity.get("schema")
            == "openubmc-agent-workflow.codex-launcher-identity.v1",
            launcher_identity.get("runtime_api")
            == expected_runtime.get("api_version"),
            launcher_identity.get("runtime_content_digest")
            == expected_runtime.get("content_digest"),
            launcher_identity.get("source_commit") == expected_source_commit,
            launcher_identity.get("entrypoint")
            == "openubmc-debug/scripts/target_runtime_mcp.py",
            launcher_identity_digest == evidence_fingerprint(launcher_identity),
        )
    )
    return [] if valid else ["launcher_identity_invalid"]


def _workflow_exchange_failures(
    workflow_exchange: Mapping[str, object],
) -> list[str]:
    checks = (
        ("workflow_tool_invalid", workflow_exchange.get("tool") == "execute"),
        (
            "workflow_state_invalid",
            workflow_exchange.get("state") == "preflight_failed",
        ),
        (
            "workflow_classification_invalid",
            workflow_exchange.get("classification") == "preflight_failure",
        ),
        (
            "workflow_error_field_invalid",
            workflow_exchange.get("error_field") == "run_id",
        ),
        (
            "workflow_retry_invalid",
            _mapping(workflow_exchange.get("canonical_retry")).get("kind")
            == "resume"
            and bool(
                _mapping(workflow_exchange.get("canonical_retry")).get("run_id")
            ),
        ),
        (
            "workflow_error_contract_invalid",
            workflow_exchange.get("is_error") is True,
        ),
    )
    return [name for name, passed in checks if not passed]


def installation_identity_failures(
    evidence: Mapping[str, object],
    *,
    expected_source_commit: str,
    expected_release: Mapping[str, object],
) -> list[str]:
    installation = _mapping(evidence.get("installation"))
    source = _mapping(installation.get("source"))
    release = _mapping(installation.get("release"))
    release_runtime = _mapping(release.get("runtime"))
    expected_runtime = _mapping(expected_release.get("runtime"))
    expected_skills = expected_release.get("skills")
    expected_skill_digests = {
        str(item.get("name")): str(item.get("digest"))
        for item in expected_skills
        if isinstance(item, Mapping)
    } if isinstance(expected_skills, list) else {}
    checks = (
        ("installation_not_ok", installation.get("ok") is True),
        (
            "operational_readiness_missing",
            installation.get("operational_ready") is True,
        ),
        (
            "release_identity_unverified",
            installation.get("release_identity_verified") is True,
        ),
        (
            "evaluation_readiness_missing",
            installation.get("evaluation_ready") is True,
        ),
        ("source_not_managed", source.get("mode") == "managed"),
        ("source_ref_not_immutable", source.get("ref_kind") in {"tag", "commit"}),
        (
            "installed_release_commit_invalid",
            _full_commit(source.get("current_commit"))
            and source.get("current_commit") == source.get("resolved_commit"),
        ),
        ("installed_source_dirty", source.get("dirty") is False),
        (
            "release_schema_invalid",
            release.get("schema")
            == "openubmc-agent-workflow.release-lock.v1",
        ),
        ("release_not_immutable", release.get("immutable") is True),
        ("release_not_verified", release.get("verified") is True),
        (
            "release_trust_mode_invalid",
            release.get("trust_mode") == "verified-immutable-source",
        ),
        ("installed_clients_invalid", installation.get("clients") == ["codex"]),
        (
            "source_commit_mismatch",
            release.get("source_commit") == expected_source_commit
            and evidence.get("source_commit") == expected_source_commit,
        ),
        (
            "lock_digest_mismatch",
            _sha256(release.get("lock_digest"))
            and release.get("lock_digest") == expected_release.get("lock_digest"),
        ),
        (
            "source_tree_digest_mismatch",
            _sha256(release.get("source_tree_digest"))
            and release.get("source_tree_digest")
            == expected_release.get("source_tree_digest"),
        ),
        (
            "workflow_digest_mismatch",
            _sha256(release.get("workflow_digest"))
            and release.get("workflow_digest")
            == expected_release.get("workflow_digest"),
        ),
        (
            "runtime_identity_mismatch",
            release_runtime.get("api_version")
            == expected_runtime.get("api_version")
            and release_runtime.get("content_digest")
            == expected_runtime.get("content_digest"),
        ),
        (
            "skill_identity_mismatch",
            bool(expected_skill_digests)
            and release.get("skill_digests") == expected_skill_digests,
        ),
    )
    return [name for name, passed in checks if not passed]


def _mcp_lifecycle_failures(
    value: object,
    *,
    expected_source_commit: str,
) -> list[str]:
    records = value if isinstance(value, list) else []
    if not records:
        return ["mcp_lifecycle_records_missing"]
    valid = all(
        isinstance(record, Mapping)
        and record.get("schema") == "openubmc.mcp-process-lifecycle.v1"
        and record.get("client") == "codex"
        and isinstance(record.get("task_id"), str)
        and bool(str(record.get("task_id", "")).strip())
        and not str(record.get("task_id", "")).startswith("unknown-")
        and isinstance(record.get("session_id"), str)
        and bool(str(record.get("session_id", "")).strip())
        and not str(record.get("session_id", "")).startswith("unknown-")
        and record.get("source_commit") == expected_source_commit
        and record.get("formal_run") is True
        and isinstance(record.get("model_identity"), Mapping)
        and bool(record.get("model_identity"))
        and isinstance(record.get("codex_identity"), Mapping)
        and bool(record.get("codex_identity"))
        and _mapping(record.get("codex_identity")).get("version")
        == PINNED_CODEX_VERSION
        and record.get("parent_identity_verified") is True
        and isinstance(record.get("parent_identity_currently_verified"), bool)
        and isinstance(record.get("start_time"), str)
        and bool(str(record.get("start_time", "")).strip())
        and isinstance(record.get("runtime_state_root"), str)
        and bool(str(record.get("runtime_state_root", "")).strip())
        and record.get("lifecycle_state") == "stopped"
        and isinstance(record.get("active_requests"), int)
        and not isinstance(record.get("active_requests"), bool)
        and record.get("active_requests") == 0
        and record.get("exit_reason") in {"client-terminated", "task-closeout"}
        for record in records
    )
    return [] if valid else ["mcp_lifecycle_identity_invalid"]


def _codex_process_failures(
    evidence: Mapping[str, object],
) -> list[str]:
    raw_runs = evidence.get("codex_process_runs")
    runs = raw_runs if isinstance(raw_runs, list) else []
    raw_records = evidence.get("mcp_lifecycle_records")
    records = raw_records if isinstance(raw_records, list) else []
    captured_tools = evidence.get("captured_model_tools")
    raw_contracts = evidence.get("captured_runtime_tool_contracts")
    contracts = raw_contracts if isinstance(raw_contracts, list) else []
    raw_orchestrator_contracts = evidence.get(
        "captured_orchestrator_tool_contracts"
    )
    orchestrator_contracts = (
        raw_orchestrator_contracts
        if isinstance(raw_orchestrator_contracts, list)
        else []
    )
    runtime_contracts = [
        contract
        for contract in contracts
        if isinstance(contract, Mapping)
        and contract.get("name") == "mcp__openubmc_target_runtime"
        and contract.get("type") == "namespace"
    ]
    nested_tool_names = {
        str(tool.get("name"))
        for contract in runtime_contracts
        for tool in (
            contract.get("tools")
            if isinstance(contract.get("tools"), list)
            else []
        )
        if isinstance(tool, Mapping)
    }
    functions_contracts = [
        contract
        for contract in orchestrator_contracts
        if isinstance(contract, Mapping)
        and contract.get("name") == "functions"
        and contract.get("type") == "namespace"
    ]
    orchestrator_tool_names = {
        str(tool.get("name"))
        for contract in functions_contracts
        for tool in (
            contract.get("tools")
            if isinstance(contract.get("tools"), list)
            else []
        )
        if isinstance(tool, Mapping)
    }
    bindings: set[tuple[int, str]] = set()
    executable_identities: set[tuple[str, str, str]] = set()
    declared_models = {
        str(_mapping(record.get("model_identity")).get("model", "")).strip()
        for record in records
        if isinstance(record, Mapping)
    }
    declared_codex_versions = {
        str(_mapping(record.get("codex_identity")).get("version", "")).strip()
        for record in records
        if isinstance(record, Mapping)
    }
    expected_model = next(iter(declared_models)) if len(declared_models) == 1 else ""
    valid_runs = len(runs) == 2
    for run in runs:
        if not isinstance(run, Mapping):
            valid_runs = False
            continue
        process_id = run.get("process_id")
        process_identity = str(run.get("process_identity", ""))
        request_models = run.get("captured_request_models")
        transport_provenance = _mapping(run.get("transport_provenance"))
        normalized_request_models = (
            request_models if isinstance(request_models, list) else []
        )
        valid = all(
            (
                isinstance(process_id, int),
                not isinstance(process_id, bool),
                int(process_id or 0) > 1,
                process_identity not in {"", "unknown"},
                run.get("parent_pid") == process_id,
                run.get("parent_identity") == process_identity,
                Path(str(run.get("executable", ""))).is_absolute(),
                _sha256(run.get("executable_sha256")),
                run.get("version") == PINNED_CODEX_VERSION,
                declared_codex_versions == {PINNED_CODEX_VERSION},
                bool(expected_model),
                run.get("requested_model") == expected_model,
                isinstance(request_models, list),
                bool(normalized_request_models),
                all(
                    model == expected_model for model in normalized_request_models
                ),
                transport_provenance
                == {
                    "provider": "local-hermetic-responses",
                    "wire_api": "responses",
                    "network_scope": "loopback",
                },
                run.get("returncode") == 0,
            )
        )
        valid_runs = valid_runs and valid
        if valid and isinstance(process_id, int):
            bindings.add((process_id, process_identity))
            executable_identities.add(
                (
                    str(run.get("executable", "")),
                    str(run.get("executable_sha256", "")),
                    str(run.get("version", "")),
                )
            )
    record_bindings = {
        (record.get("parent_pid"), str(record.get("parent_identity", "")))
        for record in records
        if isinstance(record, Mapping)
    }
    valid = all(
        (
            evidence.get("codex_process_invocation") is True,
            valid_runs,
            len(bindings) == 2,
            len(executable_identities) == 1,
            record_bindings == bindings,
            all(
                isinstance(record, Mapping)
                and record.get("exit_reason") == "client-terminated"
                for record in records
            ),
            isinstance(captured_tools, list),
            (
                (
                    "mcp__openubmc_target_runtime"
                    in {str(item) for item in captured_tools}
                    and nested_tool_names == {"execute", "observe"}
                )
                or (
                    "functions" in {str(item) for item in captured_tools}
                    and len(functions_contracts) == 1
                    and "exec" in orchestrator_tool_names
                )
            ),
            evidence.get("restart_verified") is True,
        )
    )
    return [] if valid else ["codex_process_unverified"]


def _mcp_closeout_failures(value: object, records_value: object) -> list[str]:
    closeout = _mapping(value)
    records = records_value if isinstance(records_value, list) else []
    summary = _mapping(closeout.get("summary"))
    checks = _mapping(closeout.get("closeout_checks"))
    isolation = _mapping(closeout.get("isolation"))
    operator_status = _mapping(closeout.get("operator_status"))
    operator_summary = _mapping(operator_status.get("summary"))
    operator_checks = _mapping(operator_status.get("closeout_checks"))
    try:
        qualification_root = Path(
            str(isolation.get("qualification_root", ""))
        ).absolute()
        isolated_roots = tuple(
            Path(str(isolation.get(name, ""))).absolute()
            for name in (
                "task_home",
                "codex_config_root",
                "runtime_state_root",
                "lifecycle_root",
            )
        )
        roots_isolated = (
            bool(str(isolation.get("qualification_root", "")).strip())
            and all(
                bool(str(isolation.get(name, "")).strip())
                for name in (
                    "task_home",
                    "codex_config_root",
                    "runtime_state_root",
                    "lifecycle_root",
                )
            )
            and all(path.is_relative_to(qualification_root) for path in isolated_roots)
            and len(set(isolated_roots)) == len(isolated_roots)
        )
    except (OSError, RuntimeError, ValueError):
        roots_isolated = False
    zero_fields = (
        "live_processes",
        "active_requests",
        "confirmed_live_orphans",
        "unattributed_live_processes",
        "owned_live_processes",
    )
    check_fields = (
        "active_requests_zero",
        "confirmed_live_orphans_zero",
        "unattributed_live_processes_zero",
        "owned_live_processes_zero",
    )
    valid = all(
        (
            closeout.get("status") == "passed",
            closeout.get("task_closeout_ready") is True,
            closeout.get("identity_records_valid") is True,
            closeout.get("isolation_verified") is True,
            isinstance(summary.get("record_count"), int),
            not isinstance(summary.get("record_count"), bool),
            int(summary.get("record_count", 0)) >= 2,
            summary.get("record_count") == len(records),
            all(summary.get(name) == 0 for name in zero_fields),
            all(checks.get(name) is True for name in check_fields),
            operator_status.get("schema")
            == "openubmc-agent-workflow.mcp-process-status.v1",
            operator_status.get("operation") == "status",
            operator_status.get("task_id")
            == (records[0].get("task_id") if records else None),
            operator_status.get("session_id")
            == (records[0].get("session_id") if records else None),
            operator_status.get("task_closeout_ready") is True,
            operator_summary == summary,
            operator_checks == checks,
            operator_summary.get("stopped_processes") == len(records),
            roots_isolated,
            isolation.get("global_codex_state_used") is False,
            isolation.get("installed_launcher_invocation") is True,
        )
    )
    return [] if valid else ["mcp_closeout_invalid"]


def codex_mcp_failures(
    evidence: Mapping[str, object],
    *,
    contract: Mapping[str, object],
    expected_source_commit: str,
    expected_runtime: Mapping[str, object],
) -> list[str]:
    workflow_exchange = _mapping(evidence.get("workflow_exchange"))
    launcher_identity = _mapping(evidence.get("launcher_identity"))
    launcher_identity_digest = evidence.get("launcher_identity_digest", "")
    declared_mcp = contract.get("mcp") is True
    checks = (
        ("wrong_client", evidence.get("client") == "codex"),
        (
            "adapter_contract_mismatch",
            evidence.get("adapter_available") is declared_mcp,
        ),
        (
            "support_mode_mismatch",
            evidence.get("support_mode")
            == ("skills-and-runtime-mcp" if declared_mcp else "skills-only"),
        ),
        (
            "registration_mismatch",
            evidence.get("mcp_registration_verified") is declared_mcp,
        ),
        (
            "runtime_launcher_unverified",
            evidence.get("runtime_launcher_verified") is True,
        ),
        (
            "launcher_state_unverified",
            evidence.get("launcher_state_verified") is True,
        ),
        (
            "runtime_invocation_invalid",
            evidence.get("runtime_invocation")
            == "installed-runtime-launcher-protocol",
        ),
        (
            "protocol_exchange_invalid",
            evidence.get("protocol_exchange")
            == ["initialize", "tools/list", "tools/call:execute"],
        ),
        ("agent_tools_invalid", evidence.get("tools") == ["execute", "observe"]),
        (
            "source_binding_invalid",
            evidence.get("source_commit") == expected_source_commit,
        ),
        (
            "runtime_api_mismatch",
            evidence.get("runtime_api") == expected_runtime.get("api_version"),
        ),
        (
            "runtime_digest_mismatch",
            evidence.get("runtime_content_digest")
            == expected_runtime.get("content_digest"),
        ),
        ("restart_unverified", evidence.get("restart_verified") is True),
    )
    return [
        *[name for name, passed in checks if not passed],
        *_codex_process_failures(evidence),
        *_mcp_lifecycle_failures(
            evidence.get("mcp_lifecycle_records"),
            expected_source_commit=expected_source_commit,
        ),
        *_mcp_closeout_failures(
            evidence.get("mcp_closeout"),
            evidence.get("mcp_lifecycle_records"),
        ),
        *_launcher_identity_failures(
            launcher_identity,
            launcher_identity_digest,
            expected_source_commit=expected_source_commit,
            expected_runtime=expected_runtime,
        ),
        *_workflow_exchange_failures(workflow_exchange),
    ]


def installation_dimension_failures(
    installation: Mapping[str, object],
    *,
    expected_source_commit: str,
) -> list[str]:
    checks = (
        ("source_not_clean", installation.get("source_clean") is True),
        (
            "release_version_invalid",
            isinstance(installation.get("release_version"), str)
            and bool(installation.get("release_version")),
        ),
        (
            "source_commit_mismatch",
            installation.get("source_commit") == expected_source_commit,
        ),
        (
            "release_commit_invalid",
            _full_commit(installation.get("release_commit")),
        ),
        ("lock_digest_invalid", _sha256(installation.get("lock_digest"))),
        (
            "source_tree_digest_invalid",
            _sha256(installation.get("source_tree_digest")),
        ),
        (
            "workflow_digest_invalid",
            _sha256(installation.get("workflow_digest")),
        ),
        ("clients_invalid", installation.get("clients") == ["codex"]),
        ("source_mode_invalid", installation.get("source_mode") == "managed"),
        (
            "trust_mode_invalid",
            installation.get("trust_mode") == "verified-immutable-source",
        ),
        (
            "operational_readiness_missing",
            installation.get("operational_ready") is True,
        ),
        (
            "release_identity_unverified",
            installation.get("release_identity_verified") is True,
        ),
        (
            "evaluation_readiness_missing",
            installation.get("evaluation_ready") is True,
        ),
        (
            "skill_identity_missing",
            isinstance(installation.get("skill_digests"), Mapping)
            and bool(installation.get("skill_digests")),
        ),
        (
            "runtime_api_invalid",
            installation.get("runtime_api") == "openubmc.target-runtime.v1",
        ),
        (
            "runtime_digest_invalid",
            _sha256(installation.get("runtime_content_digest")),
        ),
    )
    return [name for name, passed in checks if not passed]


def codex_mcp_dimension_failures(
    codex_mcp: Mapping[str, object],
    *,
    expected_source_commit: str,
    expected_runtime: Mapping[str, object],
) -> list[str]:
    launcher_identity = _mapping(codex_mcp.get("launcher_identity"))
    workflow_exchange = _mapping(codex_mcp.get("workflow_exchange"))
    checks = (
        ("not_configured", codex_mcp.get("configured") is True),
        (
            "registration_unverified",
            codex_mcp.get("registration_verified") is True,
        ),
        (
            "runtime_launcher_unverified",
            codex_mcp.get("runtime_launcher_verified") is True,
        ),
        (
            "runtime_invocation_invalid",
            codex_mcp.get("runtime_invocation")
            == "installed-runtime-launcher-protocol",
        ),
        (
            "protocol_exchange_invalid",
            codex_mcp.get("protocol_exchange")
            == ["initialize", "tools/list", "tools/call:execute"],
        ),
        ("agent_tools_invalid", codex_mcp.get("tools") == ["execute", "observe"]),
        ("identity_unbound", codex_mcp.get("identity_bound") is True),
        (
            "source_binding_invalid",
            codex_mcp.get("installed_source_commit") == expected_source_commit,
        ),
        (
            "runtime_api_mismatch",
            codex_mcp.get("runtime_api") == expected_runtime.get("api_version"),
        ),
        (
            "runtime_digest_mismatch",
            codex_mcp.get("runtime_content_digest")
            == expected_runtime.get("content_digest"),
        ),
        (
            "launcher_state_unverified",
            codex_mcp.get("launcher_state_verified") is True,
        ),
        ("restart_unverified", codex_mcp.get("restart_verified") is True),
    )
    return [
        *[name for name, passed in checks if not passed],
        *_codex_process_failures(codex_mcp),
        *_mcp_lifecycle_failures(
            codex_mcp.get("mcp_lifecycle_records"),
            expected_source_commit=expected_source_commit,
        ),
        *_mcp_closeout_failures(
            codex_mcp.get("mcp_closeout"),
            codex_mcp.get("mcp_lifecycle_records"),
        ),
        *_launcher_identity_failures(
            launcher_identity,
            codex_mcp.get("launcher_identity_digest", ""),
            expected_source_commit=expected_source_commit,
            expected_runtime=expected_runtime,
        ),
        *_workflow_exchange_failures(workflow_exchange),
    ]


def product_client_failures(
    evidence: Mapping[str, object],
    *,
    contract: Mapping[str, object],
    expected_source_commit: str,
    expected_release: Mapping[str, object],
) -> list[str]:
    return [
        *installation_identity_failures(
            evidence,
            expected_source_commit=expected_source_commit,
            expected_release=expected_release,
        ),
        *codex_mcp_failures(
            evidence,
            contract=contract,
            expected_source_commit=expected_source_commit,
            expected_runtime=_mapping(expected_release.get("runtime")),
        ),
    ]


def projection_dimension_failures(
    projection: Mapping[str, object],
) -> list[str]:
    """Validate one Codex projection dimension without duplicating policy."""

    sizes = tuple(
        projection.get(name)
        for name in ("full_bytes", "reference_bytes", "saved_bytes")
    )
    causes = projection.get("target_exceeded_causes")
    normalized_causes = causes if isinstance(causes, list) else []
    checks = (
        ("projection_not_passed", projection.get("status") == "passed"),
        (
            "projection_correctness_missing",
            projection.get("correctness_primary") is True,
        ),
        (
            "projection_reference_missing",
            projection.get("repeated_reference") is True,
        ),
        (
            "projection_operator_coverage_missing",
            projection.get("operator_projection_covered") is True,
        ),
        (
            "projection_repeated_fields_invalid",
            projection.get("repeated_fields") == ["diagnostic_receipt"],
        ),
        (
            "projection_sizes_invalid",
            all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in sizes
            )
            and projection.get("full_bytes", 0)
            >= projection.get("reference_bytes", 0),
        ),
        (
            "projection_target_causes_invalid",
            bool(normalized_causes)
            and all(
                isinstance(cause, Mapping)
                and bool(str(cause.get("field", "")).strip())
                and isinstance(cause.get("bytes"), int)
                and not isinstance(cause.get("bytes"), bool)
                and int(cause.get("bytes", 0)) > 0
                for cause in normalized_causes
            ),
        ),
    )
    return [name for name, passed in checks if not passed]


def verify_codex_adoption_report(
    report: Mapping[str, object],
    *,
    expected_source_commit: str | None = None,
    require_ready: bool = False,
) -> None:
    if report.get("schema") != SCHEMA:
        raise ValueError("Codex Adoption Qualification schema is invalid")
    source_commit = str(report.get("source_commit", ""))
    if not _full_commit(source_commit):
        raise ValueError("Codex Adoption Qualification source commit is invalid")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise ValueError("Codex Adoption Qualification source commit does not match")
    expected_digest = report.get("evidence_digest")
    unsigned = dict(report)
    unsigned.pop("evidence_digest", None)
    if expected_digest != evidence_fingerprint(unsigned):
        raise ValueError("Codex Adoption Qualification evidence digest is invalid")
    dimensions = report.get("dimensions")
    if not isinstance(dimensions, Mapping) or set(dimensions) != set(DIMENSION_ORDER):
        raise ValueError("Codex Adoption Qualification dimensions are incomplete")
    failed_dimensions = []
    for name in DIMENSION_ORDER:
        value = dimensions.get(name)
        if not isinstance(value, Mapping) or value.get("status") not in {
            "passed",
            "failed",
        }:
            raise ValueError(
                f"Codex Adoption Qualification {name} dimension is invalid"
            )
        if value.get("status") != "passed":
            failed_dimensions.append(name)
    installation = _mapping(dimensions.get("installation_identity"))
    installation_contract_failures = installation_dimension_failures(
        installation,
        expected_source_commit=source_commit,
    )
    if installation.get("status") == "passed" and (
        installation.get("failure_codes") != []
        or installation_contract_failures
    ):
        raise ValueError(
            "Codex Adoption Qualification installation identity is incomplete"
        )
    if installation.get("status") == "failed" and not installation.get(
        "failure_codes"
    ):
        raise ValueError(
            "Codex Adoption Qualification installation failure is unexplained"
        )
    codex_mcp = _mapping(dimensions.get("codex_mcp"))
    expected_runtime = {
        "api_version": installation.get("runtime_api"),
        "content_digest": installation.get("runtime_content_digest"),
    }
    codex_mcp_contract_failures = codex_mcp_dimension_failures(
        codex_mcp,
        expected_source_commit=source_commit,
        expected_runtime=expected_runtime,
    )
    if codex_mcp.get("status") == "passed" and (
        codex_mcp.get("failure_codes") != []
        or codex_mcp_contract_failures
    ):
        raise ValueError("Codex Adoption Qualification MCP evidence is incomplete")
    if codex_mcp.get("status") == "failed" and not codex_mcp.get(
        "failure_codes"
    ):
        raise ValueError("Codex Adoption Qualification MCP failure is unexplained")
    product_contract = _mapping(dimensions.get("product_contract"))
    if product_contract.get("status") == "passed" and not (
        product_contract.get("status") == "passed"
        and product_contract.get("returncode") == 0
        and isinstance(product_contract.get("tests"), list)
        and bool(product_contract.get("tests"))
    ):
        raise ValueError(
            "Codex Adoption Qualification product contract evidence is incomplete"
        )
    task_matrix = _mapping(dimensions.get("task_matrix"))
    risk_controls = _mapping(task_matrix.get("risk_controls"))
    if task_matrix.get("status") == "passed" and not all(
        (
            task_matrix.get("correctness_primary") is True,
            task_matrix.get("completion_primary") is True,
            task_matrix.get("terminal_contract_primary") is True,
            isinstance(task_matrix.get("groups"), Mapping),
            bool(task_matrix.get("groups")),
            risk_controls_valid(risk_controls),
        )
    ):
        raise ValueError(
            "Codex Adoption Qualification task matrix evidence is incomplete"
        )
    projection = _mapping(dimensions.get("projection"))
    if (
        projection.get("status") == "passed"
        and projection_dimension_failures(projection)
    ):
        raise ValueError(
            "Codex Adoption Qualification projection evidence is incomplete"
        )
    lifecycle = _mapping(dimensions.get("lifecycle"))
    lifecycle_closeout = _mapping(lifecycle.get("closeout"))
    lifecycle_summary = _mapping(lifecycle_closeout.get("summary"))
    lifecycle_checks = _mapping(lifecycle_closeout.get("closeout_checks"))
    if lifecycle.get("status") == "passed" and not all(
        (
            lifecycle_closeout.get("status") == "passed",
            lifecycle_closeout.get("task_closeout_ready") is True,
            lifecycle_closeout.get("identity_records_valid") is True,
            lifecycle_closeout.get("isolation_verified") is True,
            lifecycle_summary.get("active_requests") == 0,
            lifecycle_summary.get("live_processes") == 0,
            lifecycle_summary.get("confirmed_live_orphans") == 0,
            lifecycle_summary.get("unattributed_live_processes") == 0,
            lifecycle_summary.get("owned_live_processes") == 0,
            lifecycle_checks.get("active_requests_zero") is True,
            lifecycle_checks.get("confirmed_live_orphans_zero") is True,
            lifecycle_checks.get("unattributed_live_processes_zero") is True,
            lifecycle_checks.get("owned_live_processes_zero") is True,
        )
    ):
        raise ValueError(
            "Codex Adoption Qualification lifecycle evidence is incomplete"
        )
    provenance = _mapping(report.get("provenance"))
    source_provenance = _mapping(provenance.get("source"))
    if not all(
        (
            source_provenance.get("commit") == source_commit,
            _full_commit(source_provenance.get("qualification_commit")),
            _sha256(source_provenance.get("continuous_closeout_digest")),
            "model" in provenance,
            "codex" in provenance,
        )
    ):
        raise ValueError(
            "Codex Adoption Qualification provenance evidence is incomplete"
        )
    external = _mapping(report.get("external_evaluation"))
    if not (
        external.get("blocking") is False
        and external.get("required_for_maintenance_checkpoint") is False
        and isinstance(external.get("harnesses"), list)
    ):
        raise ValueError(
            "Codex Adoption Qualification external evaluation binding is invalid"
        )
    if report.get("failed_dimensions") != failed_dimensions:
        raise ValueError("Codex Adoption Qualification failed dimensions are invalid")
    qualified = report.get("qualified") is True
    if qualified != (not failed_dimensions):
        raise ValueError("Codex Adoption Qualification decision is inconsistent")
    blockers = report.get("maintenance_checkpoint_blockers")
    if not isinstance(blockers, list) or any(
        not isinstance(item, str) or not item for item in blockers
    ):
        raise ValueError("Codex Adoption Qualification blockers are invalid")
    if blockers != failed_dimensions:
        raise ValueError(
            "Codex Adoption Qualification checkpoint blockers are invalid"
        )
    checkpoint_ready = report.get("maintenance_checkpoint_ready") is True
    if checkpoint_ready != (not blockers):
        raise ValueError("Codex Adoption Qualification checkpoint is inconsistent")
    release_gate = report.get("release_gate")
    if (
        not isinstance(release_gate, Mapping)
        or release_gate.get("evidence_type") != "codex-adoption-qualification"
        or (release_gate.get("eligible") is True) != checkpoint_ready
    ):
        raise ValueError("Codex Adoption Qualification Release Gate binding is invalid")
    if require_ready and not (qualified and checkpoint_ready):
        raise ValueError("Codex Adoption Qualification is not Release Gate eligible")
