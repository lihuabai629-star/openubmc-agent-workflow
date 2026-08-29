#!/usr/bin/env python3
"""Qualify immutable product-closeout evidence through one Operator/CI seam."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
import subprocess
import sys


EVIDENCE_SCHEMA = "openubmc-agent-workflow.product-closeout-evidence.v1"
REPORT_SCHEMA = "openubmc-agent-workflow.product-closeout-qualification.v1"
MODES = {"fresh-runtime", "historical-reconstruction"}
PROTOCOLS = {"NVMe", "SATA", "SAS"}
SHA256 = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


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


def _file_identity(
    item: Mapping[str, object],
    *,
    label: str,
    violations: list[str],
    identities: list[str],
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
    actual = _digest_bytes(path.read_bytes())
    if actual != expected:
        violations.append(
            f"{label}: evidence digest mismatch: expected {expected}, actual {actual}"
        )
        return False
    identities.append(f"file:{label}:{actual}:{path.stat().st_size}")
    return True


def _evidence_dimension(
    value: object,
    *,
    label: str,
    accepted_statuses: set[str],
    violations: list[str],
    gaps: list[str],
    identities: list[str],
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
    elif not path_text or not expected or not isinstance(expected_size, int) or not version:
        violations.append(
            "artifact: verified identity requires path, sha256, integer size, and version"
        )
    else:
        path = Path(path_text).expanduser().absolute()
        if not path.is_file():
            violations.append(f"artifact: file is unavailable: {path}")
        else:
            actual_size = path.stat().st_size
            actual = _digest_bytes(path.read_bytes())
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
) -> dict[str, object]:
    document = _mapping(value)
    run_id = _text(document.get("run_id"))
    outcome = _text(document.get("terminal_outcome")) or "unavailable"
    evidence = _sequence(document.get("evidence"))
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
        ):
            verified += 1
    accepted = bool(run_id) and outcome == "completed" and bool(evidence) and verified == len(evidence)
    if mode == "historical-reconstruction" and not accepted:
        gaps.append("fresh_runtime_identity_unavailable")
    elif mode == "fresh-runtime":
        if not run_id:
            gaps.append("runtime_run_id_missing")
        if outcome != "completed":
            gaps.append(f"runtime_terminal_outcome={outcome}")
        if not evidence:
            gaps.append("runtime_outcome_evidence_missing")
    return {
        "run_id": run_id,
        "terminal_outcome": outcome,
        "accepted": accepted,
        "evidence_count": len(evidence),
        "verified_evidence_count": verified,
    }


def qualify(document: Mapping[str, object]) -> dict[str, object]:
    manifest = dict(document)
    violations: list[str] = []
    gaps: list[str] = []
    identities: list[str] = []
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        violations.append(f"schema must be {EVIDENCE_SCHEMA}")
    mode = _text(manifest.get("mode"))
    if mode not in MODES:
        violations.append("mode must be fresh-runtime or historical-reconstruction")

    case = _mapping(manifest.get("case"))
    case_required = tuple(_text(item) for item in _sequence(case.get("required_protocols")))
    if not _text(case.get("name")) or not _text(case.get("target")):
        violations.append("case name and target are required")
    if not case_required:
        violations.append("case required_protocols must be non-empty")

    dimensions = {
        "runtime": _runtime_dimension(
            manifest.get("runtime"),
            mode=mode,
            violations=violations,
            gaps=gaps,
            identities=identities,
        ),
        "diagnosis": _evidence_dimension(
            manifest.get("diagnosis"),
            label="diagnosis",
            accepted_statuses={"passed"},
            violations=violations,
            gaps=gaps,
            identities=identities,
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
    )
    dimensions["build"] = _evidence_dimension(
        validation.get("build"),
        label="build",
        accepted_statuses={"compiled"},
        violations=violations,
        gaps=gaps,
        identities=identities,
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
    )
    dimensions["freshness"] = _evidence_dimension(
        manifest.get("freshness"),
        label="freshness",
        accepted_statuses={"fresh"},
        violations=violations,
        gaps=gaps,
        identities=identities,
    )
    dimensions["hardware"] = _hardware_dimension(
        manifest.get("hardware"),
        case_required_protocols=case_required,
        violations=violations,
        gaps=gaps,
        identities=identities,
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
            "target": _text(case.get("target")),
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
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.manifest.expanduser().read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError("manifest must be a JSON object")
        report = qualify(document)
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
