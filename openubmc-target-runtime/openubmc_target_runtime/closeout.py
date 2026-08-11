"""Case-native acceptance planning and deterministic closeout derivation.

The Case event stream remains the only workflow fact source.  This module owns
the immutable acceptance contract and pure projections built from those facts;
it does not maintain a second workflow state machine.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json

from .contracts import RUNTIME_API_VERSION
from .mutation import TaskAuthorizationPolicy, mutation_journal_operation_status
from .redaction import is_secret_key, redact_text


ACCEPTANCE_PLAN_SCHEMA = f"{RUNTIME_API_VERSION}/acceptance-plan"
ACCEPTANCE_REQUIREMENT_SCHEMA = f"{RUNTIME_API_VERSION}/acceptance-requirement"
STAGE_RECEIPT_SCHEMA = f"{RUNTIME_API_VERSION}/case-stage-receipt"
CLOSEOUT_CHECK_SCHEMA = f"{RUNTIME_API_VERSION}/case-closeout-check"
CASE_CLOSEOUT_SCHEMA = f"{RUNTIME_API_VERSION}/case-closeout"
CLOSEOUT_BUNDLE_SCHEMA = f"{RUNTIME_API_VERSION}/case-closeout-bundle"
MAX_CLOSEOUT_TEXT_BYTES = 4096
MAX_CLOSEOUT_COLLECTION_ITEMS = 64
MAX_CLOSEOUT_MARKDOWN_BYTES = 16_384

def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _safe_text(value: object, *, limit: int = MAX_CLOSEOUT_TEXT_BYTES) -> str:
    text = redact_text(value).strip()
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        suffix = "…"
        available = max(0, limit - len(suffix.encode("utf-8")))
        text = encoded[:available].decode("utf-8", errors="ignore") + suffix
    return text


def _bounded_public(value: object, *, depth: int = 0) -> object:
    """Bound and redact nested report facts before they enter durable Closeout."""

    if depth >= 6:
        return "<depth-limited>"
    if isinstance(value, Mapping):
        bounded: dict[str, object] = {}
        for key, item in list(value.items())[:MAX_CLOSEOUT_COLLECTION_ITEMS]:
            name = _safe_text(key, limit=256)
            if is_secret_key(name):
                continue
            bounded[name] = _bounded_public(item, depth=depth + 1)
        return bounded
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _bounded_public(item, depth=depth + 1)
            for item in list(value)[:MAX_CLOSEOUT_COLLECTION_ITEMS]
        ]
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value)


def _truncate_utf8(value: str, *, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = "\n\n> 报告内容已按公共输出上限截断；完整阶段数据请通过 evidence_read 读取。\n"
    suffix_bytes = suffix.encode("utf-8")
    available = max(0, limit - len(suffix_bytes))
    return encoded[:available].decode("utf-8", errors="ignore").rstrip() + suffix


@dataclass(frozen=True)
class AcceptanceRequirement:
    """One immutable acceptance gate derived before workflow execution."""

    requirement_id: str
    title: str
    stage: str
    criticality: str = "required"

    def __post_init__(self) -> None:
        if not self.requirement_id or not self.requirement_id.startswith("stage."):
            raise ValueError("stage requirement_id must start with stage.")
        if not self.title.strip():
            raise ValueError("acceptance requirement title is required")
        if not self.stage.strip():
            raise ValueError("acceptance requirement stage is required")
        if self.criticality not in {"required", "optional"}:
            raise ValueError("acceptance criticality must be required or optional")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": ACCEPTANCE_REQUIREMENT_SCHEMA,
            "requirement_id": self.requirement_id,
            "title": self.title,
            "stage": self.stage,
            "criticality": self.criticality,
        }

    @classmethod
    def from_public_dict(
        cls, value: Mapping[str, object]
    ) -> "AcceptanceRequirement":
        return cls(
            requirement_id=str(value.get("requirement_id", "")),
            title=str(value.get("title", "")),
            stage=str(value.get("stage", "")),
            criticality=str(value.get("criticality", "required")),
        )


_STAGE_TITLES = {
    "bundle": "诊断日志包已采集",
    "diagnosis": "问题已完成定位并保留证据",
    "development": "源码修复和方案记录已完成",
    "build": "构建产物及其身份已确认",
    "live_patch": "Live Patch 已应用并完成内部验证",
    "upgrade": "固件已升级并完成版本验证",
    "verification": "变更后的运行态回归验证已通过",
}


def _required_stages(
    intent: str,
    delivery_strategy: str,
    entry_domain: str = "",
) -> tuple[str, ...]:
    intent = str(intent).strip().lower().replace("_", "-")
    delivery_strategy = (
        str(delivery_strategy).strip().lower().replace("_", "-")
    )
    entry_domain = str(entry_domain).strip().lower().replace("-", "_")
    if intent == "bundle-and-diagnose":
        return ("bundle", "diagnosis")
    if intent in {"live-patch", "rollback"}:
        return ("live_patch", "verification")
    if intent == "upgrade-and-verify":
        return ("upgrade", "verification")
    if intent == "diagnose-and-fix":
        stages = ["diagnosis", "development"]
        if delivery_strategy == "build-upgrade":
            stages.extend(("build", "upgrade", "verification"))
        elif delivery_strategy == "live-patch":
            stages.extend(("live_patch", "verification"))
        return tuple(stages)
    if intent == "diagnosis-only" and entry_domain == "log_analyzer":
        return ("bundle",)
    return ("diagnosis",)


@dataclass(frozen=True)
class AcceptancePlan:
    """Acceptance requirements frozen as the first durable Case fact."""

    plan_id: str
    intent: str
    delivery_strategy: str
    goal: str
    requirements: tuple[AcceptanceRequirement, ...]
    frozen_at: float
    source: str = "runtime-derived"

    @classmethod
    def freeze(
        cls,
        arguments: Mapping[str, object],
        *,
        frozen_at: float,
    ) -> "AcceptancePlan":
        intent = (
            str(arguments.get("intent", "diagnosis-only"))
            .strip()
            .lower()
            .replace("_", "-")
            or "diagnosis-only"
        )
        delivery = (
            str(arguments.get("delivery_strategy", ""))
            .strip()
            .lower()
            .replace("_", "-")
        )
        if not delivery:
            delivery = "source-only"
        goal = _safe_text(
            arguments.get("final_purpose", arguments.get("problem", "")),
            limit=2048,
        )
        entry_domain = str(arguments.get("entry_domain", ""))
        requirements = tuple(
            AcceptanceRequirement(
                requirement_id=f"stage.{stage}",
                title=_STAGE_TITLES[stage],
                stage=stage,
            )
            for stage in _required_stages(intent, delivery, entry_domain)
        )
        identity = {
            "intent": intent,
            "delivery_strategy": delivery,
            "goal": goal,
            "requirements": [item.to_public_dict() for item in requirements],
            "source": "runtime-derived",
        }
        return cls(
            plan_id="acceptance-" + _fingerprint(identity),
            intent=intent,
            delivery_strategy=delivery,
            goal=goal,
            requirements=requirements,
            frozen_at=float(frozen_at),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": ACCEPTANCE_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "intent": self.intent,
            "delivery_strategy": self.delivery_strategy,
            "goal": self.goal,
            "requirements": [item.to_public_dict() for item in self.requirements],
            "frozen_at": self.frozen_at,
            "source": self.source,
        }

    @classmethod
    def from_public_dict(cls, value: Mapping[str, object]) -> "AcceptancePlan":
        raw_requirements = value.get("requirements", ())
        if not isinstance(raw_requirements, Sequence) or isinstance(
            raw_requirements, (str, bytes, bytearray)
        ):
            raise ValueError("acceptance requirements must be an array")
        requirements = tuple(
            AcceptanceRequirement.from_public_dict(item)
            for item in raw_requirements
            if isinstance(item, Mapping)
        )
        plan = cls(
            plan_id=str(value.get("plan_id", "")),
            intent=str(value.get("intent", "")),
            delivery_strategy=str(value.get("delivery_strategy", "")),
            goal=_safe_text(value.get("goal", ""), limit=2048),
            requirements=requirements,
            frozen_at=float(value.get("frozen_at", 0.0)),
            source=str(value.get("source", "runtime-derived")),
        )
        identity = {
            "intent": plan.intent,
            "delivery_strategy": plan.delivery_strategy,
            "goal": plan.goal,
            "requirements": [item.to_public_dict() for item in requirements],
            "source": plan.source,
        }
        if plan.plan_id != "acceptance-" + _fingerprint(identity):
            raise ValueError("acceptance plan fingerprint mismatch")
        return plan


@dataclass(frozen=True)
class StageReceipt:
    """Immutable facts derived from one persisted Case stage."""

    receipt_id: str
    stage: str
    producer: str
    status: str
    summary: str
    operation_id: str
    evidence_ids: tuple[str, ...]
    facts: Mapping[str, object]
    artifacts: tuple[Mapping[str, object], ...] = ()
    target_epoch: int | None = None

    @classmethod
    def create(
        cls,
        *,
        stage: str,
        producer: str,
        status: str,
        summary: str,
        operation_id: str,
        evidence_ids: Sequence[str],
        facts: Mapping[str, object],
        artifacts: Sequence[Mapping[str, object]] = (),
        target_epoch: int | None = None,
    ) -> "StageReceipt":
        bounded_facts = _bounded_public(facts)
        if not isinstance(bounded_facts, Mapping):
            bounded_facts = {}
        bounded_artifacts = _bounded_public(list(artifacts))
        if not isinstance(bounded_artifacts, list):
            bounded_artifacts = []
        public = {
            "stage": stage,
            "producer": producer,
            "status": status,
            "summary": _safe_text(summary),
            "operation_id": operation_id,
            "evidence_ids": list(
                dict.fromkeys(str(item) for item in evidence_ids if item)
            )[:MAX_CLOSEOUT_COLLECTION_ITEMS],
            "facts": dict(bounded_facts),
            "artifacts": [
                dict(item) for item in bounded_artifacts if isinstance(item, Mapping)
            ],
            "target_epoch": target_epoch,
        }
        return cls(
            receipt_id="receipt-" + _fingerprint(public),
            stage=stage,
            producer=producer,
            status=status,
            summary=public["summary"],
            operation_id=operation_id,
            evidence_ids=tuple(public["evidence_ids"]),
            facts=dict(public["facts"]),
            artifacts=tuple(dict(item) for item in public["artifacts"]),
            target_epoch=target_epoch,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": STAGE_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "stage": self.stage,
            "producer": self.producer,
            "status": self.status,
            "summary": self.summary,
            "operation_id": self.operation_id,
            "evidence_ids": list(self.evidence_ids),
            "facts": dict(self.facts),
            "artifacts": [dict(item) for item in self.artifacts],
            "target_epoch": self.target_epoch,
        }


@dataclass(frozen=True)
class CloseoutCheck:
    requirement_id: str
    status: str
    actual: str
    reason: str
    source_receipt_id: str = ""
    evidence_ids: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": CLOSEOUT_CHECK_SCHEMA,
            "requirement_id": self.requirement_id,
            "status": self.status,
            "actual": self.actual,
            "reason": self.reason,
            "source_receipt_id": self.source_receipt_id,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class CaseCloseout:
    case_id: str
    closure_status: str
    claim_level: str
    business_acceptance: str
    identity_status: str
    freshness_status: str
    source_delivery: str
    summary: str
    acceptance_plan: AcceptancePlan
    targets: tuple[Mapping[str, object], ...]
    checks: tuple[CloseoutCheck, ...]
    receipts: tuple[StageReceipt, ...]
    reasons: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.to_public_dict(include_fingerprint=False))

    def to_public_dict(self, *, include_fingerprint: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": CASE_CLOSEOUT_SCHEMA,
            "case_id": self.case_id,
            "closure_status": self.closure_status,
            "claim_level": self.claim_level,
            "business_acceptance": self.business_acceptance,
            "identity_status": self.identity_status,
            "freshness_status": self.freshness_status,
            "source_delivery": self.source_delivery,
            "summary": self.summary,
            "acceptance_plan": self.acceptance_plan.to_public_dict(),
            "targets": [dict(item) for item in self.targets],
            "checks": [item.to_public_dict() for item in self.checks],
            "receipts": [item.to_public_dict() for item in self.receipts],
            "reasons": list(self.reasons),
        }
        if include_fingerprint:
            value["fingerprint"] = self.fingerprint
        return value


_OPERATION_STAGES = {
    "log_bundle_collect": "bundle",
    "debug_run": "diagnosis",
    "live_patch_run": "live_patch",
    "upgrade_run": "upgrade",
    "debug_collect": "verification",
}
_PHASE_STAGES = {
    "developer.change": "development",
    "build.artifact": "build",
}
_STAGE_ORDER = {
    "bundle": 10,
    "diagnosis": 20,
    "development": 30,
    "build": 40,
    "live_patch": 50,
    "upgrade": 50,
    "verification": 60,
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text_from(value: Mapping[str, object], *keys: str) -> str:
    containers = [
        value,
        _mapping(value.get("result")),
        _mapping(value.get("analysis")),
        _mapping(value.get("diagnosis")),
    ]
    for container in containers:
        for key in keys:
            candidate = container.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _safe_text(candidate)
    return ""


def _target_epoch(value: Mapping[str, object]) -> int | None:
    for candidate in (
        value.get("target_epoch"),
        value.get("epoch_after"),
        _mapping(value.get("verification")).get("target_epoch"),
        _mapping(value.get("journal")).get("epoch_after"),
    ):
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            return candidate
    return None


def _operation_status(
    operation: Mapping[str, object],
    stage: str,
    evidence: Mapping[str, object],
    *,
    evidence_loaded: bool,
) -> str:
    recorded = str(operation.get("status", ""))
    if recorded in {"failed", "cancelled", "blocked", "mutation_outcome_unknown"}:
        return "blocked" if recorded == "mutation_outcome_unknown" else recorded
    if evidence.get("ok") is False:
        return "failed"
    if not evidence_loaded:
        return "partial"
    if stage in {"live_patch", "upgrade"}:
        journal = _mapping(evidence.get("journal"))
        classified = mutation_journal_operation_status(
            journal,
            action=str(_mapping(operation.get("inputs")).get("action", "")),
        )
        if classified == "mutation_outcome_unknown":
            return "blocked"
        if classified:
            return classified
        return "partial"
    normalized_code = str(
        evidence.get("normalized_code", evidence.get("code", ""))
    ).lower()
    comparison_status = str(
        _mapping(evidence.get("comparison")).get("status", "")
    ).lower()
    if "partial" in normalized_code or comparison_status == "partial":
        return "partial"
    return "completed" if recorded in {"completed", "verified", "succeeded"} else "partial"


def _selected_facts(
    stage: str,
    value: Mapping[str, object],
    inputs: Mapping[str, object],
) -> dict[str, object]:
    facts: dict[str, object] = {}
    target_id = inputs.get("target_id", value.get("target_id"))
    if isinstance(target_id, str) and target_id.strip():
        facts["target_id"] = target_id.strip()
    if stage == "diagnosis":
        for name in (
            "symptom",
            "root_cause",
            "mechanism",
            "affected_surface",
            "code_owner",
            "call_path",
        ):
            text = _text_from(value, name)
            if text:
                facts[name] = text
        if "root_cause" not in facts:
            summary = _text_from(value, "summary")
            if summary:
                facts["root_cause"] = summary
    elif stage in {"live_patch", "upgrade"}:
        for name in (
            "artifact_path",
            "artifact_sha256",
            "allow_insecure_tls",
            "force_path",
            "product_version",
            "local_path",
            "no_backup",
            "no_remount",
            "remote_path",
            "restart_scope",
            "ssh_host_key_policy",
        ):
            if name in inputs:
                facts[name] = inputs[name]
        journal = _mapping(value.get("journal"))
        if journal:
            facts["journal"] = {
                name: journal[name]
                for name in (
                    "stage",
                    "action",
                    "epoch_before",
                    "epoch_after",
                    "artifact_reference",
                    "expected_checksum",
                    "observed_checksum",
                    "root_mount_restored",
                    "restart_state",
                    "verification_state",
                )
                if name in journal
            }
        verification = _mapping(value.get("verification"))
        if verification:
            facts["verification"] = {
                name: verification[name]
                for name in (
                    "installed_version",
                    "remote_sha256",
                    "target_epoch",
                )
                if name in verification
            }
    elif stage == "verification":
        for name in ("profile", "normalized_code", "code", "ok"):
            if name in value:
                facts[name] = value[name]
        business = value.get("business_acceptance")
        if isinstance(business, Mapping):
            business = business.get("status")
        if isinstance(business, str) and business.strip():
            facts["business_acceptance"] = business.strip().lower()
        epoch = _target_epoch(value)
        if epoch is not None:
            facts["target_epoch"] = epoch
    elif stage == "bundle":
        for name in ("bundle_root", "local_bundle_path", "bundle_path"):
            text = _text_from(value, name)
            if text:
                facts[name] = text
    return facts


def _operation_artifacts(
    stage: str,
    value: Mapping[str, object],
    inputs: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    artifacts: list[dict[str, object]] = []
    if stage == "upgrade":
        path = str(inputs.get("artifact_path", ""))
        digest = str(inputs.get("artifact_sha256", ""))
        if path or digest:
            artifacts.append(
                {
                    "kind": "hpm",
                    "path": path,
                    "sha256": digest,
                    "product_version": str(inputs.get("product_version", "")),
                }
            )
    elif stage == "live_patch":
        path = str(inputs.get("local_path", ""))
        if path:
            artifacts.append({"kind": "runtime_artifact", "path": path})
    elif stage == "bundle":
        path = _text_from(value, "bundle_root", "local_bundle_path", "bundle_path")
        if path:
            artifacts.append({"kind": "diagnostic_bundle", "path": path})
    return tuple(artifacts)


def _phase_receipt(
    phase_type: str,
    record: Mapping[str, object],
    operation: Mapping[str, object] | None,
    *,
    evidence_loaded: bool,
) -> StageReceipt:
    stage = _PHASE_STAGES[phase_type]
    evidence_ids = tuple(
        str(item)
        for item in (_mapping(operation).get("evidence_ids", []) or [])
        if isinstance(item, str)
    )
    facts: dict[str, object] = {
        name: record[name]
        for name in (
            "source_revision",
            "authored_files",
            "verification_plan",
            "design",
            "validation_results",
            "source_delivery",
            "artifact_path",
            "artifact_sha256",
            "product_version",
            "component_versions",
            "build_commands",
            "build_logs",
            "known_gaps",
            "remote_path",
            "restart_scope",
        )
        if name in record and record[name] not in ("", [], None)
    }
    artifacts: list[dict[str, object]] = []
    artifact_path = str(record.get("artifact_path", ""))
    if artifact_path:
        artifacts.append(
            {
                "kind": "hpm" if artifact_path.lower().endswith(".hpm") else "artifact",
                "path": artifact_path,
                "sha256": str(record.get("artifact_sha256", "")),
                "product_version": str(record.get("product_version", "")),
            }
        )
    if stage == "build":
        build_logs = record.get("build_logs", [])
        if isinstance(build_logs, Sequence) and not isinstance(
            build_logs, (str, bytes, bytearray)
        ):
            artifacts.extend(
                {"kind": "build_log", "path": str(path)}
                for path in build_logs
                if isinstance(path, str) and path.strip()
            )
    recorded_status = str(record.get("status", "partial"))
    status = (
        "partial"
        if recorded_status == "completed" and not evidence_loaded
        else recorded_status
    )
    return StageReceipt.create(
        stage=stage,
        producer=str(record.get("producer_identity", "external-phase")),
        status=status,
        summary=str(record.get("summary", "")),
        operation_id=str(
            record.get(
                "operation_id",
                _mapping(operation).get("operation_id", ""),
            )
        ),
        evidence_ids=evidence_ids,
        facts=facts,
        artifacts=artifacts,
    )


def _business_acceptance(receipts: Sequence[StageReceipt]) -> str:
    verification_receipts = [
        receipt for receipt in receipts if receipt.stage == "verification"
    ]
    candidates = verification_receipts or list(receipts)
    statuses: list[str] = []
    for receipt in candidates:
        candidate = receipt.facts.get("business_acceptance")
        if not isinstance(candidate, str) or not candidate.strip():
            statuses.append("unverified")
            continue
        normalized = candidate.strip().lower()
        if normalized in {"passed", "verified", "completed"}:
            statuses.append("passed")
        elif normalized == "failed":
            statuses.append("failed")
        else:
            statuses.append("unverified")
    if "failed" in statuses:
        return "failed"
    if statuses and all(status == "passed" for status in statuses):
        return "passed"
    return "unverified"


_RECEIPT_STATUS_PRIORITY = {
    "failed": 50,
    "blocked": 40,
    "cancelled": 40,
    "partial": 30,
    "completed": 10,
}


def _receipts_by_stage(
    receipts: Sequence[StageReceipt],
) -> dict[str, list[StageReceipt]]:
    grouped: dict[str, list[StageReceipt]] = {}
    for receipt in receipts:
        grouped.setdefault(receipt.stage, []).append(receipt)
    return grouped


def _representative_receipts(
    receipts: Sequence[StageReceipt],
) -> dict[str, StageReceipt]:
    return {
        stage: max(
            stage_receipts,
            key=lambda receipt: (
                _RECEIPT_STATUS_PRIORITY.get(receipt.status, 35),
                receipt.receipt_id,
            ),
        )
        for stage, stage_receipts in _receipts_by_stage(receipts).items()
    }


def _receipt_target_key(receipt: StageReceipt) -> str:
    target_id = receipt.facts.get("target_id")
    return str(target_id).strip() if isinstance(target_id, str) else ""


def _source_delivery(receipts: Sequence[StageReceipt]) -> str:
    development = next(
        (receipt for receipt in reversed(receipts) if receipt.stage == "development"),
        None,
    )
    if development is None:
        return "not_applicable"
    candidate = str(development.facts.get("source_delivery", "")).strip().lower()
    if candidate in {"local_only", "committed", "pushed", "pull_request"}:
        return candidate
    return "local_only"


def _identity_status(
    plan: AcceptancePlan,
    receipts: Sequence[StageReceipt],
) -> tuple[str, list[str]]:
    by_stage = _representative_receipts(receipts)
    if plan.delivery_strategy == "source-only":
        return "not_applicable", []
    if plan.delivery_strategy == "build-upgrade" or plan.intent == "upgrade-and-verify":
        build = by_stage.get("build")
        upgrades = [receipt for receipt in receipts if receipt.stage == "upgrade"]
        upgrade = by_stage.get("upgrade")
        if not upgrades or upgrade is None:
            return "incomplete", ["缺少升级阶段身份。"]
        if build is None and plan.intent != "upgrade-and-verify":
            return "incomplete", ["缺少构建产物身份。"]
        if build is None:
            return (
                "matched"
                if all(item.status == "completed" for item in upgrades)
                else "incomplete",
                [],
            )
        expected = str(build.facts.get("artifact_sha256", "")).lower()
        observed = [
            str(item.facts.get("artifact_sha256", "")).lower()
            for item in upgrades
        ]
        if expected and observed and all(item == expected for item in observed):
            return "matched", []
        if expected and any(observed):
            return "mismatched", ["构建产物 SHA256 与升级输入不一致。"]
        return "incomplete", ["构建到升级的产物身份链不完整。"]
    if plan.delivery_strategy == "live-patch" or plan.intent in {
        "live-patch",
        "rollback",
    }:
        patches = [
            receipt for receipt in receipts if receipt.stage == "live_patch"
        ]
        patch = by_stage.get("live_patch")
        if not patches or patch is None:
            return "incomplete", ["缺少 Live Patch 部署身份。"]
        checksum_pairs = [
            (
                str(
                    _mapping(item.facts.get("journal")).get(
                        "expected_checksum", ""
                    )
                ).lower(),
                str(
                    _mapping(item.facts.get("journal")).get(
                        "observed_checksum", ""
                    )
                ).lower(),
            )
            for item in patches
        ]
        if checksum_pairs and all(
            expected and observed and expected == observed
            for expected, observed in checksum_pairs
        ):
            return "matched", []
        if any(
            expected and observed and expected != observed
            for expected, observed in checksum_pairs
        ):
            return "mismatched", ["Live Patch 预期与远端校验和不一致。"]
        if patch.status == "completed" and patch.facts.get("remote_path"):
            return "incomplete", ["Live Patch 已完成，但校验和身份链不完整。"]
        return "incomplete", ["Live Patch 的本地到远端身份链不完整。"]
    return "not_applicable", []


def _freshness_status(
    plan: AcceptancePlan,
    receipts: Sequence[StageReceipt],
) -> tuple[str, list[str]]:
    runtime_path = plan.delivery_strategy in {
        "build-upgrade",
        "live-patch",
    } or plan.intent in {
        "upgrade-and-verify",
        "live-patch",
        "rollback",
    }
    if not runtime_path:
        return "not_applicable", []
    mutations = [
        item for item in receipts if item.stage in {"upgrade", "live_patch"}
    ]
    verifications = [
        item for item in receipts if item.stage == "verification"
    ]
    if not mutations or not verifications:
        return "incomplete", ["缺少变更后新鲜验证阶段。"]
    mutation_by_target = {
        _receipt_target_key(item): item
        for item in mutations
        if _receipt_target_key(item)
    }
    verification_by_target = {
        _receipt_target_key(item): item
        for item in verifications
        if _receipt_target_key(item)
    }
    if mutation_by_target:
        missing = set(mutation_by_target) - set(verification_by_target)
        if missing:
            return "incomplete", [
                "缺少目标级变更后验证：" + ", ".join(sorted(missing)) + "。"
            ]
        pairs = [
            (mutation, verification_by_target[target_id])
            for target_id, mutation in mutation_by_target.items()
        ]
    else:
        pairs = [
            (
                max(
                    mutations,
                    key=lambda item: item.target_epoch
                    if item.target_epoch is not None
                    else -1,
                ),
                min(
                    verifications,
                    key=lambda item: item.target_epoch
                    if item.target_epoch is not None
                    else -1,
                ),
            )
        ]
    if any(
        mutation.target_epoch is None or verification.target_epoch is None
        for mutation, verification in pairs
    ):
        return "incomplete", ["变更或验证阶段未报告 target epoch。"]
    if any(
        verification.target_epoch < mutation.target_epoch
        for mutation, verification in pairs
        if mutation.target_epoch is not None
        and verification.target_epoch is not None
    ):
        return "stale", ["验证证据早于变更后的 target epoch。"]
    return "fresh", []


def aggregate_case_closeout(
    projection: Mapping[str, object],
    evidence_reader: Callable[[Mapping[str, object]], Mapping[str, object] | None],
    *,
    terminal_status: str = "completed",
) -> CaseCloseout:
    """Derive a conservative closeout from one Case projection and its blobs."""

    raw_plan = projection.get("acceptance_plan")
    if not isinstance(raw_plan, Mapping):
        raise ValueError("Case has no frozen acceptance plan")
    plan = AcceptancePlan.from_public_dict(raw_plan)
    evidence_refs = {
        str(item.get("evidence_id", "")): item
        for item in projection.get("evidence_refs", [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    }
    operations = [
        item
        for item in projection.get("operations", [])
        if isinstance(item, Mapping)
    ]
    def operation_evidence(
        operation: Mapping[str, object],
    ) -> tuple[list[str], Mapping[str, object], bool]:
        evidence_ids = [
            str(item)
            for item in operation.get("evidence_ids", [])
            if isinstance(item, str)
        ]
        if evidence_ids:
            reference = evidence_refs.get(evidence_ids[-1])
            try:
                loaded = evidence_reader(reference) if reference is not None else None
            except Exception:
                loaded = None
            if isinstance(loaded, Mapping):
                return evidence_ids, loaded, True
        return evidence_ids, {}, False

    operation_receipts: dict[tuple[str, str], tuple[int, StageReceipt]] = {}
    for operation in operations:
        operation_name = str(operation.get("operation", ""))
        stage = _OPERATION_STAGES.get(operation_name)
        if stage is None:
            continue
        operation_evidence_ids, value, evidence_loaded = operation_evidence(operation)
        inputs = _mapping(operation.get("inputs"))
        summary = (
            _text_from(value, "summary", "root_cause")
            or str(operation.get("summary", ""))
        )
        receipt = StageReceipt.create(
            stage=stage,
            producer=operation_name,
            status=_operation_status(
                operation,
                stage,
                value,
                evidence_loaded=evidence_loaded,
            ),
            summary=summary,
            operation_id=str(operation.get("operation_id", "")),
            evidence_ids=operation_evidence_ids,
            facts=_selected_facts(stage, value, inputs),
            artifacts=_operation_artifacts(stage, value, inputs),
            target_epoch=_target_epoch(value),
        )
        order = max(
            int(operation.get(name, 0) or 0)
            for name in (
                "terminal_revision",
                "reconciled_revision",
                "started_revision",
                "accepted_revision",
            )
        )
        target_key = str(receipt.facts.get("target_id", "")).strip()
        receipt_key = (stage, target_key or "__default__")
        previous = operation_receipts.get(receipt_key)
        if previous is None or order >= previous[0]:
            operation_receipts[receipt_key] = (order, receipt)

    receipts: list[StageReceipt] = [
        receipt for _order, receipt in operation_receipts.values()
    ]

    phase_operations = [
        item for item in operations if item.get("operation") == "phase_record"
    ]
    phase_operations_by_id = {
        str(item.get("operation_id", "")): item
        for item in phase_operations
        if item.get("operation_id")
    }
    phase_operation_by_type: dict[str, Mapping[str, object]] = {}
    for operation in phase_operations:
        _evidence_ids, loaded, evidence_loaded = operation_evidence(operation)
        if not evidence_loaded:
            continue
        phase_type = str(loaded.get("phase_type", ""))
        if phase_type in _PHASE_STAGES:
            phase_operation_by_type[phase_type] = operation
    latest_phases: dict[str, Mapping[str, object]] = {}
    for record in projection.get("phase_records", []):
        if isinstance(record, Mapping) and record.get("phase_type") in _PHASE_STAGES:
            latest_phases[str(record["phase_type"])] = record
    for phase_type, record in latest_phases.items():
        recorded_operation_id = str(record.get("operation_id", ""))
        matching_operation = (
            phase_operations_by_id.get(recorded_operation_id)
            if recorded_operation_id
            else phase_operation_by_type.get(phase_type)
        )
        evidence_loaded = False
        if matching_operation is not None:
            _evidence_ids, loaded, evidence_loaded = operation_evidence(
                matching_operation
            )
            if evidence_loaded and str(loaded.get("phase_type", "")) != phase_type:
                evidence_loaded = False
        receipts.append(
            _phase_receipt(
                phase_type,
                record,
                matching_operation,
                evidence_loaded=evidence_loaded,
            )
        )

    receipts.sort(key=lambda item: (_STAGE_ORDER.get(item.stage, 999), item.receipt_id))
    stage_receipts = _receipts_by_stage(receipts)
    by_stage = _representative_receipts(receipts)
    checks: list[CloseoutCheck] = []
    reasons: list[str] = []
    for requirement in plan.requirements:
        candidates = stage_receipts.get(requirement.stage, [])
        if not candidates:
            status = "not_run"
            actual = "未形成阶段回执"
            reason = f"必需阶段 {requirement.stage} 缺失。"
            source_receipt_id = ""
            evidence_ids: tuple[str, ...] = ()
            reasons.append(reason)
        else:
            candidate_statuses = [receipt.status for receipt in candidates]
            summaries = [
                (
                    f"{_receipt_target_key(receipt)}: {receipt.summary}"
                    if _receipt_target_key(receipt)
                    else receipt.summary
                )
                for receipt in candidates
                if receipt.summary
            ]
            actual = "；".join(dict.fromkeys(summaries))
            source_receipt_id = ",".join(
                receipt.receipt_id for receipt in candidates
            )
            evidence_ids = tuple(
                dict.fromkeys(
                    evidence_id
                    for receipt in candidates
                    for evidence_id in receipt.evidence_ids
                )
            )
        if candidates and all(
            receipt_status == "completed"
            for receipt_status in candidate_statuses
        ):
            status = "passed"
            actual = actual or "阶段完成并保留证据"
            reason = ""
        elif candidates and "failed" in candidate_statuses:
            status = "failed"
            actual = actual or "阶段失败"
            reason = f"必需阶段 {requirement.stage} 失败。"
            reasons.append(reason)
        elif candidates and any(
            receipt_status in {"blocked", "cancelled"}
            for receipt_status in candidate_statuses
        ):
            status = "blocked"
            actual = actual or "阶段受阻"
            reason = f"必需阶段 {requirement.stage} 存在受阻或取消结果。"
            reasons.append(reason)
        elif candidates:
            status = "unverified"
            actual = actual or "阶段未形成完成结论"
            reason = f"必需阶段 {requirement.stage} 未形成完成结论。"
            reasons.append(reason)
        checks.append(
            CloseoutCheck(
                requirement_id=requirement.requirement_id,
                status=status,
                actual=_safe_text(actual, limit=2048),
                reason=reason,
                source_receipt_id=source_receipt_id,
                evidence_ids=evidence_ids,
            )
        )

    business = _business_acceptance(receipts)
    requires_business = (
        plan.intent in {"live-patch", "rollback", "upgrade-and-verify"}
        or plan.delivery_strategy in {"live-patch", "build-upgrade"}
    )
    if requires_business and business == "unverified":
        reasons.append("未收到可信的业务验收结果，business_acceptance=unverified。")
    identity, identity_reasons = _identity_status(plan, receipts)
    freshness, freshness_reasons = _freshness_status(plan, receipts)
    reasons.extend(identity_reasons)
    reasons.extend(freshness_reasons)
    policy: TaskAuthorizationPolicy | None = None
    raw_policy = projection.get("authorization")
    try:
        if isinstance(raw_policy, Mapping):
            policy = TaskAuthorizationPolicy.from_public_dict(raw_policy)
        else:
            workflow_inputs = _mapping(projection.get("workflow_inputs"))
            allow_insecure_tls = workflow_inputs.get("allow_insecure_tls", True)
            if not isinstance(allow_insecure_tls, bool):
                raise TypeError("allow_insecure_tls must be a boolean")
            policy = TaskAuthorizationPolicy.from_task_intent(
                str(projection.get("intent", plan.intent)),
                delivery_strategy=str(
                    projection.get("delivery_strategy", plan.delivery_strategy)
                ),
                authorized_exceptions=_mapping(
                    workflow_inputs.get("authorized_exceptions")
                ),
                allow_insecure_tls=allow_insecure_tls,
            )
    except (TypeError, ValueError) as exc:
        reasons.append(
            "任务授权策略不可验证："
            f"{_safe_text(type(exc).__name__ + ': ' + str(exc), limit=1024)}。"
        )
    unauthorized_exception_used = False
    for receipt in receipts:
        known_gaps = receipt.facts.get("known_gaps", [])
        if isinstance(known_gaps, Sequence) and not isinstance(
            known_gaps, (str, bytes, bytearray)
        ):
            reasons.extend(
                _safe_text(item, limit=2048)
                for item in known_gaps
                if str(item).strip()
            )
        if receipt.stage in {"live_patch", "upgrade"}:
            for exception_name in (
                "force_path",
                "no_backup",
                "no_remount",
                "allow_insecure_tls",
            ):
                if receipt.facts.get(exception_name) is True:
                    authorized = False
                    if policy is not None:
                        if exception_name == "allow_insecure_tls":
                            authorized = policy.allow_insecure_tls
                        else:
                            authorized = bool(
                                getattr(
                                    policy.authorized_exceptions,
                                    exception_name,
                                    False,
                                )
                            )
                    if authorized:
                        reasons.append(
                            f"任务使用了已授权例外：{exception_name}=true。"
                        )
                    else:
                        unauthorized_exception_used = True
                        reasons.append(
                            f"任务使用了未获策略授权的例外：{exception_name}=true。"
                        )
    required_statuses = [check.status for check in checks]
    definitive_failure = (
        terminal_status == "failed"
        or "failed" in required_statuses
        or business == "failed"
        or identity == "mismatched"
        or freshness == "stale"
        or unauthorized_exception_used
    )
    if definitive_failure:
        closure_status = "failed"
    elif terminal_status == "cancelled" or "blocked" in required_statuses:
        closure_status = "blocked"
    elif any(status != "passed" for status in required_statuses):
        closure_status = "partial"
    elif requires_business and business != "passed":
        closure_status = "partial"
    elif freshness not in {"fresh", "not_applicable"}:
        closure_status = "partial"
    elif identity not in {"matched", "not_applicable"}:
        closure_status = "partial"
    elif freshness == "fresh":
        closure_status = "verified"
    else:
        closure_status = "completed_in_scope"

    if closure_status == "verified":
        claim_level = "runtime_verified"
    elif by_stage.get("upgrade") and by_stage["upgrade"].status == "completed":
        claim_level = "deployment_confirmed"
    elif by_stage.get("live_patch") and by_stage["live_patch"].status == "completed":
        claim_level = "deployment_confirmed"
    elif by_stage.get("build") and by_stage["build"].status == "completed":
        claim_level = "artifact_verified"
    elif by_stage.get("development") and by_stage["development"].status == "completed":
        claim_level = "source_changed"
    elif by_stage.get("diagnosis") and by_stage["diagnosis"].status == "completed":
        claim_level = "diagnosed"
    else:
        claim_level = "unverified"
    source_delivery = _source_delivery(receipts)
    summary = {
        "verified": "变更、交付身份、新鲜运行态证据和业务验收均已闭环。",
        "completed_in_scope": "约定范围内的阶段门禁均已完成。",
        "partial": "阶段工作已有结果，但业务验收或闭环证据仍不完整。",
        "failed": "必需阶段、交付身份、新鲜度或验收存在确定失败。",
        "blocked": "必需闭环工作受阻，当前无法形成完成结论。",
    }[closure_status]
    targets = tuple(
        dict(item)
        for item in projection.get("targets", [])
        if isinstance(item, Mapping)
    )
    return CaseCloseout(
        case_id=str(projection.get("case_id", "")),
        closure_status=closure_status,
        claim_level=claim_level,
        business_acceptance=business if requires_business else "not_applicable",
        identity_status=identity,
        freshness_status=freshness,
        source_delivery=source_delivery,
        summary=summary,
        acceptance_plan=plan,
        targets=targets,
        checks=tuple(checks),
        receipts=tuple(receipts),
        reasons=tuple(dict.fromkeys(reasons)),
    )


_STATUS_ZH = {
    "verified": "已验证",
    "completed_in_scope": "范围内完成",
    "completed": "完成",
    "partial": "部分完成",
    "failed": "失败",
    "blocked": "受阻",
    "cancelled": "已取消",
    "passed": "通过",
    "unverified": "未验证",
    "not_run": "未执行",
    "not_applicable": "不适用",
    "matched": "一致",
    "mismatched": "不一致",
    "incomplete": "不完整",
    "fresh": "新鲜",
    "stale": "过期",
    "local_only": "仅本地",
    "committed": "已提交",
    "pushed": "已推送",
    "pull_request": "已创建 PR",
    "unknown": "未知",
}
_STAGE_ZH = {
    "bundle": "日志包",
    "diagnosis": "定位诊断",
    "development": "方案与修复",
    "build": "构建",
    "live_patch": "Live Patch",
    "upgrade": "升级",
    "verification": "运行态验证",
}


def _md(value: object) -> str:
    return _safe_text(value).replace("|", "\\|").replace("\n", "<br>") or "—"


def _display(value: object) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return "；".join(_display(item) for item in value)
    return str(value or "")


def _artifact_link(path: str) -> str:
    clean = _safe_text(path)
    if not clean:
        return "—"
    if clean.startswith("/"):
        escaped = clean.replace(" ", "%20").replace(")", "%29")
        return f"[{_md(clean)}]({escaped})"
    return _md(clean)


def render_markdown(result: CaseCloseout) -> str:
    """Render a high-signal Chinese report from the machine closeout."""

    receipts = _representative_receipts(result.receipts)
    diagnosis = receipts.get("diagnosis")
    development = receipts.get("development")
    build = receipts.get("build")
    mutation = receipts.get("upgrade") or receipts.get("live_patch")
    verification = receipts.get("verification")
    root_cause = (
        str(diagnosis.facts.get("root_cause", ""))
        if diagnosis is not None
        else ""
    )
    change_summary = development.summary if development is not None else ""
    design = (
        _mapping(development.facts.get("design"))
        if development is not None
        else {}
    )
    delivery = result.acceptance_plan.delivery_strategy
    version_or_patch = ""
    if build is not None:
        version_or_patch = str(build.facts.get("product_version", ""))
    if not version_or_patch and mutation is not None:
        version_or_patch = str(
            mutation.facts.get(
                "product_version",
                mutation.facts.get("remote_path", ""),
            )
        )
    passed = sum(check.status == "passed" for check in result.checks)
    failed = sum(check.status == "failed" for check in result.checks)
    unresolved = len(result.checks) - passed - failed
    targets = ", ".join(
        f"{item.get('target_id', 'target')}@{item.get('address', '')}"
        for item in result.targets
    )
    gate_summary = f"通过 {passed} / 失败 {failed} / 未决 {unresolved}"
    known_blockers = [
        reason
        for reason in result.reasons
        if not reason.startswith("任务使用了已授权例外：")
    ]
    lines = [
        "# 问题闭环报告",
        "",
        result.summary,
        "",
        "## 结果摘要",
        "",
    ]
    summary_items = [
        (
            "总体状态",
            _STATUS_ZH.get(result.closure_status, result.closure_status),
        ),
        ("任务目标", result.acceptance_plan.goal),
        ("目标", targets),
        ("根因", root_cause),
        ("修复", change_summary),
        ("方案设计", design.get("rationale", design.get("what_changed", ""))),
        (
            _STAGE_ZH.get(mutation.stage, mutation.stage) if mutation else "部署",
            mutation.summary if mutation else "",
        ),
        ("组件构建", build.summary if build else ""),
        ("组件版本", _display(build.facts.get("component_versions", "")) if build else ""),
        ("产品版本", version_or_patch),
        ("构建命令", _display(build.facts.get("build_commands", "")) if build else ""),
        (
            "回归验证",
            _display(development.facts.get("validation_results", ""))
            if development
            else "",
        ),
        ("最终验证", verification.summary if verification else ""),
        ("阶段门禁", gate_summary),
        (
            "业务验收",
            _STATUS_ZH.get(result.business_acceptance, result.business_acceptance),
        ),
        (
            "源码交付",
            _STATUS_ZH.get(result.source_delivery, result.source_delivery),
        ),
        ("已知阻塞", "；".join(known_blockers[:5])),
    ]
    for label, value in summary_items:
        if str(value or "").strip():
            lines.append(f"- {label}：{_md(value)}")
    artifacts = [
        (receipt.stage, artifact)
        for receipt in result.receipts
        for artifact in receipt.artifacts
    ]
    if artifacts:
        lines.extend(
            [
                "",
                "## 输出资料",
                "",
                "| 阶段 | 类型 | 路径或引用 | SHA256 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for stage, artifact in artifacts:
            lines.append(
                f"| {_STAGE_ZH.get(stage, stage)} | "
                f"{_md(artifact.get('kind', 'artifact'))} | "
                f"{_artifact_link(str(artifact.get('path', '')))} | "
                f"{_md(artifact.get('sha256', ''))} |"
            )
    if result.reasons:
        lines.extend(["", "## 未完成项与风险", ""])
        lines.extend(f"- {_md(reason)}" for reason in result.reasons)
    lines.extend(
        [
            f"- 交付路径：{_md(delivery)}",
            f"- 事实指纹：`{result.fingerprint}`",
            "",
            "## 阶段结果",
            "",
            "| 阶段 | 状态 | 摘要 | 证据 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for receipt in result.receipts:
        lines.append(
            f"| {_STAGE_ZH.get(receipt.stage, receipt.stage)} | "
            f"{_STATUS_ZH.get(receipt.status, receipt.status)} | "
            f"{_md(receipt.summary)} | {_md(', '.join(receipt.evidence_ids))} |"
        )
    if diagnosis is not None:
        lines.extend(
            [
                "",
                "## 问题定位",
                "",
                "| 项目 | 内容 |",
                "| --- | --- |",
                f"| 结论 | {_md(diagnosis.summary)} |",
                f"| 根因 | {_md(diagnosis.facts.get('root_cause', '未提供'))} |",
                f"| 机制 | {_md(diagnosis.facts.get('mechanism', '未提供'))} |",
                f"| 影响面 | {_md(diagnosis.facts.get('affected_surface', '未提供'))} |",
            ]
        )
    if development is not None:
        lines.extend(
            [
                "",
                "## 改动与方案设计",
                "",
                "| 项目 | 内容 |",
                "| --- | --- |",
                f"| 实现摘要 | {_md(development.summary)} |",
                f"| 变更内容 | {_md(_display(design.get('what_changed', '未提供')))} |",
                f"| 设计理由 | {_md(_display(design.get('rationale', '未提供')))} |",
                f"| 保持不变量 | {_md(_display(design.get('invariants', '未提供')))} |",
                f"| 方案权衡 | {_md(_display(design.get('tradeoffs', '未提供')))} |",
                f"| 回滚方案 | {_md(_display(design.get('rollback', '未提供')))} |",
                f"| 源码版本 | {_md(development.facts.get('source_revision', '未提供'))} |",
                f"| 修改文件 | {_md(_display(development.facts.get('authored_files', '未提供')))} |",
                f"| 验证计划 | {_md(_display(development.facts.get('verification_plan', '未提供')))} |",
                f"| 验证结果 | {_md(_display(development.facts.get('validation_results', '未提供')))} |",
            ]
        )
    if build is not None:
        lines.extend(
            [
                "",
                "## 构建与产品产物",
                "",
                "| 项目 | 内容 |",
                "| --- | --- |",
                f"| 构建结论 | {_md(build.summary)} |",
                f"| 组件/Conan 身份 | {_md(_display(build.facts.get('component_versions', '未提供')))} |",
                f"| 构建命令 | {_md(_display(build.facts.get('build_commands', '未提供')))} |",
                f"| 构建日志 | {_md(_display(build.facts.get('build_logs', '未提供')))} |",
                f"| 产品版本 | {_md(build.facts.get('product_version', '未提供'))} |",
                f"| 产物路径 | {_artifact_link(str(build.facts.get('artifact_path', '')))} |",
                f"| SHA256 | {_md(build.facts.get('artifact_sha256', '未提供'))} |",
            ]
        )
    if mutation is not None:
        lines.extend(
            [
                "",
                "## 部署与验证",
                "",
                "| 项目 | 内容 |",
                "| --- | --- |",
                f"| 部署方式 | {_STAGE_ZH.get(mutation.stage, mutation.stage)} |",
                f"| 部署结论 | {_md(mutation.summary)} |",
                f"| 身份链 | {_STATUS_ZH.get(result.identity_status, result.identity_status)} |",
                f"| 证据新鲜度 | {_STATUS_ZH.get(result.freshness_status, result.freshness_status)} |",
                f"| 验证结论 | {_md(verification.summary if verification else '未执行')} |",
            ]
        )
    lines.extend(
        [
            "",
            "## 验收矩阵",
            "",
            "| 验收项 | 阶段 | 状态 | 实际 | 原因 | 来源回执 | 证据 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    requirements = {
        item.requirement_id: item for item in result.acceptance_plan.requirements
    }
    for check in result.checks:
        requirement = requirements[check.requirement_id]
        lines.append(
            f"| {_md(requirement.title)} | {_STAGE_ZH.get(requirement.stage, requirement.stage)} | "
            f"{_STATUS_ZH.get(check.status, check.status)} | {_md(check.actual)} | "
            f"{_md(check.reason)} | {_md(check.source_receipt_id)} | "
            f"{_md(', '.join(check.evidence_ids))} |"
        )
    lines.append("")
    return _truncate_utf8(
        "\n".join(lines),
        limit=MAX_CLOSEOUT_MARKDOWN_BYTES,
    )


def build_closeout_bundle(
    result: CaseCloseout,
    markdown: str,
) -> dict[str, object]:
    """Build an immutable manifest; the Case repository stores the documents."""

    closeout = result.to_public_dict()
    closeout_bytes = _canonical_bytes(closeout)
    markdown_bytes = markdown.encode("utf-8")
    evidence_ids = sorted(
        {
            evidence_id
            for receipt in result.receipts
            for evidence_id in receipt.evidence_ids
        }
    )
    evidence_documents = [
        {
            "name": f"{receipt.stage}-evidence-{index + 1}.json",
            "stage": receipt.stage,
            "evidence_id": evidence_id,
            "media_type": "application/json",
        }
        for receipt in result.receipts
        for index, evidence_id in enumerate(receipt.evidence_ids)
    ]
    artifacts = [
        {"stage": receipt.stage, **dict(artifact)}
        for receipt in result.receipts
        for artifact in receipt.artifacts
    ]
    return {
        "schema": CLOSEOUT_BUNDLE_SCHEMA,
        "case_id": result.case_id,
        "closeout_fingerprint": result.fingerprint,
        "documents": [
            {
                "name": "closeout.json",
                "media_type": "application/json",
                "sha256": hashlib.sha256(closeout_bytes).hexdigest(),
                "byte_count": len(closeout_bytes),
                "locator": {
                    "tool": "case_read",
                    "case_id": result.case_id,
                    "field": "closeout",
                },
            },
            {
                "name": "closeout.md",
                "media_type": "text/markdown",
                "sha256": hashlib.sha256(markdown_bytes).hexdigest(),
                "byte_count": len(markdown_bytes),
                "locator": {
                    "tool": "case_read",
                    "case_id": result.case_id,
                    "field": "closeout_markdown",
                },
            },
        ],
        "evidence_ids": evidence_ids,
        "evidence_documents": evidence_documents,
        "artifacts": artifacts,
    }
