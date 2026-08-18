"""Deterministic, redacted Case replay bundles and offline validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json

from .closeout import AcceptancePlan
from .context_runtime import CaseNotFound, RuntimeRepository, project_case
from .contracts import RUNTIME_API_VERSION
from .redaction import is_secret_key, redact_text
from .workflow import WorkflowDefinition


CASE_REPLAY_BUNDLE_SCHEMA = f"{RUNTIME_API_VERSION}/case-replay-bundle-v1"
CASE_REPLAY_RESULT_SCHEMA = f"{RUNTIME_API_VERSION}/case-replay-result-v1"
CASE_REPLAY_BUNDLE_VERSION = 1


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def redact_replay_value(value: object) -> object:
    """Remove credential material while preserving deterministic Case facts."""

    if isinstance(value, Mapping):
        public: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            if name == "_credential_values" or is_secret_key(name):
                continue
            public[name] = redact_replay_value(item)
        return public
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [redact_replay_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)


@dataclass(frozen=True)
class CaseReplayBundle:
    case_id: str
    workflow_definition: Mapping[str, object]
    events: tuple[Mapping[str, object], ...]
    receipts: tuple[Mapping[str, object], ...]
    evidence_metadata: tuple[Mapping[str, object], ...]
    target_epochs: Mapping[str, int]
    acceptance_plan: Mapping[str, object]
    expected_outcome: Mapping[str, object]
    fingerprint: str
    runtime_api_version: str = RUNTIME_API_VERSION
    bundle_version: int = CASE_REPLAY_BUNDLE_VERSION

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        workflow_definition: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        receipts: Sequence[Mapping[str, object]],
        evidence_metadata: Sequence[Mapping[str, object]],
        target_epochs: Mapping[str, int],
        acceptance_plan: Mapping[str, object],
        expected_outcome: Mapping[str, object],
        runtime_api_version: str = RUNTIME_API_VERSION,
        bundle_version: int = CASE_REPLAY_BUNDLE_VERSION,
    ) -> "CaseReplayBundle":
        if not str(case_id).strip():
            raise ValueError("replay bundle case_id is required")
        if runtime_api_version != RUNTIME_API_VERSION:
            raise ValueError(
                f"replay bundle Runtime {runtime_api_version} is incompatible with "
                f"{RUNTIME_API_VERSION}"
            )
        if bundle_version != CASE_REPLAY_BUNDLE_VERSION:
            raise ValueError(f"unsupported replay bundle version: {bundle_version}")
        public = {
            "schema": CASE_REPLAY_BUNDLE_SCHEMA,
            "bundle_version": bundle_version,
            "runtime_api_version": runtime_api_version,
            "case_id": str(case_id),
            "workflow_definition": redact_replay_value(workflow_definition),
            "events": redact_replay_value(events),
            "receipts": redact_replay_value(receipts),
            "evidence_metadata": redact_replay_value(evidence_metadata),
            "target_epochs": redact_replay_value(target_epochs),
            "acceptance_plan": redact_replay_value(acceptance_plan),
            "expected_outcome": redact_replay_value(expected_outcome),
        }
        return cls(
            case_id=str(case_id),
            workflow_definition=dict(public["workflow_definition"]),
            events=tuple(dict(item) for item in public["events"]),
            receipts=tuple(dict(item) for item in public["receipts"]),
            evidence_metadata=tuple(
                dict(item) for item in public["evidence_metadata"]
            ),
            target_epochs={
                str(key): int(value)
                for key, value in dict(public["target_epochs"]).items()
            },
            acceptance_plan=dict(public["acceptance_plan"]),
            expected_outcome=dict(public["expected_outcome"]),
            fingerprint=_fingerprint(public),
            runtime_api_version=runtime_api_version,
            bundle_version=bundle_version,
        )

    def _identity(self) -> dict[str, object]:
        return {
            "schema": CASE_REPLAY_BUNDLE_SCHEMA,
            "bundle_version": self.bundle_version,
            "runtime_api_version": self.runtime_api_version,
            "case_id": self.case_id,
            "workflow_definition": dict(self.workflow_definition),
            "events": [dict(item) for item in self.events],
            "receipts": [dict(item) for item in self.receipts],
            "evidence_metadata": [dict(item) for item in self.evidence_metadata],
            "target_epochs": dict(self.target_epochs),
            "acceptance_plan": dict(self.acceptance_plan),
            "expected_outcome": dict(self.expected_outcome),
        }

    def to_public_dict(self) -> dict[str, object]:
        return {**self._identity(), "fingerprint": self.fingerprint}

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "CaseReplayBundle":
        if value.get("schema") != CASE_REPLAY_BUNDLE_SCHEMA:
            raise ValueError("unsupported Case Replay Bundle schema")
        for name in ("events", "receipts", "evidence_metadata"):
            raw = value.get(name)
            if not isinstance(raw, list) or not all(
                isinstance(item, Mapping) for item in raw
            ):
                raise ValueError(f"replay bundle {name} must be an array of objects")
        for name in (
            "workflow_definition",
            "target_epochs",
            "acceptance_plan",
            "expected_outcome",
        ):
            if not isinstance(value.get(name), Mapping):
                raise ValueError(f"replay bundle {name} must be an object")
        bundle = cls.create(
            case_id=str(value.get("case_id", "")),
            workflow_definition=value["workflow_definition"],
            events=value["events"],
            receipts=value["receipts"],
            evidence_metadata=value["evidence_metadata"],
            target_epochs=value["target_epochs"],
            acceptance_plan=value["acceptance_plan"],
            expected_outcome=value["expected_outcome"],
            runtime_api_version=str(value.get("runtime_api_version", "")),
            bundle_version=int(value.get("bundle_version", 0)),
        )
        if str(value.get("fingerprint", "")) != bundle.fingerprint:
            raise ValueError("Case Replay Bundle fingerprint mismatch")
        return bundle


@dataclass(frozen=True)
class CaseReplayResult:
    status: str
    outcome: Mapping[str, object]
    findings: tuple[Mapping[str, object], ...]
    bundle_fingerprint: str
    result_fingerprint: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": CASE_REPLAY_RESULT_SCHEMA,
            "status": self.status,
            "outcome": dict(self.outcome),
            "findings": [dict(item) for item in self.findings],
            "bundle_fingerprint": self.bundle_fingerprint,
            "result_fingerprint": self.result_fingerprint,
        }


def _expected_outcome(projection: Mapping[str, object]) -> dict[str, object]:
    closeout = projection.get("closeout")
    if isinstance(closeout, Mapping) and closeout:
        return {
            "status": str(closeout.get("closure_status", "")),
            "claim_level": str(closeout.get("claim_level", "")),
            "business_acceptance": str(
                closeout.get("business_acceptance", "")
            ),
        }
    unknown = projection.get("mutation_outcome_unknown_operations")
    if isinstance(unknown, Mapping) and unknown:
        return {"status": "mutation_outcome_unknown"}
    return {"status": str(projection.get("status", "open"))}


def _target_epochs(projection: Mapping[str, object]) -> dict[str, int]:
    epochs: dict[str, int] = {}
    floors = projection.get("target_epoch_floors")
    if isinstance(floors, Mapping):
        for target_id, value in floors.items():
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                epochs[str(target_id)] = value
    for operation in projection.get("operations", []):
        if not isinstance(operation, Mapping):
            continue
        target_id = str(operation.get("target_id", ""))
        value = operation.get("target_epoch")
        if (
            target_id
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            epochs[target_id] = max(epochs.get(target_id, 0), value)
    return epochs


class CaseReplayService:
    """Export and replay Cases without domain adapters or external effects."""

    def __init__(self, repository: RuntimeRepository) -> None:
        self.repository = repository

    def export(self, case_id: str) -> CaseReplayBundle:
        projection = self.repository.load(case_id)
        if projection is None:
            raise CaseNotFound(case_id)
        definition = projection.get("workflow_definition")
        if not isinstance(definition, Mapping) or not definition:
            raise ValueError("Case has no versioned WorkflowDefinition")
        acceptance = projection.get("acceptance_plan")
        if not isinstance(acceptance, Mapping) or not acceptance:
            raise ValueError("Case has no frozen AcceptancePlan")
        closeout = projection.get("closeout")
        receipts = (
            closeout.get("receipts", [])
            if isinstance(closeout, Mapping)
            else []
        )
        if not isinstance(receipts, list):
            receipts = []
        return CaseReplayBundle.create(
            case_id=case_id,
            workflow_definition=definition,
            events=self.repository.events(case_id),
            receipts=[item for item in receipts if isinstance(item, Mapping)],
            evidence_metadata=self.repository.evidence_references(case_id),
            target_epochs=_target_epochs(projection),
            acceptance_plan=acceptance,
            expected_outcome=_expected_outcome(projection),
        )

    @staticmethod
    def replay(bundle: CaseReplayBundle | Mapping[str, object]) -> CaseReplayResult:
        selected = (
            bundle
            if isinstance(bundle, CaseReplayBundle)
            else CaseReplayBundle.from_public_dict(bundle)
        )
        findings: list[dict[str, object]] = []

        try:
            definition = WorkflowDefinition.from_public_dict(
                selected.workflow_definition
            )
        except (TypeError, ValueError) as exc:
            definition = None
            findings.append(
                {
                    "code": "workflow_plan_drift",
                    "severity": "error",
                    "message": redact_text(exc),
                }
            )

        try:
            plan = AcceptancePlan.from_public_dict(selected.acceptance_plan)
        except (TypeError, ValueError) as exc:
            plan = None
            findings.append(
                {
                    "code": "acceptance_chain_break",
                    "severity": "error",
                    "message": redact_text(exc),
                }
            )

        projection = project_case(selected.case_id, selected.events)
        recorded_definition = projection.get("workflow_definition")
        if (
            definition is not None
            and isinstance(recorded_definition, Mapping)
            and str(recorded_definition.get("fingerprint", ""))
            != definition.fingerprint
        ):
            findings.append(
                {
                    "code": "workflow_plan_drift",
                    "severity": "error",
                    "message": "event WorkflowDefinition differs from replay bundle",
                }
            )
        recorded_plan = projection.get("acceptance_plan")
        if (
            plan is not None
            and isinstance(recorded_plan, Mapping)
            and str(recorded_plan.get("plan_id", "")) != plan.plan_id
        ):
            findings.append(
                {
                    "code": "acceptance_chain_break",
                    "severity": "error",
                    "message": "event AcceptancePlan differs from replay bundle",
                }
            )

        closeout = projection.get("closeout")
        if plan is not None and isinstance(closeout, Mapping) and closeout:
            checks = closeout.get("checks", [])
            checks = checks if isinstance(checks, list) else []
            checked_requirements = {
                str(item.get("requirement_id", ""))
                for item in checks
                if isinstance(item, Mapping)
                and str(item.get("requirement_id", ""))
            }
            missing_requirements = sorted(
                requirement.requirement_id
                for requirement in plan.requirements
                if requirement.criticality == "required"
                and requirement.requirement_id not in checked_requirements
            )
            receipt_ids = {
                str(item.get("receipt_id", ""))
                for item in selected.receipts
                if str(item.get("receipt_id", ""))
            }
            dangling_receipts = sorted(
                {
                    str(item.get("source_receipt_id", ""))
                    for item in checks
                    if isinstance(item, Mapping)
                    and str(item.get("source_receipt_id", ""))
                    and str(item.get("source_receipt_id", "")) not in receipt_ids
                }
            )
            if missing_requirements or dangling_receipts:
                findings.append(
                    {
                        "code": "acceptance_chain_break",
                        "severity": "error",
                        "message": "Closeout acceptance checks do not link to the frozen plan and receipts",
                        "missing_requirement_ids": missing_requirements,
                        "dangling_receipt_ids": dangling_receipts,
                    }
                )

        evidence_ids = {
            str(item.get("evidence_id", ""))
            for item in selected.evidence_metadata
            if str(item.get("evidence_id", ""))
        }
        attached_ids = {
            str(reference.get("evidence_id", ""))
            for event in selected.events
            if event.get("kind") == "EvidenceAttached"
            for payload in [event.get("payload", {})]
            if isinstance(payload, Mapping)
            for reference in [payload.get("evidence", {})]
            if isinstance(reference, Mapping)
            and str(reference.get("evidence_id", ""))
        }
        missing_evidence = sorted(attached_ids - evidence_ids)
        if missing_evidence:
            findings.append(
                {
                    "code": "evidence_index_gap",
                    "severity": "error",
                    "message": "Replay Bundle omits indexed Evidence metadata",
                    "evidence_ids": missing_evidence,
                }
            )
        elif projection.get("projection_truncated") is True and attached_ids:
            findings.append(
                {
                    "code": "evidence_projection_truncated_preserved",
                    "severity": "info",
                    "message": "full Evidence metadata survived bounded projection",
                    "evidence_count": len(attached_ids),
                }
            )

        executions: dict[str, tuple[object, ...]] = {}
        accepted: dict[str, Mapping[str, object]] = {}
        for event in selected.events:
            payload = event.get("payload", {})
            payload = payload if isinstance(payload, Mapping) else {}
            operation_id = str(event.get("operation_id", ""))
            if event.get("kind") == "OperationAccepted":
                accepted[operation_id] = payload
                execution_id = str(payload.get("workflow_execution_id", ""))
                if execution_id:
                    identity = (
                        payload.get("workflow_definition_fingerprint"),
                        payload.get("workflow_input_fingerprint"),
                        payload.get("workflow_target_epoch"),
                        payload.get("workflow_attempt"),
                        payload.get("target_version"),
                        payload.get("target_id"),
                    )
                    previous = executions.get(execution_id)
                    if previous is not None and previous != identity:
                        findings.append(
                            {
                                "code": "stale_receipt_reuse",
                                "severity": "error",
                                "message": "one workflow execution identity has conflicting inputs",
                                "workflow_execution_id": execution_id,
                            }
                        )
                    executions[execution_id] = identity
            elif event.get("kind") in {"OperationTerminal", "OperationReconciled"}:
                original = accepted.get(operation_id, {})
                minimum = original.get("workflow_target_epoch")
                observed = payload.get("target_epoch")
                if (
                    isinstance(minimum, int)
                    and not isinstance(minimum, bool)
                    and isinstance(observed, int)
                    and not isinstance(observed, bool)
                    and observed < minimum
                ):
                    findings.append(
                        {
                            "code": "stale_receipt_reuse",
                            "severity": "error",
                            "message": "operation receipt predates its required target epoch",
                            "operation_id": operation_id,
                        }
                    )

        actual_outcome = _expected_outcome(projection)
        if actual_outcome != dict(selected.expected_outcome):
            findings.append(
                {
                    "code": "outcome_mismatch",
                    "severity": "error",
                    "message": "replayed outcome differs from expected outcome",
                    "expected": dict(selected.expected_outcome),
                    "actual": actual_outcome,
                }
            )
        if actual_outcome.get("status") == "mutation_outcome_unknown":
            findings.append(
                {
                    "code": "upgrade_outcome_unknown",
                    "severity": "info",
                    "message": "unknown mutation outcome remains blocked for reconciliation",
                }
            )

        status = (
            "failed"
            if any(item.get("severity") == "error" for item in findings)
            else "passed"
        )
        result_identity = {
            "schema": CASE_REPLAY_RESULT_SCHEMA,
            "status": status,
            "outcome": actual_outcome,
            "findings": findings,
            "bundle_fingerprint": selected.fingerprint,
        }
        return CaseReplayResult(
            status=status,
            outcome=actual_outcome,
            findings=tuple(findings),
            bundle_fingerprint=selected.fingerprint,
            result_fingerprint=_fingerprint(result_identity),
        )
