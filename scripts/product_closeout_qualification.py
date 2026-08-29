#!/usr/bin/env python3
"""Qualify immutable product-closeout evidence through one Operator/CI seam."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import SQLiteRuntimeRepository  # noqa: E402


EVIDENCE_SCHEMA = "openubmc-agent-workflow.product-closeout-evidence.v1"
REPORT_SCHEMA = "openubmc-agent-workflow.product-closeout-qualification.v1"
PROOF_SCHEMA = "openubmc-agent-workflow.product-closeout-proof.v1"
MODES = {"fresh-runtime", "historical-reconstruction"}
PROTOCOLS = {"NVMe", "SATA", "SAS"}
DRIVE_PROTOCOL_BY_CODE = {3: "SATA", 4: "SAS", 6: "NVMe"}
SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
PASSED_COUNT = re.compile(r"(?<!\d)(\d+)\s*/\s*(\d+)\s+passed\b", re.IGNORECASE)
PACKAGE_REVISION = re.compile(
    r"Created package revision\s+([0-9a-f]{32,64})", re.IGNORECASE
)
FULL_PACKAGE_REFERENCE = re.compile(
    r"Full package reference:\s+\S+#[0-9a-f]{32,64}:[0-9a-f]{32,64}#"
    r"([0-9a-f]{32,64})",
    re.IGNORECASE,
)
DRIVE_TIMELINE = re.compile(
    r"elapsed=(\d+)s\s+drives=(\d+)\s+direct=(\d+)\s+"
    r"direct_attributed=(\d+)\s+raid=(\d+)\s+raid_zero=(\d+)\s+"
    r"health_ok=(\d+)\s+presence_ok=(\d+)\s+serial_ok=(\d+)"
)
HISTORICAL_EVIDENCE_DIMENSIONS = {
    "workflow-diagnosis-record": "diagnosis",
    "workflow-official-ut-record": "official_ut",
    "workflow-upgrade-record": "upgrade",
    "component-build-log": "build",
    "product-build-log": "build",
    "reboot-acceptance-timeline": "freshness",
    "drive-summary-json": "hardware",
}
FRESH_SUPPORT_TYPES = {
    "diagnosis": {"workflow-diagnosis-record"},
    "official_ut": {"workflow-official-ut-record"},
    "build": {"component-build-log", "product-build-log"},
    "upgrade": {"workflow-upgrade-record"},
    "freshness": {"reboot-acceptance-timeline"},
    "hardware": {"drive-summary-json"},
}


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _expected_sha256(value: object) -> str:
    match = SHA256.fullmatch(_text(value).lower())
    return match.group(1) if match is not None else ""


def _nested_value(document: object, path: str) -> object:
    current = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _timestamp(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _decoded_text(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _last_content_line(text: str) -> str:
    lines = [ANSI_ESCAPE.sub("", line).strip() for line in text.splitlines()]
    return next((line for line in reversed(lines) if line), "")


def _historical_diagnosis_record(raw: bytes) -> str | None:
    text = _decoded_text(raw)
    if text is None:
        return "diagnosis record must be UTF-8 text"
    has_cause = any(
        marker in text.lower()
        for marker in ("失败位置", "根因", "root cause", "diagnosis")
    )
    has_fix = any(marker in text.lower() for marker in ("修复", "fix"))
    if not has_cause or not has_fix:
        return "diagnosis record must contain a diagnosis/root-cause and fix chain"
    return None


def _historical_official_ut_record(raw: bytes) -> str | None:
    text = _decoded_text(raw)
    if text is None:
        return "official UT record must be UTF-8 text"
    results = [(int(passed), int(total)) for passed, total in PASSED_COUNT.findall(text)]
    if not results or not any(total > 0 and passed == total for passed, total in results):
        return "official UT record must contain a non-zero N/N passed result"
    if any(passed != total for passed, total in results):
        return "official UT record contains an incomplete passed result"
    return None


def _historical_component_build_log(raw: bytes) -> str | None:
    text = _decoded_text(raw)
    if text is None:
        return "component build log must be UTF-8 text"
    clean = ANSI_ESCAPE.sub("", text)
    revisions = set(PACKAGE_REVISION.findall(clean))
    references = set(FULL_PACKAGE_REFERENCE.findall(clean))
    if not revisions:
        return "component build log is missing a package revision"
    if not references or not revisions.intersection(references):
        return "component build log is missing a matching full package reference"
    if _last_content_line(clean) != "构建成功":
        return "component build log is missing its successful terminal state"
    return None


def _historical_product_build_log(raw: bytes) -> str | None:
    text = _decoded_text(raw)
    if text is None:
        return "product build log must be UTF-8 text"
    clean = ANSI_ESCAPE.sub("", text)
    if not re.search(r"hpm\s+构建成功", clean, re.IGNORECASE):
        return "product build log is missing HPM build success"
    if not re.search(r"给\s*hpm\s*包.*签名", clean, re.IGNORECASE):
        return "product build log is missing HPM signing"
    if _last_content_line(clean) != "任务 personal 执行成功":
        return "product build log is missing its successful terminal task"
    return None


def _historical_upgrade_record(
    raw: bytes, requirements: Mapping[str, object]
) -> str | None:
    text = _decoded_text(raw)
    if text is None:
        return "upgrade record must be UTF-8 text"
    if not re.search(r"上传与激活\s*\|\s*完成", text):
        return "upgrade record must show upload and activation completion"
    version_match = re.search(
        r"安装版本确认\s*\|\s*`?([0-9A-Za-z][0-9A-Za-z._-]*)`?", text
    )
    if version_match is None:
        return "upgrade record must contain the installed version"
    expected_version = _text(requirements.get("installed_version"))
    if expected_version and version_match.group(1) != expected_version:
        return "upgrade record installed version does not match the artifact"
    return None


def _historical_reboot_timeline(raw: bytes) -> str | None:
    text = _decoded_text(raw)
    if text is None:
        return "reboot acceptance timeline must be UTF-8 text"
    if "manager_ready" not in text:
        return "reboot acceptance timeline is missing manager readiness"
    states = [tuple(int(value) for value in match) for match in DRIVE_TIMELINE.findall(text)]
    if not states:
        return "reboot acceptance timeline is missing drive convergence"
    elapsed, drives, direct, attributed, raid, raid_zero, health, presence, serial = states[-1]
    if (
        drives <= 0
        or direct + raid != drives
        or attributed != direct
        or raid_zero != raid
        or health != drives
        or presence != drives
        or serial != drives
    ):
        return "reboot acceptance timeline final drive state is not converged"
    accepted = re.findall(r"accepted_elapsed=(\d+)s", text)
    if not accepted or int(accepted[-1]) != elapsed:
        return "reboot acceptance timeline is missing its accepted elapsed time"
    return None


def _historical_drive_summary(
    raw: bytes, requirements: Mapping[str, object]
) -> str | None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "drive summary must be UTF-8 JSON"
    if not isinstance(document, Mapping):
        return "drive summary must be a JSON object"
    summary = _mapping(document.get("summary"))
    drives = [
        _mapping(item)
        for item in _sequence(document.get("drives"))
        if isinstance(item, Mapping)
    ]
    fields = (
        "drives",
        "direct",
        "direct_attributed",
        "raid",
        "raid_zero",
        "health_ok",
        "presence_ok",
        "serial_ok",
    )
    counts = {field: summary.get(field) for field in fields}
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        return "drive summary counts must be non-negative integers"
    if counts["drives"] <= 0 or len(drives) != counts["drives"]:
        return "drive summary drive count does not match its records"
    direct = [drive for drive in drives if drive.get("controller") == 255]
    raid = [drive for drive in drives if drive.get("controller") != 255]
    actual = {
        "drives": len(drives),
        "direct": len(direct),
        "direct_attributed": sum(
            isinstance(drive.get("resource"), int)
            and not isinstance(drive.get("resource"), bool)
            and drive.get("resource", 0) > 0
            for drive in direct
        ),
        "raid": len(raid),
        "raid_zero": sum(drive.get("resource") == 0 for drive in raid),
        "health_ok": sum(drive.get("health") == 0 for drive in drives),
        "presence_ok": sum(drive.get("presence") == 1 for drive in drives),
        "serial_ok": sum(drive.get("serial_present") is True for drive in drives),
    }
    if any(counts[field] != actual[field] for field in fields):
        return "drive summary counts are inconsistent with its drive records"
    expected_protocols = {
        int(match.group(1)): _text(device.get("protocol"))
        for device in _sequence(requirements.get("devices"))
        if isinstance(device, Mapping)
        and (match := re.fullmatch(r"Drive(\d+)", _text(device.get("device_id"))))
    }
    actual_ids = {
        drive.get("id")
        for drive in direct
        if isinstance(drive.get("id"), int) and not isinstance(drive.get("id"), bool)
    }
    if expected_protocols and actual_ids != set(expected_protocols):
        return "drive summary direct device identities do not match the hardware scope"
    actual_by_id = {
        drive.get("id"): DRIVE_PROTOCOL_BY_CODE.get(drive.get("protocol"))
        for drive in direct
    }
    if any(
        actual_by_id.get(device_id) != protocol
        for device_id, protocol in expected_protocols.items()
    ):
        return "drive summary protocol does not match the hardware scope"
    return None


def _verify_historical_evidence(
    item: Mapping[str, object],
    *,
    raw: bytes,
    label: str,
    requirements: Mapping[str, object],
    violations: list[str],
) -> bool:
    claims = _sequence(item.get("claims"))
    if claims:
        violations.append(f"{label}: manifest-authored claims are unsupported")
        return False
    evidence_type = _text(item.get("evidence_type"))
    if not evidence_type:
        violations.append(f"{label}: historical evidence_type is required")
    elif evidence_type not in HISTORICAL_EVIDENCE_DIMENSIONS:
        violations.append(
            f"{label}: unknown historical evidence_type {evidence_type!r}"
        )
    elif HISTORICAL_EVIDENCE_DIMENSIONS[evidence_type] != _text(
        requirements.get("dimension")
    ):
        violations.append(
            f"{label}: historical evidence_type {evidence_type!r} does not match "
            f"{_text(requirements.get('dimension'))} dimension"
        )
    else:
        verifier = {
            "workflow-diagnosis-record": lambda: _historical_diagnosis_record(raw),
            "workflow-official-ut-record": lambda: _historical_official_ut_record(raw),
            "workflow-upgrade-record": lambda: _historical_upgrade_record(
                raw, requirements
            ),
            "component-build-log": lambda: _historical_component_build_log(raw),
            "product-build-log": lambda: _historical_product_build_log(raw),
            "reboot-acceptance-timeline": lambda: _historical_reboot_timeline(raw),
            "drive-summary-json": lambda: _historical_drive_summary(raw, requirements),
        }[evidence_type]
        reason = verifier()
        if reason is None:
            return True
        violations.append(f"{label}: {reason}")
    return False


def _verify_fixed_supporting_evidence(
    item: Mapping[str, object],
    proof: Mapping[str, object],
    *,
    label: str,
    requirements: Mapping[str, object],
    violations: list[str],
    identities: list[str],
    runtime_evidence_digests: set[str],
) -> bool:
    dimension = _text(requirements.get("dimension"))
    if dimension == "runtime":
        return True
    source = _mapping(item.get("supporting_evidence"))
    if not source:
        violations.append(f"{label}: fixed supporting evidence is required")
        return False
    evidence_type = _text(source.get("evidence_type"))
    if evidence_type not in FRESH_SUPPORT_TYPES.get(dimension, set()):
        violations.append(
            f"{label}: fixed supporting evidence type does not match {dimension}"
        )
        return False
    expected = _expected_sha256(source.get("sha256"))
    proof_binding = _mapping(proof.get("supporting_evidence"))
    if (
        proof_binding.get("evidence_type") != evidence_type
        or _expected_sha256(proof_binding.get("sha256")) != expected
    ):
        violations.append(
            f"{label}: structured proof does not bind its supporting evidence"
        )
        return False
    path_text = _text(source.get("path"))
    if not path_text or not expected:
        violations.append(
            f"{label}.supporting_evidence: path and sha256 are required"
        )
        return False
    path = Path(path_text).expanduser().absolute()
    if not path.is_file():
        violations.append(
            f"{label}.supporting_evidence: file is unavailable: {path}"
        )
        return False
    try:
        raw = path.read_bytes()
    except OSError as error:
        violations.append(
            f"{label}.supporting_evidence: cannot read evidence content: {error}"
        )
        return False
    actual = _digest_bytes(raw)
    if actual != expected:
        violations.append(
            f"{label}.supporting_evidence: digest mismatch: expected {expected}, actual {actual}"
        )
        return False
    if actual not in runtime_evidence_digests:
        violations.append(
            f"{label}.supporting_evidence: evidence is not attached to the Runtime Run"
        )
        return False
    accepted = _verify_historical_evidence(
        source,
        raw=raw,
        label=f"{label}.supporting_evidence",
        requirements=requirements,
        violations=violations,
    )
    if accepted:
        identities.append(
            f"file:{label}.supporting_evidence:{actual}:{len(raw)}"
        )
    return accepted


def _verify_structured_proof(
    item: Mapping[str, object],
    *,
    raw: bytes,
    label: str,
    requirements: Mapping[str, object],
    violations: list[str],
    identities: list[str],
    runtime_evidence_digests: set[str],
    parsed_proofs: list[Mapping[str, object]],
    required: bool,
) -> bool:
    if not required:
        return _verify_historical_evidence(
            item,
            raw=raw,
            label=label,
            requirements=requirements,
            violations=violations,
        )
    try:
        proof = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        proof = None
    if not isinstance(proof, Mapping) or proof.get("schema") != PROOF_SCHEMA:
        violations.append(f"{label}: Runtime evidence requires a structured proof")
        return False
    parsed_proofs.append(proof)
    accepted = _verify_fixed_supporting_evidence(
        item,
        proof,
        label=label,
        requirements=requirements,
        violations=violations,
        identities=identities,
        runtime_evidence_digests=runtime_evidence_digests,
    )
    for field, expected in requirements.items():
        actual = _nested_value(proof, field)
        if actual != expected:
            field_label = (
                f"artifact identity {field.removeprefix('artifact.')}"
                if field.startswith("artifact.")
                else field
            )
            violations.append(
                f"{label}: structured proof {field_label} mismatch: expected {expected!r}, actual {actual!r}"
            )
            accepted = False
    dimension = _text(requirements.get("dimension"))
    if dimension == "diagnosis" and not _sequence(proof.get("evidence_ids")):
        violations.append(f"{label}: diagnosis proof requires evidence_ids")
        accepted = False
    elif dimension == "official_ut":
        tests_run = proof.get("tests_run")
        tests_failed = proof.get("tests_failed")
        if (
            isinstance(tests_run, bool)
            or not isinstance(tests_run, int)
            or tests_run <= 0
            or tests_failed != 0
        ):
            violations.append(
                f"{label}: official UT proof requires tests_run>0 and tests_failed=0"
            )
            accepted = False
    elif dimension == "build":
        compiled_units = proof.get("compiled_units")
        if (
            isinstance(compiled_units, bool)
            or not isinstance(compiled_units, int)
            or compiled_units <= 0
        ):
            violations.append(f"{label}: build proof requires compiled_units>0")
            accepted = False
    elif dimension in {"freshness", "hardware"}:
        if _timestamp(proof.get("observed_at")) is None:
            violations.append(
                f"{label}: {dimension} proof requires a timezone-aware observed_at"
            )
            accepted = False
    elif dimension in {"runtime", "upgrade"}:
        if _timestamp(proof.get("completed_at")) is None:
            violations.append(
                f"{label}: {dimension} proof requires a timezone-aware completed_at"
            )
            accepted = False
    return accepted


def _verify_fresh_timeline(
    proofs: Mapping[str, list[Mapping[str, object]]],
    *,
    max_age: object,
    violations: list[str],
) -> None:
    if (
        isinstance(max_age, bool)
        or not isinstance(max_age, int)
        or max_age <= 0
        or max_age > 86_400
    ):
        violations.append(
            "freshness: max_age_seconds must be an integer from 1 through 86400"
        )
        return
    if not all(proofs.values()):
        return
    runtime_completed = [
        _timestamp(proof.get("completed_at")) for proof in proofs["runtime"]
    ]
    upgrade_completed = [
        _timestamp(proof.get("completed_at")) for proof in proofs["upgrade"]
    ]
    freshness_observed = [
        _timestamp(proof.get("observed_at")) for proof in proofs["freshness"]
    ]
    hardware_observed = [
        _timestamp(proof.get("observed_at")) for proof in proofs["hardware"]
    ]
    if any(
        timestamp is None
        for timestamps in (
            runtime_completed,
            upgrade_completed,
            freshness_observed,
            hardware_observed,
        )
        for timestamp in timestamps
    ):
        return
    runtime_times = [timestamp for timestamp in runtime_completed if timestamp]
    upgrade_times = [timestamp for timestamp in upgrade_completed if timestamp]
    freshness_times = [timestamp for timestamp in freshness_observed if timestamp]
    hardware_times = [timestamp for timestamp in hardware_observed if timestamp]
    latest_target = max(*freshness_times, *hardware_times)
    earliest_upgrade = min(upgrade_times)
    latest_upgrade = max(upgrade_times)
    earliest_runtime = min(runtime_times)
    if min(freshness_times) < latest_upgrade:
        violations.append("freshness evidence predates upgrade completion")
    if min(hardware_times) < latest_upgrade:
        violations.append("hardware evidence predates upgrade completion")
    if (latest_target - earliest_upgrade).total_seconds() > max_age:
        violations.append("freshness evidence exceeds max_age_seconds after upgrade")
    if earliest_runtime < latest_target:
        violations.append("Runtime terminal Outcome predates target acceptance evidence")


def _runtime_ledger(
    value: object,
    *,
    repository_path: Path | None,
    target: str,
    run_id: str,
    violations: list[str],
    identities: list[str],
) -> tuple[bool, set[str]]:
    repository_ref = _mapping(_mapping(value).get("repository"))
    expected = _expected_sha256(repository_ref.get("sha256"))
    if repository_path is None or not expected:
        violations.append(
            "runtime.repository: fresh Runtime evidence requires an operator-selected "
            "repository and sha256"
        )
        return False, set()
    path = repository_path.expanduser().absolute()
    if not path.is_file():
        violations.append(f"runtime.repository: file is unavailable: {path}")
        return False, set()
    try:
        repository = SQLiteRuntimeRepository(path)
        projection = repository.load(run_id)
        events = repository.events(run_id)
    except (OSError, ValueError, sqlite3.DatabaseError) as error:
        violations.append(f"runtime.repository: cannot replay Run ledger: {error}")
        return False, set()
    actual = _digest_bytes(_json_bytes(events))
    if actual != expected:
        violations.append(
            f"runtime.repository: ledger digest mismatch: expected {expected}, actual {actual}"
        )
        return False, set()
    if not isinstance(projection, Mapping):
        violations.append("runtime.repository: Run is unavailable")
        return False, set()
    targets = [
        _mapping(item)
        for item in _sequence(projection.get("targets"))
        if isinstance(item, Mapping)
    ]
    matched_targets = [
        item
        for item in targets
        if target in {_text(item.get("target_id")), _text(item.get("address"))}
    ]
    if not matched_targets:
        violations.append("runtime.repository: target does not match the Run ledger")
    matched_target_ids = {
        _text(item.get("target_id")) or _text(item.get("address"))
        for item in matched_targets
    }
    matched_target_ids.discard("")
    outcome = _mapping(projection.get("run_outcome"))
    if _text(outcome.get("status")) != "completed":
        violations.append("runtime.repository: terminal Outcome is not completed")
    outcome_revisions = [
        int(event.get("revision", 0))
        for event in events
        if event.get("kind") == "RunOutcomeRecorded"
    ]
    if not outcome_revisions:
        violations.append("runtime.repository: RunOutcomeRecorded is missing")
        return False, set()
    outcome_revision = min(outcome_revisions)
    evidence_digests = {
        _text(_mapping(_mapping(event.get("payload")).get("evidence")).get("blob_id"))
        for event in events
        if event.get("kind") == "EvidenceAttached"
        and int(event.get("revision", 0)) < outcome_revision
        and _text(
            _mapping(_mapping(event.get("payload")).get("evidence")).get(
                "target_id"
            )
        )
        in matched_target_ids
    }
    evidence_digests.discard("")
    accepted = not any(item.startswith("runtime.repository:") for item in violations)
    if accepted:
        identities.append(
            f"runtime-ledger:{run_id}:{actual}:{outcome_revision}:{len(evidence_digests)}"
        )
    return accepted, evidence_digests


def _file_identity(
    item: Mapping[str, object],
    *,
    label: str,
    violations: list[str],
    identities: list[str],
    proof_requirements: Mapping[str, object] | None = None,
    structured_proof_required: bool = False,
    runtime_evidence_digests: set[str] | None = None,
    parsed_proofs: list[Mapping[str, object]] | None = None,
) -> bool:
    path_text = _text(item.get("path"))
    expected = _expected_sha256(item.get("sha256"))
    if not path_text:
        violations.append(f"{label}: evidence path is required")
        return False
    if not expected:
        violations.append(f"{label}: evidence sha256 must be 64 lowercase hex characters")
        return False
    path = Path(path_text).expanduser().absolute()
    if not path.is_file():
        violations.append(f"{label}: evidence file is unavailable: {path}")
        return False
    try:
        raw = path.read_bytes()
    except OSError as error:
        violations.append(f"{label}: cannot read evidence content: {error}")
        return False
    actual = _digest_bytes(raw)
    if actual != expected:
        violations.append(
            f"{label}: evidence digest mismatch: expected {expected}, actual {actual}"
        )
        return False
    content_valid = _verify_structured_proof(
        item,
        raw=raw,
        label=label,
        requirements=proof_requirements or {},
        violations=violations,
        identities=identities,
        runtime_evidence_digests=runtime_evidence_digests or set(),
        parsed_proofs=parsed_proofs if parsed_proofs is not None else [],
        required=structured_proof_required,
    )
    if (
        content_valid
        and structured_proof_required
        and actual not in (runtime_evidence_digests or set())
    ):
        violations.append(f"{label}: evidence is not attached to the Runtime Run")
        content_valid = False
    if content_valid:
        identities.append(f"file:{label}:{actual}:{len(raw)}")
    return content_valid


def _evidence_dimension(
    value: object,
    *,
    label: str,
    accepted_statuses: set[str],
    violations: list[str],
    gaps: list[str],
    identities: list[str],
    proof_requirements: Mapping[str, object],
    structured_proof_required: bool,
    runtime_evidence_digests: set[str] | None = None,
    parsed_proofs: list[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status")) or "not_reported"
    evidence = _sequence(document.get("evidence"))
    valid_evidence = 0
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            violations.append(f"{label}.evidence[{index}] must be an object")
            continue
        if _file_identity(
            item,
            label=f"{label}.evidence[{index}]",
            violations=violations,
            identities=identities,
            proof_requirements=proof_requirements,
            structured_proof_required=structured_proof_required,
            runtime_evidence_digests=runtime_evidence_digests,
            parsed_proofs=parsed_proofs,
        ):
            valid_evidence += 1
    accepted = status in accepted_statuses and valid_evidence == len(evidence) and bool(evidence)
    if status not in accepted_statuses:
        gaps.append(f"{label}={status}")
    elif not evidence:
        gaps.append(f"{label}_evidence_missing")
    return {
        "status": status,
        "accepted": accepted,
        "evidence_count": len(evidence),
        "verified_evidence_count": valid_evidence,
    }


def _source_dimension(
    value: object,
    *,
    violations: list[str],
    gaps: list[str],
    identities: list[str],
) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status")) or "not_reported"
    repositories = _sequence(document.get("repositories"))
    verified = 0
    for index, item in enumerate(repositories):
        if not isinstance(item, Mapping):
            violations.append(f"source.repositories[{index}] must be an object")
            continue
        repository = _mapping(item)
        name = _text(repository.get("name")) or f"repository-{index + 1}"
        path = Path(_text(repository.get("path"))).expanduser().absolute()
        expected = _text(repository.get("commit")).lower()
        if not path.is_dir() or not (path / ".git").exists():
            violations.append(f"source.{name}: git repository is unavailable: {path}")
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            violations.append(f"source.{name}: commit must be a full Git SHA")
            continue
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            text=True,
            capture_output=True,
            check=False,
        )
        actual = completed.stdout.strip().lower()
        if completed.returncode != 0 or actual != expected:
            violations.append(
                f"source.{name}: commit mismatch: expected {expected}, actual {actual or 'unavailable'}"
            )
            continue
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            text=True,
            capture_output=True,
            check=False,
        )
        if dirty.returncode != 0 or dirty.stdout.strip():
            violations.append(f"source.{name}: repository is not clean at the recorded commit")
            continue
        identities.append(f"git:{name}:{actual}")
        verified += 1
    accepted = status == "completed" and bool(repositories) and verified == len(repositories)
    if status != "completed":
        gaps.append(f"source={status}")
    elif not repositories:
        gaps.append("source_repositories_missing")
    return {
        "status": status,
        "accepted": accepted,
        "repository_count": len(repositories),
        "verified_repository_count": verified,
    }


def _artifact_dimension(
    value: object,
    *,
    violations: list[str],
    gaps: list[str],
    identities: list[str],
) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status")) or "not_reported"
    path_text = _text(document.get("path"))
    expected = _expected_sha256(document.get("sha256"))
    expected_size = document.get("size")
    version = _text(document.get("version"))
    accepted = False
    actual_size = 0
    if status != "verified":
        gaps.append(f"artifact={status}")
    elif (
        not path_text
        or not expected
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or not version
    ):
        violations.append(
            "artifact: verified identity requires path, sha256, integer size, and version"
        )
    else:
        path = Path(path_text).expanduser().absolute()
        if not path.is_file():
            violations.append(f"artifact: file is unavailable: {path}")
        else:
            try:
                raw = path.read_bytes()
            except OSError as error:
                violations.append(f"artifact: cannot read content: {error}")
                raw = b""
            actual_size = len(raw)
            actual = _digest_bytes(raw)
            if actual != expected:
                violations.append(
                    f"artifact: digest mismatch: expected {expected}, actual {actual}"
                )
            elif actual_size != expected_size:
                violations.append(
                    f"artifact: size mismatch: expected {expected_size}, actual {actual_size}"
                )
            else:
                identities.append(f"artifact:{version}:{actual}:{actual_size}")
                accepted = True
    return {
        "status": status,
        "accepted": accepted,
        "version": version,
        "size": actual_size,
    }


def _hardware_dimension(
    value: object,
    *,
    case_required_protocols: tuple[str, ...],
    violations: list[str],
    gaps: list[str],
    identities: list[str],
    target: str,
    run_id: str,
    structured_proof_required: bool,
    runtime_evidence_digests: set[str] | None = None,
    parsed_proofs: list[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status")) or "not_reported"
    declared = tuple(_text(item) for item in _sequence(document.get("required_protocols")))
    devices = [
        _mapping(item) for item in _sequence(document.get("devices")) if isinstance(item, Mapping)
    ]
    evidence_result = _evidence_dimension(
        document,
        label="hardware",
        accepted_statuses={"covered"},
        violations=violations,
        gaps=gaps,
        identities=identities,
        proof_requirements={
            "dimension": "hardware",
            "status": "covered",
            "target": target,
            "run_id": run_id,
            "required_protocols": list(case_required_protocols),
            "devices": devices,
        },
        structured_proof_required=structured_proof_required,
        runtime_evidence_digests=runtime_evidence_digests,
        parsed_proofs=parsed_proofs,
    )
    invalid_protocols = sorted(
        protocol
        for protocol in (*declared, *case_required_protocols)
        if protocol not in PROTOCOLS
    )
    if invalid_protocols:
        violations.append(
            "hardware: unsupported protocols: " + ", ".join(invalid_protocols)
        )
    if declared != case_required_protocols:
        violations.append(
            "hardware: required_protocols do not match the immutable case scope"
        )
    observed = {
        _text(device.get("protocol"))
        for device in devices
        if _text(device.get("device_id")) and _text(device.get("protocol")) in PROTOCOLS
    }
    missing = sorted(set(case_required_protocols) - observed)
    if missing:
        gaps.append("hardware_protocols_missing=" + ",".join(missing))
    accepted = bool(evidence_result["accepted"]) and not missing and declared == case_required_protocols
    return {
        **evidence_result,
        "accepted": accepted,
        "required_protocols": list(declared),
        "observed_protocols": sorted(observed),
        "device_count": len(devices),
    }


def _runtime_dimension(
    value: object,
    *,
    mode: str,
    violations: list[str],
    gaps: list[str],
    identities: list[str],
    target: str,
    runtime_repository: Path | None,
    parsed_proofs: list[Mapping[str, object]],
) -> tuple[dict[str, object], set[str]]:
    document = _mapping(value)
    run_id = _text(document.get("run_id"))
    outcome = _text(document.get("terminal_outcome")) or "unavailable"
    evidence = _sequence(document.get("evidence"))
    ledger_accepted = True
    runtime_evidence_digests: set[str] = set()
    if mode == "fresh-runtime":
        ledger_accepted, runtime_evidence_digests = _runtime_ledger(
            document,
            repository_path=runtime_repository,
            target=target,
            run_id=run_id,
            violations=violations,
            identities=identities,
        )
    verified = 0
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            violations.append(f"runtime.evidence[{index}] must be an object")
            continue
        if _file_identity(
            item,
            label=f"runtime.evidence[{index}]",
            violations=violations,
            identities=identities,
            proof_requirements={
                "dimension": "runtime",
                "status": "completed",
                "target": target,
                "run_id": run_id,
                "terminal_outcome": "completed",
            },
            structured_proof_required=mode == "fresh-runtime",
            runtime_evidence_digests=runtime_evidence_digests,
            parsed_proofs=parsed_proofs,
        ):
            verified += 1
    accepted = (
        bool(run_id)
        and outcome == "completed"
        and bool(evidence)
        and verified == len(evidence)
        and ledger_accepted
    )
    if mode == "historical-reconstruction" and not accepted:
        gaps.append("fresh_runtime_identity_unavailable")
    elif mode == "fresh-runtime":
        if not run_id:
            gaps.append("runtime_run_id_missing")
        if outcome != "completed":
            gaps.append(f"runtime_terminal_outcome={outcome}")
        if not evidence:
            gaps.append("runtime_outcome_evidence_missing")
    return (
        {
            "run_id": run_id,
            "terminal_outcome": outcome,
            "accepted": accepted,
            "evidence_count": len(evidence),
            "verified_evidence_count": verified,
            "ledger_verified": ledger_accepted if mode == "fresh-runtime" else False,
        },
        runtime_evidence_digests,
    )


def qualify(
    document: Mapping[str, object],
    *,
    runtime_repository: Path | None = None,
) -> dict[str, object]:
    manifest = dict(document)
    violations: list[str] = []
    gaps: list[str] = []
    identities: list[str] = []
    parsed_proofs: dict[str, list[Mapping[str, object]]] = {
        "runtime": [],
        "diagnosis": [],
        "official_ut": [],
        "build": [],
        "upgrade": [],
        "freshness": [],
        "hardware": [],
    }
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        violations.append(f"schema must be {EVIDENCE_SCHEMA}")
    mode = _text(manifest.get("mode"))
    if mode not in MODES:
        violations.append("mode must be fresh-runtime or historical-reconstruction")

    case = _mapping(manifest.get("case"))
    case_target = _text(case.get("target"))
    case_required = tuple(_text(item) for item in _sequence(case.get("required_protocols")))
    if not _text(case.get("name")) or not case_target:
        violations.append("case name and target are required")
    if not case_required:
        violations.append("case required_protocols must be non-empty")

    runtime_document = _mapping(manifest.get("runtime"))
    run_id = _text(runtime_document.get("run_id"))
    artifact_document = _mapping(manifest.get("artifact"))
    artifact_sha256 = _expected_sha256(artifact_document.get("sha256"))
    artifact_requirements = {
        "artifact.sha256": artifact_sha256,
        "artifact.size": artifact_document.get("size"),
        "artifact.version": _text(artifact_document.get("version")),
    }
    source_commits = [
        _text(_mapping(item).get("commit")).lower()
        for item in _sequence(_mapping(manifest.get("source")).get("repositories"))
        if isinstance(item, Mapping)
    ]
    runtime_result, runtime_evidence_digests = _runtime_dimension(
        manifest.get("runtime"),
        mode=mode,
        violations=violations,
        gaps=gaps,
        identities=identities,
        target=case_target,
        runtime_repository=runtime_repository,
        parsed_proofs=parsed_proofs["runtime"],
    )
    dimensions = {
        "runtime": runtime_result,
        "diagnosis": _evidence_dimension(
            manifest.get("diagnosis"),
            label="diagnosis",
            accepted_statuses={"passed"},
            violations=violations,
            gaps=gaps,
            identities=identities,
            proof_requirements={
                "dimension": "diagnosis",
                "status": "passed",
                "target": case_target,
                "run_id": run_id,
            },
            structured_proof_required=mode == "fresh-runtime",
            runtime_evidence_digests=runtime_evidence_digests,
            parsed_proofs=parsed_proofs["diagnosis"],
        ),
        "source": _source_dimension(
            manifest.get("source"),
            violations=violations,
            gaps=gaps,
            identities=identities,
        ),
    }
    validation = _mapping(manifest.get("validation"))
    dimensions["official_ut"] = _evidence_dimension(
        validation.get("official_ut"),
        label="official_ut",
        accepted_statuses={"passed"},
        violations=violations,
        gaps=gaps,
        identities=identities,
        proof_requirements={
            "dimension": "official_ut",
            "status": "passed",
            "target": case_target,
            "run_id": run_id,
            "source_commits": source_commits,
        },
        structured_proof_required=mode == "fresh-runtime",
        runtime_evidence_digests=runtime_evidence_digests,
        parsed_proofs=parsed_proofs["official_ut"],
    )
    dimensions["build"] = _evidence_dimension(
        validation.get("build"),
        label="build",
        accepted_statuses={"compiled"},
        violations=violations,
        gaps=gaps,
        identities=identities,
        proof_requirements={
            "dimension": "build",
            "status": "compiled",
            "target": case_target,
            "run_id": run_id,
            "source_commits": source_commits,
        },
        structured_proof_required=mode == "fresh-runtime",
        runtime_evidence_digests=runtime_evidence_digests,
        parsed_proofs=parsed_proofs["build"],
    )
    dimensions["artifact"] = _artifact_dimension(
        manifest.get("artifact"),
        violations=violations,
        gaps=gaps,
        identities=identities,
    )
    dimensions["upgrade"] = _evidence_dimension(
        manifest.get("upgrade"),
        label="upgrade",
        accepted_statuses={"completed"},
        violations=violations,
        gaps=gaps,
        identities=identities,
        proof_requirements={
            "dimension": "upgrade",
            "status": "completed",
            "target": case_target,
            "run_id": run_id,
            **artifact_requirements,
            "installed_version": _text(artifact_document.get("version")),
        },
        structured_proof_required=mode == "fresh-runtime",
        runtime_evidence_digests=runtime_evidence_digests,
        parsed_proofs=parsed_proofs["upgrade"],
    )
    dimensions["freshness"] = _evidence_dimension(
        manifest.get("freshness"),
        label="freshness",
        accepted_statuses={"fresh"},
        violations=violations,
        gaps=gaps,
        identities=identities,
        proof_requirements={
            "dimension": "freshness",
            "status": "fresh",
            "target": case_target,
            "run_id": run_id,
            "artifact_sha256": artifact_sha256,
        },
        structured_proof_required=mode == "fresh-runtime",
        runtime_evidence_digests=runtime_evidence_digests,
        parsed_proofs=parsed_proofs["freshness"],
    )
    dimensions["hardware"] = _hardware_dimension(
        manifest.get("hardware"),
        case_required_protocols=case_required,
        violations=violations,
        gaps=gaps,
        identities=identities,
        target=case_target,
        run_id=run_id,
        structured_proof_required=mode == "fresh-runtime",
        runtime_evidence_digests=runtime_evidence_digests,
        parsed_proofs=parsed_proofs["hardware"],
    )
    if mode == "fresh-runtime":
        _verify_fresh_timeline(
            {
                dimension: parsed_proofs[dimension]
                for dimension in ("runtime", "upgrade", "freshness", "hardware")
            },
            max_age=_mapping(manifest.get("freshness")).get("max_age_seconds"),
            violations=violations,
        )

    product_dimensions = tuple(name for name in dimensions if name != "runtime")
    product_qualified = not violations and all(
        bool(_mapping(dimensions[name]).get("accepted")) for name in product_dimensions
    )
    qualified = product_qualified and (
        mode == "historical-reconstruction"
        or bool(_mapping(dimensions["runtime"]).get("accepted"))
    )
    promotable = (
        qualified
        and mode == "fresh-runtime"
        and bool(_mapping(dimensions["runtime"]).get("accepted"))
    )
    claim_level = (
        "fresh-runtime-product-closed"
        if promotable
        else "historical-product-validated"
        if qualified and mode == "historical-reconstruction"
        else "unqualified"
    )
    return {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "case": {
            "name": _text(case.get("name")),
            "target": case_target,
            "required_protocols": list(case_required),
        },
        "manifest_digest": "sha256:" + _digest_bytes(_json_bytes(manifest)),
        "evidence_digest": "sha256:" + _digest_bytes(
            "\n".join(sorted(identities)).encode("utf-8")
        ),
        "dimensions": dimensions,
        "qualified": qualified,
        "promotable": promotable,
        "claim_level": claim_level,
        "gaps": list(dict.fromkeys(gaps)),
        "violations": list(dict.fromkeys(violations)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify immutable product-closeout evidence."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--runtime-repository",
        type=Path,
        help=(
            "Operator-selected Runtime SQLite ledger. Required for fresh-runtime "
            "qualification; manifest repository paths are not trusted as authority."
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.manifest.expanduser().read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError("manifest must be a JSON object")
        report = qualify(document, runtime_repository=args.runtime_repository)
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
