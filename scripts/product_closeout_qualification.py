#!/usr/bin/env python3
"""Qualify immutable product-closeout evidence through one Operator/CI seam."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    ContextRuntimeError,
    SQLiteRuntimeRepository,
)
from scripts.runtime_ledger_snapshot import stable_runtime_ledger_copy  # noqa: E402


EVIDENCE_SCHEMA = "openubmc-agent-workflow.product-closeout-evidence.v1"
REPORT_SCHEMA = "openubmc-agent-workflow.product-closeout-qualification.v1"
PROOF_SCHEMA = "openubmc-agent-workflow.product-closeout-proof.v1"
MODES = {"fresh-runtime", "historical-reconstruction"}
PROTOCOLS = {"NVMe", "SATA", "SAS"}


class DriveProtocol(IntEnum):
    SATA = 3
    SAS = 4
    NVME = 6


class DrivePresence(IntEnum):
    ABSENT = 0
    PRESENT = 1


class DriveHealth(IntEnum):
    OK = 0


class DriveController(IntEnum):
    DIRECT = 255


DRIVE_PROTOCOL_LABEL = {
    DriveProtocol.SATA: "SATA",
    DriveProtocol.SAS: "SAS",
    DriveProtocol.NVME: "NVMe",
}
DRIVE_PROTOCOL_BY_CODE = {
    int(protocol): label for protocol, label in DRIVE_PROTOCOL_LABEL.items()
}
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
    "upgrade": {"runtime-upgrade-evidence"},
    "freshness": {"runtime-debug-evidence"},
    "hardware": {"runtime-debug-evidence"},
    "recovery": {"firmware-recovery-artifact-record"},
}


@dataclass(frozen=True)
class RuntimeEvidenceFact:
    digest: str
    operation_id: str
    producer: str
    evidence_type: str
    byte_count: int
    target_id: str
    target_epoch: int | None
    observed_at: datetime
    revision: int


@dataclass(frozen=True)
class RuntimeLedgerFacts:
    evidence_by_digest: Mapping[str, tuple[RuntimeEvidenceFact, ...]]
    outcome_revision: int
    outcome_at: datetime
    build_artifact_ref: Mapping[str, object]
    build_source_revision: str
    upgrade_operation_id: str
    upgrade_started_revision: int
    upgrade_completed_at: datetime
    upgrade_target_epoch: int
    debug_operation_id: str
    debug_observed_at: datetime
    debug_target_epoch: int


EMPTY_RUNTIME_FACTS = RuntimeLedgerFacts(
    evidence_by_digest={},
    outcome_revision=0,
    outcome_at=datetime.fromtimestamp(0, tz=UTC),
    build_artifact_ref={},
    build_source_revision="",
    upgrade_operation_id="",
    upgrade_started_revision=0,
    upgrade_completed_at=datetime.fromtimestamp(0, tz=UTC),
    upgrade_target_epoch=0,
    debug_operation_id="",
    debug_observed_at=datetime.fromtimestamp(0, tz=UTC),
    debug_target_epoch=0,
)


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


def _unix_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


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


def _runtime_upgrade_evidence(
    raw: bytes, requirements: Mapping[str, object]
) -> str | None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Runtime Upgrade evidence must be UTF-8 JSON"
    if not isinstance(document, Mapping):
        return "Runtime Upgrade evidence must be a JSON object"
    journal = _mapping(document.get("journal"))
    verification = _mapping(document.get("verification"))
    mutation = _mapping(document.get("mutation"))
    operation_id = _text(document.get("operation_id"))
    trusted_operation_id = _text(requirements.get("runtime.operation_id"))
    if (
        not operation_id
        or operation_id != trusted_operation_id
        or _text(document.get("action")) != "upgrade"
        or _text(journal.get("operation_id")) != operation_id
        or _text(journal.get("action")) != "upgrade"
        or _text(journal.get("stage")) != "verified"
        or journal.get("effects_started") is not True
    ):
        return "Runtime Upgrade evidence does not contain one verified upgrade journal"
    if not mutation:
        return "Runtime Upgrade evidence is missing its mutation result"
    parameters = _mapping(mutation.get("parameters"))
    if (
        parameters.get("ActiveMode") != "ResetBMC"
        or parameters.get("ForceUpdate") is not True
    ):
        return "Runtime Upgrade evidence does not request a forced BMC reset"
    manager_before = _mapping(mutation.get("manager_before"))
    manager_after = _mapping(verification.get("version"))
    reset_before = _timestamp(manager_before.get("last_reset_time"))
    reset_after = _timestamp(manager_after.get("last_reset_time"))
    if reset_before is None or reset_after is None or reset_after <= reset_before:
        return "Runtime Upgrade evidence does not prove a BMC reboot boundary"
    expected_artifact = _text(requirements.get("artifact.sha256"))
    if _expected_sha256(journal.get("expected_checksum")) != expected_artifact:
        return "Runtime Upgrade evidence artifact digest does not match the qualified HPM"
    if _text(verification.get("installed_version")) != _text(
        requirements.get("installed_version")
    ):
        return "Runtime Upgrade evidence installed version does not match the artifact"
    epoch_before = document.get("epoch_before")
    epoch_after = document.get("epoch_after")
    if (
        isinstance(epoch_before, bool)
        or not isinstance(epoch_before, int)
        or epoch_before < 0
        or isinstance(epoch_after, bool)
        or not isinstance(epoch_after, int)
        or epoch_after <= epoch_before
        or journal.get("epoch_before") != epoch_before
        or journal.get("epoch_after") != epoch_after
        or verification.get("target_epoch") != epoch_after
        or epoch_after != requirements.get("runtime.target_epoch")
    ):
        return "Runtime Upgrade evidence does not prove one monotonic verified target epoch"
    return None


def _runtime_property(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value.strip()


def _runtime_debug_evidence(
    raw: bytes, requirements: Mapping[str, object]
) -> str | None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Runtime Debug evidence must be UTF-8 JSON"
    if not isinstance(document, Mapping) or document.get("ok") is not True:
        return "Runtime Debug evidence is not a successful native result"
    observed_at = _timestamp(document.get("observed_at"))
    if observed_at is None:
        return "Runtime Debug evidence requires a timezone-aware observed_at"
    upgrade_completed_at = _timestamp(requirements.get("runtime.upgrade_completed_at"))
    if upgrade_completed_at is not None and observed_at < upgrade_completed_at:
        return "Runtime Debug evidence observed_at predates the trusted upgrade completion"
    target_epoch = document.get("target_epoch")
    expected_target_epoch = requirements.get("runtime.target_epoch")
    if (
        expected_target_epoch is not None
        and (
            isinstance(target_epoch, bool)
            or not isinstance(target_epoch, int)
            or target_epoch != expected_target_epoch
        )
    ):
        return "Runtime Debug evidence target epoch does not match Runtime provenance"
    result = _mapping(document.get("result"))
    freshness = _mapping(result.get("freshness"))
    if (
        _text(freshness.get("status")) not in {"complete", "fresh"}
        or freshness.get("complete") is not True
        or freshness.get("after_last_reboot_or_change") is not True
        or _sequence(freshness.get("stale_evidence"))
        or _sequence(freshness.get("lost_dimensions"))
        or _sequence(freshness.get("unavailable_dimensions"))
    ):
        return "Runtime Debug evidence is incomplete, stale, or predates the last change"
    if _text(requirements.get("dimension")) == "freshness":
        return None
    ssh = _mapping(_mapping(result.get("lanes")).get("ssh"))
    drives: dict[int, dict[str, object]] = {}
    for name, raw_lane in ssh.items():
        if not name.startswith("mdbctl_expand_") or not isinstance(raw_lane, Mapping):
            continue
        lane = _mapping(raw_lane)
        if lane.get("ok") is not True:
            continue
        properties = _mapping(_mapping(lane.get("result")).get("properties"))
        drive = _mapping(properties.get("bmc.kepler.Systems.Storage.Drive"))
        status = _mapping(
            properties.get("bmc.kepler.Systems.Storage.Drive.DriveStatus")
        )
        inventory = _mapping(properties.get("bmc.kepler.Inventory.Hardware"))
        drive_id = _runtime_property(drive.get("Id"))
        if isinstance(drive_id, bool) or not isinstance(drive_id, int):
            continue
        protocol_value = _runtime_property(drive.get("Protocol"))
        controller = _runtime_property(drive.get("RefControllerId"))
        resource = _runtime_property(drive.get("ResourceId"))
        presence = _runtime_property(drive.get("Presence"))
        health = _runtime_property(status.get("Health"))
        serial = _text(_runtime_property(inventory.get("SerialNumber")))
        try:
            protocol = DriveProtocol(protocol_value)
        except (TypeError, ValueError):
            return f"Runtime Debug Drive{drive_id} has an unsupported protocol"
        if (
            presence != DrivePresence.PRESENT
            or health != DriveHealth.OK
            or not serial
        ):
            return f"Runtime Debug Drive{drive_id} is not healthy, present, and identified"
        if protocol == DriveProtocol.NVME and (
            controller != DriveController.DIRECT
            or not isinstance(resource, int)
            or isinstance(resource, bool)
            or resource <= 0
        ):
            return f"Runtime Debug Drive{drive_id} NVMe resource attribution is invalid"
        if protocol in {DriveProtocol.SATA, DriveProtocol.SAS} and resource != 0:
            return f"Runtime Debug Drive{drive_id} SATA/SAS ResourceId must be zero"
        drives[drive_id] = {
            "protocol": DRIVE_PROTOCOL_LABEL[protocol],
            "controller": controller,
            "resource": resource,
        }
    expected_devices: dict[int, str] = {}
    for device in _sequence(requirements.get("devices")):
        if not isinstance(device, Mapping):
            continue
        match = re.fullmatch(r"(?:Drive|Disk)(\d+)", _text(device.get("device_id")))
        if match is not None:
            expected_devices[int(match.group(1))] = _text(device.get("protocol"))
    if not expected_devices:
        return "Runtime Debug hardware proof has no scoped devices"
    missing = sorted(set(expected_devices) - set(drives))
    if missing:
        return "Runtime Debug evidence is missing scoped drives: " + ", ".join(
            f"Drive{drive_id}" for drive_id in missing
        )
    mismatched = sorted(
        drive_id
        for drive_id, protocol in expected_devices.items()
        if drives[drive_id]["protocol"] != protocol
    )
    if mismatched:
        return "Runtime Debug evidence protocol does not match scoped drives: " + ", ".join(
            f"Drive{drive_id}" for drive_id in mismatched
        )
    required_protocols = {_text(item) for item in _sequence(requirements.get("required_protocols"))}
    observed_protocols = {drives[drive_id]["protocol"] for drive_id in expected_devices}
    if not required_protocols.issubset(observed_protocols):
        return "Runtime Debug evidence does not cover every required protocol"
    return None


def _recovery_artifact_record(
    raw: bytes, requirements: Mapping[str, object]
) -> str | None:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "recovery artifact record must be UTF-8 JSON"
    if (
        not isinstance(document, Mapping)
        or document.get("schema")
        != "openubmc-agent-workflow/recovery-artifact-record-v1"
    ):
        return "recovery artifact record schema is unsupported"
    artifact = _mapping(document.get("artifact"))
    expected = {
        "path": _text(requirements.get("artifact.path")),
        "sha256": _expected_sha256(requirements.get("artifact.sha256")),
        "size": requirements.get("artifact.size"),
        "version": _text(requirements.get("artifact.version")),
    }
    actual = {
        "path": _text(artifact.get("path")),
        "sha256": _expected_sha256(artifact.get("sha256")),
        "size": artifact.get("size"),
        "version": _text(artifact.get("version")),
    }
    if actual != expected:
        return "recovery artifact record does not match the qualified recovery package"
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
    direct = [
        drive
        for drive in drives
        if drive.get("controller") == DriveController.DIRECT
    ]
    raid = [
        drive
        for drive in drives
        if drive.get("controller") != DriveController.DIRECT
    ]
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
        "health_ok": sum(
            drive.get("health") == DriveHealth.OK for drive in drives
        ),
        "presence_ok": sum(
            drive.get("presence") == DrivePresence.PRESENT for drive in drives
        ),
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
    runtime_facts: RuntimeLedgerFacts,
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
    bindings = runtime_facts.evidence_by_digest.get(actual, ())
    if not bindings:
        violations.append(
            f"{label}.supporting_evidence: evidence is not attached to the Runtime Run"
        )
        return False
    if evidence_type == "runtime-upgrade-evidence":
        native = [
            fact
            for fact in bindings
            if fact.operation_id == runtime_facts.upgrade_operation_id
            and fact.producer == "upgrade_run"
        ]
        if len(native) != 1:
            violations.append(
                f"{label}.supporting_evidence: native Upgrade evidence is not bound "
                "to the trusted upgrade_run operation"
            )
            return False
        reason = _runtime_upgrade_evidence(
            raw,
            {
                **dict(requirements),
                "runtime.operation_id": runtime_facts.upgrade_operation_id,
                "runtime.target_epoch": runtime_facts.upgrade_target_epoch,
            },
        )
        accepted = reason is None
        if reason is not None:
            violations.append(f"{label}.supporting_evidence: {reason}")
    elif evidence_type == "runtime-debug-evidence":
        native = [
            fact
            for fact in bindings
            if fact.operation_id == runtime_facts.debug_operation_id
            and fact.producer == "debug_collect"
            and fact.target_epoch == runtime_facts.debug_target_epoch
        ]
        if len(native) != 1:
            violations.append(
                f"{label}.supporting_evidence: native Debug evidence is not bound "
                "to the trusted debug_collect Observation provenance"
            )
            return False
        reason = _runtime_debug_evidence(
            raw,
            {
                **dict(requirements),
                "runtime.target_epoch": runtime_facts.debug_target_epoch,
                "runtime.upgrade_completed_at": runtime_facts.upgrade_completed_at.isoformat(),
            },
        )
        accepted = reason is None
        if reason is not None:
            violations.append(f"{label}.supporting_evidence: {reason}")
    else:
        if evidence_type == "firmware-recovery-artifact-record":
            if (
                runtime_facts.upgrade_started_revision <= 0
                or not any(
                    fact.revision < runtime_facts.upgrade_started_revision
                    for fact in bindings
                )
            ):
                violations.append(
                    f"{label}.supporting_evidence: recovery artifact must be "
                    "attached before upgrade_run starts"
                )
                return False
            recovery_digest = _expected_sha256(
                requirements.get("artifact.sha256")
            )
            recovery_size = requirements.get("artifact.size")
            recovery_package_bindings = [
                fact
                for fact in runtime_facts.evidence_by_digest.get(
                    recovery_digest, ()
                )
                if fact.producer == "operator-evidence-attach"
                and fact.evidence_type == "firmware-recovery-artifact"
                and fact.byte_count == recovery_size
                and fact.revision < runtime_facts.upgrade_started_revision
            ]
            if len(recovery_package_bindings) != 1:
                violations.append(
                    f"{label}.supporting_evidence: recovery package bytes must be "
                    "digest-bound to the Runtime Run before upgrade_run starts"
                )
                return False
            reason = _recovery_artifact_record(raw, requirements)
            accepted = reason is None
            if reason is not None:
                violations.append(f"{label}.supporting_evidence: {reason}")
        else:
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
    runtime_facts: RuntimeLedgerFacts,
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
        runtime_facts=runtime_facts,
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
    elif dimension in {"runtime", "upgrade", "recovery"}:
        if _timestamp(proof.get("completed_at")) is None:
            violations.append(
                f"{label}: {dimension} proof requires a timezone-aware completed_at"
            )
            accepted = False
    return accepted


def _verify_fresh_timeline(
    runtime_facts: RuntimeLedgerFacts,
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
    if not runtime_facts.upgrade_operation_id or not runtime_facts.debug_operation_id:
        return
    if runtime_facts.debug_observed_at < runtime_facts.upgrade_completed_at:
        violations.append("freshness evidence predates upgrade completion")
    if (
        runtime_facts.debug_observed_at - runtime_facts.upgrade_completed_at
    ).total_seconds() > max_age:
        violations.append("freshness evidence exceeds max_age_seconds after upgrade")
    if runtime_facts.outcome_at < runtime_facts.debug_observed_at:
        violations.append("Runtime terminal Outcome predates target acceptance evidence")


def _runtime_ledger(
    value: object,
    *,
    repository_path: Path | None,
    target: str,
    run_id: str,
    violations: list[str],
    identities: list[str],
) -> tuple[bool, RuntimeLedgerFacts]:
    runtime_violations: list[str] = []

    def reject(message: str) -> None:
        runtime_violations.append(f"runtime.repository: {message}")

    repository_ref = _mapping(_mapping(value).get("repository"))
    expected = _expected_sha256(repository_ref.get("sha256"))
    if repository_path is None or not expected:
        reject(
            "fresh Runtime evidence requires an operator-selected repository and sha256"
        )
        violations.extend(runtime_violations)
        return False, EMPTY_RUNTIME_FACTS
    path = repository_path.expanduser().absolute()
    if not path.is_file():
        reject(f"file is unavailable: {path}")
        violations.extend(runtime_violations)
        return False, EMPTY_RUNTIME_FACTS
    try:
        with stable_runtime_ledger_copy(path) as snapshot:
            repository = SQLiteRuntimeRepository(snapshot)
            projection = repository.load(run_id)
            events = repository.events(run_id)
    except (OSError, ValueError, sqlite3.DatabaseError, ContextRuntimeError) as error:
        reject(f"cannot replay Run ledger: {error}")
        violations.extend(runtime_violations)
        return False, EMPTY_RUNTIME_FACTS
    actual = _digest_bytes(_json_bytes(events))
    if actual != expected:
        reject(f"ledger digest mismatch: expected {expected}, actual {actual}")
        violations.extend(runtime_violations)
        return False, EMPTY_RUNTIME_FACTS
    if not isinstance(projection, Mapping):
        reject("Run is unavailable")
        violations.extend(runtime_violations)
        return False, EMPTY_RUNTIME_FACTS
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
        reject("target does not match the Run ledger")
    matched_target_ids = {
        _text(item.get("target_id")) or _text(item.get("address"))
        for item in matched_targets
    }
    matched_target_ids.discard("")
    opened = next((event for event in events if event.get("kind") == "CaseOpened"), None)
    opened_payload = _mapping(_mapping(opened).get("payload"))
    authorization = _mapping(opened_payload.get("authorization"))
    allowed_actions = {_text(item) for item in _sequence(authorization.get("allowed_actions"))}
    if "upgrade" not in allowed_actions:
        reject("current task upgrade authorization is missing")
    if (
        _text(opened_payload.get("intent")) != "diagnose-and-fix"
        or _text(opened_payload.get("delivery_strategy")) != "build-upgrade"
    ):
        reject("Run is not the required diagnose-and-fix build-upgrade workflow")
    workflow = _mapping(opened_payload.get("workflow_definition"))
    workflow_steps = [
        (_text(_mapping(item).get("kind")), _text(_mapping(item).get("name")))
        for item in _sequence(workflow.get("steps"))
        if isinstance(item, Mapping)
    ]
    required_steps = [
        ("operation", "debug_run"),
        ("phase", "diagnosis.acceptance"),
        ("phase", "developer.change"),
        ("phase", "build.artifact"),
        ("operation", "upgrade_run"),
        ("operation", "debug_collect"),
    ]
    if workflow_steps != required_steps:
        reject("pinned workflow does not contain the required ordered closeout steps")
    outcome = _mapping(projection.get("run_outcome"))
    if _text(outcome.get("status")) != "completed":
        reject("terminal Outcome is not completed")
    outcome_events = [
        event
        for event in events
        if event.get("kind") == "RunOutcomeRecorded"
    ]
    if len(outcome_events) != 1:
        reject("exactly one RunOutcomeRecorded is required")
        violations.extend(runtime_violations)
        return False, EMPTY_RUNTIME_FACTS
    outcome_event = outcome_events[0]
    outcome_revision = int(outcome_event.get("revision", 0))
    outcome_at = _unix_timestamp(outcome_event.get("created_at"))
    if outcome_at is None:
        reject("RunOutcomeRecorded has no trustworthy event timestamp")
        outcome_at = EMPTY_RUNTIME_FACTS.outcome_at
    acceptance = {
        _text(_mapping(item).get("requirement_id")): _text(_mapping(item).get("status"))
        for item in _sequence(outcome.get("acceptance"))
        if isinstance(item, Mapping)
    }
    for requirement in (
        "stage.diagnosis",
        "stage.development",
        "stage.build",
        "stage.upgrade",
        "stage.verification",
    ):
        if acceptance.get(requirement) != "passed":
            reject(f"terminal Outcome does not pass {requirement}")

    phase_events: dict[str, list[tuple[int, Mapping[str, object]]]] = {}
    for event in events:
        if event.get("kind") != "RunGateSubmitted":
            continue
        phase = _mapping(_mapping(event.get("payload")).get("phase"))
        name = _text(phase.get("phase_type"))
        if name:
            phase_events.setdefault(name, []).append(
                (int(event.get("revision", 0)), phase)
            )
    phase_order: list[int] = []
    selected_phases: dict[str, Mapping[str, object]] = {}
    for name in ("diagnosis.acceptance", "developer.change", "build.artifact"):
        completed = [
            (revision, phase)
            for revision, phase in phase_events.get(name, [])
            if _text(phase.get("status")) == "completed" and revision < outcome_revision
        ]
        if not completed:
            reject(f"completed {name} Gate is missing")
            phase_order.append(0)
            selected_phases[name] = {}
            continue
        revision, phase = completed[-1]
        phase_order.append(revision)
        selected_phases[name] = phase
    if phase_order and phase_order != sorted(phase_order):
        reject("diagnosis.acceptance must precede developer.change and build.artifact")
    build_phase = _mapping(selected_phases.get("build.artifact"))
    build_artifact_ref = _mapping(build_phase.get("artifact_ref"))
    build_source_revision = _text(build_phase.get("source_revision"))
    if not build_artifact_ref or not build_source_revision:
        reject("build.artifact does not contain its ArtifactRef and source revision")

    operation_events: dict[str, dict[str, object]] = {}
    for event in events:
        operation_id = _text(event.get("operation_id"))
        if not operation_id:
            continue
        kind = _text(event.get("kind"))
        payload = _mapping(event.get("payload"))
        state = operation_events.setdefault(operation_id, {"evidence": []})
        if kind == "OperationAccepted":
            state["operation"] = _text(payload.get("operation"))
            state["target_id"] = _text(payload.get("target_id"))
            state["accepted_revision"] = int(event.get("revision", 0))
        elif kind == "OperationStarted":
            state["started_revision"] = int(event.get("revision", 0))
        elif kind in {"OperationTerminal", "OperationReconciled"}:
            state["terminal_revision"] = int(event.get("revision", 0))
            state["terminal_status"] = _text(payload.get("status"))
            state["target_epoch"] = payload.get("target_epoch")
            state["completed_at"] = event.get("created_at")
        elif kind == "EvidenceAttached":
            state["evidence"].append(event)

    def selected_operation(
        name: str, *, after_revision: int
    ) -> tuple[str, Mapping[str, object]]:
        candidates: list[tuple[int, str, Mapping[str, object]]] = []
        for operation_id, raw_state in operation_events.items():
            state = _mapping(raw_state)
            accepted_revision = int(state.get("accepted_revision", 0) or 0)
            started_revision = int(state.get("started_revision", 0) or 0)
            terminal_revision = int(state.get("terminal_revision", 0) or 0)
            if (
                _text(state.get("operation")) == name
                and _text(state.get("terminal_status")) == "completed"
                and after_revision
                < accepted_revision
                < started_revision
                < terminal_revision
                < outcome_revision
                and _text(state.get("target_id")) in matched_target_ids
            ):
                candidates.append((accepted_revision, operation_id, state))
        if len(candidates) != 1:
            reject(f"exactly one completed ordered {name} Runtime operation is required")
            return "", {}
        _revision, operation_id, state = candidates[0]
        return operation_id, state

    build_revision = phase_order[2] if len(phase_order) == 3 else 0
    diagnosis_operation_id, diagnosis_state = selected_operation(
        "debug_run", after_revision=0
    )
    diagnosis_terminal_revision = int(
        diagnosis_state.get("terminal_revision", 0) or 0
    )
    diagnosis_gate_revision = phase_order[0] if phase_order else 0
    if (
        diagnosis_operation_id
        and (
            diagnosis_gate_revision <= 0
            or diagnosis_terminal_revision >= diagnosis_gate_revision
        )
    ):
        reject("debug_run must complete before diagnosis.acceptance")
    upgrade_operation_id, upgrade_state = selected_operation(
        "upgrade_run", after_revision=build_revision
    )
    upgrade_started_revision = int(upgrade_state.get("started_revision", 0) or 0)
    upgrade_terminal_revision = int(upgrade_state.get("terminal_revision", 0) or 0)
    debug_operation_id, debug_state = selected_operation(
        "debug_collect", after_revision=upgrade_terminal_revision
    )
    upgrade_epoch = upgrade_state.get("target_epoch")
    debug_epoch = debug_state.get("target_epoch")
    if (
        isinstance(upgrade_epoch, bool)
        or not isinstance(upgrade_epoch, int)
        or upgrade_epoch <= 0
    ):
        reject("upgrade_run does not prove an advanced target epoch")
        upgrade_epoch = 0
    if (
        isinstance(debug_epoch, bool)
        or not isinstance(debug_epoch, int)
        or debug_epoch < upgrade_epoch
    ):
        reject("debug_collect does not prove the post-upgrade target epoch")
        debug_epoch = 0
    upgrade_completed_at = _unix_timestamp(upgrade_state.get("completed_at"))
    debug_observed_at = _unix_timestamp(debug_state.get("completed_at"))
    if upgrade_completed_at is None or debug_observed_at is None:
        reject("native Upgrade and Debug operation timestamps are unavailable")
        upgrade_completed_at = EMPTY_RUNTIME_FACTS.upgrade_completed_at
        debug_observed_at = EMPTY_RUNTIME_FACTS.debug_observed_at
    elif debug_observed_at < upgrade_completed_at:
        reject("debug_collect completed before upgrade_run")

    evidence_by_digest: dict[str, list[RuntimeEvidenceFact]] = {}
    for event in events:
        if event.get("kind") != "EvidenceAttached":
            continue
        revision = int(event.get("revision", 0))
        if revision >= outcome_revision:
            continue
        reference = _mapping(_mapping(event.get("payload")).get("evidence"))
        target_id = _text(reference.get("target_id"))
        if target_id not in matched_target_ids:
            continue
        digest = _text(reference.get("blob_id"))
        observed_at = _unix_timestamp(reference.get("observed_at"))
        if not digest or observed_at is None:
            continue
        target_epoch = reference.get("target_epoch")
        evidence_by_digest.setdefault(digest, []).append(
            RuntimeEvidenceFact(
                digest=digest,
                operation_id=_text(event.get("operation_id")),
                producer=_text(reference.get("producer")),
                evidence_type=_text(reference.get("evidence_type")),
                byte_count=(
                    reference.get("byte_count")
                    if isinstance(reference.get("byte_count"), int)
                    and not isinstance(reference.get("byte_count"), bool)
                    else 0
                ),
                target_id=target_id,
                target_epoch=(
                    target_epoch
                    if isinstance(target_epoch, int) and not isinstance(target_epoch, bool)
                    else None
                ),
                observed_at=observed_at,
                revision=revision,
            )
        )
    for name, operation_id in (
        ("debug_run", diagnosis_operation_id),
        ("upgrade_run", upgrade_operation_id),
        ("debug_collect", debug_operation_id),
    ):
        if operation_id and not any(
            fact.operation_id == operation_id and fact.producer == name
            for facts in evidence_by_digest.values()
            for fact in facts
        ):
            reject(f"{name} has no Runtime-owned native EvidenceAttached fact")

    violations.extend(runtime_violations)
    accepted = not runtime_violations
    facts = RuntimeLedgerFacts(
        evidence_by_digest={
            digest: tuple(items) for digest, items in evidence_by_digest.items()
        },
        outcome_revision=outcome_revision,
        outcome_at=outcome_at,
        build_artifact_ref=build_artifact_ref,
        build_source_revision=build_source_revision,
        upgrade_operation_id=upgrade_operation_id,
        upgrade_started_revision=upgrade_started_revision,
        upgrade_completed_at=upgrade_completed_at,
        upgrade_target_epoch=int(upgrade_epoch),
        debug_operation_id=debug_operation_id,
        debug_observed_at=debug_observed_at,
        debug_target_epoch=int(debug_epoch),
    )
    if accepted:
        identities.append(
            f"runtime-ledger:{run_id}:{actual}:{outcome_revision}:{len(evidence_by_digest)}"
        )
    return accepted, facts


def _file_identity(
    item: Mapping[str, object],
    *,
    label: str,
    violations: list[str],
    identities: list[str],
    proof_requirements: Mapping[str, object] | None = None,
    structured_proof_required: bool = False,
    runtime_facts: RuntimeLedgerFacts | None = None,
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
        runtime_facts=runtime_facts or EMPTY_RUNTIME_FACTS,
        parsed_proofs=parsed_proofs if parsed_proofs is not None else [],
        required=structured_proof_required,
    )
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
    runtime_facts: RuntimeLedgerFacts | None = None,
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
            runtime_facts=runtime_facts,
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
    target: str,
    run_id: str,
    expected_source_revision: str,
    runtime_facts: RuntimeLedgerFacts,
    fresh_runtime: bool,
) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status")) or "not_reported"
    path_text = _text(document.get("path"))
    expected = _expected_sha256(document.get("sha256"))
    expected_size = document.get("size")
    version = _text(document.get("version"))
    provenance = _text(document.get("provenance"))
    source_revision = _text(document.get("source_revision"))
    artifact_target = _text(document.get("target"))
    artifact_run_id = _text(document.get("run_id"))
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
        or (
            fresh_runtime
            and (
                not provenance
                or not source_revision
                or not artifact_target
                or not artifact_run_id
            )
        )
    ):
        violations.append(
            "artifact: verified identity requires absolute path, sha256, integer size, "
            "version, provenance, source_revision, target, and run_id"
        )
    else:
        selected_path = Path(path_text).expanduser()
        if not selected_path.is_absolute():
            violations.append("artifact: path must be absolute")
            path = selected_path.absolute()
        else:
            path = selected_path
        if fresh_runtime and artifact_target != target:
            violations.append("artifact: target does not match the qualified Runtime target")
        if fresh_runtime and artifact_run_id != run_id:
            violations.append("artifact: run_id does not match the qualified Runtime Run")
        if fresh_runtime and source_revision != expected_source_revision:
            violations.append("artifact: source_revision does not match the clean source set")
        build_ref = _mapping(runtime_facts.build_artifact_ref)
        build_handle = _text(build_ref.get("handle"))
        build_digest = _expected_sha256(build_ref.get("digest"))
        if fresh_runtime and (
            build_handle != str(path)
            or build_digest != expected
            or build_ref.get("size") != expected_size
            or _text(build_ref.get("version")) != version
            or _text(build_ref.get("provenance")) != provenance
            or _text(build_ref.get("target")) != artifact_target
            or _text(build_ref.get("run_id")) != artifact_run_id
            or runtime_facts.build_source_revision != source_revision
        ):
            violations.append(
                "artifact: identity does not match the trusted build.artifact Gate"
            )
        if not path.is_file():
            violations.append(f"artifact: file is unavailable: {path}")
        else:
            try:
                raw = path.read_bytes()
            except OSError as error:
                violations.append(f"artifact: cannot read content: {error}")
                return {
                    "status": status,
                    "accepted": False,
                    "version": version,
                    "size": 0,
                }
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
                artifact_violations = [
                    item for item in violations if item.startswith("artifact:")
                ]
                if not artifact_violations:
                    identity = f"artifact:{version}:{actual}:{actual_size}"
                    if fresh_runtime:
                        identity += (
                            f":{provenance}:{source_revision}:{artifact_target}:"
                            f"{artifact_run_id}"
                        )
                    identities.append(identity)
                    accepted = True
    return {
        "status": status,
        "accepted": accepted,
        "version": version,
        "size": actual_size,
    }


def _recovery_dimension(
    value: object,
    *,
    primary_artifact: Mapping[str, object],
    violations: list[str],
    gaps: list[str],
    identities: list[str],
    target: str,
    run_id: str,
    runtime_facts: RuntimeLedgerFacts,
    parsed_proofs: list[Mapping[str, object]],
) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status")) or "not_reported"
    path_text = _text(document.get("path"))
    expected = _expected_sha256(document.get("sha256"))
    expected_size = document.get("size")
    version = _text(document.get("version"))
    if status != "verified":
        gaps.append(f"recovery={status}")
    if (
        not path_text
        or not expected
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
        or not version
    ):
        violations.append(
            "recovery: verified identity requires absolute path, sha256, positive "
            "integer size, and version"
        )
        return {"status": status, "accepted": False, "version": version, "size": 0}
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        violations.append("recovery: path must be absolute")
        path = path.absolute()
    if (
        str(path) == _text(primary_artifact.get("path"))
        or expected == _expected_sha256(primary_artifact.get("sha256"))
    ):
        violations.append(
            "recovery: package must be independently identified from the upgrade artifact"
        )
    actual_size = 0
    if not path.is_file():
        violations.append(f"recovery: file is unavailable: {path}")
    else:
        try:
            raw = path.read_bytes()
        except OSError as error:
            violations.append(f"recovery: cannot read content: {error}")
        else:
            actual_size = len(raw)
            actual = _digest_bytes(raw)
            if actual != expected:
                violations.append(
                    f"recovery: digest mismatch: expected {expected}, actual {actual}"
                )
            if actual_size != expected_size:
                violations.append(
                    f"recovery: size mismatch: expected {expected_size}, actual {actual_size}"
                )
    evidence_result = _evidence_dimension(
        document,
        label="recovery",
        accepted_statuses={"verified"},
        violations=violations,
        gaps=gaps,
        identities=identities,
        proof_requirements={
            "dimension": "recovery",
            "status": "verified",
            "target": target,
            "run_id": run_id,
            "artifact.path": str(path),
            "artifact.sha256": expected,
            "artifact.size": expected_size,
            "artifact.version": version,
        },
        structured_proof_required=True,
        runtime_facts=runtime_facts,
        parsed_proofs=parsed_proofs,
    )
    accepted = bool(evidence_result["accepted"]) and not any(
        item.startswith("recovery:") for item in violations
    )
    if accepted:
        identities.append(f"recovery:{version}:{expected}:{actual_size}")
    return {
        **evidence_result,
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
    runtime_facts: RuntimeLedgerFacts | None = None,
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
        runtime_facts=runtime_facts,
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
) -> tuple[dict[str, object], RuntimeLedgerFacts]:
    document = _mapping(value)
    run_id = _text(document.get("run_id"))
    outcome = _text(document.get("terminal_outcome")) or "unavailable"
    evidence = _sequence(document.get("evidence"))
    ledger_accepted = True
    runtime_facts = EMPTY_RUNTIME_FACTS
    if mode == "fresh-runtime":
        ledger_accepted, runtime_facts = _runtime_ledger(
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
            runtime_facts=runtime_facts,
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
        runtime_facts,
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
        "recovery": [],
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
        "artifact.path": _text(artifact_document.get("path")),
        "artifact.sha256": artifact_sha256,
        "artifact.size": artifact_document.get("size"),
        "artifact.version": _text(artifact_document.get("version")),
        "artifact.provenance": _text(artifact_document.get("provenance")),
        "artifact.source_revision": _text(artifact_document.get("source_revision")),
        "artifact.target": _text(artifact_document.get("target")),
        "artifact.run_id": _text(artifact_document.get("run_id")),
    }
    source_repositories = [
        _mapping(item)
        for item in _sequence(_mapping(manifest.get("source")).get("repositories"))
        if isinstance(item, Mapping)
    ]
    source_commits = [_text(item.get("commit")).lower() for item in source_repositories]
    source_revision = ";".join(
        f"{_text(item.get('name'))}:{_text(item.get('commit')).lower()}"
        for item in source_repositories
    )
    runtime_result, runtime_facts = _runtime_dimension(
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
            runtime_facts=runtime_facts,
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
        runtime_facts=runtime_facts,
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
        runtime_facts=runtime_facts,
        parsed_proofs=parsed_proofs["build"],
    )
    dimensions["artifact"] = _artifact_dimension(
        manifest.get("artifact"),
        violations=violations,
        gaps=gaps,
        identities=identities,
        target=case_target,
        run_id=run_id,
        expected_source_revision=source_revision,
        runtime_facts=runtime_facts,
        fresh_runtime=mode == "fresh-runtime",
    )
    if mode == "fresh-runtime":
        dimensions["recovery"] = _recovery_dimension(
            manifest.get("recovery"),
            primary_artifact=artifact_document,
            violations=violations,
            gaps=gaps,
            identities=identities,
            target=case_target,
            run_id=run_id,
            runtime_facts=runtime_facts,
            parsed_proofs=parsed_proofs["recovery"],
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
        runtime_facts=runtime_facts,
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
        runtime_facts=runtime_facts,
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
        runtime_facts=runtime_facts,
        parsed_proofs=parsed_proofs["hardware"],
    )
    if mode == "fresh-runtime":
        _verify_fresh_timeline(
            runtime_facts,
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
