from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "product_closeout_qualification.py"
RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    FilesystemBlobRepository,
    RuntimeMcpService,
    SQLiteRuntimeRepository,
)
from openubmc_target_runtime.context_runtime import PendingCaseEvent  # noqa: E402


AUTO_RUNTIME_REPOSITORY = object()


class _QualificationTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class _QualificationBackend:
    def open_task(self, task_id: str) -> _QualificationTask:
        return _QualificationTask(task_id)

    @staticmethod
    def close_task(_task: _QualificationTask) -> None:
        return None

    @staticmethod
    def maintain_task(_task: _QualificationTask) -> int:
        return 0

    @staticmethod
    def task_status(task: _QualificationTask) -> dict[str, object]:
        return {"task_id": task.task_id}

    @staticmethod
    def debug_run(_task, _arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        return {
            "ok": True,
            "schema": "openubmc-debug.v1",
            "summary": "Drive resource attribution defect isolated",
            "root_cause": "global and local slot identity mismatch",
            "hardware_devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
            "observed_at": datetime.now(UTC).isoformat(),
            "freshness": {"status": "fresh"},
        }

    @staticmethod
    def upgrade_run(_task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        digest = str(arguments.get("artifact_sha256", ""))
        version = str(arguments.get("product_version", ""))
        target_epoch = int(arguments.get("_minimum_target_epoch", 0)) + 1
        return {
            "ok": True,
            "operation_id": context.operation_id,
            "action": "upgrade",
            "epoch_before": target_epoch - 1,
            "epoch_after": target_epoch,
            "target_epoch": target_epoch,
            "mutation": {
                "method": "MultipartHttpPushUri",
                "task_uri": "/redfish/v1/TaskService/Tasks/1",
                "parameters": {"ActiveMode": "ResetBMC", "ForceUpdate": True},
                "manager_before": {
                    "version": version,
                    "last_reset_time": "2026-08-29T10:00:00Z",
                },
            },
            "verification": {
                "installed_version": version,
                "target_epoch": target_epoch,
                "version": {
                    "version": version,
                    "last_reset_time": "2026-08-29T11:00:00Z",
                },
            },
            "journal": {
                "operation_id": context.operation_id,
                "action": "upgrade",
                "stage": "verified",
                "effects_started": True,
                "expected_checksum": digest,
                "epoch_before": target_epoch - 1,
                "epoch_after": target_epoch,
            },
        }

    @staticmethod
    def debug_collect(_task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        target_epoch = int(arguments.get("_minimum_target_epoch", 0))
        return {
            "ok": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "target_epoch": target_epoch,
            "business_acceptance": "passed",
            "result": {
                "freshness": {
                    "status": "fresh",
                    "complete": True,
                    "after_last_reboot_or_change": True,
                    "stale_evidence": [],
                    "lost_dimensions": [],
                    "unavailable_dimensions": [],
                    "bmc_time_delta": {
                        "before": "2026-08-29 11:00:00 +0000",
                        "after": "2026-08-29 11:00:01 +0000",
                        "elapsed_seconds": 1.0,
                        "comparable": True,
                        "clock_moved_backwards": False,
                    },
                },
                "lanes": {
                    "ssh": {
                        "mdbctl_expand_1": {
                            "ok": True,
                            "result": {"stdout_lines": ["Drive_1_010102"]},
                        },
                        "mdbctl_expand_1_object_drive1": {
                            "ok": True,
                            "result": {
                                "properties": {
                                    "Id": 1,
                                    "Protocol": 6,
                                    "Presence": 1,
                                    "Health": 0,
                                    "ResourceId": 7,
                                    "SerialNumber": "NVME-SERIAL-1",
                                    "bmc.kepler.Systems.Storage.Drive": {
                                        "Id": "1",
                                        "Name": "\"Drive1\"",
                                        "Protocol": "6",
                                        "RefControllerId": "255",
                                        "ResourceId": "7",
                                        "Presence": "1",
                                    },
                                    "bmc.kepler.Systems.Storage.Drive.DriveStatus": {
                                        "Health": "0"
                                    },
                                    "bmc.kepler.Inventory.Hardware": {
                                        "SerialNumber": "\"NVME-SERIAL-1\""
                                    },
                                }
                            },
                        }
                    }
                },
                "runtime": {
                    "status": {
                        "targets": [
                            {
                                "target_id": str(arguments.get("target_id", "")),
                                "epochs": {"target_epoch": target_epoch},
                            }
                        ]
                    }
                },
            },
        }


def _gate_binding(turn: Mapping[str, object]) -> dict[str, object]:
    gate = turn["gate"]
    assert isinstance(gate, Mapping)
    return {
        "gate_id": gate["gate_id"],
        "gate_version": gate["gate_version"],
        "schema_digest": gate["schema_digest"],
    }


def _compiled_validation_payload(evidence_ids: list[str]) -> dict[str, object]:
    readiness_id = "qualification-readiness"
    return {
        "dependency_readiness": {
            "readiness_id": readiness_id,
            "status": "ready",
            "resolution": "available",
            "summary": "build dependencies resolved",
            "check_commands": ["conan graph info ."],
            "evidence_ids": list(evidence_ids),
            "attempt_count": 1,
            "reused_by": ["official_ut", "build"],
        },
        "validation_results": [
            {
                "kind": "official_ut",
                "status": "passed",
                "summary": "official UT passed",
                "commands": ["bingo test"],
                "evidence_ids": list(evidence_ids),
                "dependency_readiness_id": readiness_id,
            },
            {
                "kind": "build",
                "status": "compiled",
                "summary": "firmware compilation completed",
                "commands": ["bmcgo build"],
                "evidence_ids": list(evidence_ids),
                "dependency_readiness_id": readiness_id,
            },
        ],
        "hardware_coverage": {
            "status": "covered",
            "required_protocols": ["NVMe"],
            "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
            "evidence_ids": list(evidence_ids),
            "gaps": [],
        },
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def structured_proof(
    root: Path, name: str, payload: dict[str, object]
) -> tuple[dict[str, object], Path]:
    path = root / f"{name}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {"path": str(path), "sha256": sha256(path)}, path


def all_manifest_evidence(manifest: dict[str, object]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    for dimension in manifest_dimensions(manifest):
        for item in dimension.get("evidence", []):
            refs.append(item)
            support = item.get("supporting_evidence")
            if isinstance(support, dict):
                refs.append(support)
    return refs


def manifest_dimensions(manifest: dict[str, object]) -> tuple[dict[str, object], ...]:
    validation = manifest["validation"]
    dimensions = [
        manifest["runtime"],
        manifest["diagnosis"],
        validation["official_ut"],
        validation["build"],
        manifest["upgrade"],
        manifest["freshness"],
        manifest["hardware"],
    ]
    recovery = manifest.get("recovery")
    if isinstance(recovery, dict):
        dimensions.append(recovery)
    return tuple(dimensions)


def _refresh_proof_bindings(manifest: dict[str, object]) -> None:
    run_id = manifest["runtime"]["run_id"]
    target = manifest["case"]["target"]
    for dimension in manifest_dimensions(manifest):
        for item in dimension.get("evidence", []):
            proof_path = Path(item["path"])
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["run_id"] = run_id
            proof["target"] = target
            support = item.get("supporting_evidence")
            if isinstance(support, dict):
                proof["supporting_evidence"] = {
                    "evidence_type": support["evidence_type"],
                    "sha256": sha256(Path(support["path"])),
                }
                support["sha256"] = proof["supporting_evidence"]["sha256"]
            if proof.get("dimension") == "upgrade":
                proof["artifact"] = {
                    key: manifest["artifact"][key]
                    for key in (
                        "path",
                        "sha256",
                        "size",
                        "version",
                        "provenance",
                        "source_revision",
                        "target",
                        "run_id",
                    )
                }
                proof["installed_version"] = manifest["artifact"]["version"]
            elif proof.get("dimension") == "freshness":
                proof["artifact_sha256"] = manifest["artifact"]["sha256"]
            elif proof.get("dimension") == "recovery":
                proof["artifact"] = {
                    key: manifest["recovery"][key]
                    for key in ("path", "sha256", "size", "version")
                }
            proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
            item["sha256"] = sha256(proof_path)


def _build_public_runtime_ledger(manifest: dict[str, object]) -> None:
    repository_path = Path(manifest["runtime"]["repository"]["path"])
    repository = SQLiteRuntimeRepository(repository_path)
    blobs = FilesystemBlobRepository(repository_path.with_suffix(".blobs"))
    backend = _QualificationBackend()
    agent = RuntimeMcpService(
        backend,
        context_repository=repository,
        blob_repository=blobs,
    )
    operator = RuntimeMcpService(
        backend,
        context_repository=repository,
        blob_repository=blobs,
        interface_profile="operator",
    )
    try:
        diagnosis = agent.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": manifest["case"]["target"],
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "entry_operation": "debug_run",
                "entry_arguments": {"mdb_expand_classes": ["Drive"]},
                "purpose": "qualify one public Runtime product closeout",
            },
            task_id="product-closeout-qualification",
            operation_id="product-closeout-start",
        )
        run_id = str(diagnosis["run_id"])
        manifest["runtime"]["run_id"] = run_id
        manifest["artifact"]["run_id"] = run_id
        if diagnosis["gate"]["name"] != "diagnosis.acceptance":
            raise AssertionError("qualification Run did not expose diagnosis acceptance")
        evidence_ids = [
            item["evidence_id"]
            for item in diagnosis["diagnostic_receipt"]["evidence"]
        ]
        developer = agent.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": run_id,
                **_gate_binding(diagnosis),
                "response": {
                    "status": "completed",
                    "summary": "diagnosis accepted",
                    "payload": {
                        "root_cause": "global and local slot identity mismatch",
                        "evidence_ids": evidence_ids,
                        "known_gaps": [],
                    },
                },
            },
            task_id="product-closeout-qualification",
            operation_id="product-closeout-diagnosis",
        )
        build = agent.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": run_id,
                **_gate_binding(developer),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": manifest["artifact"]["source_revision"],
                        "authored_files": ["fix.lua"],
                        "verification_plan": ["official UT", "build", "upgrade"],
                    },
                },
            },
            task_id="product-closeout-qualification",
            operation_id="product-closeout-developer",
        )
        attached: set[str] = set()
        for dimension in manifest_dimensions(manifest):
            for item in dimension.get("evidence", []):
                support = item.get("supporting_evidence")
                if not isinstance(support, dict):
                    continue
                evidence_type = str(support.get("evidence_type", ""))
                if evidence_type in {
                    "runtime-upgrade-evidence",
                    "runtime-debug-evidence",
                }:
                    continue
                digest = str(support["sha256"])
                if digest in attached:
                    continue
                attached.add(digest)
                operator.call_exposed_tool(
                    "evidence_attach",
                    {
                        "run_id": run_id,
                        "target": manifest["case"]["target"],
                        "path": support["path"],
                        "sha256": digest,
                        "evidence_type": evidence_type,
                    },
                    task_id="product-closeout-qualification-operator",
                    operation_id=f"attach-{evidence_type}",
                )
        artifact_path = Path(manifest["artifact"]["path"])
        final = agent.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": run_id,
                **_gate_binding(build),
                "response": {
                    "status": "completed",
                    "summary": "firmware artifact completed",
                    "payload": {
                        "source_revision": manifest["artifact"]["source_revision"],
                        "artifact_ref": {
                            "handle": str(artifact_path),
                            "digest": "sha256:" + manifest["artifact"]["sha256"],
                            "kind": "openubmc-hpm",
                            "size": manifest["artifact"]["size"],
                            "provenance": manifest["artifact"]["provenance"],
                            "retention_hint": "run-lifetime",
                            "version": manifest["artifact"]["version"],
                            "target": manifest["case"]["target"],
                            "run_id": run_id,
                        },
                        **_compiled_validation_payload(evidence_ids),
                    },
                },
            },
            task_id="product-closeout-qualification",
            operation_id="product-closeout-build",
        )
        if final["state"] != "completed":
            raise AssertionError(f"qualification Run did not complete: {final}")
        projection = operator.call_exposed_tool(
            "case_read",
            {"case_id": run_id},
            task_id="product-closeout-qualification-operator",
            operation_id="product-closeout-case-read",
        )
        native_paths = {
            "upgrade_run": Path(
                manifest["upgrade"]["evidence"][0]["supporting_evidence"]["path"]
            ),
            "debug_collect": Path(
                manifest["freshness"]["evidence"][0]["supporting_evidence"]["path"]
            ),
        }
        for operation in projection["operations"]:
            name = operation.get("operation")
            if name not in native_paths:
                continue
            evidence_id = operation["evidence_ids"][-1]
            loaded = operator.call_exposed_tool(
                "evidence_read",
                {
                    "case_id": run_id,
                    "evidence_id": evidence_id,
                    "target_id": "target-1",
                },
                task_id="product-closeout-qualification-operator",
                operation_id=f"product-closeout-read-{name}",
            )
            native_paths[name].write_text(loaded["body"], encoding="utf-8")
        _refresh_proof_bindings(manifest)
        manifest["runtime"]["repository"]["sha256"] = hashlib.sha256(
            json.dumps(
                repository.events(run_id),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    finally:
        operator.close()
        agent.close()


def forge_runtime_ledger_for_negative_test(
    manifest: dict[str, object],
    *,
    evidence_target: str | None = None,
    additional_targets: tuple[str, ...] = (),
    attach_proofs: bool = True,
    attach_supporting: bool = True,
    authorize_upgrade: bool = True,
    include_diagnosis_gate: bool = True,
    include_native_operations: bool = True,
    include_native_evidence: bool = True,
    native_operations_in_order: bool = True,
) -> None:
    """Construct a deliberately forged ledger for qualification rejection tests.

    Positive fixtures always use ``_build_public_runtime_ledger`` and therefore
    exercise the public Agent and Operator interfaces.  This helper is limited
    to tests that need an impossible or malicious persisted history that the
    public Runtime correctly refuses to create.
    """
    runtime = manifest["runtime"]
    repository_ref = runtime["repository"]
    database = Path(repository_ref["path"])
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        if candidate.exists():
            candidate.unlink()
    run_id = runtime["run_id"]
    target = manifest["case"]["target"]
    targets = [
        {"target_id": target, "address": target, "role": "candidate"},
        *(
            {
                "target_id": additional_target,
                "address": additional_target,
                "role": "comparison",
            }
            for additional_target in additional_targets
        ),
    ]
    artifact = manifest["artifact"]
    source_revision = artifact["source_revision"]
    artifact_ref = {
        "handle": artifact["path"],
        "digest": "sha256:" + artifact["sha256"],
        "kind": "openubmc-hpm",
        "size": artifact["size"],
        "provenance": artifact["provenance"],
        "retention_hint": "run-lifetime",
        "version": artifact["version"],
        "target": artifact["target"],
        "run_id": artifact["run_id"],
    }
    events = [
        PendingCaseEvent(
            "CaseOpened",
            {
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "targets": targets,
                "authorization": {
                    "allowed_actions": ["upgrade"] if authorize_upgrade else [],
                    "original_intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "parse_count": 1,
                },
                "workflow_definition": {
                    "definition_id": "diagnose-and-fix.debug.build-upgrade",
                    "version": 2,
                    "intent": "diagnose-and-fix",
                    "entry_domain": "debug",
                    "entry_operation": "",
                    "delivery_strategy": "build-upgrade",
                    "steps": [
                        {
                            "step_id": "step-01-debug-run",
                            "kind": "operation",
                            "name": "debug_run",
                            "owner": "openubmc-debug",
                            "receipt_schema": "",
                        },
                        {
                            "step_id": "step-02-diagnosis-acceptance",
                            "kind": "phase",
                            "name": "diagnosis.acceptance",
                            "owner": "openubmc-debug",
                            "receipt_schema": "diagnosis-receipt-v1",
                        },
                        {
                            "step_id": "step-03-developer-change",
                            "kind": "phase",
                            "name": "developer.change",
                            "owner": "openubmc-developer",
                            "receipt_schema": "developer-receipt-v1",
                        },
                        {
                            "step_id": "step-04-build-artifact",
                            "kind": "phase",
                            "name": "build.artifact",
                            "owner": "openubmc-build",
                            "receipt_schema": "build-receipt-v1",
                        },
                        {
                            "step_id": "step-05-upgrade-run",
                            "kind": "operation",
                            "name": "upgrade_run",
                            "owner": "openubmc-upgrade",
                            "receipt_schema": "",
                        },
                        {
                            "step_id": "step-06-debug-collect",
                            "kind": "operation",
                            "name": "debug_collect",
                            "owner": "openubmc-debug",
                            "receipt_schema": "",
                        },
                    ],
                },
            },
            "product-closeout-start",
        ),
        PendingCaseEvent(
            "OperationAccepted",
            {"operation": "debug_run", "target_id": target},
            "diagnosis-operation",
        ),
        PendingCaseEvent("OperationStarted", {}, "diagnosis-operation"),
        PendingCaseEvent(
            "OperationTerminal",
            {"status": "completed", "target_epoch": 0},
            "diagnosis-operation",
        ),
    ]
    if include_diagnosis_gate:
        events.append(
            PendingCaseEvent(
                "RunGateSubmitted",
                {
                    "phase": {
                        "phase_type": "diagnosis.acceptance",
                        "status": "completed",
                        "summary": "diagnosis accepted",
                        "evidence_ids": ["observation-1"],
                        "root_cause": "global and local slot identity mismatch",
                    }
                },
                "diagnosis-gate",
            )
        )
    events.extend(
        (
            PendingCaseEvent(
                "RunGateSubmitted",
                {
                    "phase": {
                        "phase_type": "developer.change",
                        "status": "completed",
                        "summary": "source repair completed",
                        "source_revision": source_revision,
                    }
                },
                "developer-gate",
            ),
            PendingCaseEvent(
                "RunGateSubmitted",
                {
                    "phase": {
                        "phase_type": "build.artifact",
                        "status": "completed",
                        "summary": "firmware compiled",
                        "source_revision": source_revision,
                        "artifact_ref": artifact_ref,
                    }
                },
                "build-gate",
            ),
        )
    )
    selected_evidence: list[dict[str, object]] = []
    for dimension in manifest_dimensions(manifest):
        for item in dimension.get("evidence", []):
            if attach_proofs:
                selected_evidence.append(item)
            support = item.get("supporting_evidence")
            if attach_supporting and isinstance(support, dict):
                selected_evidence.append(support)
    native_items: list[tuple[str, dict[str, object]]] = []
    operator_items: list[dict[str, object]] = []
    for item in selected_evidence:
        evidence_type = item.get("evidence_type")
        if evidence_type == "runtime-upgrade-evidence":
            if include_native_evidence:
                native_items.append(("upgrade_run", item))
            else:
                operator_items.append(item)
        elif evidence_type == "runtime-debug-evidence":
            if include_native_evidence:
                native_items.append(("debug_collect", item))
            else:
                operator_items.append(item)
        else:
            operator_items.append(item)
    for index, item in enumerate(operator_items, start=1):
        digest = item["sha256"]
        events.append(
            PendingCaseEvent(
                "EvidenceAttached",
                {
                    "evidence": {
                        "evidence_id": f"product-closeout-{index}",
                        "blob_id": digest,
                        "media_type": "application/octet-stream",
                        "byte_count": Path(item["path"]).stat().st_size,
                        "target_id": evidence_target or target,
                        "generation": "fresh-product-closeout",
                        "provenance": "product-closeout-qualification",
                        "observed_at": 1.0 + index,
                        "case_id": run_id,
                        "producer": "operator-evidence-attach",
                    }
                },
                f"product-closeout-evidence-{index}",
            )
        )
    if include_native_operations:
        operations = (
            ("upgrade_run", "upgrade-effect-1", 4),
            ("debug_collect", "debug-effect-1", 4),
        )
        if not native_operations_in_order:
            operations = tuple(reversed(operations))
        for operation, operation_id, target_epoch in operations:
            events.extend(
                (
                    PendingCaseEvent(
                        "OperationAccepted",
                        {"operation": operation, "target_id": target},
                        operation_id,
                    ),
                    PendingCaseEvent("OperationStarted", {}, operation_id),
                )
            )
            operation_items = [item for name, item in native_items if name == operation]
            seen: set[str] = set()
            for item in operation_items:
                digest = item["sha256"]
                if digest in seen:
                    continue
                seen.add(digest)
                events.append(
                    PendingCaseEvent(
                        "EvidenceAttached",
                        {
                            "evidence": {
                                "evidence_id": f"{operation}-{digest[:12]}",
                                "blob_id": digest,
                                "media_type": "application/json",
                                "byte_count": Path(item["path"]).stat().st_size,
                                "target_id": target,
                                "target_epoch": target_epoch,
                                "generation": "fresh-product-closeout",
                                "provenance": f"{operation}:{operation_id}",
                                "observed_at": 20.0 if operation == "upgrade_run" else 30.0,
                                "case_id": run_id,
                                "producer": operation,
                            }
                        },
                        operation_id,
                    )
                )
            events.append(
                PendingCaseEvent(
                    "OperationTerminal",
                    {"status": "completed", "target_epoch": target_epoch},
                    operation_id,
                )
            )
    events.append(
        PendingCaseEvent(
            "RunOutcomeRecorded",
            {
                "outcome": {
                        "status": "completed",
                        "summary": "fresh product closeout completed",
                        "acceptance": [
                            {"requirement_id": stage, "status": "passed"}
                            for stage in (
                                "stage.diagnosis",
                                "stage.development",
                                "stage.build",
                                "stage.upgrade",
                                "stage.verification",
                            )
                        ],
                }
            },
            "product-closeout-outcome",
        )
    )
    repository = SQLiteRuntimeRepository(database)
    repository.commit(
        run_id, expected_revision=0, events=tuple(events)
    )
    repository_ref["sha256"] = hashlib.sha256(
        json.dumps(
            repository.events(run_id),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def complete_manifest(
    root: Path,
    *,
    mode: str = "fresh-runtime",
) -> tuple[dict[str, object], Path, Path, Path]:
    source = root / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "qualification@example.com"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Qualification"],
        cwd=source,
        check=True,
    )
    (source / "fix.lua").write_text("return true\n", encoding="utf-8")
    subprocess.run(["git", "add", "fix.lua"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "fix"], cwd=source, check=True)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    artifact = root / "firmware.hpm"
    artifact.write_bytes(b"firmware")
    recovery_artifact = root / "recovery.hpm"
    recovery_artifact.write_bytes(b"recovery-firmware")
    source_revision = f"source:{source_commit}"
    artifact_identity = {
        "path": str(artifact),
        "sha256": sha256(artifact),
        "size": artifact.stat().st_size,
        "version": "1.0.0",
        "provenance": "openubmc-build",
        "source_revision": source_revision,
        "target": "target-1",
        "run_id": "run-product-closeout-1",
    }
    Path(str(artifact) + ".metadata.json").write_text(
        json.dumps(
            {
                "schema": "openubmc-agent-workflow/artifact-metadata-v1",
                "artifact": {
                    "sha256": artifact_identity["sha256"],
                    "size": artifact_identity["size"],
                    "kind": "openubmc-hpm",
                },
                "product_version": artifact_identity["version"],
                "provenance": artifact_identity["provenance"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    proof_base = {
        "schema": "openubmc-agent-workflow.product-closeout-proof.v1",
        "target": "target-1",
        "run_id": "run-product-closeout-1",
    }
    runtime_ref, runtime_proof = structured_proof(
        root,
        "runtime-proof",
        {
            **proof_base,
            "dimension": "runtime",
            "status": "completed",
            "terminal_outcome": "completed",
            "completed_at": "2026-08-29T12:00:00Z",
        },
    )
    diagnosis_ref, diagnosis_proof = structured_proof(
        root,
        "diagnosis-proof",
        {
            **proof_base,
            "dimension": "diagnosis",
            "status": "passed",
            "evidence_ids": ["observation-1"],
        },
    )
    official_ut_ref, official_ut_proof = structured_proof(
        root,
        "official-ut-proof",
        {
            **proof_base,
            "dimension": "official_ut",
            "status": "passed",
            "source_commits": [source_commit],
            "tests_run": 1,
            "tests_failed": 0,
        },
    )
    build_ref, build_proof = structured_proof(
        root,
        "build-proof",
        {
            **proof_base,
            "dimension": "build",
            "status": "compiled",
            "source_commits": [source_commit],
            "compiled_units": 1,
        },
    )
    upgrade_ref, upgrade_proof = structured_proof(
        root,
        "upgrade-proof",
        {
            **proof_base,
            "dimension": "upgrade",
            "status": "completed",
            "artifact": artifact_identity,
            "installed_version": "1.0.0",
            "completed_at": "2026-08-29T11:00:00Z",
        },
    )
    freshness_ref, freshness_proof = structured_proof(
        root,
        "freshness-proof",
        {
            **proof_base,
            "dimension": "freshness",
            "status": "fresh",
            "artifact_sha256": artifact_identity["sha256"],
            "observed_at": "2026-08-29T12:00:00Z",
        },
    )
    hardware_ref, hardware_proof = structured_proof(
        root,
        "hardware-proof",
        {
            **proof_base,
            "dimension": "hardware",
            "status": "covered",
            "required_protocols": ["NVMe"],
            "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
            "observed_at": "2026-08-29T12:00:00Z",
        },
    )
    recovery_identity = {
        "path": str(recovery_artifact),
        "sha256": sha256(recovery_artifact),
        "size": recovery_artifact.stat().st_size,
        "version": "0.9.0",
    }
    recovery_ref, recovery_proof = structured_proof(
        root,
        "recovery-proof",
        {
            **proof_base,
            "dimension": "recovery",
            "status": "verified",
            "artifact": recovery_identity,
            "completed_at": "2026-08-29T10:30:00Z",
        },
    )
    diagnosis_support = root / "diagnosis-record.md"
    diagnosis_support.write_text(
        "根因：目标盘资源关联键错误。\n修复：使用全局盘位映射。\n",
        encoding="utf-8",
    )
    official_ut_support = root / "official-ut.log"
    official_ut_support.write_text("1/1 passed\n", encoding="utf-8")
    build_support = root / "component-build.log"
    build_support.write_text(
        "component/1.0.0@openubmc/stable: Created package revision "
        "0123456789abcdef0123456789abcdef\n"
        "component/1.0.0@openubmc/stable: Full package reference: "
        "component/1.0.0@openubmc/stable#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb#"
        "0123456789abcdef0123456789abcdef\n"
        "构建成功\n",
        encoding="utf-8",
    )
    upgrade_support = root / "runtime-upgrade.json"
    upgrade_support.write_text(
        json.dumps(
            {
                "operation_id": "upgrade-effect-1",
                "action": "upgrade",
                "epoch_before": 3,
                "epoch_after": 4,
                "mutation": {
                    "method": "MultipartHttpPushUri",
                    "task_uri": "/redfish/v1/TaskService/Tasks/1",
                    "parameters": {
                        "ActiveMode": "ResetBMC",
                        "ForceUpdate": True,
                    },
                    "manager_before": {
                        "version": "1.0.0",
                        "last_reset_time": "2026-08-29T10:00:00Z",
                    },
                },
                "verification": {
                    "installed_version": "1.0.0",
                    "target_epoch": 4,
                    "version": {
                        "version": "1.0.0",
                        "last_reset_time": "2026-08-29T11:00:00Z",
                    },
                },
                "journal": {
                    "operation_id": "upgrade-effect-1",
                    "action": "upgrade",
                    "stage": "verified",
                    "effects_started": True,
                    "expected_checksum": artifact_identity["sha256"],
                    "epoch_before": 3,
                    "epoch_after": 4,
                },
            }
        , sort_keys=True),
        encoding="utf-8",
    )
    debug_support = root / "runtime-debug.json"
    debug_support.write_text(
        json.dumps(
            {
                "ok": True,
                "observed_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
                "target_epoch": 4,
                "result": {
                    "freshness": {
                        "status": "complete",
                        "complete": True,
                        "after_last_reboot_or_change": True,
                        "stale_evidence": [],
                        "lost_dimensions": [],
                        "unavailable_dimensions": [],
                    },
                    "lanes": {
                        "ssh": {
                            "mdbctl_expand_1_object_drive1": {
                                "ok": True,
                                "result": {
                                    "properties": {
                                        "bmc.kepler.Systems.Storage.Drive": {
                                            "Id": "1",
                                            "Name": "\"Drive1\"",
                                            "Protocol": "6",
                                            "RefControllerId": "255",
                                            "ResourceId": "7",
                                            "Presence": "1",
                                        },
                                        "bmc.kepler.Systems.Storage.Drive.DriveStatus": {
                                            "Health": "0"
                                        },
                                        "bmc.kepler.Inventory.Hardware": {
                                            "SerialNumber": "\"NVME-SERIAL-1\""
                                        },
                                    }
                                },
                            }
                        }
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    recovery_support = root / "recovery-artifact-record.json"
    recovery_support.write_text(
        json.dumps(
            {
                "schema": "openubmc-agent-workflow/recovery-artifact-record-v1",
                "artifact": recovery_identity,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    def support(path: Path, evidence_type: str) -> dict[str, str]:
        return {
            "path": str(path),
            "sha256": sha256(path),
            "evidence_type": evidence_type,
        }

    def bind_support(
        proof_ref: dict[str, object],
        proof_path: Path,
        source_ref: dict[str, str],
    ) -> None:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["supporting_evidence"] = {
            "evidence_type": source_ref["evidence_type"],
            "sha256": source_ref["sha256"],
        }
        proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
        proof_ref["sha256"] = sha256(proof_path)
        proof_ref["supporting_evidence"] = source_ref

    bind_support(
        diagnosis_ref,
        diagnosis_proof,
        support(diagnosis_support, "workflow-diagnosis-record"),
    )
    bind_support(
        official_ut_ref,
        official_ut_proof,
        support(official_ut_support, "workflow-official-ut-record"),
    )
    bind_support(
        build_ref,
        build_proof,
        support(build_support, "component-build-log"),
    )
    bind_support(
        upgrade_ref,
        upgrade_proof,
        support(upgrade_support, "runtime-upgrade-evidence"),
    )
    bind_support(
        freshness_ref,
        freshness_proof,
        support(debug_support, "runtime-debug-evidence"),
    )
    bind_support(
        hardware_ref,
        hardware_proof,
        support(debug_support, "runtime-debug-evidence"),
    )
    bind_support(
        recovery_ref,
        recovery_proof,
        support(recovery_support, "firmware-recovery-artifact-record"),
    )
    runtime_database = root / "runtime.sqlite3"
    manifest: dict[str, object] = {
        "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
        "mode": mode,
        "case": {
            "name": "fresh closeout",
            "target": "target-1",
            "required_protocols": ["NVMe"],
        },
        "runtime": {
            "run_id": "run-product-closeout-1",
            "terminal_outcome": "completed",
            "repository": {"path": str(runtime_database), "sha256": ""},
            "evidence": [runtime_ref],
        },
        "diagnosis": {"status": "passed", "evidence": [diagnosis_ref]},
        "source": {
            "status": "completed",
            "repositories": [
                {"name": "source", "path": str(source), "commit": source_commit}
            ],
        },
        "validation": {
            "official_ut": {"status": "passed", "evidence": [official_ut_ref]},
            "build": {"status": "compiled", "evidence": [build_ref]},
        },
        "artifact": {
            "status": "verified",
            **artifact_identity,
        },
        "recovery": {
            "status": "verified",
            **recovery_identity,
            "evidence": [recovery_ref],
        },
        "upgrade": {"status": "completed", "evidence": [upgrade_ref]},
        "freshness": {
            "status": "fresh",
            "max_age_seconds": 3600,
            "evidence": [freshness_ref],
        },
        "hardware": {
            "status": "covered",
            "required_protocols": ["NVMe"],
            "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
            "evidence": [hardware_ref],
        },
    }
    _build_public_runtime_ledger(manifest)
    del runtime_proof
    return manifest, source, diagnosis_proof, artifact


def run_qualification(
    root: Path,
    manifest: dict[str, object],
    *,
    runtime_repository: Path | None | object = AUTO_RUNTIME_REPOSITORY,
) -> subprocess.CompletedProcess[str]:
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), str(manifest_path)]
    selected_repository = runtime_repository
    if selected_repository is AUTO_RUNTIME_REPOSITORY:
        repository_ref = manifest.get("runtime", {}).get("repository", {})
        selected_repository = (
            Path(repository_ref["path"])
            if isinstance(repository_ref, dict) and repository_ref.get("path")
            else None
        )
    if isinstance(selected_repository, Path):
        command.extend(["--runtime-repository", str(selected_repository)])
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )


class ProductCloseoutQualificationTests(unittest.TestCase):
    def test_structured_proofs_may_be_projected_after_the_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["qualified"], report["violations"])
        self.assertTrue(report["promotable"], report["gaps"])

    def test_raw_supporting_evidence_must_be_attached_before_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            forge_runtime_ledger_for_negative_test(
                manifest,
                attach_proofs=False,
                attach_supporting=False,
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any(
                "supporting_evidence: evidence is not attached" in item
                for item in report["violations"]
            ),
            report["violations"],
        )

    def test_native_runtime_upgrade_evidence_verifies_artifact_version_and_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["dimensions"]["upgrade"]["accepted"], report["violations"])
        self.assertTrue(report["promotable"], report["gaps"])

    def test_native_runtime_debug_evidence_verifies_fresh_nvme_drive_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["dimensions"]["freshness"]["accepted"], report["violations"])
        self.assertTrue(report["dimensions"]["hardware"]["accepted"], report["violations"])
        self.assertTrue(report["promotable"], report["gaps"])

    def test_complete_fresh_runtime_closeout_is_promotable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["qualified"])
        self.assertTrue(report["promotable"])
        self.assertEqual(report["claim_level"], "fresh-runtime-product-closed")
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["violations"], [])

    def test_fresh_runtime_requires_an_independent_recovery_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["recovery"].update(
                {
                    "path": manifest["artifact"]["path"],
                    "sha256": manifest["artifact"]["sha256"],
                    "size": manifest["artifact"]["size"],
                    "version": manifest["artifact"]["version"],
                }
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("independent" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_runtime_requires_an_observed_manager_reboot_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            upgrade_ref = manifest["upgrade"]["evidence"][0]
            support = Path(upgrade_ref["supporting_evidence"]["path"])
            document = json.loads(support.read_text(encoding="utf-8"))
            document["operation_id"] = "upgrade-effect-1"
            document["journal"]["operation_id"] = "upgrade-effect-1"
            document["epoch_before"] = 3
            document["epoch_after"] = 4
            document["journal"]["epoch_before"] = 3
            document["journal"]["epoch_after"] = 4
            document["verification"]["target_epoch"] = 4
            document["verification"]["version"]["last_reset_time"] = (
                document["mutation"]["manager_before"]["last_reset_time"]
            )
            support.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            support_digest = sha256(support)
            upgrade_ref["supporting_evidence"]["sha256"] = support_digest
            proof = Path(upgrade_ref["path"])
            proof_document = json.loads(proof.read_text(encoding="utf-8"))
            proof_document["supporting_evidence"]["sha256"] = support_digest
            proof.write_text(
                json.dumps(proof_document, sort_keys=True),
                encoding="utf-8",
            )
            upgrade_ref["sha256"] = sha256(proof)
            forge_runtime_ledger_for_negative_test(manifest, attach_proofs=False)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("BMC reboot boundary" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_runtime_requires_current_task_upgrade_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            forge_runtime_ledger_for_negative_test(
                manifest, authorize_upgrade=False
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("upgrade authorization" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_runtime_requires_diagnosis_acceptance_before_development(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            forge_runtime_ledger_for_negative_test(
                manifest, include_diagnosis_gate=False
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("diagnosis.acceptance" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_runtime_requires_upgrade_before_debug_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            forge_runtime_ledger_for_negative_test(
                manifest, native_operations_in_order=False
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("ordered debug_collect" in item for item in report["violations"]),
            report["violations"],
        )

    def test_operator_attached_native_json_cannot_impersonate_runtime_operations(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            forge_runtime_ledger_for_negative_test(
                manifest, include_native_evidence=False
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("Runtime-owned native EvidenceAttached" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_runtime_requires_native_debug_observation_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            for dimension in (manifest["freshness"], manifest["hardware"]):
                evidence_ref = dimension["evidence"][0]
                support = evidence_ref["supporting_evidence"]
                support_path = Path(support["path"])
                document = json.loads(support_path.read_text(encoding="utf-8"))
                document.pop("observed_at", None)
                support_path.write_text(
                    json.dumps(document, sort_keys=True), encoding="utf-8"
                )
                support["sha256"] = sha256(support_path)
                proof_path = Path(evidence_ref["path"])
                proof = json.loads(proof_path.read_text(encoding="utf-8"))
                proof["supporting_evidence"]["sha256"] = support["sha256"]
                proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
                evidence_ref["sha256"] = sha256(proof_path)
            forge_runtime_ledger_for_negative_test(manifest, attach_proofs=False)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("observed_at" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_runtime_requires_complete_artifact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            for field in ("provenance", "source_revision", "target", "run_id"):
                manifest["artifact"].pop(field)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("artifact" in item and "provenance" in item for item in report["violations"]),
            report["violations"],
        )

    def test_manifest_cannot_select_the_runtime_authority(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            trusted_repository = Path(manifest["runtime"]["repository"]["path"])
            manifest["runtime"]["repository"]["path"] = str(
                root / "manifest-selected-forgery.sqlite3"
            )
            completed = run_qualification(
                root,
                manifest,
                runtime_repository=trusted_repository,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["dimensions"]["runtime"]["ledger_verified"])

    def test_runtime_evidence_must_be_bound_to_the_qualified_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            forge_runtime_ledger_for_negative_test(
                manifest,
                evidence_target="target-2",
                additional_targets=("target-2",),
            )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any(
                "not attached to the Runtime Run" in item
                for item in report["violations"]
            ),
            report["violations"],
        )

    def test_digest_bound_evidence_is_parsed_from_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, diagnosis_proof, _ = complete_manifest(root)
            trusted_repository = Path(manifest["runtime"]["repository"]["path"])
            original_read_text = Path.read_text

            def replacing_read_text(path: Path, *args: object, **kwargs: object) -> str:
                if path == diagnosis_proof:
                    return '{"schema":"replaced-after-digest"}'
                return original_read_text(path, *args, **kwargs)

            sys.path.insert(0, str(ROOT))
            from scripts import product_closeout_qualification as qualification

            with mock.patch.object(Path, "read_text", replacing_read_text):
                report = qualification.qualify(
                    manifest,
                    runtime_repository=trusted_repository,
                )

        self.assertTrue(report["promotable"], report["violations"])

    def test_malformed_runtime_repository_produces_a_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            repository = Path(manifest["runtime"]["repository"]["path"])
            for suffix in ("-wal", "-shm"):
                companion = Path(str(repository) + suffix)
                if companion.exists():
                    companion.unlink()
            repository.write_bytes(b"not-a-sqlite-database")
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("cannot replay Run ledger" in item for item in report["violations"]),
            report["violations"],
        )

    def test_missing_runtime_run_produces_a_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["runtime"]["run_id"] = "run-that-does-not-exist"
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("cannot replay Run ledger" in item for item in report["violations"]),
            report["violations"],
        )

    def test_unsupported_runtime_storage_version_produces_a_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            repository = Path(manifest["runtime"]["repository"]["path"])
            with sqlite3.connect(repository) as connection:
                connection.execute(
                    "UPDATE runtime_meta SET value = '999' WHERE key = 'storage_version'"
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("cannot replay Run ledger" in item for item in report["violations"]),
            report["violations"],
        )

    def test_qualification_does_not_migrate_the_trusted_runtime_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            repository = Path(manifest["runtime"]["repository"]["path"])
            with sqlite3.connect(repository) as connection:
                connection.execute("DROP INDEX evidence_index_blob")
                connection.execute("DROP TABLE evidence_index")
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = repository.read_bytes()
            sidecars_before = {
                suffix: Path(str(repository) + suffix).read_bytes()
                for suffix in ("-wal", "-shm")
                if Path(str(repository) + suffix).exists()
            }
            completed = run_qualification(root, manifest)
            sidecars_after = {
                suffix: Path(str(repository) + suffix).read_bytes()
                for suffix in ("-wal", "-shm")
                if Path(str(repository) + suffix).exists()
            }
            after = repository.read_bytes()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(after, before)
        self.assertEqual(sidecars_after, sidecars_before)

    def test_read_only_snapshot_replays_committed_wal_events(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            repository = Path(manifest["runtime"]["repository"]["path"])
            run_id = manifest["runtime"]["run_id"]
            with sqlite3.connect(repository) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute(
                    "UPDATE case_events SET operation_id = ? "
                    "WHERE case_id = ? AND revision = 1",
                    ("wal-only-product-closeout-start", run_id),
                )
                connection.commit()
                rows = connection.execute(
                    "SELECT revision, kind, operation_id, payload_json, created_at "
                    "FROM case_events WHERE case_id = ? ORDER BY revision",
                    (run_id,),
                ).fetchall()
                events = tuple(
                    {
                        "revision": int(row["revision"]),
                        "kind": str(row["kind"]),
                        "operation_id": str(row["operation_id"]),
                        "payload": json.loads(row["payload_json"]),
                        "created_at": float(row["created_at"]),
                    }
                    for row in rows
                )
                manifest["runtime"]["repository"]["sha256"] = hashlib.sha256(
                    json.dumps(
                        events,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                before = {
                    suffix: Path(str(repository) + suffix).read_bytes()
                    for suffix in ("", "-wal", "-shm")
                    if Path(str(repository) + suffix).exists()
                }
                completed = run_qualification(root, manifest)
                after = {
                    suffix: Path(str(repository) + suffix).read_bytes()
                    for suffix in ("", "-wal", "-shm")
                    if Path(str(repository) + suffix).exists()
                }

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(after, before)

    def test_artifact_read_failure_cannot_accept_the_artifact_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, artifact = complete_manifest(root)
            manifest["artifact"]["sha256"] = hashlib.sha256(b"").hexdigest()
            manifest["artifact"]["size"] = 0
            trusted_repository = Path(manifest["runtime"]["repository"]["path"])
            original_read_bytes = Path.read_bytes

            def failing_read_bytes(path: Path) -> bytes:
                if path == artifact:
                    raise OSError("artifact became unreadable")
                return original_read_bytes(path)

            sys.path.insert(0, str(ROOT))
            from scripts import product_closeout_qualification as qualification

            with mock.patch.object(Path, "read_bytes", failing_read_bytes):
                report = qualification.qualify(
                    manifest,
                    runtime_repository=trusted_repository,
                )

        self.assertFalse(report["dimensions"]["artifact"]["accepted"])
        self.assertTrue(
            any("cannot read content" in item for item in report["violations"]),
            report["violations"],
        )

    def test_boolean_artifact_size_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["artifact"]["size"] = True
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["dimensions"]["artifact"]["accepted"])
        self.assertTrue(
            any("integer size" in item for item in report["violations"]),
            report["violations"],
        )

    def test_retained_630_manifest_matches_the_machine_report(self) -> None:
        manifest_path = (
            ROOT / "docs" / "qualification" / "630-nvme-product-closeout-manifest.json"
        )
        report_path = ROOT / "docs" / "qualification" / "630-nvme-product-closeout.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        manifest_digest = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        self.assertEqual(report["manifest_digest"], f"sha256:{manifest_digest}")
        self.assertEqual(manifest["case"]["name"], report["case"]["name"])
        self.assertEqual(
            manifest["artifact"]["sha256"],
            "2fc339ddadc4fb4b1d550257f7f07986f290f562a964c50a535030f6b9ace987",
        )

    @unittest.skipUnless(
        Path("/home/workspace/openubmc-nvme-replay-20260812").is_dir(),
        "original 630 replay bundle is not installed",
    )
    def test_retained_630_evidence_replays_when_bundle_is_available(self) -> None:
        manifest_path = (
            ROOT / "docs" / "qualification" / "630-nvme-product-closeout-manifest.json"
        )
        report_path = ROOT / "docs" / "qualification" / "630-nvme-product-closeout.json"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(manifest_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            json.loads(report_path.read_text(encoding="utf-8")),
        )

    def test_evidence_digest_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, proof, _ = complete_manifest(root)
            proof.write_text('{"tampered":true}', encoding="utf-8")
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("evidence digest mismatch" in item for item in report["violations"])
        )

    def test_fresh_closeout_rejects_hash_valid_self_attested_empty_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            empty = root / "empty-proof.json"
            empty.write_text("{}", encoding="utf-8")
            manifest["diagnosis"]["evidence"] = [
                {"path": str(empty), "sha256": sha256(empty)}
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("diagnosis" in item and "structured proof" in item for item in report["violations"])
        )

    def test_fresh_closeout_rejects_proofs_without_fixed_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            for item in all_manifest_evidence(manifest):
                item.pop("supporting_evidence", None)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any(
                "fixed supporting evidence" in item
                for item in report["violations"]
            ),
            report["violations"],
        )

    def test_fresh_closeout_rejects_proofs_without_a_runtime_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["runtime"]["repository"] = {}
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("runtime.repository" in item for item in report["violations"]),
            report["violations"],
        )

    def test_fresh_closeout_rejects_upgrade_proof_for_another_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            upgrade_ref = manifest["upgrade"]["evidence"][0]
            upgrade_path = Path(upgrade_ref["path"])
            proof = json.loads(upgrade_path.read_text(encoding="utf-8"))
            proof["artifact"]["sha256"] = "0" * 64
            upgrade_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
            upgrade_ref["sha256"] = sha256(upgrade_path)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("upgrade" in item and "artifact identity" in item for item in report["violations"])
        )

    def test_fresh_closeout_rejects_stale_or_misordered_target_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            support_ref = manifest["freshness"]["evidence"][0][
                "supporting_evidence"
            ]
            support_path = Path(support_ref["path"])
            native = json.loads(support_path.read_text(encoding="utf-8"))
            native["observed_at"] = "2000-01-01T00:00:00Z"
            support_path.write_text(json.dumps(native, sort_keys=True), encoding="utf-8")
            support_digest = sha256(support_path)
            for dimension in ("freshness", "hardware"):
                evidence_ref = manifest[dimension]["evidence"][0]
                evidence_ref["supporting_evidence"]["sha256"] = support_digest
                evidence_path = Path(evidence_ref["path"])
                proof = json.loads(evidence_path.read_text(encoding="utf-8"))
                proof["supporting_evidence"]["sha256"] = support_digest
                evidence_path.write_text(
                    json.dumps(proof, sort_keys=True), encoding="utf-8"
                )
                evidence_ref["sha256"] = sha256(evidence_path)
            forge_runtime_ledger_for_negative_test(manifest, attach_proofs=False)
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["promotable"])
        self.assertTrue(
            any("observed_at predates" in item for item in report["violations"])
        )

    def test_fresh_closeout_uses_ledger_timeline_over_projection_timestamps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            contradictions = {
                "runtime": ("completed_at", "2026-08-29T10:00:00Z"),
                "upgrade": ("completed_at", "2026-08-29T11:45:00Z"),
                "freshness": ("observed_at", "2000-01-01T00:00:00Z"),
                "hardware": ("observed_at", "2000-01-01T00:00:00Z"),
            }
            for dimension, (field, value) in contradictions.items():
                original_ref = manifest[dimension]["evidence"][0]
                original = json.loads(
                    Path(original_ref["path"]).read_text(encoding="utf-8")
                )
                original[field] = value
                contradictory = root / f"{dimension}-contradictory.json"
                contradictory.write_text(
                    json.dumps(original, sort_keys=True), encoding="utf-8"
                )
                manifest[dimension]["evidence"].append(
                    {
                        "path": str(contradictory),
                        "sha256": sha256(contradictory),
                        **(
                            {
                                "supporting_evidence": original_ref[
                                    "supporting_evidence"
                                ]
                            }
                            if "supporting_evidence" in original_ref
                            else {}
                        ),
                    }
                )
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["promotable"], report["violations"])
        self.assertEqual(report["dimensions"]["runtime"]["verified_evidence_count"], 2)
        self.assertEqual(report["violations"], [])

    def test_source_commit_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["source"]["repositories"][0]["commit"] = "f" * 40
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("commit mismatch" in item for item in report["violations"])
        )

    def test_artifact_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(root)
            manifest["artifact"]["sha256"] = "0" * 64
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("artifact: digest mismatch" in item for item in report["violations"])
        )

    def test_historical_evidence_rejects_manifest_authored_claims(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(
                root, mode="historical-reconstruction"
            )
            unrelated = root / "unrelated.json"
            unrelated.write_text('{"name":"official-ut"}', encoding="utf-8")
            manifest["diagnosis"]["evidence"] = [
                {
                    "path": str(unrelated),
                    "sha256": sha256(unrelated),
                    "claims": [
                        {
                            "kind": "json_equals",
                            "path": "name",
                            "value": "official-ut",
                        }
                    ],
                }
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any(
                "manifest-authored claims are unsupported" in item
                for item in report["violations"]
            )
        )

    def test_historical_evidence_rejects_unknown_evidence_type(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(
                root, mode="historical-reconstruction"
            )
            diagnosis = root / "diagnosis.txt"
            diagnosis.write_text(
                "根因：错误关联键\n修复：使用全局映射\n", encoding="utf-8"
            )
            manifest["diagnosis"]["evidence"] = [
                {
                    "path": str(diagnosis),
                    "sha256": sha256(diagnosis),
                    "evidence_type": "invented-diagnosis-format",
                }
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("unknown historical evidence_type" in item for item in report["violations"])
        )

    def test_historical_evidence_type_must_match_its_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest, _, _, _ = complete_manifest(
                root, mode="historical-reconstruction"
            )
            record = root / "record.txt"
            record.write_text("10/10 passed\n", encoding="utf-8")
            manifest["diagnosis"]["evidence"] = [
                {
                    "path": str(record),
                    "sha256": sha256(record),
                    "evidence_type": "workflow-official-ut-record",
                }
            ]
            completed = run_qualification(root, manifest)

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertTrue(
            any("does not match diagnosis dimension" in item for item in report["violations"])
        )

    def test_historical_product_evidence_is_qualified_but_not_fresh_runtime_promotable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "storage"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "qualification@example.com"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Qualification"],
                cwd=source,
                check=True,
            )
            (source / "fix.lua").write_text("return true\n", encoding="utf-8")
            subprocess.run(["git", "add", "fix.lua"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fix"], cwd=source, check=True)
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()

            diagnosis = root / "diagnosis.md"
            diagnosis.write_text(
                "失败位置：storage 使用了错误的关联键。\n"
                "修复方案：改为消费全局盘位映射。\n",
                encoding="utf-8",
            )
            official_ut = root / "official-ut.log"
            official_ut.write_text("10/10 passed\n", encoding="utf-8")
            component_build = root / "component-build.log"
            component_build.write_text(
                "storage/1.0.0@openubmc/stable: Created package revision "
                "0123456789abcdef0123456789abcdef\n"
                "storage/1.0.0@openubmc/stable: Full package reference: "
                "storage/1.0.0@openubmc/stable#aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb#"
                "0123456789abcdef0123456789abcdef\n"
                "构建成功\n",
                encoding="utf-8",
            )
            product_build = root / "product-build.log"
            product_build.write_text(
                "hpm 构建成功 !!\n"
                "给 hpm 包 rootfs_openUBMC.hpm 签名\n"
                "任务 personal 执行成功\n",
                encoding="utf-8",
            )
            upgrade = root / "upgrade.md"
            upgrade.write_text(
                "上传与激活 | 完成\n安装版本确认 | `12.08.21.10`\n",
                encoding="utf-8",
            )
            freshness = root / "timeline.log"
            freshness.write_text(
                "elapsed=50s manager_ready\n"
                "elapsed=158s drives=1 direct=1 direct_attributed=1 raid=0 "
                "raid_zero=0 health_ok=1 presence_ok=1 serial_ok=1\n"
                "accepted_elapsed=158s\n",
                encoding="utf-8",
            )
            hardware = root / "drive-summary.json"
            hardware.write_text(
                json.dumps(
                    {
                        "accepted_elapsed_seconds": 158,
                        "summary": {
                            "drives": 1,
                            "direct": 1,
                            "direct_attributed": 1,
                            "raid": 0,
                            "raid_zero": 0,
                            "health_ok": 1,
                            "presence_ok": 1,
                            "serial_ok": 1,
                        },
                        "drives": [
                            {
                                "id": 23,
                                "protocol": 6,
                                "controller": 255,
                                "resource": 1,
                                "health": 0,
                                "presence": 1,
                                "serial_present": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def historical_ref(path: Path, evidence_type: str) -> dict[str, str]:
                return {
                    "path": str(path),
                    "sha256": sha256(path),
                    "evidence_type": evidence_type,
                }

            artifact = root / "openubmc.hpm"
            artifact.write_bytes(b"verified-historical-firmware")

            manifest = {
                "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
                "mode": "historical-reconstruction",
                "case": {
                    "name": "630 NVMe attribution",
                    "target": "historical-630-target",
                    "required_protocols": ["NVMe"],
                },
                "runtime": {
                    "run_id": "",
                    "terminal_outcome": "unavailable",
                },
                "diagnosis": {
                    "status": "passed",
                    "evidence": [
                        historical_ref(diagnosis, "workflow-diagnosis-record")
                    ],
                },
                "source": {
                    "status": "completed",
                    "repositories": [
                        {"name": "storage", "path": str(source), "commit": source_commit}
                    ],
                },
                "validation": {
                    "official_ut": {
                        "status": "passed",
                        "evidence": [
                            historical_ref(
                                official_ut, "workflow-official-ut-record"
                            )
                        ],
                    },
                    "build": {
                        "status": "compiled",
                        "evidence": [
                            historical_ref(component_build, "component-build-log"),
                            historical_ref(product_build, "product-build-log"),
                        ],
                    },
                },
                "artifact": {
                    "status": "verified",
                    "path": str(artifact),
                    "sha256": sha256(artifact),
                    "size": artifact.stat().st_size,
                    "version": "12.08.21.10",
                },
                "upgrade": {
                    "status": "completed",
                    "evidence": [
                        historical_ref(upgrade, "workflow-upgrade-record")
                    ],
                },
                "freshness": {
                    "status": "fresh",
                    "evidence": [
                        historical_ref(freshness, "reboot-acceptance-timeline")
                    ],
                },
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive23", "protocol": "NVMe"}],
                    "evidence": [historical_ref(hardware, "drive-summary-json")],
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(manifest_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["qualified"])
        self.assertFalse(report["promotable"])
        self.assertEqual(report["claim_level"], "historical-product-validated")
        self.assertEqual(report["violations"], [])
        self.assertIn("fresh_runtime_identity_unavailable", report["gaps"])
        self.assertTrue(report["evidence_digest"].startswith("sha256:"))

    def test_fresh_closeout_without_terminal_runtime_outcome_is_unqualified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.email", "qualification@example.com"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Qualification"],
                cwd=source,
                check=True,
            )
            (source / "fix.lua").write_text("return true\n", encoding="utf-8")
            subprocess.run(["git", "add", "fix.lua"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fix"], cwd=source, check=True)
            source_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=source,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            proof = root / "proof.json"
            proof.write_text("{}", encoding="utf-8")
            proof_ref = {"path": str(proof), "sha256": sha256(proof)}
            artifact = root / "firmware.hpm"
            artifact.write_bytes(b"firmware")
            manifest = {
                "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
                "mode": "fresh-runtime",
                "case": {
                    "name": "fresh closeout",
                    "target": "target-1",
                    "required_protocols": ["NVMe"],
                },
                "runtime": {"run_id": "", "terminal_outcome": "unavailable"},
                "diagnosis": {"status": "passed", "evidence": [proof_ref]},
                "source": {
                    "status": "completed",
                    "repositories": [
                        {"name": "source", "path": str(source), "commit": source_commit}
                    ],
                },
                "validation": {
                    "official_ut": {"status": "passed", "evidence": [proof_ref]},
                    "build": {"status": "compiled", "evidence": [proof_ref]},
                },
                "artifact": {
                    "status": "verified",
                    "path": str(artifact),
                    "sha256": sha256(artifact),
                    "size": artifact.stat().st_size,
                    "version": "1.0.0",
                },
                "upgrade": {"status": "completed", "evidence": [proof_ref]},
                "freshness": {"status": "fresh", "evidence": [proof_ref]},
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
                    "evidence": [proof_ref],
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(manifest_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["qualified"])
        self.assertFalse(report["promotable"])
        self.assertIn("runtime_run_id_missing", report["gaps"])
        self.assertIn("runtime_terminal_outcome=unavailable", report["gaps"])

    def test_sata_evidence_cannot_satisfy_nvme_scope(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            proof = root / "hardware.json"
            proof.write_text(
                json.dumps(
                    {
                        "summary": {
                            "drives": 1,
                            "direct": 1,
                            "direct_attributed": 1,
                            "raid": 0,
                            "raid_zero": 0,
                            "health_ok": 1,
                            "presence_ok": 1,
                            "serial_ok": 1,
                        },
                        "drives": [
                            {
                                "id": 1,
                                "protocol": 3,
                                "controller": 255,
                                "resource": 1,
                                "health": 0,
                                "presence": 1,
                                "serial_present": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = {
                "schema": "openubmc-agent-workflow.product-closeout-evidence.v1",
                "mode": "historical-reconstruction",
                "case": {
                    "name": "protocol scope",
                    "target": "target-1",
                    "required_protocols": ["NVMe"],
                },
                "hardware": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [{"device_id": "Drive1", "protocol": "NVMe"}],
                    "evidence": [
                        {
                            "path": str(proof),
                            "sha256": sha256(proof),
                            "evidence_type": "drive-summary-json",
                        }
                    ],
                },
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(manifest_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        report = json.loads(completed.stdout)
        self.assertFalse(report["dimensions"]["hardware"]["accepted"])
        self.assertTrue(
            any("protocol" in item for item in report["violations"]),
            report["violations"],
        )


if __name__ == "__main__":
    unittest.main()
