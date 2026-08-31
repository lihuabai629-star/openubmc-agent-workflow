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
from scripts.continuous_closeout_qualification import (  # noqa: E402
    qualify as qualify_closeout,
)
from scripts.evidence_report import evidence_fingerprint  # noqa: E402


SCHEMA = "openubmc-agent-workflow.codex-adoption-qualification.v1"
DIMENSION_ORDER = (
    "installation_identity",
    "codex_mcp",
    "product_contract",
    "task_matrix",
    "projection",
    "lifecycle",
)


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _normalized_identity(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ValueError("identity must be a JSON object")
    return decoded


def _passed(value: Mapping[str, object]) -> bool:
    return value.get("status") == "passed"


def qualify(
    *,
    source_commit: str | None = None,
    model_identity: Mapping[str, object] | None = None,
    codex_identity: Mapping[str, object] | None = None,
) -> dict[str, object]:
    closeout = qualify_closeout()
    qualification_commit = str(closeout.get("source_commit", ""))
    selected_source_commit = source_commit or qualification_commit
    release = build_release_lock(ROOT, source_commit=selected_source_commit)

    compatibility = _mapping(release.get("compatibility"))
    release_clients = _mapping(compatibility.get("clients"))
    runtime = _mapping(release.get("runtime"))
    source_clean = closeout.get("source_clean") is True
    installation_passed = all(
        (
            source_clean,
            release.get("source_commit") == selected_source_commit,
            list(release_clients) == ["codex"],
            isinstance(release.get("lock_digest"), str),
            isinstance(release.get("source_tree_digest"), str),
            isinstance(release.get("workflow_digest"), str),
            runtime.get("api_version") == "openubmc.target-runtime.v1",
            isinstance(runtime.get("content_digest"), str),
        )
    )
    installation_identity = {
        "status": "passed" if installation_passed else "failed",
        "source_clean": source_clean,
        "release_version": str(release.get("release_version", "")),
        "source_commit": selected_source_commit,
        "lock_digest": str(release.get("lock_digest", "")),
        "source_tree_digest": str(release.get("source_tree_digest", "")),
        "workflow_digest": str(release.get("workflow_digest", "")),
        "clients": list(release_clients),
        "runtime_api": str(runtime.get("api_version", "")),
        "runtime_content_digest": str(runtime.get("content_digest", "")),
    }

    client_matrix = _mapping(closeout.get("client_matrix"))
    runs = _mapping(client_matrix.get("runs"))
    codex_run = _mapping(runs.get("codex"))
    codex_mcp_passed = all(
        (
            client_matrix.get("status") == "passed",
            client_matrix.get("product_clients") == ["codex"],
            client_matrix.get("overlap") == [],
            codex_run.get("status") == "passed",
            codex_run.get("client") == "codex",
            codex_run.get("adapter_available") is True,
            codex_run.get("support_mode") == "skills-and-runtime-mcp",
            codex_run.get("declared_mcp") is True,
            codex_run.get("mcp_registration_verified") is True,
            codex_run.get("runtime_launcher_verified") is True,
            codex_run.get("runtime_invocation")
            == "client-configured-mcp-command",
            codex_run.get("protocol_exchange") == ["initialize", "tools/list"],
            codex_run.get("tools") == ["execute", "observe"],
        )
    )
    codex_mcp = {
        "status": "passed" if codex_mcp_passed else "failed",
        "configured": codex_run.get("declared_mcp") is True,
        "registration_verified": codex_run.get("mcp_registration_verified")
        is True,
        "runtime_launcher_verified": codex_run.get("runtime_launcher_verified")
        is True,
        "runtime_invocation": str(codex_run.get("runtime_invocation", "")),
        "protocol_exchange": list(codex_run.get("protocol_exchange", [])),
        "tools": list(codex_run.get("tools", [])),
    }

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
    report: dict[str, object] = {
        "schema": SCHEMA,
        "source_commit": selected_source_commit,
        "qualified": qualified,
        "maintenance_checkpoint_ready": qualified,
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
            "model": _normalized_identity(model_identity),
            "codex": _normalized_identity(codex_identity),
        },
        "external_evaluation": {
            "blocking": False,
            "harnesses": list(client_matrix.get("evaluation_harnesses", [])),
            "isolation": _mapping(closeout.get("evaluation_isolation")),
        },
        "release_gate": {
            "evidence_type": "codex-adoption-qualification",
            "eligible": qualified,
        },
    }
    report["evidence_digest"] = evidence_fingerprint(report)
    return report


def _identity_argument(value: str) -> dict[str, object]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("identity must be valid JSON") from exc
    if not isinstance(document, dict):
        raise argparse.ArgumentTypeError("identity must be a JSON object")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-identity", type=_identity_argument)
    parser.add_argument("--codex-identity", type=_identity_argument)
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
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
