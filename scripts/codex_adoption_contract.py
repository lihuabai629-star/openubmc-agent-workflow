"""Shared validation for Codex product-client and adoption evidence."""

from __future__ import annotations

from collections.abc import Mapping

from scripts.evidence_report import evidence_fingerprint


SCHEMA = "openubmc-agent-workflow.codex-adoption-qualification.v1"
DIMENSION_ORDER = (
    "installation_identity",
    "codex_mcp",
    "product_contract",
    "task_matrix",
    "projection",
    "lifecycle",
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
        and isinstance(record.get("exit_reason"), str)
        and bool(str(record.get("exit_reason", "")).strip())
        for record in records
    )
    return [] if valid else ["mcp_lifecycle_identity_invalid"]


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
            evidence.get("runtime_invocation") == "client-configured-mcp-command",
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
    )
    return [
        *[name for name, passed in checks if not passed],
        *_mcp_lifecycle_failures(
            evidence.get("mcp_lifecycle_records"),
            expected_source_commit=expected_source_commit,
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
            == "client-configured-mcp-command",
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
    )
    return [
        *[name for name, passed in checks if not passed],
        *_mcp_lifecycle_failures(
            codex_mcp.get("mcp_lifecycle_records"),
            expected_source_commit=expected_source_commit,
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
    if task_matrix.get("status") == "passed" and not all(
        (
            task_matrix.get("correctness_primary") is True,
            task_matrix.get("completion_primary") is True,
            task_matrix.get("terminal_contract_primary") is True,
            isinstance(task_matrix.get("groups"), Mapping),
            bool(task_matrix.get("groups")),
        )
    ):
        raise ValueError(
            "Codex Adoption Qualification task matrix evidence is incomplete"
        )
    projection = _mapping(dimensions.get("projection"))
    projection_sizes = tuple(
        projection.get(name)
        for name in ("full_bytes", "reference_bytes", "saved_bytes")
    )
    if projection.get("status") == "passed" and not all(
        (
            projection.get("correctness_primary") is True,
            projection.get("repeated_reference") is True,
            projection.get("operator_projection_covered") is True,
            all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in projection_sizes
            ),
            projection.get("full_bytes", 0)
            >= projection.get("reference_bytes", 0),
        )
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
