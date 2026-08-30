#!/usr/bin/env python3
"""Assemble a fresh product-closeout manifest from trusted evidence sources."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import SQLiteRuntimeRepository  # noqa: E402

from scripts.product_closeout_qualification import (  # noqa: E402
    EVIDENCE_SCHEMA,
    PROOF_SCHEMA,
    qualify,
)
from scripts.runtime_ledger_snapshot import stable_runtime_ledger_copy  # noqa: E402


INGESTION_SCHEMA = "openubmc-agent-workflow.product-closeout-ingestion.v1"
ARTIFACT_METADATA_SCHEMA = "openubmc-agent-workflow/artifact-metadata-v1"
DIMENSIONS = (
    "runtime",
    "diagnosis",
    "official_ut",
    "build",
    "upgrade",
    "freshness",
    "hardware",
)


@dataclass(frozen=True)
class RuntimeSnapshot:
    target: str
    terminal_outcome: str
    run_events_sha256: str


@dataclass(frozen=True)
class DimensionEvidence:
    references: tuple[dict[str, object], ...]
    proofs: tuple[dict[str, object], ...]

    @property
    def primary_proof(self) -> dict[str, object]:
        return self.proofs[0]


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be an array")
    return list(value)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_json(path: Path, label: str) -> tuple[dict[str, object], bytes]:
    absolute = path.expanduser().absolute()
    try:
        raw = absolute.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not readable JSON: {error}") from error
    return _mapping(value, label), raw


def _runtime_snapshot(
    repository_path: Path,
    *,
    run_id: str,
    selected_target: str,
) -> RuntimeSnapshot:
    with stable_runtime_ledger_copy(repository_path) as snapshot:
        repository = SQLiteRuntimeRepository(snapshot)
        projection = repository.load(run_id)
        events = repository.events(run_id)
    if not isinstance(projection, Mapping):
        raise ValueError("Runtime run_id is unavailable")
    targets = [
        dict(item)
        for item in projection.get("targets", [])
        if isinstance(item, Mapping)
    ]
    if selected_target:
        matches = [
            target
            for target in targets
            if selected_target
            in {_text(target.get("target_id")), _text(target.get("address"))}
        ]
        if len(matches) != 1:
            raise ValueError("case.target does not select exactly one Runtime target")
        target = matches[0]
    elif len(targets) == 1:
        target = targets[0]
    elif len(targets) > 1:
        raise ValueError("multiple Runtime targets require case.target")
    else:
        raise ValueError("Runtime Run has no target binding")
    target_identity = _text(target.get("target_id")) or _text(target.get("address"))
    outcome = projection.get("run_outcome")
    outcome = dict(outcome) if isinstance(outcome, Mapping) else {}
    terminal_outcome = _text(outcome.get("status"))
    if terminal_outcome != "completed":
        raise ValueError("Runtime terminal Outcome is not completed")
    return RuntimeSnapshot(
        target=target_identity,
        terminal_outcome=terminal_outcome,
        run_events_sha256=_sha256_bytes(_json_bytes(events)),
    )


def _evidence_refs(
    value: object,
    *,
    dimension: str,
    target: str,
    run_id: str,
) -> DimensionEvidence:
    refs: list[dict[str, object]] = []
    proofs: list[dict[str, object]] = []
    for index, raw_item in enumerate(_sequence(value, f"{dimension}.evidence")):
        item = _mapping(raw_item, f"{dimension}.evidence[{index}]")
        proof_path = Path(_text(item.get("proof_path"))).expanduser().absolute()
        proof, proof_raw = _read_json(
            proof_path,
            f"{dimension}.evidence[{index}].proof",
        )
        if proof.get("schema") != PROOF_SCHEMA:
            raise ValueError(f"{dimension} proof schema is unsupported")
        if proof.get("dimension") != dimension:
            raise ValueError(f"{dimension} proof dimension does not match")
        if proof.get("run_id") != run_id:
            raise ValueError(f"{dimension} proof run_id does not match the Runtime Run")
        if proof.get("target") != target:
            raise ValueError(f"{dimension} proof target does not match the Runtime target")
        reference: dict[str, object] = {
            "path": str(proof_path),
            "sha256": _sha256_bytes(proof_raw),
        }
        binding = proof.get("supporting_evidence")
        if dimension != "runtime":
            binding = _mapping(binding, f"{dimension} proof supporting_evidence")
            support_path = Path(_text(item.get("support_path"))).expanduser().absolute()
            try:
                support_raw = support_path.read_bytes()
            except OSError as error:
                raise ValueError(
                    f"{dimension} supporting evidence is unavailable: {error}"
                ) from error
            actual = _sha256_bytes(support_raw)
            if actual != _text(binding.get("sha256")):
                raise ValueError(
                    "supporting evidence digest does not match the proof binding"
                )
            reference["supporting_evidence"] = {
                "path": str(support_path),
                "sha256": actual,
                "evidence_type": _text(binding.get("evidence_type")),
            }
        refs.append(reference)
        proofs.append(proof)
    if not refs:
        raise ValueError(f"{dimension}.evidence must not be empty")
    return DimensionEvidence(tuple(refs), tuple(proofs))


def _source_repositories(value: object) -> list[dict[str, str]]:
    repositories: list[dict[str, str]] = []
    for index, raw_item in enumerate(_sequence(value, "source_repositories")):
        item = _mapping(raw_item, f"source_repositories[{index}]")
        path = Path(_text(item.get("path"))).expanduser().absolute()
        name = _text(item.get("name")) or path.name
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if head.returncode != 0 or dirty.returncode != 0:
            raise ValueError(f"source repository is unavailable: {path}")
        if dirty.stdout.strip():
            raise ValueError(f"source repository is not clean: {path}")
        repositories.append(
            {"name": name, "path": str(path), "commit": head.stdout.strip()}
        )
    if not repositories:
        raise ValueError("source_repositories must not be empty")
    return repositories


def _artifact(path_value: object) -> dict[str, object]:
    path = Path(_text(path_value)).expanduser().absolute()
    metadata, _raw = _read_json(
        Path(str(path) + ".metadata.json"),
        "artifact metadata",
    )
    if metadata.get("schema") != ARTIFACT_METADATA_SCHEMA:
        raise ValueError("artifact metadata schema is unsupported")
    identity = _mapping(metadata.get("artifact"), "artifact metadata identity")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"artifact is unavailable: {error}") from error
    digest = _sha256_bytes(raw)
    size = len(raw)
    if digest != _text(identity.get("sha256")) or size != identity.get("size"):
        raise ValueError("artifact content does not match its metadata")
    version = _text(metadata.get("product_version"))
    if not version:
        raise ValueError("artifact metadata product_version is required")
    return {
        "status": "verified",
        "path": str(path),
        "sha256": digest,
        "size": size,
        "version": version,
    }


def assemble_manifest(
    descriptor: Mapping[str, object],
    *,
    runtime_repository: Path,
) -> dict[str, object]:
    document = _mapping(descriptor, "ingestion input")
    if document.get("schema") != INGESTION_SCHEMA:
        raise ValueError("ingestion input schema is unsupported")
    case = _mapping(document.get("case"), "case")
    runtime = _mapping(document.get("runtime"), "runtime")
    run_id = _text(runtime.get("run_id"))
    if not run_id:
        raise ValueError("runtime.run_id is required")
    runtime_snapshot = _runtime_snapshot(
        runtime_repository,
        run_id=run_id,
        selected_target=_text(case.get("target")),
    )
    target = runtime_snapshot.target
    runtime_evidence = _evidence_refs(
        runtime.get("evidence"),
        dimension="runtime",
        target=target,
        run_id=run_id,
    )
    evidence = _mapping(document.get("evidence"), "evidence")
    dimensions = {
        dimension: _evidence_refs(
            evidence.get(dimension),
            dimension=dimension,
            target=target,
            run_id=run_id,
        )
        for dimension in DIMENSIONS[1:]
    }
    required_protocols = [
        _text(item)
        for item in _sequence(case.get("required_protocols"), "case.required_protocols")
        if _text(item)
    ]
    hardware_proof = dimensions["hardware"].primary_proof
    hardware_protocols = list(hardware_proof.get("required_protocols", []))
    if hardware_protocols != required_protocols:
        raise ValueError("hardware proof protocols do not match case.required_protocols")
    manifest: dict[str, object] = {
        "schema": EVIDENCE_SCHEMA,
        "mode": "fresh-runtime",
        "case": {
            "name": _text(case.get("name")),
            "target": target,
            "required_protocols": required_protocols,
        },
        "runtime": {
            "run_id": run_id,
            "terminal_outcome": runtime_snapshot.terminal_outcome,
            "repository": {
                "path": str(runtime_repository.expanduser().absolute()),
                "sha256": runtime_snapshot.run_events_sha256,
                "digest_scope": "run-events",
            },
            "evidence": list(runtime_evidence.references),
        },
        "diagnosis": {
            "status": _text(dimensions["diagnosis"].primary_proof.get("status")),
            "evidence": list(dimensions["diagnosis"].references),
        },
        "source": {
            "status": "completed",
            "repositories": _source_repositories(document.get("source_repositories")),
        },
        "validation": {
            "official_ut": {
                "status": _text(
                    dimensions["official_ut"].primary_proof.get("status")
                ),
                "evidence": list(dimensions["official_ut"].references),
            },
            "build": {
                "status": _text(dimensions["build"].primary_proof.get("status")),
                "evidence": list(dimensions["build"].references),
            },
        },
        "artifact": _artifact(document.get("artifact_path")),
        "upgrade": {
            "status": _text(dimensions["upgrade"].primary_proof.get("status")),
            "evidence": list(dimensions["upgrade"].references),
        },
        "freshness": {
            "status": _text(dimensions["freshness"].primary_proof.get("status")),
            "max_age_seconds": document.get("freshness_max_age_seconds", 3600),
            "evidence": list(dimensions["freshness"].references),
        },
        "hardware": {
            "status": _text(hardware_proof.get("status")),
            "required_protocols": required_protocols,
            "devices": list(hardware_proof.get("devices", [])),
            "evidence": list(dimensions["hardware"].references),
        },
    }
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("descriptor", type=Path)
    parser.add_argument("--runtime-repository", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--output-report", type=Path)
    args = parser.parse_args(argv)
    try:
        descriptor, _raw = _read_json(args.descriptor, "ingestion input")
        manifest = assemble_manifest(
            descriptor,
            runtime_repository=args.runtime_repository,
        )
        report = qualify(manifest, runtime_repository=args.runtime_repository)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output_manifest = args.output_manifest.expanduser().absolute()
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.write_text(manifest_text, encoding="utf-8")
    if args.output_report is not None:
        output_report = args.output_report.expanduser().absolute()
        output_report.parent.mkdir(parents=True, exist_ok=True)
        output_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(manifest_text, end="")
    return 0 if report.get("promotable") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
