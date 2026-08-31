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


def codex_mcp_failures(
    evidence: Mapping[str, object],
    *,
    contract: Mapping[str, object],
    expected_source_commit: str,
    expected_runtime: Mapping[str, object],
) -> list[str]:
    workflow_exchange = _mapping(evidence.get("workflow_exchange"))
    launcher_sha256 = str(evidence.get("launcher_sha256", ""))
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
            "launcher_digest_invalid",
            len(launcher_sha256) == 64
            and all(
                character in "0123456789abcdef" for character in launcher_sha256
            ),
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
        ("workflow_tool_invalid", workflow_exchange.get("tool") == "execute"),
        (
            "workflow_state_invalid",
            workflow_exchange.get("state") == "preflight_failed",
        ),
        (
            "workflow_classification_invalid",
            workflow_exchange.get("classification") == "preflight_failure",
        ),
        ("workflow_error_field_invalid", workflow_exchange.get("error_field") == "run_id"),
        (
            "workflow_retry_invalid",
            _mapping(workflow_exchange.get("canonical_retry")).get("kind")
            == "resume"
            and bool(
                _mapping(workflow_exchange.get("canonical_retry")).get("run_id")
            ),
        ),
        ("workflow_error_contract_invalid", workflow_exchange.get("is_error") is True),
    )
    return [name for name, passed in checks if not passed]


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
