from __future__ import annotations

from collections.abc import Callable
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    AgentGateway,
    AgentGatewayError,
    CommandConflict,
    DIAGNOSTIC_RECEIPT_MAX_BYTES,
    DiagnosticReceipt,
    EvidenceUnavailable,
    EventRunStore,
    Gate,
    GateConflict,
    InMemoryRuntimeRepository,
    Incident,
    ObservationQuery,
    OBSERVATION_MAX_BYTES,
    Outcome,
    PendingCaseEvent,
    RevisionConflict,
    ReferenceViolation,
    ReconcileRun,
    ResumeRun,
    RunDecision,
    RunEngine,
    ResultProjector,
    RunTarget,
    STDIO_FRAME_MAX_BYTES,
    TOOLS_LIST_MAX_BYTES,
    TURN_MAX_BYTES,
    JsonRpcMcpEndpoint,
    FilesystemBlobRepository,
    InMemoryBlobRepository,
    RunTurn,
    RuntimeMcpService,
    RuntimeSDKContext,
    SQLiteCompatibilityTelemetryRepository,
    SQLiteRuntimeRepository,
    ScopeContract,
    ScopeViolation,
    StartRun,
    StdioMcpServer,
    SubmitGate,
    WorkflowDefinition,
    WorkflowStepDefinition,
    decode_run_command,
)
from openubmc_target_runtime.context_runtime import (  # noqa: E402
    BufferedRuntimeRepository,
)
from openubmc_target_runtime.agent_gateway import (  # noqa: E402
    render_execute_turn_text,
)
from openubmc_target_runtime.agent_interaction import (  # noqa: E402
    interaction_telemetry,
)
from openubmc_target_runtime.diagnostic_receipt import (  # noqa: E402
    build_diagnostic_receipt,
    latest_diagnostic_receipt,
)
from openubmc_target_runtime.run_store import RunCommitRequest  # noqa: E402
from openubmc_target_runtime.run_engine import (  # noqa: E402
    _evidence_supports_device,
    gate_input_schema,
)
from openubmc_target_runtime.validation_readiness import (  # noqa: E402
    normalize_hardware_protocol,
)
from tests.compatibility_history import seed_compatibility_history  # noqa: E402
def encoded_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def gate_binding(turn: dict[str, object]) -> dict[str, object]:
    gate = turn["gate"]
    assert isinstance(gate, dict)
    return {
        "gate_id": gate["gate_id"],
        "gate_version": gate["gate_version"],
        "schema_digest": gate["schema_digest"],
    }


def diagnostic_receipt_fixture(receipt_id: str) -> dict[str, object]:
    return {
        "receipt_id": receipt_id,
        "operation": "debug_run",
        "status": "complete",
        "coverage": {
            "requested": 1,
            "evaluable": 1,
            "unavailable": 0,
            "not_checked": 0,
            "complete": True,
        },
        "results": [
            {
                "result_id": "diagnosis",
                "kind": "diagnosis",
                "status": "available",
                "value": {"root_cause": receipt_id},
            }
        ],
        "freshness": {
            "status": "fresh",
            "observed_at": "2026-08-29T00:00:00Z",
            "complete": True,
        },
        "content_complete": True,
        "evidence": [],
        "gaps": [],
    }


class HardwareProtocolNormalizationTests(unittest.TestCase):
    def test_validation_and_evidence_use_the_same_protocol_aliases(self) -> None:
        self.assertEqual(normalize_hardware_protocol("nvme/of"), "NVMe-oF")
        self.assertTrue(
            _evidence_supports_device(
                {"Disk23": {"interface": "nvme-of"}},
                "Disk23",
                normalize_hardware_protocol("NVMe/oF"),
            )
        )


def artifact_ref(
    path: Path,
    *,
    kind: str,
    target: str,
    run_id: str,
    version: str = "",
) -> dict[str, object]:
    body = path.read_bytes()
    reference: dict[str, object] = {
        "handle": str(path),
        "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
        "kind": kind,
        "size": len(body),
        "provenance": "test-build",
        "retention_hint": "run-lifetime",
        "target": target,
        "run_id": run_id,
    }
    if version:
        reference["version"] = version
        Path(str(path) + ".metadata.json").write_text(
            json.dumps(
                {
                    "schema": "openubmc-agent-workflow/artifact-metadata-v1",
                    "artifact": {
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body),
                        "kind": kind,
                    },
                    "product_version": version,
                    "provenance": reference["provenance"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return reference


def compiled_validation_payload(identity: str) -> dict[str, object]:
    readiness_id = f"{identity}-readiness"
    return {
        "dependency_readiness": {
            "readiness_id": readiness_id,
            "status": "ready",
            "resolution": "available",
            "summary": "build dependencies resolved",
            "check_commands": ["conan graph info ."],
            "evidence_ids": [f"{identity}-dependency-log"],
            "attempt_count": 1,
            "reused_by": ["build"],
        },
        "validation_results": [
            {
                "kind": "build",
                "status": "compiled",
                "summary": "build compilation completed",
                "commands": ["bmcgo build"],
                "evidence_ids": [f"{identity}-build-log"],
                "dependency_readiness_id": readiness_id,
            }
        ],
    }


class FakeTask:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id


class CommitThenConflictRepository(InMemoryRuntimeRepository):
    """Simulate a competing writer winning immediately before our commit returns."""

    def __init__(self, conflict_kind: str) -> None:
        super().__init__()
        self.conflict_kind = conflict_kind
        self.conflicted = False

    def commit(self, case_id, *, expected_revision, events):
        pending = tuple(events)
        projection = super().commit(
            case_id,
            expected_revision=expected_revision,
            events=pending,
        )
        if not self.conflicted and any(
            event.kind == self.conflict_kind for event in pending
        ):
            self.conflicted = True
            raise RevisionConflict("simulated concurrent commit")
        return projection


class RecordingCommitRepository(InMemoryRuntimeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.commits: list[tuple[str, ...]] = []

    def commit(self, case_id, *, expected_revision, events):
        pending = tuple(events)
        self.commits.append(tuple(event.kind for event in pending))
        return super().commit(
            case_id,
            expected_revision=expected_revision,
            events=pending,
        )


class FailOnceRunReceiptRepository(InMemoryRuntimeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_completion = False
        self.claims: dict[tuple[str, str], str] = {}

    def claim_idempotency(self, case_id, key, fingerprint):
        self.claims[(case_id, key)] = fingerprint
        return super().claim_idempotency(case_id, key, fingerprint)

    def complete_idempotency(self, case_id, key, receipt) -> None:
        if self.fail_next_completion:
            self.fail_next_completion = False
            raise OSError("simulated Run receipt completion interruption")
        super().complete_idempotency(case_id, key, receipt)


class FailOnceBlobRepository(InMemoryBlobRepository):
    def __init__(self) -> None:
        super().__init__()
        self.failures_remaining = 1

    def put(self, body: bytes) -> str:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OSError("simulated evidence persistence interruption")
        return super().put(body)


class SemanticBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def open_task(self, task_id: str) -> FakeTask:
        return FakeTask(task_id)

    @staticmethod
    def close_task(_task: FakeTask) -> None:
        return None

    @staticmethod
    def maintain_task(_task: FakeTask) -> int:
        return 0

    @staticmethod
    def task_status(task: FakeTask) -> dict[str, object]:
        return {"task_id": task.task_id}

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_collect", dict(arguments)))
        queries = list(arguments.get("mdb_queries", []))
        ssh: dict[str, object] = {}
        for index, query in enumerate(queries):
            name = "mdbctl" if index == 0 else f"mdbctl_{index + 1}"
            ssh[name] = {
                "ok": True,
                "payload": {
                    "result": {
                        "properties": {
                            f"Object{index}": {"Query": query, "Value": index}
                        }
                    }
                },
            }
        value = {
            "ok": True,
            "observed_at": "2026-08-19T00:00:00Z",
            "result": {
                "capabilities": {
                    "ssh_transport": True,
                    "mdbctl": True,
                    "busctl": False,
                    "active_alarm_transport": True,
                    "active_alarm_endpoint_verified": False,
                    "active_alarms": True,
                },
                "lanes": {"ssh": ssh},
            },
        }
        if arguments.get("profile") == "freshness" or arguments.get(
            "_minimum_target_epoch"
        ):
            value["business_acceptance"] = "passed"
        minimum_epoch = arguments.get("_minimum_target_epoch")
        if isinstance(minimum_epoch, int) and not isinstance(minimum_epoch, bool):
            value["target_epoch"] = minimum_epoch
        return value

    def observe_query(self, task, arguments, context) -> dict[str, object]:
        value = self.debug_collect(task, arguments, context)
        anchor = str(value.get("observed_at", ""))
        value["observation_timing"] = {
            "started_at": anchor,
            "completed_at": anchor,
            "selectors": [
                {
                    "selector_id": str(selector.get("id", "")),
                    "kind": str(selector.get("kind", "")),
                    "started_at": anchor,
                    "completed_at": anchor,
                    "status": "observed",
                }
                for selector in arguments.get("selectors", [])
                if isinstance(selector, dict)
            ],
        }
        return value

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("mdb_only"):
            return self.debug_collect(task, arguments, context)
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "schema": "openubmc-debug.v1",
            "task": task.task_id,
            "summary": "diagnosis completed",
            "root_cause": "a bounded source defect was isolated",
            "hardware_devices": [
                {"device_id": "Disk23", "protocol": "SATA"},
                {"device_id": "Disk24", "protocol": "SAS"},
            ],
            "observed_at": "2026-08-19T00:00:00Z",
            "freshness": {"status": "fresh"},
        }

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        artifact_sha256 = str(arguments.get("artifact_sha256", ""))
        return {
            "ok": True,
            "summary": "live patch verified",
            "target_epoch": 1,
            "mutation": {
                "local_sha256": artifact_sha256,
                "remote_after_sha256": artifact_sha256,
                "root_mount_restored": True,
            },
            "verification": {
                "remote_sha256": artifact_sha256,
                "target_epoch": 1,
            },
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "live_patch",
                "expected_checksum": artifact_sha256,
                "observed_checksum": artifact_sha256,
                "root_mount_restored": True,
            },
        }

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("upgrade_run", dict(arguments)))
        product_version = str(arguments.get("product_version", ""))
        return {
            "ok": True,
            "summary": "upgrade verified",
            "target_epoch": 1,
            "verification": {
                "installed_version": product_version,
                "target_epoch": 1,
            },
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "upgrade",
            },
        }


class CompleteDriveVerificationSemanticBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        if arguments.get("mdb_expand_classes") != ["Drive"]:
            return value
        observed_at = "2026-08-19T00:00:00Z"
        ssh = value["result"]["lanes"]["ssh"]
        ssh.update(
            {
                "mdbctl_expand_1": {
                    "ok": True,
                    "observed_at": observed_at,
                    "result": {"stdout_lines": ["Drive_1_010102"]},
                },
                "mdbctl_expand_1_object_drive23": {
                    "ok": True,
                    "observed_at": observed_at,
                    "result": {
                        "properties": {
                            "Id": 23,
                            "Protocol": 6,
                            "Presence": 1,
                            "Health": 0,
                            "ResourceId": 1,
                            "SerialNumber": "NVME-DRIVE-23",
                        }
                    },
                },
            }
        )
        value["result"]["freshness"] = {
            "status": "fresh",
            "complete": True,
            "bmc_time_delta": {
                "before": "2026-08-19 00:00:00 +0000",
                "after": "2026-08-19 00:00:01 +0000",
                "elapsed_seconds": 1.0,
                "comparable": True,
                "clock_moved_backwards": False,
            },
        }
        value.pop("target_epoch", None)
        value["result"]["runtime"] = {
            "status": {
                "targets": [
                    {
                        "target_id": str(arguments.get("target_id", "")),
                        "epochs": {"target_epoch": 1},
                    }
                ]
            }
        }
        return value


class GenericCompletionBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {"ok": True, "summary": "Domain operation completed"}


class BoundedDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T04:42:55Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {
                "logs": "app.log",
                "files": [
                    "/etc/version.json",
                    "/proc/uptime",
                    "/tmp/custom.txt",
                ],
                "tree_service": "bmc.kepler.devmon",
                "mdb_queries": ["lsmc"],
                "mdb_expand_classes": ["Drive"],
                "mdb_only": False,
                "freshness_requested": True,
                "source_correlation_requested": True,
            },
            "result": {
                "completed_at": observed_at,
                "capabilities": {
                    "ssh_transport": True,
                    "remote_log_file": True,
                    "mdbctl": True,
                    "busctl": True,
                },
                "lanes": {
                    "ssh": {
                        "busctl": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "result": {
                                "stdout_lines": ["/bmc/kepler/devmon"],
                                "stdout_truncated": False,
                            },
                        },
                        "mdbctl": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "result": {
                                "stdout_lines": ["Drive_1_010102"],
                                "stdout_truncated": False,
                            },
                        },
                        "mdbctl_expand_1": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "result": {
                                "stdout_lines": ["Drive_1_010102"],
                                "stdout_truncated": False,
                            },
                        },
                        "mdbctl_expand_1_object_demo": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "result": {
                                "stdout_lines": ["Health=OK"],
                                "stdout_truncated": False,
                            },
                        },
                        "active_alarms": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "result": {
                                "records": [{"event_name": "DiskTimeout"}],
                                "truncated": False,
                            },
                        },
                    },
                    "telnet": {
                        "logs": {
                            "ok": True,
                            "code": "ok",
                            "observed_at": observed_at,
                            "result": {
                                "entries": [
                                    {
                                        "path": "/var/log/app.log",
                                        "line_count": 31,
                                        "lines_preview": [
                                            "mctpd request timeout",
                                            "storage PluginRequestEx timeout",
                                        ],
                                        "truncated": True,
                                        "content_complete": False,
                                    }
                                ]
                            },
                        },
                        "files": {
                            "/etc/version.json": {
                                "ok": True,
                                "code": "ok",
                                "observed_at": observed_at,
                                "result": {
                                    "lines": ['{"version":"12.08.21.06"}'],
                                    "truncated": False,
                                    "content_complete": True,
                                },
                            },
                            "/proc/uptime": {
                                "ok": True,
                                "code": "ok",
                                "observed_at": observed_at,
                                "result": {
                                    "lines": ["86400.00 100.00"],
                                    "truncated": False,
                                    "content_complete": True,
                                },
                            },
                            "/tmp/custom.txt": {
                                "ok": True,
                                "code": "ok",
                                "completed_at": observed_at,
                                "payload": {
                                    "observed_at": observed_at,
                                    "result": {
                                        "lines": ["custom diagnostic value"],
                                        "truncated": False,
                                    },
                                },
                            },
                        },
                    },
                },
                "freshness": {
                    "status": "partial",
                    "complete": False,
                    "bmc_time_delta": {
                        "before": "2026-08-25 04:42:50 +0000",
                        "after": "2026-08-25 04:42:55 +0000",
                        "elapsed_seconds": 5.0,
                        "comparable": True,
                        "clock_moved_backwards": False,
                    },
                    "unavailable_dimensions": ["active_alarms"],
                    "stale_evidence": [],
                },
                "correlation": {
                    "records": [{"event_name": "DiskTimeout", "confidence": 0.9}],
                    "correlation_complete": True,
                },
            },
        }


class CompleteBoundedDiagnosticBackend(BoundedDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        runtime_result = value["result"]
        log_entry = runtime_result["lanes"]["telnet"]["logs"]["result"][
            "entries"
        ][0]
        log_entry["truncated"] = False
        log_entry["content_complete"] = True
        runtime_result["lanes"]["telnet"]["files"]["/tmp/custom.txt"][
            "payload"
        ]["result"]["content_complete"] = True
        runtime_result["freshness"] = {
            "status": "fresh",
            "complete": True,
            "bmc_time_delta": runtime_result["freshness"]["bmc_time_delta"],
        }
        return value


class MissingTargetClockDiagnosticBackend(CompleteBoundedDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["result"]["freshness"].pop("bmc_time_delta")
        return value


class EmptyStructuredDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T04:42:55Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"files": ["/etc/version.json"]},
            "result": {
                "completed_at": observed_at,
                "lanes": {
                    "telnet": {
                        "files": {
                            "/etc/version.json": {
                                "ok": True,
                                "code": "ok",
                                "observed_at": observed_at,
                                "result": {},
                            }
                        }
                    }
                },
                "freshness": {"status": "fresh"},
            },
        }


class MetadataOnlyDiagnosticBackend(EmptyStructuredDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["result"]["lanes"]["telnet"]["files"]["/etc/version.json"][
            "result"
        ] = {"content_complete": True}
        return value


class ProjectionCompactedDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T04:42:55Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"files": ["/tmp/records.json"]},
            "result": {
                "completed_at": observed_at,
                "lanes": {
                    "telnet": {
                        "files": {
                            "/tmp/records.json": {
                                "ok": True,
                                "code": "ok",
                                "observed_at": observed_at,
                                "result": {
                                    "records": [
                                        {"record_id": index}
                                        for index in range(17)
                                    ],
                                    "content_complete": True,
                                },
                            }
                        }
                    }
                },
                "freshness": {
                    "status": "fresh",
                    "bmc_time_delta": {
                        "before": "2026-08-25 04:42:50 +0000",
                        "after": "2026-08-25 04:42:55 +0000",
                        "elapsed_seconds": 5.0,
                        "comparable": True,
                        "clock_moved_backwards": False,
                    },
                },
            },
        }


class MissingContentCompleteDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T04:42:55Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"files": ["/tmp/records.json"]},
            "result": {
                "completed_at": observed_at,
                "lanes": {
                    "telnet": {
                        "files": {
                            "/tmp/records.json": {
                                "ok": True,
                                "observed_at": observed_at,
                                "result": {"records": [{"record_id": 1}]},
                            }
                        }
                    }
                },
                "freshness": {"status": "fresh"},
            },
        }


class TimestampOnlyDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "root_cause": "an old bounded conclusion exists",
            "observed_at": "2020-01-01T00:00:00Z",
        }


class StaleEvidenceDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "root_cause": "connector timeout",
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {
                "status": "fresh",
                "complete": True,
                "stale_evidence": [
                    {
                        "evidence_id": "evidence-old",
                        "reason": "target_epoch_mismatch",
                    }
                ],
            },
        }


class OmittedRequestDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "root_cause": "the adapter omitted the requested log check",
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {"status": "fresh"},
        }


class SkippedCorrelationDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T00:00:00Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"source_correlation_requested": True},
            "result": {
                "completed_at": observed_at,
                "freshness": {"status": "fresh"},
                "correlation": {
                    "source_search": {
                        "ok": False,
                        "code": "skipped",
                        "error": "Source correlation disabled",
                    },
                    "records": [],
                },
            },
        }


class MultiTargetDiagnosticBackend(SemanticBackend):
    @staticmethod
    def _target_result(version: str) -> dict[str, object]:
        observed_at = "2026-08-25T00:00:00Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"files": ["/etc/version.json"]},
            "result": {
                "completed_at": observed_at,
                "freshness": {"status": "fresh"},
                "capabilities": {"remote_log_file": True},
                "lanes": {
                    "telnet": {
                        "files": {
                            "/etc/version.json": {
                                "ok": True,
                                "observed_at": observed_at,
                                "result": {
                                    "lines": [version],
                                    "content_complete": True,
                                },
                            }
                        }
                    }
                },
            },
        }

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T00:00:00Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "targets": [
                {
                    "role": "reference",
                    "target_id": "reference",
                    "status": "ok",
                    "result": self._target_result("12.08.21.06"),
                },
                {
                    "role": "candidate",
                    "target_id": "candidate",
                    "status": "ok",
                    "result": self._target_result("12.08.21.07"),
                },
            ],
            "comparison": {
                "status": "complete",
                "differences": [
                    {
                        "path": "$.version",
                        "reference": "12.08.21.06",
                        "candidate": "12.08.21.07",
                    }
                ],
            },
        }


class MissingTargetDiagnosticBackend(MultiTargetDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["targets"] = value["targets"][1:]
        return value


class MissingComparisonDiagnosticBackend(MultiTargetDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value.pop("comparison")
        return value


class MissingTargetFreshnessDiagnosticBackend(MultiTargetDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["targets"][1]["result"]["result"].pop("freshness")
        return value


class IncompleteTargetFreshnessDiagnosticBackend(MultiTargetDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["targets"][1]["result"]["result"]["freshness"] = {
            "status": "fresh",
            "complete": False,
        }
        return value


class TopLevelFreshMissingTargetFreshnessDiagnosticBackend(
    MissingTargetFreshnessDiagnosticBackend
):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        value["freshness"] = {"status": "fresh"}
        return value


class SymmetricMultiTargetDiagnosticBackend(MultiTargetDiagnosticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_run(task, arguments, context)
        for index, target in enumerate(value["targets"]):
            target["role"] = f"target-{'a' if index == 0 else 'b'}"
            target["target_id"] = target["role"]
        return value


class OversizedDurableDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T00:00:00Z"
        files = [f"/tmp/diagnostic-{index}.txt" for index in range(64)]
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"files": files},
            "result": {
                "completed_at": observed_at,
                "freshness": {"status": "fresh"},
                "lanes": {
                    "telnet": {
                        "files": {
                            path: {
                                "ok": True,
                                "observed_at": observed_at,
                                "result": {
                                    f"field-{field}": "x" * 20_000
                                    for field in range(16)
                                },
                            }
                            for path in files
                        }
                    }
                },
            },
        }


class SecretOnlyDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        observed_at = "2026-08-25T00:00:00Z"
        return {
            "ok": True,
            "observed_at": observed_at,
            "request": {"files": ["/tmp/secret.txt"]},
            "result": {
                "completed_at": observed_at,
                "freshness": {"status": "fresh"},
                "lanes": {
                    "telnet": {
                        "files": {
                            "/tmp/secret.txt": {
                                "ok": True,
                                "observed_at": observed_at,
                                "result": {"password": "must-not-leak"},
                            }
                        }
                    }
                },
            },
        }


class TruncatedDiagnosisTextBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "root_cause": "root-cause-" + "x" * 20_000,
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {"status": "fresh"},
        }


class SourceTruncatedDiagnosisBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "root_cause": "connector timeout",
            "observed_at": "2026-08-25T00:00:00Z",
            "freshness": {"status": "fresh", "complete": True},
            "truncated": True,
            "content_complete": False,
        }


class LargeObservationBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        ssh = value["result"]["lanes"]["ssh"]
        for child in ssh.values():
            child["payload"]["result"]["properties"]["Large"] = {
                "Value": "x" * 12_000
            }
        return value


class OversizedObservationBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value["result"]["runtime"] = {
            "status": {
                "targets": [
                    {
                        "target": {
                            "host": "host-" + "h" * 20_000,
                            "fingerprint": "fingerprint-" + "f" * 20_000,
                        },
                        "epochs": {"target_epoch": 1},
                    }
                ]
            }
        }
        return value


class FailOnceUpgradeSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.upgrade_attempts = 0

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.upgrade_attempts += 1
        self.calls.append(("upgrade_run", dict(arguments)))
        if self.upgrade_attempts == 1:
            raise OSError("upload connection lost")
        return {
            "ok": True,
            "summary": "upgrade reconciled and verified",
            "target_epoch": 1,
            "verification": {
                "installed_version": str(arguments.get("product_version", "")),
                "target_epoch": 1,
            },
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "upgrade",
            },
        }


class FailLivePatchSemanticBackend(SemanticBackend):
    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        raise OSError("live patch connection lost")


class MissingFreshEpochSemanticBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value.pop("target_epoch", None)
        return value


class IncompleteAcceptanceSemanticBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value.pop("business_acceptance", None)
        return value

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        return {
            "ok": True,
            "summary": "live patch returned without integrity evidence",
            "target_epoch": 1,
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "live_patch",
            },
        }


class ConflictingAcceptanceSemanticBackend(SemanticBackend):
    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value["business_acceptance"] = "passed"
        value["acceptance_results"] = [
            {
                "requirement_id": "stage.verification",
                "status": "failed",
            }
        ]
        return value


class AdapterProjectionBuildUpgradeBackend(SemanticBackend):
    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("upgrade_run", dict(arguments)))
        product_version = str(arguments.get("product_version", ""))
        return {
            "operation_id": context.operation_id,
            "action": "upgrade",
            "epoch_before": 0,
            "epoch_after": 1,
            "verification": {
                "installed_version": product_version,
                "target_epoch": 1,
            },
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "upgrade",
                "epoch_before": 0,
                "epoch_after": 1,
                "verification_state": "verified",
            },
        }

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value.pop("business_acceptance", None)
        value.pop("target_epoch", None)
        target_id = str(arguments.get("target_id", ""))
        result = value["result"]
        result["runtime"] = {
            "status": {
                "targets": [
                    {
                        "target_id": target_id,
                        "epochs": {"target_epoch": 1},
                    }
                ]
            }
        }
        return value


class DeferredAdapterProjectionBuildUpgradeBackend(
    AdapterProjectionBuildUpgradeBackend
):
    def __init__(self) -> None:
        super().__init__()
        self.verification_attempts = 0

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        self.verification_attempts += 1
        if self.verification_attempts <= 2:
            self.calls.append(("debug_collect", dict(arguments)))
            raise OSError("verification transport is temporarily unavailable")
        return super().debug_collect(task, arguments, context)


class DeferredVerificationSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.verification_attempts = 0

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.verification_attempts += 1
        if self.verification_attempts <= 2:
            self.calls.append(("debug_collect", dict(arguments)))
            raise OSError("verification transport is temporarily unavailable")
        return super().debug_collect(task, arguments, context)


class RunningUpgradeSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.upgrade_attempts = 0
        self.upgrade_operation_ids: list[str] = []

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.upgrade_attempts += 1
        self.upgrade_operation_ids.append(str(context.operation_id))
        self.calls.append(("upgrade_run", dict(arguments)))
        if self.upgrade_attempts == 1:
            return {
                "ok": True,
                "status": "running",
                "operation_id": context.operation_id,
                "summary": "firmware upload accepted",
                "target_epoch": 1,
            }
        return {
            "ok": True,
            "summary": "upgrade reattached and verified",
            "target_epoch": 1,
            "verification": {
                "installed_version": str(arguments.get("product_version", "")),
                "target_epoch": 1,
            },
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "upgrade",
            },
        }


class BlockingUnknownRecoveryUpgradeBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_started = threading.Event()
        self.release_recovery = threading.Event()
        self.apply_calls = 0
        self.recovery_calls = 0
        self.operation_ids: list[str] = []

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.operation_ids.append(str(context.operation_id))
        if arguments.get("_runtime_effect_recovery") == "reconcile":
            self.recovery_calls += 1
            self.recovery_started.set()
            self.release_recovery.wait(timeout=2)
            return {
                "ok": True,
                "summary": "upgrade reconciled and verified",
                "target_epoch": 1,
                "verification": {
                    "installed_version": str(
                        arguments.get("product_version", "")
                    ),
                    "target_epoch": 1,
                },
                "journal": {
                    "operation_id": context.operation_id,
                    "stage": "verified",
                    "action": "upgrade",
                },
            }
        self.apply_calls += 1
        raise OSError("upgrade result was lost after target execution started")


class BlockingLivePatchSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.intent_was_persisted = False
        self.inspect_persisted_intent = lambda _operation_id: False
        self.operation_ids: list[str] = []

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.intent_was_persisted = bool(
            self.inspect_persisted_intent(str(context.operation_id))
        )
        self.operation_ids.append(str(context.operation_id))
        self.started.set()
        self.release.wait(timeout=2)
        context.raise_if_stopped()
        self.calls.append(("live_patch_run", dict(arguments)))
        return {
            "ok": True,
            "summary": "live patch verified after bounded wait",
            "target_epoch": 1,
            "mutation": {
                "local_sha256": str(arguments.get("artifact_sha256", "")),
                "remote_after_sha256": str(arguments.get("artifact_sha256", "")),
                "root_mount_restored": True,
            },
            "verification": {
                "remote_sha256": str(arguments.get("artifact_sha256", "")),
                "target_epoch": 1,
            },
            "journal": {
                "operation_id": context.operation_id,
                "stage": "verified",
                "action": "live_patch",
                "expected_checksum": str(arguments.get("artifact_sha256", "")),
                "observed_checksum": str(arguments.get("artifact_sha256", "")),
                "root_mount_restored": True,
            },
        }


class DormantEffectRunner:
    def __init__(self) -> None:
        self.intents = []

    def has_seen(self, _intent) -> bool:
        return False

    def ensure(self, intent, *, mode, settlement_generation=0, claim=None):
        if claim is not None and not claim():
            return None
        self.intents.append((intent, mode, settlement_generation))
        return SimpleNamespace(
            mode=mode,
            future=object(),
            settlement_generation=settlement_generation,
        )

    @staticmethod
    def wait(_future, _timeout: float) -> bool:
        return False

    @staticmethod
    def close() -> None:
        return None


class RecoveryAwareLivePatchBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.apply_calls = 0
        self.reconcile_calls = 0
        self.operation_ids: list[str] = []

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.operation_ids.append(str(context.operation_id))
        if arguments.get("_runtime_effect_recovery") == "reconcile":
            self.reconcile_calls += 1
        else:
            self.apply_calls += 1
        return super().live_patch_run(task, arguments, context)


class MissingJournalRecoveryBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.apply_calls = 0
        self.recovery_calls = 0

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        if arguments.get("_runtime_effect_recovery") == "reconcile":
            self.recovery_calls += 1
            raise OSError("no durable mutation journal")
        self.apply_calls += 1
        return super().live_patch_run(task, arguments, context)


class FailThenBlockRecoveryLivePatchBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.apply_calls = 0
        self.recovery_calls = 0
        self.recovery_started = threading.Event()
        self.release_recovery = threading.Event()
        self.operation_ids: list[str] = []

    def live_patch_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.operation_ids.append(str(context.operation_id))
        if arguments.get("_runtime_effect_recovery") == "reconcile":
            self.recovery_calls += 1
            if self.recovery_calls == 1:
                raise OSError("recovery inspection was temporarily unavailable")
            self.recovery_started.set()
            self.release_recovery.wait(timeout=2)
            return super().live_patch_run(task, arguments, context)
        self.apply_calls += 1
        raise OSError("live patch result was lost after target execution started")


class RecoveryBoundaryConflictOnceStore:
    def __init__(self, delegate: EventRunStore) -> None:
        self.delegate = delegate
        self.raced = False

    def load(self, run_id: str, **kwargs):
        return self.delegate.load(run_id, **kwargs)

    def commit(self, request):
        if not isinstance(request, RunCommitRequest):
            return self.delegate.commit(request)

        def build(draft):
            decision = request.build(draft)
            if (
                decision is not None
                and request.command_id.startswith("recover-")
                and not self.raced
            ):
                self.raced = True
                self.delegate.commit(
                    RunDecision(
                        run_id=decision.run_id,
                        command_id=(
                            "concurrent-"
                            + decision.command_id.removeprefix("recover-")
                        ),
                        input_digest="f" * 64,
                        expected_revision=decision.expected_revision,
                        events=(),
                        turn=decision.turn,
                    )
                )
            return decision

        return self.delegate.commit(
            RunCommitRequest(
                run_id=request.run_id,
                command_id=request.command_id,
                input_digest=request.input_digest,
                build=build,
                retry_conflicts=request.retry_conflicts,
                exhausted_message=request.exhausted_message,
                task_id=request.task_id,
            )
        )


class BlockingDebugSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.operation_ids: list[str] = []

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("mdb_only"):
            return self.debug_collect(task, arguments, context)
        context.raise_if_stopped()
        self.operation_ids.append(str(context.operation_id))
        if len(self.operation_ids) >= 2:
            self.started.set()
        self.release.wait(timeout=2)
        return super().debug_run(task, arguments, context)


class BlockingOnceDebugSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("mdb_only"):
            return self.debug_collect(task, arguments, context)
        self.started.set()
        self.release.wait(timeout=2)
        return super().debug_run(task, arguments, context)


class SequencedEvidenceRetryBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._attempt = 0
        self.first_started = threading.Event()
        self.first_release = threading.Event()
        self.second_started = threading.Event()
        self.second_release = threading.Event()

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("mdb_only"):
            return self.debug_collect(task, arguments, context)
        context.raise_if_stopped()
        with self._lock:
            self._attempt += 1
            attempt = self._attempt
        if attempt == 1:
            self.first_started.set()
            self.first_release.wait(timeout=3)
        else:
            self.second_started.set()
            self.second_release.wait(timeout=3)
        return {"ok": True, "summary": f"observation-{attempt}"}


class PersistentlyRunningUpgradeBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.upgrade_attempts = 0

    def upgrade_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.upgrade_attempts += 1
        self.calls.append(("upgrade_run", dict(arguments)))
        return {
            "ok": True,
            "status": "running",
            "summary": "upgrade is still active",
            "target_epoch": 1,
        }


class PersistentlyRunningDebugBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.debug_attempts = 0

    def debug_run(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("mdb_only"):
            return self.debug_collect(task, arguments, context)
        context.raise_if_stopped()
        self.debug_attempts += 1
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "status": "running",
            "summary": "debug collection is still active",
        }


class AutoAssuranceSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.assurance_calls: list[bool] = []
        self.selector_scopes: list[list[dict[str, object]]] = []
        self.mdb_collections = 0

    def observe_query(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        assured = bool(arguments.get("assured"))
        prior = arguments.get("prior_observation")
        self.assurance_calls.append(assured)
        self.selector_scopes.append(
            [
                dict(selector)
                for selector in arguments.get("selectors", [])
                if isinstance(selector, dict)
            ]
        )
        if isinstance(prior, dict):
            value = json.loads(json.dumps(prior))
            value["result"]["capabilities"]["remote_log_file"] = True
            value["observed_at"] = "2026-08-19T00:00:01Z"
            value["observation_timing"] = {
                "started_at": "2026-08-19T00:00:00Z",
                "completed_at": "2026-08-19T00:00:01Z",
                "selectors": [
                    {
                        "selector_id": str(selector.get("id", "")),
                        "kind": str(selector.get("kind", "")),
                        "started_at": "2026-08-19T00:00:00Z",
                        "completed_at": (
                            "2026-08-19T00:00:01Z"
                            if selector.get("kind") == "capability"
                            else "2026-08-19T00:00:00Z"
                        ),
                        "status": "observed",
                    }
                    for selector in arguments.get("selectors", [])
                    if isinstance(selector, dict)
                ],
            }
            return value
        self.mdb_collections += 1
        value = self.debug_collect(task, arguments, context)
        value["result"]["capabilities"].pop("remote_log_file", None)
        value["observation_timing"] = {
            "started_at": "2026-08-19T00:00:00Z",
            "completed_at": "2026-08-19T00:00:00Z",
            "selectors": [
                {
                    "selector_id": str(selector.get("id", "")),
                    "kind": str(selector.get("kind", "")),
                    "started_at": "2026-08-19T00:00:00Z",
                    "completed_at": "2026-08-19T00:00:00Z",
                    "status": (
                        "missing"
                        if selector.get("kind") == "capability"
                        else "observed"
                    ),
                }
                for selector in arguments.get("selectors", [])
                if isinstance(selector, dict)
            ],
        }
        return value


class FailingAssuranceSemanticBackend(AutoAssuranceSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("assured"):
            self.assurance_calls.append(True)
            raise OSError("assurance transport is temporarily unavailable")
        return super().observe_query(task, arguments, context)


class SelectorTimingSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.selector_scopes: list[list[dict[str, object]]] = []

    def observe_query(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        selectors = arguments.get("selectors")
        if not isinstance(selectors, list):
            raise TypeError("selectors must reach the observation adapter")
        self.selector_scopes.append(
            [dict(selector) for selector in selectors if isinstance(selector, dict)]
        )
        value = self.debug_collect(task, arguments, context)
        value["observation_timing"] = {
            "started_at": "2026-08-19T00:00:00Z",
            "completed_at": "2026-08-19T00:00:02Z",
            "selectors": [
                {
                    "selector_id": "mdb-second",
                    "kind": "mdb",
                    "started_at": "2026-08-19T00:00:00Z",
                    "completed_at": "2026-08-19T00:00:01Z",
                    "status": "observed",
                },
                {
                    "selector_id": "caps-first",
                    "kind": "capability",
                    "started_at": "2026-08-19T00:00:01Z",
                    "completed_at": "2026-08-19T00:00:02Z",
                    "status": "observed",
                },
            ],
        }
        return value


class MissingSelectorTimingSemanticBackend(SelectorTimingSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        value = super().observe_query(task, arguments, context)
        value["observation_timing"]["selectors"][0].update(
            {"started_at": "", "completed_at": "", "status": "missing"}
        )
        return value


class StaleSelectorTimingSemanticBackend(SelectorTimingSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        value = super().observe_query(task, arguments, context)
        value["observation_timing"]["started_at"] = "2026-08-19T00:00:01Z"
        value["observation_timing"]["selectors"][0].update(
            {
                "started_at": "2026-08-19T00:00:00Z",
                "completed_at": "2026-08-19T00:00:00Z",
            }
        )
        return value


class NoSelectorTimingSemanticBackend(SelectorTimingSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        value = super().observe_query(task, arguments, context)
        value.pop("observation_timing", None)
        return value


class AssuranceUnavailableSemanticBackend(SemanticBackend):
    def __init__(self) -> None:
        super().__init__()
        self.observe_query = None

    def debug_collect(self, task, arguments, context) -> dict[str, object]:
        value = super().debug_collect(task, arguments, context)
        value["result"]["capabilities"].pop("remote_log_file", None)
        return value


class SkewedSelectorTimingSemanticBackend(SelectorTimingSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        value = super().observe_query(task, arguments, context)
        value["observation_timing"]["completed_at"] = "2026-08-19T00:00:20Z"
        value["observation_timing"]["selectors"][1]["started_at"] = (
            "2026-08-19T00:00:19Z"
        )
        value["observation_timing"]["selectors"][1]["completed_at"] = (
            "2026-08-19T00:00:20Z"
        )
        return value


class NonImprovingAssuranceSemanticBackend(AutoAssuranceSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        value = super().observe_query(task, arguments, context)
        if arguments.get("assured"):
            value["result"]["lanes"]["ssh"]["mdbctl"]["payload"]["result"] = {
                "properties": {"AssuredValue": {"Value": "must-not-win"}}
            }
            value["observation_timing"]["completed_at"] = "2026-08-19T00:00:20Z"
            value["observation_timing"]["selectors"][0]["completed_at"] = (
                "2026-08-19T00:00:20Z"
            )
        return value


class OversizedTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        return RunTurn(
            run_id="case-oversized-turn",
            state="blocked",
            gate={
                "kind": "blocker",
                "name": "oversized-blocker",
                "message": "message-" + "m" * 20_000,
            },
            facts=({"value": "f" * 20_000},),
            gaps=("gap-" + "g" * 20_000,),
            next_action="next-" + "n" * 20_000,
        )


class RejectDispatchRuntime:
    def __init__(self) -> None:
        self.execute_calls = 0

    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        self.execute_calls += 1
        raise AssertionError("invalid Action must be rejected before Runtime dispatch")


class OversizedGateTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-oversized-gate",
            state="waiting_response",
            gate={
                "kind": "phase",
                "gate_id": "gate-oversized",
                "gate_version": 1,
                "schema_digest": "sha256:" + "a" * 64,
                "name": "developer.change",
                "owner": "openubmc-developer",
                "input_schema": {
                    "type": "object",
                    "description": "x" * 5_000,
                },
            },
        )


class OversizedNoProgressGateTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-oversized-no-progress-gate",
            state="waiting_response",
            gate={
                "kind": "phase",
                "gate_id": "gate-oversized-no-progress",
                "gate_version": 1,
                "schema_digest": "sha256:" + "b" * 64,
                "name": "diagnosis.acceptance",
                "owner": "openubmc-debug",
                "input_schema": {
                    "type": "object",
                    "description": "x" * 12_000,
                },
            },
            response_required=True,
            progress={"status": "no_progress", "reason": "response_required"},
        )


class OversizedIncidentTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-oversized-incident",
            state="incident",
            incident=Incident(
                incident_id="incident-oversized",
                code="artifact_reference_invalid",
                message="incident-" + "i" * 20_000,
                effect_id="effect-oversized",
            ),
            facts=({"value": "f" * 20_000},),
            gaps=("incident-gap-" + "g" * 20_000,),
            next_action="restore the ArtifactRef and resume the same Run",
        )


class AdapterExpandedDiagnosticBackend(SemanticBackend):
    def debug_run(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        self.calls.append(("debug_run", dict(arguments)))
        return {
            "ok": True,
            "observed_at": "2026-08-26T00:00:00Z",
            "request": {
                "files": [f"/tmp/adapter-{index}.txt" for index in range(1025)],
                "mdb_only": True,
            },
            "freshness": {"status": "fresh"},
        }


class OversizedDiagnosticTurnRuntime:
    def __init__(self, result_count: int = 20) -> None:
        self.result_count = result_count

    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-oversized-diagnostic",
            state="completed",
            diagnostic_receipt=DiagnosticReceipt.from_public_dict({
                "schema": "openubmc.target-runtime.v1/diagnostic-receipt-v1",
                "receipt_id": "diagnostic-oversized",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": self.result_count,
                    "evaluable": self.result_count,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": f"result-{index}",
                        "kind": "bounded-logs",
                        "request": "r" * 20_000,
                        "status": "available",
                        "observed_at": "2026-08-25T04:42:55Z" + "o" * 20_000,
                        "gap": "g" * 20_000,
                        "value": {
                            f"field-{field}": "x" * 20_000
                            for field in range(10)
                        },
                        "evidence_ids": ["e" * 20_000 for _ in range(20)],
                    }
                    for index in range(self.result_count)
                ],
                "freshness": {
                    "status": "partial",
                    "observed_at": "2026-08-25T04:42:55Z",
                    "unavailable_dimensions": [
                        {
                            f"dimension-{field}": "x" * 20_000
                            for field in range(16)
                        }
                        for _ in range(32)
                    ],
                    "lost_dimensions": [
                        {"dimension": "x" * 20_000} for _ in range(32)
                    ],
                    "stale_evidence": [
                        {"evidence_id": "x" * 20_000} for _ in range(32)
                    ],
                },
                "capabilities": {"telnet": "available"},
                "truncated": False,
                "content_complete": True,
                "evidence": [
                    {
                        "evidence_id": f"evidence-{index}-" + "e" * 20_000,
                        "target_id": "target-" + "t" * 20_000,
                        "observed_at": "2026-08-25T04:42:55Z" + "o" * 20_000,
                        "target_epoch": index,
                        "byte_count": 20_000,
                    }
                    for index in range(8)
                ],
                "gaps": [],
            }),
        )


class RepeatedAcceptedDiagnosticTurnRuntime(OversizedDiagnosticTurnRuntime):
    def __init__(
        self,
        *,
        change_receipt: bool = False,
        incomplete_receipt: bool = False,
    ) -> None:
        super().__init__()
        self.calls = 0
        self.change_receipt = change_receipt
        self.incomplete_receipt = incomplete_receipt

    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        self.calls += 1
        receipt = super().execute(None, task_id="", operation_id="").diagnostic_receipt
        assert receipt is not None
        if self.calls > 1 and self.change_receipt:
            changed = receipt.to_public_dict()
            changed["receipt_id"] = "diagnostic-oversized-new-cycle"
            receipt = DiagnosticReceipt.from_public_dict(changed)
        if self.incomplete_receipt:
            incomplete = receipt.to_public_dict()
            incomplete["status"] = "incomplete"
            incomplete["coverage"] = {
                "requested": self.result_count,
                "evaluable": self.result_count - 1,
                "unavailable": 0,
                "not_checked": 1,
                "complete": False,
            }
            incomplete["results"][-1]["status"] = "not_checked"
            incomplete["results"][-1]["gap"] = "diagnostic_result_not_visible"
            incomplete["content_complete"] = False
            incomplete["gaps"] = ["diagnostic_result_not_visible"]
            receipt = DiagnosticReceipt.from_public_dict(incomplete)
        if self.calls == 1:
            return RunTurn(
                run_id="case-repeated-accepted-diagnostic",
                state="waiting_response",
                gate={
                    "kind": "phase",
                    "gate_id": "gate-developer-change",
                    "gate_version": 1,
                    "schema_digest": "sha256:" + "a" * 64,
                    "name": "developer.change",
                    "owner": "openubmc-developer",
                    "input_schema": {"type": "object"},
                },
                diagnostic_receipt=receipt,
            )
        return RunTurn(
            run_id="case-repeated-accepted-diagnostic",
            state="completed",
            outcome=Outcome(
                status="completed",
                summary="product closeout completed",
                acceptance=[
                    {"requirement_id": "runtime", "status": "passed"}
                ],
            ),
            outcome_recorded=True,
            diagnostic_receipt=receipt,
        )


class OneShotAcceptedDiagnosticTurnRuntime(OversizedDiagnosticTurnRuntime):
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        receipt = super().execute(None, task_id="", operation_id="").diagnostic_receipt
        assert receipt is not None
        return RunTurn(
            run_id="case-one-shot-accepted-diagnostic",
            state="completed",
            outcome=Outcome(
                status="completed",
                summary="diagnosis-only completed",
                acceptance=[{"requirement_id": "diagnosis", "status": "passed"}],
            ),
            outcome_recorded=True,
            diagnostic_receipt=receipt,
        )


class DeeplyNestedDiagnosticTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-deeply-nested-diagnostic",
            state="completed",
            facts=tuple(
                {"fact_id": index, "details": "x" * 512}
                for index in range(32)
            ),
            diagnostic_receipt=DiagnosticReceipt.from_public_dict({
                "receipt_id": "diagnostic-deeply-nested",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": 4,
                    "evaluable": 4,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": "version",
                        "kind": "target-version",
                        "request": "/etc/version.json",
                        "status": "available",
                        "value": {
                            "bytes_returned": 32,
                            "command": "cat /etc/version.json",
                            "content_complete": True,
                            "empty": False,
                            "empty_message": "",
                            "line_count": 1,
                            "lines": ['{"version":"12.08.21.06"}'],
                        },
                    },
                    {
                        "result_id": "logs",
                        "kind": "bounded-logs",
                        "request": "app.log",
                        "status": "available",
                        "value": {
                            "boot_time": None,
                            "entries": [
                                {
                                    "path": "app.log",
                                    "line_count": 31,
                                    "lines_preview": [
                                        "mctpd request timeout"
                                    ],
                                    "lines_truncated": True,
                                    "truncated": True,
                                    "content_complete": False,
                                }
                            ],
                            "redacted": False,
                            "since_boot_applied": False,
                            "utc_offset_minutes": 0,
                            "written_files": [],
                        },
                    },
                    {
                        "result_id": "service",
                        "kind": "service-tree",
                        "request": "bmc.kepler.mctpd",
                        "status": "available",
                        "value": {
                            "dbus_env": {"XDG_RUNTIME_DIR": "/run/user/502"},
                            "stderr": "",
                            "stderr_lines": [],
                            "stdout": "bmc.kepler.mctpd service visible",
                            "stdout_lines": [
                                "bmc.kepler.mctpd service visible"
                            ],
                            "structured": None,
                        },
                    },
                    {
                        "result_id": "empty-logs",
                        "kind": "bounded-logs",
                        "request": "storage,hwproxy,request timeout",
                        "status": "available",
                        "value": {
                            "boot_time": "2026-08-17 03:58:40",
                            "entries": [
                                {
                                    "path": "app.log",
                                    "line_count": 0,
                                    "lines_preview": [],
                                    "empty": True,
                                    "empty_message": "No matching log lines",
                                    "truncated": False,
                                    "content_complete": True,
                                }
                            ],
                            "since_boot_applied": True,
                        },
                    }
                ],
                "freshness": {
                    "status": "fresh",
                    "observed_at": "2026-08-25T04:42:55Z",
                },
                "capabilities": {},
                "truncated": False,
                "content_complete": True,
                "evidence": [],
                "gaps": [],
            }),
        )


class PersistedDiagnosticSummaryTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-persisted-diagnostic-summary",
            state="completed",
            facts=tuple(
                {"fact_id": index, "details": "x" * 512}
                for index in range(32)
            ),
            diagnostic_receipt=DiagnosticReceipt.from_public_dict({
                "receipt_id": "diagnostic-persisted-summary",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": 1,
                    "evaluable": 1,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": "logs",
                        "kind": "bounded-logs",
                        "request": "storage,hwproxy,request timeout",
                        "status": "available",
                        "value": {
                            "boot_time": "2026-08-17 03:58:40",
                            "redacted": False,
                            "since_boot_applied": True,
                            "utc_offset_minutes": 0,
                            "summary": [
                                {
                                    "path": "$.entries[0].lines[0]",
                                    "value": (
                                        "storage request timeout: remote service "
                                        "bmc.kepler.hwproxy method PluginRequestEx"
                                    ),
                                }
                            ],
                        },
                        "projection_truncated": True,
                    }
                ],
                "freshness": {
                    "status": "complete",
                    "observed_at": "2026-08-25T04:42:55Z",
                    "complete": True,
                },
                "capabilities": {},
                "truncated": False,
                "content_complete": True,
                "evidence": [],
                "gaps": ["diagnostic_receipt_compacted"],
                "content_compacted": True,
            }),
        )


class NonEvaluableDiagnosticTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        return RunTurn(
            run_id="case-non-evaluable-diagnostic",
            state="completed",
            facts=tuple(
                {"fact_id": index, "details": "x" * 512}
                for index in range(32)
            ),
            diagnostic_receipt=DiagnosticReceipt.from_public_dict({
                "receipt_id": "diagnostic-non-evaluable",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": 1,
                    "evaluable": 1,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": "version",
                        "kind": "target-version",
                        "request": "/etc/version.json",
                        "status": "available",
                        "value": {
                            "content_complete": True,
                            "projection_truncated": True,
                        },
                    }
                ],
                "freshness": {
                    "status": "complete",
                    "observed_at": "2026-08-25T04:42:55Z",
                    "complete": True,
                },
                "capabilities": {},
                "truncated": False,
                "content_complete": True,
                "evidence": [],
                "gaps": [],
            }),
        )


class UncompactableDiagnosticTurnRuntime:
    def execute(self, command, *, task_id, operation_id):
        del command, task_id, operation_id
        value: object = "substantive diagnostic evidence" + "x" * 20_000
        for index in range(10):
            value = {f"level-{index}": value}
        return RunTurn(
            run_id="case-uncompactable-diagnostic",
            state="completed",
            facts=tuple(
                {"fact_id": index, "details": "x" * 512}
                for index in range(32)
            ),
            diagnostic_receipt=DiagnosticReceipt.from_public_dict({
                "receipt_id": "diagnostic-uncompactable",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": 1,
                    "evaluable": 1,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": "nested-evidence",
                        "kind": "diagnosis",
                        "request": "nested diagnostic evidence",
                        "status": "available",
                        "value": value,
                    }
                ],
                "freshness": {
                    "status": "complete",
                    "observed_at": "2026-08-25T04:42:55Z",
                    "complete": True,
                },
                "capabilities": {},
                "truncated": False,
                "content_complete": True,
                "evidence": [],
                "gaps": [],
            }),
        )


class OversizedTerminalTurnRuntime(OversizedDiagnosticTurnRuntime):
    acceptance = [
        {
            "requirement_id": f"requirement-{index}",
            "status": "passed",
            "details": "x" * 2_000,
        }
        for index in range(8)
    ]

    def execute(self, command, *, task_id, operation_id):
        turn = super().execute(
            command,
            task_id=task_id,
            operation_id=operation_id,
        )
        return RunTurn(
            run_id=turn.run_id,
            state=turn.state,
            outcome=Outcome(
                status="completed",
                summary="terminal outcome remains Runtime-owned",
                acceptance=self.acceptance,
            ),
            next_action="retain the Runtime-owned terminal instruction exactly",
            diagnostic_receipt=turn.diagnostic_receipt,
        )


class RequiredIdentityPressureTurnRuntime(OversizedTerminalTurnRuntime):
    def execute(self, command, *, task_id, operation_id):
        turn = super().execute(
            command,
            task_id=task_id,
            operation_id=operation_id,
        )
        return RunTurn(
            run_id=turn.run_id,
            state=turn.state,
            gaps=tuple(
                f"required-gap-{index}-" + "g" * 2_000
                for index in range(16)
            ),
            outcome=Outcome(
                status="completed",
                summary="terminal outcome remains Runtime-owned" + "s" * 20_000,
                acceptance=[
                    {
                        "requirement_id": f"requirement-{index}-" + "a" * 2_000,
                        "status": "passed",
                    }
                    for index in range(8)
                ],
            ),
            next_action=(
                "retain the Runtime-owned terminal instruction exactly "
                + "n" * 20_000
            ),
            diagnostic_receipt=turn.diagnostic_receipt,
        )


class PersistentUnknownRunDriver:
    def __init__(self, repository=None, *, effect_id: str = "mutation-unknown-1") -> None:
        self.repository = repository
        self.effect_id = effect_id

    def _snapshot(self) -> dict[str, object]:
        persisted = (
            self.repository.load("run-persistent-unknown")
            if self.repository is not None
            else None
        )
        incident = (
            persisted.get("current_incident", {})
            if isinstance(persisted, dict)
            else {}
        )
        projection: dict[str, object] = {
            "case_id": "run-persistent-unknown",
            "status": "incident" if incident else "open",
            "operations": [
                {
                    "operation": "live_patch_run",
                    "operation_id": self.effect_id,
                    "status": "mutation_outcome_unknown",
                }
            ],
            "workflow_step_states": {},
            "current_incident": incident,
        }
        return {"projection": projection, "continuation": {}}

    def run_snapshot(self, _run_id: str) -> dict[str, object]:
        return self._snapshot()

    @staticmethod
    def domain_metadata(operation: str) -> dict[str, object]:
        return {"mutation": operation == "live_patch_run"}

    @staticmethod
    def domain_artifact_metadata(_phase_type: str) -> dict[str, object]:
        return {}

    @staticmethod
    def derive_closeout(_run_id: str, *, terminal_status: str):
        del terminal_status
        return {
            "closeout": {},
            "closeout_markdown": "",
            "closeout_bundle": None,
        }


class AgentGatewayTests(unittest.TestCase):
    def test_incident_always_requires_operator_attention(self) -> None:
        telemetry = interaction_telemetry(
            {"state": "incident", "incident": {"code": "incomplete"}}
        )

        assert telemetry is not None
        self.assertEqual(telemetry["classification"], "incident")
        self.assertTrue(telemetry["incident_present"])
        self.assertTrue(telemetry["operator_attention_required"])

    def test_unscoped_legacy_diagnostic_receipt_is_cycle_one_only(self) -> None:
        legacy = {
            "operation": "debug_run",
            "diagnostic_receipt": diagnostic_receipt_fixture("legacy-cycle-1"),
        }

        cycle_one = latest_diagnostic_receipt(
            {
                "workflow_cycle_id": "cycle-1",
                "operations": [legacy],
                "phase_records": [],
            }
        )
        cycle_two = latest_diagnostic_receipt(
            {
                "workflow_cycle_id": "cycle-2",
                "operations": [legacy],
                "phase_records": [],
            }
        )

        self.assertIsNotNone(cycle_one)
        self.assertEqual(cycle_one.receipt_id, "legacy-cycle-1")
        self.assertIsNone(cycle_two)

    def setUp(self) -> None:
        self.artifact_directory = tempfile.TemporaryDirectory()
        self.artifact_root = Path(self.artifact_directory.name)
        self.backend = SemanticBackend()
        self.service = RuntimeMcpService(self.backend)

    def tearDown(self) -> None:
        try:
            self.service.close()
        finally:
            self.artifact_directory.cleanup()

    def assert_bounded_running_turn(
        self,
        call: Callable[[], dict[str, object]],
        *,
        started: threading.Event,
        maximum_elapsed: float = 0.5,
    ) -> dict[str, object]:
        started_at = time.monotonic()
        turn = call()
        elapsed = time.monotonic() - started_at
        self.assertTrue(started.wait(timeout=maximum_elapsed))
        self.assertLess(elapsed, maximum_elapsed)
        self.assertEqual(turn["state"], "running")
        return turn

    def open_tampered_live_patch_incident(
        self,
        *,
        scenario: str,
        target: str,
        restart_scope: str,
    ) -> tuple[SemanticBackend, RuntimeMcpService, dict[str, object], Path, bytes]:
        backend = SemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / f"{scenario}-fix.lua"
        original_body = b"return 'validated-content'\n"
        patch_file.write_bytes(original_body)
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": target,
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id=scenario,
                operation_id=f"{scenario}-start",
            )
            transactions = service._test.context_runtime.repository
            original_stage = transactions.stage

            def replace_after_persist(*args, **kwargs):
                result = original_stage(*args, **kwargs)
                patch_file.write_bytes(b"return 'tampered-after-gate'\n")
                return result

            with patch.object(
                transactions,
                "stage",
                side_effect=replace_after_persist,
            ):
                blocked = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": f"{scenario}-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target=target,
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": restart_scope,
                            },
                        },
                    },
                    task_id=scenario,
                    operation_id=f"{scenario}-response",
                )
        except Exception:
            service.close()
            raise
        return backend, service, blocked, patch_file, original_body

    def test_agent_gateway_owns_bounded_run_fact_projection(self) -> None:
        projection = {
            "workflow_cycle_id": "cycle-1",
            "operations": [
                {
                    "operation": f"domain_{index}",
                    "status": "completed",
                    "summary": f"completed {index}",
                    "workflow_cycle_id": "cycle-1",
                }
                for index in range(10)
            ],
            "phase_records": [],
        }
        facts = self.service._test.projector.run_facts(projection)
        turn = RunTurn(
            run_id="run-fact-projection",
            state="running",
            facts=facts,
        )
        projected = self.service._test.projector.turn(turn)

        self.assertEqual(len(projected["facts"]), 8)
        self.assertEqual(projected["facts"][0]["name"], "domain_2")
        self.assertEqual(projected["facts"][-1]["name"], "domain_9")

    def test_default_interface_has_two_small_semantic_tools(self) -> None:
        definitions = self.service.tool_definitions()
        self.assertEqual([item["name"] for item in definitions], ["observe", "execute"])
        self.assertLessEqual(encoded_size(definitions), TOOLS_LIST_MAX_BYTES)
        self.assertEqual(self.service.interface_catalog.names(), ("observe", "execute"))
        execute_schema = definitions[1]["inputSchema"]
        observe_selector = definitions[0]["inputSchema"]["properties"]["selectors"][
            "items"
        ]
        capability_item = observe_selector["properties"]["names"]["items"]
        self.assertNotIn("enum", capability_item)
        self.assertIn(
            "mdbctl",
            capability_item["description"],
        )
        self.assertIn("response_required", definitions[1]["description"])
        self.assertIn("sole structured reusable action", definitions[1]["description"])
        action_shapes = {
            branch["properties"]["kind"]["const"]: branch
            for branch in execute_schema["oneOf"]
        }
        self.assertEqual(
            set(action_shapes),
            {"start", "respond", "resume", "control"},
        )
        self.assertIn("intent", action_shapes["start"]["required"])
        self.assertIn(
            {"required": ["target"]},
            action_shapes["start"]["anyOf"],
        )
        self.assertIn(
            {"required": ["targets"]},
            action_shapes["start"]["anyOf"],
        )
        self.assertEqual(
            set(action_shapes["respond"]["required"]),
            {
                "kind",
                "run_id",
                "gate_id",
                "gate_version",
                "schema_digest",
                "response",
            },
        )
        self.assertEqual(
            set(action_shapes["resume"]["properties"]),
            {"kind", "run_id", "deadline"},
        )
        self.assertIn("oneOf", action_shapes["control"])
        self.assertNotIn("max_steps", json.dumps(execute_schema))

    def test_execute_rejects_invalid_action_shapes_before_dispatch(self) -> None:
        runtime = RejectDispatchRuntime()
        gateway = AgentGateway(runtime)
        invalid_actions = (
            ({"kind": "start", "target": "192.0.2.10"}, "intent"),
            (
                {
                    "kind": "start",
                    "target": 1920210,
                    "intent": "diagnosis-only",
                },
                "target must be a string",
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": 42,
                },
                "intent must be a string",
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "",
                },
                "non-empty intent",
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "observation_ref": "observation://not-an-object",
                },
                "observation_ref must be an object",
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "run_id": "run-from-another-action",
                },
                "another Action kind",
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "target_epoch": 9,
                },
                "Runtime-owned",
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "disk_id": "Disk23",
                        "recovery_mode": "reconcile",
                    },
                },
                "Runtime-owned",
            ),
            (
                {
                    "kind": "respond",
                    "run_id": "run-one",
                    "gate_id": 17,
                    "gate_version": 1,
                    "schema_digest": "a" * 64,
                    "response": {
                        "status": "completed",
                        "summary": "done",
                        "payload": {},
                    },
                },
                "gate_id must be a string",
            ),
            (
                {
                    "kind": "respond",
                    "run_id": "run-one",
                    "gate_version": 1,
                    "schema_digest": "a" * 64,
                    "response": {},
                },
                "gate_id",
            ),
            (
                {
                    "kind": "respond",
                    "run_id": "run-one",
                    "gate_id": "gate-one",
                    "gate_version": 1,
                    "schema_digest": "a" * 64,
                    "response": {
                        "status": "completed",
                        "summary": "done",
                    },
                },
                "response requires fields: payload",
            ),
            (
                {
                    "kind": "resume",
                    "run_id": 123,
                },
                "run_id must be a string",
            ),
            (
                {
                    "kind": "resume",
                    "run_id": "run-one",
                    "response": {},
                },
                "another Action kind",
            ),
            (
                {
                    "kind": "control",
                    "run_id": "run-one",
                    "command": "reconcile",
                    "incident_id": "incident-one",
                },
                "reconcile",
            ),
            (
                {"kind": "control", "run_id": "run-one", "command": "cancel"},
                "Gate binding or incident_id",
            ),
        )

        for action, message in invalid_actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(AgentGatewayError, message):
                    gateway.execute(
                        action,
                        task_id="invalid-action",
                        operation_id="invalid-action-command",
                    )

        self.assertEqual(runtime.execute_calls, 0)

    def test_execute_deadline_error_reports_the_accepted_range(self) -> None:
        for deadline in (
            0,
            120.1,
            "later",
            float("nan"),
            float("inf"),
            10**400,
        ):
            with self.subTest(deadline=deadline):
                with self.assertRaisesRegex(
                    AgentGatewayError,
                    "greater than 0 and at most 120 seconds",
                ):
                    decode_run_command(
                        {
                            "kind": "resume",
                            "run_id": "run-deadline",
                            "deadline": deadline,
                        },
                        operation_id="deadline-command",
                    )

    def test_execute_preflight_returns_canonical_examples_before_runtime_dispatch(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight",
        )
        cases = (
            (
                {"kind": "resume"},
                "run_id",
                {"required": True},
                {"kind": "resume", "run_id": "<current Run ID>"},
            ),
            (
                {
                    "kind": "resume",
                    "run_id": "run-one",
                    "recovery_mode": "reconcile",
                },
                "recovery_mode",
                {"ownership": "Runtime"},
                {
                    "kind": "resume",
                    "run_id": "run-one",
                },
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "disk_id": "Disk23",
                        "recovery_mode": "reconcile",
                    },
                },
                "entry_arguments.recovery_mode",
                {"ownership": "Runtime"},
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {"disk_id": "Disk23"},
                },
            ),
            (
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {"disk_id": "Disk23"},
                    "deadline": 121,
                },
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {"disk_id": "Disk23"},
                    "deadline": 120,
                },
            ),
            (
                {"kind": "resume", "run_id": "run-one", "deadline": 121},
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "resume",
                    "run_id": "run-one",
                    "deadline": 120,
                },
            ),
            (
                {
                    "kind": "start",
                    "targets": [
                        {"ip": "192.0.2.10", "role": "reference"},
                        {"ip": "192.0.2.11", "role": "candidate"},
                    ],
                    "intent": "diagnose-and-fix",
                    "purpose": "compare Disk23 behavior",
                    "delivery_strategy": "source-only",
                    "observation_ref": {"handle": "observation-one"},
                    "deadline": 121,
                },
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "start",
                    "targets": [
                        {"ip": "192.0.2.10", "role": "reference"},
                        {"ip": "192.0.2.11", "role": "candidate"},
                    ],
                    "intent": "diagnose-and-fix",
                    "purpose": "compare Disk23 behavior",
                    "delivery_strategy": "source-only",
                    "observation_ref": {"handle": "observation-one"},
                    "deadline": 120,
                },
            ),
            (
                {"kind": "resume", "run_id": "run-one", "deadline": "later"},
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "resume",
                    "run_id": "run-one",
                    "deadline": 120,
                },
            ),
            (
                {"kind": "resume", "run_id": "run-one", "deadline": float("nan")},
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "resume",
                    "run_id": "run-one",
                    "deadline": 120,
                },
            ),
            (
                {"kind": "resume", "run_id": "run-one", "deadline": 10**400},
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "resume",
                    "run_id": "run-one",
                    "deadline": 120,
                },
            ),
            (
                {
                    "kind": "respond",
                    "run_id": "bad run id",
                    "gate_id": "bad gate id",
                    "gate_version": 0,
                    "schema_digest": "not-a-digest",
                    "submission_id": "bad submission id",
                    "response": {
                        "status": "completed",
                        "summary": "source delivery completed",
                        "payload": {},
                    },
                    "deadline": 121,
                },
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "respond",
                    "run_id": "<current Run ID>",
                    "gate_id": "<current Gate ID>",
                    "gate_version": 1,
                    "schema_digest": "<current Gate schema digest>",
                    "response": {
                        "status": "completed",
                        "summary": "source delivery completed",
                        "payload": {},
                    },
                    "submission_id": "<new submission identity>",
                    "deadline": 120,
                },
            ),
            (
                {
                    "kind": "control",
                    "run_id": "run-one",
                    "command": "Cancel",
                    "incident_id": "incident-one",
                    "deadline": float("inf"),
                },
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "control",
                    "run_id": "run-one",
                    "command": "cancel",
                    "incident_id": "incident-one",
                    "deadline": 120,
                },
            ),
            (
                {
                    "kind": "control",
                    "run_id": "run-one",
                    "command": "cancel",
                    "gate_id": "gate-one",
                    "gate_version": 3,
                    "schema_digest": "sha256:" + "a" * 64,
                    "deadline": float("nan"),
                },
                "deadline",
                {"exclusive_minimum": 0, "maximum_seconds": 120},
                {
                    "kind": "control",
                    "run_id": "run-one",
                    "command": "cancel",
                    "gate_id": "gate-one",
                    "gate_version": 3,
                    "schema_digest": "sha256:" + "a" * 64,
                    "deadline": 120,
                },
            ),
        )

        for request_id, (arguments, field, limit, example) in enumerate(
            cases,
            start=100,
        ):
            with self.subTest(field=field):
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "execute", "arguments": arguments},
                    }
                )
                structured = response["result"]["structuredContent"]
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(structured["error"]["field"], field)
                self.assertEqual(structured["error"]["limit"], limit)
                self.assertEqual(structured["error"]["example"], example)
                encoded_example = json.dumps(example)
                has_placeholder = any(
                    marker in encoded_example
                    for marker in (
                        "<current ",
                        "<new submission identity>",
                    )
                )
                try:
                    if has_placeholder:
                        raise AgentGatewayError("example requires external input")
                    decode_run_command(
                        example,
                        operation_id=f"execute-preflight-example-{request_id}",
                    )
                except AgentGatewayError:
                    expected_action = None
                else:
                    expected_action = example
                self.assertEqual(
                    structured["next_action"],
                    expected_action,
                )
                self.assertTrue(structured["next_guidance"])
                self.assertNotIn("structuredContent", json.dumps(structured))

    def test_execute_preflight_preserves_large_response_in_canonical_example(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-large-response",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 110,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": "run-one",
                        "gate_id": "gate-one",
                        "gate_version": 1,
                        "schema_digest": "sha256:" + "a" * 64,
                        "response": {
                            "status": "completed",
                            "summary": "source delivery completed",
                            "payload": {"build_log": "x" * (128 * 1024)},
                        },
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        example_response = structured["error"]["example"]["response"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(example_response["status"], "completed")
        self.assertEqual(example_response["summary"], "source delivery completed")
        self.assertEqual(
            example_response["payload"]["build_log"],
            "x" * (128 * 1024),
        )
        self.assertFalse(structured["projection_compacted"])
        self.assertTrue(structured["projection_target_exceeded"])
        self.assertFalse(structured["manual_narrowing_required"])
        self.assertFalse(structured["budget_blocker"])

    def test_execute_preflight_makes_malformed_gate_bindings_actionable(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-malformed-gate-binding",
        )
        original_response = {
            "status": "completed",
            "summary": "source delivery completed",
            "payload": {
                "source_revision": "revision-one",
                "authored_files": ["src/fix.lua"],
                "verification_plan": ["run focused tests"],
            },
        }
        cases = (
            ("gate_id", "bad gate id", "<current Gate ID>"),
            ("gate_version", 0, 1),
            (
                "schema_digest",
                "not-a-digest",
                "<current Gate schema digest>",
            ),
            (
                "submission_id",
                "bad submission id",
                "<new submission identity>",
            ),
        )

        for request_id, (field, invalid, corrected) in enumerate(cases, start=116):
            with self.subTest(field=field):
                arguments = {
                    "kind": "respond",
                    "run_id": "run-one",
                    "gate_id": "gate-one",
                    "gate_version": 1,
                    "schema_digest": "sha256:" + "a" * 64,
                    "submission_id": "submission-one",
                    "response": original_response,
                }
                arguments[field] = invalid
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "execute", "arguments": arguments},
                    }
                )

                structured = response["result"]["structuredContent"]
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(structured["error"]["field"], field)
                self.assertEqual(
                    structured["error"]["limit"],
                    {"binding": "current Gate"},
                )
                example = structured["error"]["example"]
                self.assertEqual(example[field], corrected)
                self.assertEqual(example["response"], original_response)
                for preserved in {
                    "run_id",
                    "gate_id",
                    "gate_version",
                    "schema_digest",
                    "submission_id",
                } - {field}:
                    self.assertEqual(example[preserved], arguments[preserved])
                self.assertEqual(
                    structured["next_action"],
                    None,
                )
                self.assertEqual(
                    structured["next_guidance"],
                    "retry execute with the projected GateBinding, response, and submission identity",
                )

    def test_execute_preflight_corrects_stale_gate_binding_only(
        self,
    ) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.88",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="stale-gate-binding",
            operation_id="stale-gate-binding-start",
        )
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="stale-gate-binding",
        )
        current = gate_binding(waiting)
        original_response = {
            "status": "completed",
            "summary": "source delivery completed",
            "payload": {
                "source_revision": "revision-one",
                "authored_files": ["src/fix.lua"],
                "verification_plan": ["run focused tests"],
            },
        }
        stale_bindings = (
            ("gate_id", "gate-stale"),
            ("gate_version", current["gate_version"] + 1),
            ("schema_digest", "sha256:" + "f" * 64),
        )
        before = self.service._test.context_runtime.read_case(waiting["run_id"])

        for request_id, (field, stale_value) in enumerate(
            stale_bindings,
            start=120,
        ):
            with self.subTest(field=field):
                arguments = {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **current,
                    "submission_id": f"stale-{field}-submission",
                    "response": original_response,
                }
                arguments[field] = stale_value
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "execute", "arguments": arguments},
                    }
                )

                structured = response["result"]["structuredContent"]
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(structured["error"]["field"], field)
                example = structured["error"]["example"]
                self.assertEqual(example[field], current[field])
                self.assertEqual(example["response"], original_response)
                self.assertEqual(
                    example["submission_id"],
                    arguments["submission_id"],
                )
                self.assertEqual(
                    structured["next_action"],
                    example,
                )
                self.assertEqual(
                    structured["next_guidance"],
                    "retry execute with the projected GateBinding, response, and submission identity",
                )

        after = self.service._test.context_runtime.read_case(waiting["run_id"])
        self.assertEqual(after["revision"], before["revision"])
        continued = self.service.call_exposed_tool(
            "execute",
            structured["next_action"],
            task_id="stale-gate-binding-corrected",
            operation_id="stale-gate-binding-corrected",
        )
        self.assertEqual(continued["state"], "completed")
        self.assertIsNone(continued["next_action"])

    def test_execute_preflight_preserves_wide_canonical_example_semantics(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-wide-response",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 111,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": "run-one",
                        "gate_id": "gate-one",
                        "gate_version": 1,
                        "schema_digest": "sha256:" + "a" * 64,
                        "submission_id": "submission-one",
                        "response": {
                            "status": "completed",
                            "summary": "source delivery completed",
                            "payload": {
                                ("log_" + str(index) + "_" + "k" * 110): "x" * 1000
                                for index in range(16)
                            },
                        },
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        example = structured["error"]["example"]
        self.assertGreater(
            len(json.dumps(structured, ensure_ascii=False).encode("utf-8")),
            TURN_MAX_BYTES,
        )
        self.assertEqual(example["run_id"], "run-one")
        self.assertEqual(example["gate_id"], "gate-one")
        self.assertEqual(example["schema_digest"], "sha256:" + "a" * 64)
        self.assertEqual(example["submission_id"], "submission-one")
        self.assertEqual(example["deadline"], 120)
        self.assertEqual(
            example["response"]["payload"]["log_0_" + "k" * 110],
            "x" * 1000,
        )
        self.assertFalse(structured["projection_compacted"])
        self.assertTrue(structured["projection_target_exceeded"])

    def test_execute_preflight_preserves_large_valid_start_fields(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-large-start",
        )
        target = "t" * 300

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 112,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": target,
                        "intent": "x" * (128 * 1024),
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        example = structured["error"]["example"]
        self.assertGreater(
            len(json.dumps(structured, ensure_ascii=False).encode("utf-8")),
            TURN_MAX_BYTES,
        )
        self.assertEqual(example["target"], target)
        self.assertEqual(example["intent"], "x" * (128 * 1024))
        self.assertEqual(example["deadline"], 120)
        self.assertFalse(structured["projection_compacted"])
        self.assertTrue(structured["projection_target_exceeded"])

    def test_execute_preflight_replaces_invalid_oversized_binding_in_example(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-large-binding",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 113,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "resume",
                        "run_id": "x" * (128 * 1024),
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        example = structured["error"]["example"]
        self.assertLessEqual(
            len(json.dumps(structured, ensure_ascii=False).encode("utf-8")),
            TURN_MAX_BYTES,
        )
        self.assertEqual(example["run_id"], "<current Run ID>")
        self.assertEqual(example["deadline"], 120)
        self.assertNotIn("projection_compacted", structured)

    def test_execute_preflight_preserves_artifact_and_payload_identity(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-artifact-identity",
        )
        artifact_ref = {
            "handle": "/tmp/" + "h" * 5000,
            "digest": "sha256:" + "b" * 64,
            "kind": "openubmc-hpm",
            "size": 1024,
            "provenance": "openubmc-build",
            "retention_hint": "run-lifetime",
            "version": "1.2.3",
            "target": "t" * 300,
            "run_id": "run-one",
        }

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 114,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": "run-one",
                        "gate_id": "gate-one",
                        "gate_version": 1,
                        "schema_digest": "sha256:" + "a" * 64,
                        "response": {
                            "status": "completed",
                            "summary": "artifact produced",
                            "payload": {
                                "artifact_ref": artifact_ref,
                                **{
                                    f"log_{index}": "x" * 1000
                                    for index in range(15)
                                },
                                "source_revision": "s" * 5004,
                            },
                        },
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        projected_ref = structured["error"]["example"]["response"]["payload"][
            "artifact_ref"
        ]
        self.assertEqual(projected_ref["handle"], artifact_ref["handle"])
        self.assertEqual(projected_ref["digest"], artifact_ref["digest"])
        self.assertEqual(projected_ref["target"], artifact_ref["target"])
        self.assertEqual(projected_ref["run_id"], artifact_ref["run_id"])
        self.assertEqual(
            structured["error"]["example"]["response"]["payload"][
                "source_revision"
            ],
            "s" * 5004,
        )
        self.assertFalse(structured["projection_compacted"])
        self.assertTrue(structured["projection_target_exceeded"])
        self.assertFalse(structured["budget_blocker"])

    def test_execute_preflight_reports_final_projection_target_exceeded(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-preflight-final-size",
        )

        def projected(handle_size: int) -> dict[str, object]:
            response = endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 115,
                    "method": "tools/call",
                    "params": {
                        "name": "execute",
                        "arguments": {
                            "kind": "respond",
                            "run_id": "run-one",
                            "gate_id": "gate-one",
                            "gate_version": 1,
                            "schema_digest": "sha256:" + "a" * 64,
                            "response": {
                                "status": "completed",
                                "summary": "artifact produced",
                                "payload": {
                                    "artifact_ref": {
                                        "schema": (
                                            "openubmc.target-runtime.v1/"
                                            "semantic-runtime-v1/artifact-ref"
                                        ),
                                        "handle": "/tmp/" + "h" * handle_size,
                                        "digest": "sha256:" + "b" * 64,
                                        "kind": "openubmc-hpm",
                                        "size": 1024,
                                        "provenance": "openubmc-build",
                                        "retention_hint": "run-lifetime",
                                        "version": "1.2.3",
                                        "target": "192.0.2.10",
                                        "run_id": "run-one",
                                    }
                                },
                            },
                            "deadline": 121,
                        },
                    },
                }
            )
            return response["result"]["structuredContent"]

        def projected_bytes(value: object) -> int:
            return len(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

        low = 1
        high = TURN_MAX_BYTES * 2
        while low < high:
            midpoint = (low + high) // 2
            size = projected_bytes(projected(midpoint))
            if size > TURN_MAX_BYTES:
                high = midpoint
            else:
                low = midpoint + 1

        structured = projected(low)
        self.assertGreater(projected_bytes(structured), TURN_MAX_BYTES)
        self.assertTrue(structured["projection_target_exceeded"])
        self.assertEqual(
            structured["projection_target_overage_bytes"],
            projected_bytes(structured) - TURN_MAX_BYTES,
        )
        self.assertFalse(structured["budget_blocker"])

        self.assertEqual(self.backend.calls, [])

    def test_observe_rejects_selector_shape_and_scalar_types_before_target_access(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="selector-shape-preflight",
        )
        cases = (
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "mixed",
                            "kind": "capability",
                            "names": ["ssh"],
                            "queries": ["lsprop Object0"],
                        }
                    ],
                },
                "selectors[0].queries",
            ),
            (
                {
                    "target": 1234,
                    "selectors": [
                        {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                },
                "target",
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": 1234, "kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                },
                "selectors[0].id",
            ),
        )

        for request_id, (arguments, field) in enumerate(cases, start=150):
            with self.subTest(field=field):
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "observe", "arguments": arguments},
                    }
                )
                structured = response["result"]["structuredContent"]
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(structured["error"]["field"], field)
                self.assertTrue(structured["error"]["example"])
                if field == "selectors[0].queries":
                    self.assertEqual(
                        structured["next_action"],
                        structured["error"]["example"],
                    )
                else:
                    self.assertIsNone(structured["next_action"])
                self.assertTrue(structured["next_guidance"])

        self.assertEqual(self.backend.calls, [])

    def test_observe_freshness_and_deadline_failures_are_actionable(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="observe-value-preflight",
        )
        cases = (
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "freshness": {"mode": "live", "max_age_seconds": 1},
                },
                "freshness.max_age_seconds",
                {"allowed": [0]},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "freshness": {"mode": "", "max_age_seconds": 0},
                },
                "freshness.mode",
                {"allowed": ["live"]},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "deadline": "later",
                },
                "deadline",
                {"type": "positive number"},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "deadline": 0,
                },
                "deadline",
                {"exclusive_minimum": 0},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "deadline": float("nan"),
                },
                "deadline",
                {"type": "positive finite number"},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "deadline": 10**400,
                },
                "deadline",
                {"type": "positive finite number"},
            ),
        )

        for request_id, (arguments, field, limit) in enumerate(cases, start=160):
            with self.subTest(field=field, limit=limit):
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "observe", "arguments": arguments},
                    }
                )
                structured = response["result"]["structuredContent"]
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(structured["error"]["field"], field)
                self.assertEqual(structured["error"]["limit"], limit)
                self.assertTrue(structured["error"]["example"])
                self.assertEqual(
                    structured["next_action"],
                    structured["error"]["example"],
                )
                self.assertTrue(structured["next_guidance"])

        self.assertEqual(self.backend.calls, [])

    def test_reconcile_without_unknown_mutation_is_actionable_and_side_effect_free(
        self,
    ) -> None:
        terminal = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.10",
                "intent": "diagnosis-only",
            },
            task_id="reconcile-preflight",
            operation_id="reconcile-preflight-start",
        )
        before = self.service._test.context_runtime.read_case(terminal["run_id"])
        call_count = len(self.backend.calls)
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="reconcile-preflight",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 200,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "control",
                        "run_id": terminal["run_id"],
                        "command": "reconcile",
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(structured["error"]["field"], "command")
        self.assertEqual(
            structured["error"]["limit"],
            {"precondition": "same Run has an unknown mutation outcome"},
        )
        self.assertEqual(structured["error"]["example"], {})
        self.assertIsNone(structured["next_action"])
        self.assertTrue(structured["next_guidance"])
        self.assertEqual(len(self.backend.calls), call_count)
        self.assertEqual(
            self.service._test.context_runtime.read_case(terminal["run_id"])[
                "revision"
            ],
            before["revision"],
        )

    def test_observe_preflight_returns_one_complete_valid_retry(self) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="observe-complete-retry",
        )
        long_query = "lsprop Object" + "x" * 180
        selectors = [
            *[
                {
                    "id": f"capabilities-{index}",
                    "kind": "capability",
                    "names": ["mdb"] * 16,
                }
                for index in range(3)
            ],
            *[
                {
                    "id": f"mdb-{index}",
                    "kind": "mdb",
                    "queries": [long_query if index == 0 else f"lsprop Object{index}"],
                }
                for index in range(13)
            ],
        ]

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 201,
                "method": "tools/call",
                "params": {
                    "name": "observe",
                    "arguments": {
                        "target": "192.0.2.10",
                        "selectors": selectors,
                        "freshness": {"mode": "live", "max_age_seconds": 0},
                        "deadline": 0,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        action = structured["next_action"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(structured["error"]["field"], "selectors[0].names[0]")
        self.assertIsInstance(action, dict)
        self.assertEqual(len(action["selectors"]), 16)
        for selector in action["selectors"][:3]:
            self.assertEqual(selector["names"], ["mdbctl"] * 16)
        self.assertEqual(action["selectors"][3]["queries"], [long_query])
        self.assertEqual(action["deadline"], 180)
        self.assertEqual(action, structured["error"]["example"])
        ObservationQuery.from_query(action)
        self.assertEqual(self.backend.calls, [])

    def test_preflight_placeholder_detection_preserves_literal_angle_brackets(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="literal-angle-brackets",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 202,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.10",
                        "intent": "diagnosis-only",
                        "purpose": "<BMC IP>",
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(structured["error"]["field"], "deadline")
        self.assertEqual(
            structured["next_action"]["purpose"],
            "<BMC IP>",
        )
        self.assertEqual(structured["next_action"]["deadline"], 120)
        decode_run_command(
            structured["next_action"],
            operation_id="literal-angle-brackets-retry",
        )

    def test_execute_preflight_does_not_promote_a_still_invalid_retry(self) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="execute-complete-retry",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 203,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.10",
                        "intent": "diagnosis-only",
                        "delivery_strategy": "unsupported-delivery",
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(structured["error"]["field"], "deadline")
        self.assertEqual(structured["error"]["example"]["deadline"], 120)
        self.assertEqual(
            structured["error"]["example"]["delivery_strategy"],
            "unsupported-delivery",
        )
        self.assertIsNone(structured["next_action"])

    def test_retry_validation_projects_reference_errors_instead_of_raising(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="invalid-reference-retry",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 204,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.10",
                        "intent": "diagnosis-only",
                        "observation_ref": {"handle": "missing-bindings"},
                        "deadline": 121,
                    },
                },
            }
        )

        structured = response["result"]["structuredContent"]
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(structured["error"]["field"], "deadline")
        self.assertEqual(structured["error"]["example"]["deadline"], 120)
        self.assertIsNone(structured["next_action"])

    def test_build_gate_preflights_artifact_ref_and_run_binding_before_effects(
        self,
    ) -> None:
        developer = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.10",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id="artifact-preflight",
            operation_id="artifact-preflight-start",
        )
        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer["run_id"],
                **gate_binding(developer),
                "response": {
                    "status": "completed",
                    "summary": "source delivered",
                    "payload": {
                        "source_revision": "artifact-preflight-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build official product"],
                    },
                },
            },
            task_id="artifact-preflight",
            operation_id="artifact-preflight-developer",
        )
        self.assertEqual(build_gate["gate"]["name"], "build.artifact")
        before = self.service._test.context_runtime.read_case(build_gate["run_id"])
        call_count = len(self.backend.calls)
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="artifact-preflight",
        )

        missing_ref = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 300,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": build_gate["run_id"],
                        **gate_binding(build_gate),
                        "response": {
                            "status": "completed",
                            "summary": "build completed",
                            "payload": {
                                "source_revision": "artifact-preflight-source"
                            },
                        },
                    },
                },
            }
        )
        missing_binding = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": build_gate["run_id"],
                        **gate_binding(build_gate),
                        "response": {
                            "status": "completed",
                            "summary": "build completed",
                            "payload": {
                                "source_revision": "artifact-preflight-source",
                                "artifact_ref": {
                                    "handle": "/tmp/product.hpm",
                                    "digest": "sha256:" + "a" * 64,
                                    "kind": "openubmc-hpm",
                                    "size": 1,
                                    "provenance": "openubmc-build",
                                    "retention_hint": "run-lifetime",
                                    "version": "1.0.0",
                                    "target": "192.0.2.10",
                                },
                            },
                        },
                    },
                },
            }
        )
        missing_target = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 302,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": build_gate["run_id"],
                        **gate_binding(build_gate),
                        "response": {
                            "status": "completed",
                            "summary": "build completed",
                            "payload": {
                                "source_revision": "artifact-preflight-source",
                                "artifact_ref": {
                                    "handle": "/tmp/product.hpm",
                                    "digest": "sha256:" + "a" * 64,
                                    "kind": "openubmc-hpm",
                                    "size": 1,
                                    "provenance": "openubmc-build",
                                    "retention_hint": "run-lifetime",
                                    "version": "1.0.0",
                                    "run_id": build_gate["run_id"],
                                },
                            },
                        },
                    },
                },
            }
        )

        def artifact_response(*, target: object, run_id: str, request_id: int):
            return endpoint.handle(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": "execute",
                        "arguments": {
                            "kind": "respond",
                            "run_id": build_gate["run_id"],
                            **gate_binding(build_gate),
                            "submission_id": f"artifact-binding-{request_id}",
                            "response": {
                                "status": "completed",
                                "summary": "build completed",
                                "payload": {
                                    "source_revision": "artifact-preflight-source",
                                    "component_versions": ["storage=1.2.3"],
                                    "build_commands": ["bmcgo build"],
                                    "build_logs": ["build.log"],
                                    "known_gaps": ["official repository unavailable"],
                                    "artifact_ref": {
                                        "handle": "/tmp/product.hpm",
                                        "digest": "sha256:" + "a" * 64,
                                        "kind": "openubmc-hpm",
                                        "size": 1,
                                        "provenance": "openubmc-build",
                                        "retention_hint": "run-lifetime",
                                        "version": "1.0.0",
                                        "target": target,
                                        "run_id": run_id,
                                    },
                                },
                            },
                        },
                    },
                }
            )

        wrong_target = artifact_response(
            target="192.0.2.99",
            run_id=build_gate["run_id"],
            request_id=303,
        )
        wrong_run = artifact_response(
            target="192.0.2.10",
            run_id="run-other",
            request_id=304,
        )
        invalid_target_type = artifact_response(
            target=1234,
            run_id=build_gate["run_id"],
            request_id=305,
        )

        for projected, request_id, corrected_field, corrected_value in (
            (wrong_target, 303, "target", "192.0.2.10"),
            (wrong_run, 304, "run_id", build_gate["run_id"]),
        ):
            example = projected["result"]["structuredContent"]["error"]["example"]
            self.assertEqual(
                example["submission_id"],
                f"artifact-binding-{request_id}",
            )
            self.assertEqual(example["response"]["summary"], "build completed")
            example_payload = example["response"]["payload"]
            self.assertEqual(
                example_payload["source_revision"],
                "artifact-preflight-source",
            )
            self.assertEqual(
                example_payload["component_versions"],
                ["storage=1.2.3"],
            )
            self.assertEqual(example_payload["build_commands"], ["bmcgo build"])
            self.assertEqual(example_payload["build_logs"], ["build.log"])
            self.assertEqual(
                example_payload["known_gaps"],
                ["official repository unavailable"],
            )
            projected_ref = example_payload["artifact_ref"]
            self.assertEqual(projected_ref["handle"], "/tmp/product.hpm")
            self.assertEqual(projected_ref["digest"], "sha256:" + "a" * 64)
            self.assertEqual(projected_ref["size"], 1)
            self.assertEqual(projected_ref["version"], "1.0.0")
            self.assertEqual(projected_ref[corrected_field], corrected_value)

        for response, field in (
            (missing_ref, "response.payload.artifact_ref"),
            (missing_binding, "response.payload.artifact_ref.run_id"),
            (missing_target, "response.payload.artifact_ref.target"),
            (wrong_target, "response.payload.artifact_ref.target"),
            (wrong_run, "response.payload.artifact_ref.run_id"),
            (invalid_target_type, "response.payload.artifact_ref.target"),
        ):
            structured = response["result"]["structuredContent"]
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(structured["error"]["field"], field)
            self.assertEqual(
                structured["error"]["limit"],
                {"binding": "current Run and target"},
            )
            example = structured["error"]["example"]
            self.assertEqual(example["run_id"], build_gate["run_id"])
            self.assertEqual(
                example["response"]["payload"]["artifact_ref"]["run_id"],
                build_gate["run_id"],
            )
            self.assertEqual(
                example["response"]["payload"]["artifact_ref"]["target"],
                "192.0.2.10",
            )
            if response is missing_ref:
                self.assertIsNone(structured["next_action"])
            else:
                self.assertEqual(structured["next_action"], example)
            self.assertTrue(structured["next_guidance"])

        after = self.service._test.context_runtime.read_case(build_gate["run_id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(len(self.backend.calls), call_count)

    def test_live_patch_artifact_preflight_example_covers_required_payload(
        self,
    ) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.73",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
            },
            task_id="live-patch-artifact-preflight",
            operation_id="live-patch-artifact-start",
        )
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="live-patch-artifact-preflight",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 306,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "submission_id": "live-patch-artifact-submission",
                        "response": {
                            "status": "completed",
                            "summary": "live patch source is ready",
                            "payload": {
                                "source_revision": "source-one",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["verify on target"],
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                },
            }
        )

        payload = response["result"]["structuredContent"]["error"]["example"][
            "response"
        ]["payload"]
        example = response["result"]["structuredContent"]["error"]["example"]
        self.assertIsNone(
            response["result"]["structuredContent"]["next_action"]
        )
        self.assertTrue(
            response["result"]["structuredContent"]["next_guidance"]
        )
        self.assertEqual(
            example["submission_id"],
            "live-patch-artifact-submission",
        )
        self.assertEqual(
            example["response"]["summary"],
            "live patch source is ready",
        )
        self.assertEqual(
            set(payload),
            {
                "source_revision",
                "authored_files",
                "verification_plan",
                "artifact_ref",
                "remote_path",
                "restart_scope",
            },
        )
        self.assertEqual(payload["source_revision"], "source-one")
        self.assertEqual(payload["authored_files"], ["src/fix.lua"])
        self.assertEqual(payload["verification_plan"], ["verify on target"])
        self.assertEqual(payload["remote_path"], "/opt/bmc/apps/fix.lua")
        self.assertEqual(payload["restart_scope"], "skynet")

    def test_public_preflight_error_identifies_field_and_canonical_retry(self) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="actionable-preflight",
        )

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "observe",
                    "arguments": {
                        "target": "192.0.2.10",
                        "selectors": [
                            {
                                "id": "capabilities",
                                "kind": "capability",
                                "names": ["mdb"],
                            }
                        ],
                    },
                },
            }
        )

        result = response["result"]
        self.assertTrue(result["isError"])
        error = result["structuredContent"]["error"]
        self.assertEqual(error["field"], "selectors[0].names[0]")
        self.assertIn("mdbctl", error["supported"])
        self.assertEqual(
            error["example"],
            {
                "target": "192.0.2.10",
                "selectors": [
                    {
                        "id": "capabilities",
                        "kind": "capability",
                        "names": ["mdbctl"],
                    }
                ],
            },
        )
        self.assertEqual(
            result["structuredContent"]["next_action"],
            error["example"],
        )
        self.assertEqual(
            result["structuredContent"]["next_guidance"],
            "retry observe with the corrected canonical capability name",
        )
        self.assertEqual(
            result["structuredContent"]["interaction_telemetry"],
            {
                "classification": "preflight_failure",
                "preflight_failure": True,
                "no_progress_retry": False,
                "incident_present": False,
                "operator_attention_required": False,
                "projection_target_exceeded": False,
                "budget_blocker": False,
            },
        )

    def test_mixed_case_canonical_capability_remains_compatible(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {
                        "id": "capabilities",
                        "kind": "capability",
                        "names": ["SSH", "MDBCTL"],
                    }
                ],
            },
            task_id="mixed-case-capability",
            operation_id="mixed-case-capability-observe",
        )

        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(
            receipt["scope"]["selectors"][0]["names"],
            ["ssh", "mdbctl"],
        )

    def test_observe_preflight_reports_selector_location_and_limit_before_target_access(
        self,
    ) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="selector-preflight",
        )
        cases = (
            (
                [{"id": "unsafe", "kind": "shell", "queries": ["id"]}],
                "selectors[0].kind",
                ["capability", "mdb"],
                None,
            ),
            (
                [{"id": "mdb", "kind": "mdb", "queries": ["x" * 1025]}],
                "selectors[0].queries[0]",
                None,
                {"unit": "UTF-8 bytes", "maximum": 1024},
            ),
            (
                [
                    {
                        "id": "mdb",
                        "kind": "mdb",
                        "queries": ["setprop Object0 Interface Value"],
                    }
                ],
                "selectors[0].queries[0]",
                None,
                {"grammar": "read-only mdbctl", "maximum_queries": 32},
            ),
        )

        for request_id, (selectors, field, supported, limit) in enumerate(
            cases,
            start=1,
        ):
            with self.subTest(field=field):
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {
                            "name": "observe",
                            "arguments": {
                                "target": "192.0.2.10",
                                "selectors": selectors,
                            },
                        },
                    }
                )
                structured = response["result"]["structuredContent"]
                self.assertTrue(response["result"]["isError"])
                self.assertEqual(structured["error"]["field"], field)
                if supported is not None:
                    self.assertEqual(structured["error"]["supported"], supported)
                if limit is not None:
                    self.assertEqual(structured["error"]["limit"], limit)
                self.assertTrue(structured["error"]["example"])
                self.assertIsNone(structured["next_action"])
                self.assertTrue(structured["next_guidance"])

        self.assertEqual(self.backend.calls, [])

    def test_observe_scope_preflight_covers_every_bounded_container(self) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="scope-container-preflight",
        )
        cases = (
            (
                {
                    "target": "x" * 513,
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                },
                "target",
                {"unit": "UTF-8 bytes", "maximum": 512},
            ),
            (
                {"target": "192.0.2.10", "selectors": ["not-an-object"]},
                "selectors[0]",
                {"type": "object"},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "caps", "kind": "capability", "names": ["ssh"], "shell": "id"}
                    ],
                },
                "selectors[0].shell",
                {"allowed_fields": ["id", "kind", "names", "queries"]},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "caps", "kind": "capability", "names": ["ssh"] * 17}
                    ],
                },
                "selectors[0].names",
                {"maximum_items": 16},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "mdb", "kind": "mdb", "queries": ["ls"] * 33}
                    ],
                },
                "selectors[0].queries",
                {"maximum_items": 32},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "same", "kind": "capability", "names": ["ssh"]},
                        {"id": "same", "kind": "mdb", "queries": ["lsprop Object0"]},
                    ],
                },
                "selectors[1].id",
                {"constraint": "unique within the observe request"},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": f"caps-{index}", "kind": "capability", "names": ["ssh"]}
                        for index in range(17)
                    ],
                },
                "selectors",
                {"maximum_items": 16},
            ),
            (
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "mdb", "queries": ["lsprop Object0"]}],
                    "freshness": {"mode": "cache", "max_age_seconds": 60},
                },
                "freshness.mode",
                {"allowed": ["live"]},
            ),
        )

        for request_id, (arguments, field, limit) in enumerate(cases, start=20):
            with self.subTest(field=field):
                response = endpoint.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "observe", "arguments": arguments},
                    }
                )
                structured = response["result"]["structuredContent"]
                self.assertEqual(structured["error"]["field"], field)
                self.assertEqual(structured["error"]["limit"], limit)
                self.assertTrue(structured["error"]["example"])

        self.assertEqual(self.backend.calls, [])

    def test_actionable_turns_expose_only_a_valid_suggested_action(self) -> None:
        gate_schema = {
            "type": "object",
            "required": ["status", "summary", "payload"],
            "properties": {
                "payload": {
                    "type": "object",
                    "required": ["artifact_ref"],
                    "properties": {
                        "artifact_ref": {
                            "type": "object",
                            "required": ["handle", "digest", "run_id"],
                        }
                    },
                }
            },
        }
        turns = {
            "gate": RunTurn(
                run_id="run-gate",
                state="waiting_response",
                gate={
                    "kind": "phase",
                    "gate_id": "gate-one",
                    "gate_version": 3,
                    "schema_digest": "sha256:" + "a" * 64,
                    "name": "build.artifact",
                    "owner": "openubmc-build",
                    "input_schema": gate_schema,
                },
            ),
            "running": RunTurn(run_id="run-running", state="running"),
            "resume_incident": RunTurn(
                run_id="run-artifact-incident",
                state="incident",
                incident=Incident(
                    incident_id="incident-artifact",
                    code="artifact_reference_invalid",
                    message="restore the ArtifactRef",
                ),
            ),
            "reconcile_incident": RunTurn(
                run_id="run-unknown-incident",
                state="incident",
                incident=Incident(
                    incident_id="incident-unknown",
                    code="mutation_outcome_unknown",
                    message="reconcile the durable Effect",
                ),
            ),
            "cancel_incident": RunTurn(
                run_id="run-invalid-continuation",
                state="incident",
                incident=Incident(
                    incident_id="incident-invalid",
                    code="invalid_run_continuation",
                    message="cancel the Run",
                ),
            ),
            "terminal": RunTurn(
                run_id="run-terminal",
                state="completed",
                outcome=Outcome(status="completed", summary="done"),
            ),
        }

        projected = {
            name: self.service._test.projector.turn(turn)
            for name, turn in turns.items()
        }

        self.assertIsNone(projected["gate"]["next_action"])
        self.assertEqual(
            projected["gate"]["gate"]["submission_id"],
            "gate-submit-4b436fa3cbfc58763c93fea6562340e0",
        )
        self.assertEqual(projected["gate"]["gate"]["input_schema"], gate_schema)
        self.assertEqual(
            projected["running"]["next_action"],
            {"kind": "resume", "run_id": "run-running"},
        )
        self.assertEqual(
            projected["resume_incident"]["next_action"],
            {"kind": "resume", "run_id": "run-artifact-incident"},
        )
        self.assertNotIn(
            "reconcile",
            json.dumps(projected["resume_incident"]["next_action"]),
        )
        self.assertEqual(
            projected["reconcile_incident"]["next_action"],
            {
                "kind": "control",
                "run_id": "run-unknown-incident",
                "command": "reconcile",
            },
        )
        self.assertEqual(
            projected["cancel_incident"]["next_action"],
            {
                "kind": "control",
                "run_id": "run-invalid-continuation",
                "command": "cancel",
                "incident_id": "incident-invalid",
            },
        )
        self.assertIsNone(projected["terminal"]["next_action"])

    def test_resume_that_advances_to_a_new_gate_is_not_reported_as_no_progress(
        self,
    ) -> None:
        assert_is_instance = self.assertIsInstance

        class AdvancingResumeRuntime:
            def execute(self, command, *, task_id, operation_id):
                del task_id, operation_id
                assert_is_instance(command, ResumeRun)
                return RunTurn(
                    run_id=command.run_id,
                    state="waiting_response",
                    gate={
                        "kind": "phase",
                        "gate_id": "new-gate",
                        "gate_version": 1,
                        "schema_digest": "sha256:" + "a" * 64,
                        "name": "developer.change",
                        "owner": "openubmc-developer",
                        "input_schema": {"type": "object"},
                    },
                )
        turn = AgentGateway(AdvancingResumeRuntime()).execute(
            {"kind": "resume", "run_id": "run-advancing"},
            task_id="advancing-resume",
            operation_id="advancing-resume-1",
        )

        self.assertTrue(turn["response_required"])
        self.assertNotIn("progress", turn)
        self.assertIsNone(turn["next_action"])
        self.assertEqual(turn["gate"]["gate_id"], "new-gate")
        self.assertTrue(turn["gate"]["submission_id"].startswith("gate-submit-"))

    def test_observe_is_bounded_grounded_and_does_not_open_a_case(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {"id": "caps", "kind": "capability", "names": ["ssh", "telnet", "busctl"]},
                    {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]},
                ],
                "freshness": {"mode": "live", "max_age_seconds": 0},
            },
            task_id="observe-task",
            operation_id="observe-1",
        )

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertIsNone(receipt["next_action"])
        states = {
            item["name"]: item["status"]
            for item in receipt["results"]["caps"]["values"]
        }
        self.assertEqual(
            states,
            {"ssh": "available", "telnet": "not_checked", "busctl": "unavailable"},
        )
        self.assertTrue(
            all(claim["receipt_id"] == receipt["receipt_id"] for claim in receipt["claims"])
        )
        self.assertIsNone(
            self.service._test.context_runtime.repository.case_for_task("observe-task")
        )

    def test_one_receipt_fits_four_capabilities_and_nine_exact_getprop_values(self) -> None:
        queries = [
            f"getprop Drive_1_010102 bmc.kepler.Interface Property{index}"
            for index in range(9)
        ]
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {
                        "id": "caps",
                        "kind": "capability",
                        "names": ["ssh", "telnet", "mdbctl", "busctl"],
                    },
                    {"id": "mdb", "kind": "mdb", "queries": queries},
                ],
            },
            task_id="observe-nine-properties",
            operation_id="observe-nine-properties-1",
        )

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertNotIn("content_compacted", receipt)
        self.assertEqual(receipt["coverage"]["requested"], 13)
        self.assertEqual(len(receipt["results"]["mdb"]["values"]), 9)
        self.assertEqual(len(receipt["claims"]), 2)

    def test_alarm_capability_is_not_checked_until_the_endpoint_is_verified(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {"id": "alarm-capability", "kind": "capability", "names": ["alarms"]}
                ],
            },
            task_id="observe-alarm-capability",
            operation_id="observe-alarm-capability-1",
        )

        self.assertEqual(
            receipt["results"]["alarm-capability"]["values"],
            [{"name": "alarms", "status": "not_checked"}],
        )
        self.assertEqual(receipt["status"], "incomplete")

    def test_observe_projection_target_preserves_complete_source_semantics(self) -> None:
        service = RuntimeMcpService(LargeObservationBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "large",
                            "kind": "mdb",
                            "queries": [f"lsprop Object{index}" for index in range(16)],
                        }
                    ],
                },
                task_id="observe-large",
                operation_id="observe-large-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["coverage"]["complete"])
        self.assertTrue(receipt["content_compacted"])
        self.assertTrue(receipt["projection_truncated"])
        self.assertFalse(receipt["manual_narrowing_required"])
        self.assertFalse(receipt["budget_blocker"])
        self.assertIn("observation_ref", receipt)
        self.assertNotIn("narrow the selectors", json.dumps(receipt))
        target_metrics = receipt["projection_metrics"]["soft_target"]
        self.assertEqual(target_metrics["target_bytes"], OBSERVATION_MAX_BYTES)
        self.assertEqual(
            target_metrics["target_exceeded_causes"][0]["field"], "results"
        )
        projected_value = receipt["results"]["large"]["values"][0]["value"]
        self.assertIn("Large", json.dumps(projected_value))
        self.assertNotIn("<compacted>", json.dumps(projected_value))

    def test_observe_projection_target_does_not_rewrite_oversized_target_metadata(self) -> None:
        service = RuntimeMcpService(OversizedObservationBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                "selectors": [
                    {"id": "caps", "kind": "capability", "names": ["ssh"]}
                ],
                },
                task_id="observe-oversized-target",
                operation_id="observe-oversized-target-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["coverage"]["complete"])
        self.assertFalse(receipt["content_compacted"])
        self.assertFalse(receipt["projection_truncated"])
        self.assertTrue(receipt["projection_target_exceeded"])
        self.assertIn("observation_ref", receipt)
        self.assertEqual(
            receipt["interaction_telemetry"],
            {
                "classification": "projection_target_exceeded",
                "preflight_failure": False,
                "no_progress_retry": False,
                "incident_present": False,
                "operator_attention_required": False,
                "projection_target_exceeded": True,
                "budget_blocker": False,
            },
        )

    def test_observe_soft_target_survives_maximum_legal_scope_and_large_result(self) -> None:
        service = RuntimeMcpService(LargeObservationBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "t" * 512,
                    "selectors": [
                        {
                            "id": "s" * 64,
                            "kind": "mdb",
                            "queries": ["lsprop " + "q" * 1017],
                        }
                    ],
                },
                task_id="observe-maximum-scope",
                operation_id="observe-maximum-scope-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["coverage"]["complete"])
        self.assertTrue(receipt["content_compacted"])
        self.assertTrue(receipt["projection_truncated"])
        self.assertFalse(receipt["manual_narrowing_required"])
        self.assertIn("observation_ref", receipt)

    def test_compacted_observation_preserves_selector_identity_and_order(self) -> None:
        service = RuntimeMcpService(OversizedObservationBackend())
        selector_ids = [f"{index:02d}" + "s" * 62 for index in range(16)]
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "t" * 64,
                    "selectors": [
                        {"id": selector_id, "kind": "capability", "names": ["ssh"]}
                        for selector_id in selector_ids
                    ],
                },
                task_id="observe-selector-identity-compaction",
                operation_id="observe-selector-identity-compaction-1",
            )
        finally:
            service.close()

        self.assertGreater(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertFalse(receipt["content_compacted"])
        self.assertTrue(receipt["projection_target_exceeded"])
        self.assertIn("observation_ref", receipt)
        self.assertEqual(
            [selector["id"] for selector in receipt["scope"]["selectors"]],
            selector_ids,
        )

    def test_scope_contract_fails_closed_for_undeclared_surface_or_freshness(self) -> None:
        with self.assertRaises(ScopeViolation):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "shell", "queries": ["id"]}],
                }
            )

        with self.assertRaisesRegex(ValueError, "target"):
            self.service.call_exposed_tool(
                "observe",
                {
                    "target": "x" * 513,
                    "selectors": [
                        {"kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                },
                task_id="oversized-scope",
                operation_id="oversized-scope-1",
            )

        with self.assertRaisesRegex(ScopeViolation, "undeclared fields"):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                    "mdb_concurrency": "unbounded",
                }
            )

    def test_scope_contract_rejects_duplicate_selector_ids(self) -> None:
        with self.assertRaisesRegex(ScopeViolation, "selector ids must be unique"):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "duplicate", "kind": "capability", "names": ["ssh"]},
                        {
                            "id": "duplicate",
                            "kind": "mdb",
                            "queries": ["lsprop Object0"],
                        },
                    ],
                }
            )

    def test_agent_request_shape_budgets_apply_before_schema_validation(self) -> None:
        nested: dict[str, object] = {}
        cursor = nested
        for _index in range(40):
            child: dict[str, object] = {}
            cursor["nested"] = child
            cursor = child
        with self.assertRaisesRegex(AgentGatewayError, "nesting"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "purpose": nested,
                },
                task_id="shape-budget",
                operation_id="shape-budget-depth",
            )

        with self.assertRaisesRegex(AgentGatewayError, "1024-field"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": "run-shape-budget",
                    "response": {
                        "status": "completed",
                        "summary": "wide payload",
                        "payload": {
                            f"field_{index}": index for index in range(1025)
                        },
                    },
                },
                task_id="shape-budget",
                operation_id="shape-budget-width",
            )

        with self.assertRaisesRegex(AgentGatewayError, "128 KiB"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "purpose": "x" * (128 * 1024 + 1),
                },
                task_id="shape-budget",
                operation_id="shape-budget-string",
            )

    def test_agent_request_budget_failure_has_explicit_budget_telemetry(self) -> None:
        endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="shape-budget-telemetry",
        )
        nested: dict[str, object] = {}
        cursor = nested
        for _index in range(40):
            child: dict[str, object] = {}
            cursor["nested"] = child
            cursor = child

        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.10",
                        "purpose": nested,
                    },
                },
            }
        )
        structured = response["result"]["structuredContent"]

        self.assertTrue(response["result"]["isError"])
        self.assertTrue(structured["budget_blocker"])
        self.assertIsNone(structured["next_action"])
        self.assertEqual(
            structured["next_guidance"],
            "reduce the request structure or move large content behind an ArtifactRef, then retry the same operation",
        )
        self.assertEqual(
            structured["interaction_telemetry"],
            {
                "classification": "budget_blocker",
                "preflight_failure": False,
                "no_progress_retry": False,
                "incident_present": False,
                "operator_attention_required": False,
                "projection_target_exceeded": False,
                "budget_blocker": True,
            },
        )

    def test_observe_rejects_the_retired_assurance_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "assurance.*unexpected"):
            self.service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                    "assurance": "assured",
                },
                task_id="retired-assurance",
                operation_id="retired-assurance-1",
            )

        with self.assertRaisesRegex(ScopeViolation, "undeclared fields.*assurance"):
            ObservationQuery.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"kind": "mdb", "queries": ["lsprop Object0"]}
                    ],
                    "assurance": "assured",
                }
            )

    def test_wide_observation_query_is_partitioned_without_becoming_a_blocker(
        self,
    ) -> None:
        backend = SemanticBackend()
        service = RuntimeMcpService(backend)
        selectors = [
            {
                "id": f"mdb-{index}",
                "kind": "mdb",
                "queries": [
                    f"lsprop Object{index}_{'x' * 180}",
                ],
            }
            for index in range(16)
        ]
        self.assertGreater(
            encoded_size(
                {
                    "target": "192.0.2.10",
                    "selectors": selectors,
                    "freshness": {"mode": "live", "max_age_seconds": 0},
                }
            ),
            2 * 1024,
        )

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="wide-observation",
                operation_id="wide-observation-1",
            )
            reference = receipt["observation_ref"]
            stored = service._test.context_runtime.load_observation(
                {
                    "schema": "openubmc.runtime.v1/observation-source-v1",
                    "blob_id": str(reference["digest"]).removeprefix("sha256:"),
                    "sha256": str(reference["digest"]).removeprefix("sha256:"),
                    "uri": reference["handle"],
                    "byte_count": reference["size"],
                    "kind": reference["kind"],
                    "provenance": reference["provenance"],
                    "retention_hint": reference["retention_hint"],
                    "target": reference["target"],
                    "scope_digest": str(reference["scope_digest"]).removeprefix(
                        "sha256:"
                    ),
                    "observed_at": reference["observed_at"],
                    "target_fingerprint": reference["target_fingerprint"],
                    "target_epoch": reference["target_epoch"],
                }
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(list(receipt["results"]), [item["id"] for item in selectors])
        self.assertIn("observation_ref", receipt)
        self.assertFalse(receipt["manual_narrowing_required"])
        calls = [
            arguments
            for name, arguments in backend.calls
            if name == "debug_collect"
        ]
        self.assertGreater(len(calls), 1)
        self.assertTrue(
            all(
                encoded_size(
                    {
                        "target": arguments["ip"],
                        "selectors": arguments["selectors"],
                        "freshness": {
                            "mode": "live",
                            "max_age_seconds": 0,
                        },
                    }
                )
                <= 2 * 1024
                for arguments in calls
            )
        )
        raw = stored["raw"]
        collection_partitions = raw["result"]["collection_partitions"]
        self.assertEqual(len(collection_partitions), len(calls))
        self.assertEqual(
            [
                selector["id"]
                for partition in collection_partitions
                for selector in partition["scope"]["selectors"]
            ],
            [item["id"] for item in selectors],
        )
        self.assertEqual(len(raw["result"]["lanes"]["ssh"]), 16)

    def test_wide_observation_rejects_cross_partition_target_epoch_drift(
        self,
    ) -> None:
        class DriftingPartitionBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                value = super().observe_query(task, arguments, context)
                call_index = len(self.calls)
                value["result"]["runtime"] = {
                    "status": {
                        "targets": [
                            {
                                "target": {
                                    "host": "192.0.2.10",
                                    "fingerprint": "ssh:stable-target",
                                },
                                "identity": {
                                    "fingerprint": "ssh:stable-target",
                                },
                                "epochs": {"target_epoch": call_index},
                            }
                        ]
                    }
                }
                return value

        backend = DriftingPartitionBackend()
        service = RuntimeMcpService(backend)
        selectors = [
            {
                "id": f"mdb-{index}",
                "kind": "mdb",
                "queries": [f"lsprop Object{index}_{'x' * 180}"],
            }
            for index in range(16)
        ]

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="drifting-wide-observation",
                operation_id="drifting-wide-observation-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertNotIn("observation_ref", receipt)
        self.assertTrue(
            any("target identity or epoch changed" in gap for gap in receipt["gaps"])
        )

    def test_wide_observation_rejects_missing_partition_timing(self) -> None:
        class MissingPartitionTimingBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                value = super().observe_query(task, arguments, context)
                if len(self.calls) % 2 == 0:
                    value.pop("observation_timing", None)
                return value

        backend = MissingPartitionTimingBackend()
        service = RuntimeMcpService(backend)
        selectors = [
            {
                "id": "mdb-wide",
                "kind": "mdb",
                "queries": [
                    f"lsprop Object{index}_{'x' * 180}" for index in range(16)
                ],
            }
        ]

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="missing-partition-timing",
                operation_id="missing-partition-timing-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertNotIn("observation_ref", receipt)
        self.assertTrue(
            any("partition selector timing" in gap for gap in receipt["gaps"])
        )

    def test_wide_observation_assurance_cannot_mask_fast_target_epoch_drift(
        self,
    ) -> None:
        class DriftingFastAssuranceBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                value = super().observe_query(task, arguments, context)
                prior = arguments.get("prior_observation")
                epoch = 2 if isinstance(prior, dict) else len(self.calls)
                value["result"]["runtime"] = {
                    "status": {
                        "targets": [
                            {
                                "target": {
                                    "host": "192.0.2.10",
                                    "fingerprint": "ssh:stable-target",
                                },
                                "identity": {
                                    "fingerprint": "ssh:stable-target",
                                },
                                "epochs": {"target_epoch": epoch},
                            }
                        ]
                    }
                }
                return value

        service = RuntimeMcpService(DriftingFastAssuranceBackend())
        selectors = [
            {
                "id": "mdb-wide",
                "kind": "mdb",
                "queries": [
                    f"lsprop Object{index}_{'x' * 180}" for index in range(16)
                ],
            }
        ]

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="assurance-target-drift",
                operation_id="assurance-target-drift-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertNotIn("observation_ref", receipt)
        self.assertTrue(
            any("target identity or epoch changed" in gap for gap in receipt["gaps"])
        )

    def test_wide_observation_keeps_selected_capability_partition_fact(
        self,
    ) -> None:
        class CapabilityPartitionBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                value = super().observe_query(task, arguments, context)
                selected_capability = any(
                    selector.get("kind") == "capability"
                    for selector in arguments.get("selectors", [])
                    if isinstance(selector, dict)
                )
                value["result"]["capabilities"]["remote_log_file"] = (
                    selected_capability
                )
                return value

        service = RuntimeMcpService(CapabilityPartitionBackend())
        selectors = [
            {
                "id": "caps",
                "kind": "capability",
                "names": ["telnet"],
            },
            {
                "id": "mdb-wide",
                "kind": "mdb",
                "queries": [
                    f"lsprop Object{index}_{'x' * 180}" for index in range(16)
                ],
            },
        ]

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="selected-capability-partition",
                operation_id="selected-capability-partition-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(
            receipt["results"]["caps"]["values"],
            [{"name": "telnet", "status": "available"}],
        )
        self.assertIn("observation_ref", receipt)

    def test_wide_observation_does_not_replace_a_missing_selected_capability_fact(
        self,
    ) -> None:
        class MissingCapabilityPartitionFactBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                value = super().observe_query(task, arguments, context)
                selected_capability = any(
                    selector.get("kind") == "capability"
                    for selector in arguments.get("selectors", [])
                    if isinstance(selector, dict)
                )
                if selected_capability:
                    value["result"]["capabilities"].pop(
                        "remote_log_file", None
                    )
                else:
                    value["result"]["capabilities"]["remote_log_file"] = False
                return value

        service = RuntimeMcpService(MissingCapabilityPartitionFactBackend())
        selectors = [
            {
                "id": "caps",
                "kind": "capability",
                "names": ["telnet"],
            },
            {
                "id": "mdb-wide",
                "kind": "mdb",
                "queries": [
                    f"lsprop Object{index}_{'x' * 180}" for index in range(16)
                ],
            },
        ]

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="missing-selected-capability-fact",
                operation_id="missing-selected-capability-fact-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(
            receipt["results"]["caps"]["values"],
            [{"name": "telnet", "status": "not_checked"}],
        )
        self.assertNotIn("observation_ref", receipt)
        self.assertTrue(
            any(
                "selected capability fact" in gap
                for gap in receipt["gaps"]
            )
        )

    def test_wide_observation_does_not_share_a_capability_fact_between_selectors(
        self,
    ) -> None:
        class DuplicateCapabilitySelectorBackend(SemanticBackend):
            def observe_query(self, task, arguments, context):
                value = super().observe_query(task, arguments, context)
                selector_ids = {
                    str(selector.get("id", ""))
                    for selector in arguments.get("selectors", [])
                    if isinstance(selector, dict)
                }
                if "caps-missing" in selector_ids:
                    value["result"]["capabilities"].pop(
                        "remote_log_file", None
                    )
                if "caps-observed" in selector_ids:
                    value["result"]["capabilities"]["remote_log_file"] = True
                return value

        service = RuntimeMcpService(DuplicateCapabilitySelectorBackend())
        selectors = [
            {
                "id": "caps-missing",
                "kind": "capability",
                "names": ["telnet"],
            },
            {
                "id": "mdb-wide",
                "kind": "mdb",
                "queries": [
                    f"lsprop Object{index}_{'x' * 180}" for index in range(16)
                ],
            },
            {
                "id": "caps-observed",
                "kind": "capability",
                "names": ["telnet"],
            },
        ]

        try:
            receipt = service.call_exposed_tool(
                "observe",
                {"target": "192.0.2.10", "selectors": selectors},
                task_id="duplicate-capability-selectors",
                operation_id="duplicate-capability-selectors-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(
            receipt["results"]["caps-missing"]["values"],
            [{"name": "telnet", "status": "not_checked"}],
        )
        self.assertEqual(
            receipt["results"]["caps-observed"]["values"],
            [{"name": "telnet", "status": "not_checked"}],
        )
        self.assertNotIn("observation_ref", receipt)

    def test_observe_validation_errors_name_supported_capabilities_and_mdb_fix(
        self,
    ) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            with self.assertRaisesRegex(
                ScopeViolation,
                "invalid capability.*made-up.*supported.*alarms.*ssh",
            ):
                service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.10",
                        "selectors": [
                            {
                                "id": "caps",
                                "kind": "capability",
                                "names": ["ssh", "made-up"],
                            }
                        ],
                    },
                    task_id="invalid-capability",
                    operation_id="invalid-capability-1",
                )

            with self.assertRaisesRegex(
                ScopeViolation,
                "invalid MDB query.*setprop Object0.*lsprop <object> \\[interface\\]",
            ):
                service.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.10",
                        "selectors": [
                            {
                                "id": "mdb",
                                "kind": "mdb",
                                "queries": ["setprop Object0 Interface Value"],
                            }
                        ],
                    },
                    task_id="invalid-mdb",
                    operation_id="invalid-mdb-1",
                )
        finally:
            service.close()

    def test_execute_rejects_the_retired_control_continue_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "command.*must be one of"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "control",
                    "run_id": "run-retired-control-continue",
                    "command": "continue",
                },
                task_id="retired-control-continue",
                operation_id="retired-control-continue-1",
            )

    def test_execute_rejects_the_retired_full_observation_receipt(self) -> None:
        with self.assertRaisesRegex(ValueError, "observation_receipt.*unexpected"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "observation_receipt": {
                        "receipt_id": "receipt-retired",
                        "status": "complete",
                        "observation_ref": {},
                        "scope": {},
                    },
                },
                task_id="retired-observation-receipt",
                operation_id="retired-observation-receipt-1",
            )

        with self.assertRaisesRegex(
            AgentGatewayError,
            "observation_receipt.*retired",
        ):
            decode_run_command(
                {
                    "kind": "start",
                    "target": "192.0.2.10",
                    "observation_receipt": {
                        "receipt_id": "receipt-retired",
                    },
                },
                operation_id="retired-observation-receipt-direct",
            )

    def test_execute_rejects_undeclared_multi_target_fields(self) -> None:
        action = {
            "kind": "start",
            "targets": [
                {
                    "ip": "192.0.2.10",
                    "role": "reference",
                    "target_id": "reference",
                    "unexpected_policy": "agent-authored",
                },
                {
                    "ip": "192.0.2.11",
                    "role": "candidate",
                    "target_id": "candidate",
                },
            ],
            "intent": "diagnosis-only",
            "entry_operation": "debug_run",
        }

        with self.assertRaisesRegex(ValueError, "unexpected_policy.*unexpected"):
            self.service.call_exposed_tool(
                "execute",
                action,
                task_id="unexpected-target-field",
                operation_id="unexpected-target-field-1",
            )

        with self.assertRaisesRegex(
            AgentGatewayError,
            "target fields.*unexpected_policy",
        ):
            decode_run_command(
                action,
                operation_id="unexpected-target-field-direct",
            )

    def test_runtime_rejects_the_retired_compatibility_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "agent or operator"):
            RuntimeMcpService(
                SemanticBackend(),
                interface_profile="compatibility",
            )

    def test_auto_assurance_upgrades_and_reuses_the_fast_observation(self) -> None:
        backend = AutoAssuranceSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "caps", "kind": "capability", "names": ["telnet"]},
                        {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]},
                    ],
                },
                task_id="auto-assurance",
                operation_id="auto-assurance-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertNotIn("assurance", receipt)
        self.assertEqual(backend.assurance_calls, [False, True])
        self.assertEqual(backend.selector_scopes[0], backend.selector_scopes[1])
        self.assertEqual(backend.mdb_collections, 1)
        with self.assertRaises(ScopeViolation):
            ScopeContract.from_query(
                {
                    "target": "192.0.2.10",
                    "selectors": [{"kind": "capability", "names": ["ssh"]}],
                    "freshness": {"mode": "cached", "max_age_seconds": 60},
                }
            )

    def test_auto_assurance_transport_failure_preserves_the_fast_observation(self) -> None:
        backend = FailingAssuranceSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {"id": "caps", "kind": "capability", "names": ["telnet"]},
                        {"id": "mdb", "kind": "mdb", "queries": ["lsprop Object0"]},
                    ],
                },
                task_id="assurance-fallback",
                operation_id="assurance-fallback-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(backend.assurance_calls, [False, True, True])
        self.assertEqual(backend.mdb_collections, 1)
        self.assertIn(
            "Object0",
            receipt["results"]["mdb"]["values"][0]["value"]["properties"],
        )
        self.assertTrue(
            any("automatic assurance failed" in gap for gap in receipt["gaps"]),
            receipt["gaps"],
        )

    def test_selector_order_and_timing_survive_adapter_persistence_and_projection(
        self,
    ) -> None:
        backend = SelectorTimingSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "mdb-second",
                            "kind": "mdb",
                            "queries": ["lsprop Object0"],
                        },
                        {
                            "id": "caps-first",
                            "kind": "capability",
                            "names": ["ssh"],
                        },
                    ],
                },
                task_id="selector-timing",
                operation_id="selector-timing-1",
            )
            reference = receipt["observation_ref"]
            stored = service._test.context_runtime.load_observation(
                {
                    "schema": "openubmc.runtime.v1/observation-source-v1",
                    "blob_id": str(reference["digest"]).removeprefix("sha256:"),
                    "sha256": str(reference["digest"]).removeprefix("sha256:"),
                    "uri": reference["handle"],
                    "byte_count": reference["size"],
                    "kind": reference["kind"],
                    "provenance": reference["provenance"],
                    "retention_hint": reference["retention_hint"],
                    "target": reference["target"],
                    "scope_digest": str(reference["scope_digest"]).removeprefix(
                        "sha256:"
                    ),
                    "observed_at": reference["observed_at"],
                    "target_fingerprint": reference["target_fingerprint"],
                    "target_epoch": reference["target_epoch"],
                }
            )
        finally:
            service.close()

        self.assertEqual(
            [selector["id"] for selector in backend.selector_scopes[0]],
            ["mdb-second", "caps-first"],
        )
        self.assertEqual(
            list(receipt["results"]),
            ["mdb-second", "caps-first"],
        )
        self.assertEqual(receipt["consistency"]["classification"], "coherent")
        self.assertEqual(
            [
                selector["selector_id"]
                for selector in receipt["consistency"]["selectors"]
            ],
            ["mdb-second", "caps-first"],
        )
        self.assertEqual(
            stored["raw"]["observation_timing"],
            receipt["consistency"],
        )
        self.assertEqual(
            stored["observation_timing"],
            receipt["consistency"],
        )
        self.assertTrue(stored["reusable"])

    def test_over_skew_observation_remains_visible_but_has_no_reusable_ref(
        self,
    ) -> None:
        service = RuntimeMcpService(SkewedSelectorTimingSemanticBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "mdb-second",
                            "kind": "mdb",
                            "queries": ["lsprop Object0"],
                        },
                        {
                            "id": "caps-first",
                            "kind": "capability",
                            "names": ["ssh"],
                        },
                    ],
                },
                task_id="selector-skew",
                operation_id="selector-skew-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["consistency"]["classification"], "inconsistent")
        self.assertNotIn("observation_ref", receipt)
        self.assertTrue(
            any("skew" in gap for gap in receipt["gaps"]),
            receipt["gaps"],
        )
        self.assertEqual(receipt["results"]["mdb-second"]["kind"], "mdb")
        self.assertTrue(
            all(item["uri"].startswith("blob://") for item in receipt["evidence"])
        )

    def test_missing_and_stale_selector_timing_are_explicit_non_reusable_gaps(
        self,
    ) -> None:
        for backend_type, expected in (
            (MissingSelectorTimingSemanticBackend, "missing"),
            (StaleSelectorTimingSemanticBackend, "stale"),
        ):
            with self.subTest(expected=expected):
                service = RuntimeMcpService(backend_type())
                try:
                    receipt = service.call_exposed_tool(
                        "observe",
                        {
                            "target": "192.0.2.10",
                            "selectors": [
                                {
                                    "id": "mdb-second",
                                    "kind": "mdb",
                                    "queries": ["lsprop Object0"],
                                },
                                {
                                    "id": "caps-first",
                                    "kind": "capability",
                                    "names": ["ssh"],
                                },
                            ],
                        },
                        task_id=f"selector-{expected}",
                        operation_id=f"selector-{expected}-1",
                    )
                finally:
                    service.close()

                self.assertEqual(receipt["status"], "incomplete")
                self.assertEqual(
                    receipt["consistency"]["classification"], "partial"
                )
                self.assertNotIn("observation_ref", receipt)
                self.assertTrue(
                    any(expected in gap for gap in receipt["gaps"]),
                    receipt["gaps"],
                )

    def test_missing_selector_timing_is_not_inferred_as_a_coherent_snapshot(
        self,
    ) -> None:
        service = RuntimeMcpService(NoSelectorTimingSemanticBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "facts",
                            "kind": "mdb",
                            "queries": ["lsprop Object0"],
                        }
                    ],
                },
                task_id="selector-timing-absent",
                operation_id="selector-timing-absent-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["consistency"]["classification"], "partial")
        self.assertNotIn("observation_ref", receipt)
        self.assertTrue(
            any("missing" in gap for gap in receipt["gaps"]),
            receipt["gaps"],
        )

    def test_assurance_unavailable_retains_fast_result_with_an_explicit_gap(
        self,
    ) -> None:
        service = RuntimeMcpService(AssuranceUnavailableSemanticBackend())
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "caps",
                            "kind": "capability",
                            "names": ["telnet"],
                        }
                    ],
                },
                task_id="assurance-unavailable",
                operation_id="assurance-unavailable-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(
            any("assurance unavailable" in gap for gap in receipt["gaps"]),
            receipt["gaps"],
        )

    def test_assurance_keeps_fast_scope_when_temporal_consistency_does_not_improve(
        self,
    ) -> None:
        backend = NonImprovingAssuranceSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            receipt = service.call_exposed_tool(
                "observe",
                {
                    "target": "192.0.2.10",
                    "selectors": [
                        {
                            "id": "caps",
                            "kind": "capability",
                            "names": ["telnet"],
                        },
                        {
                            "id": "mdb",
                            "kind": "mdb",
                            "queries": ["lsprop Object0"],
                        },
                    ],
                },
                task_id="selector-assurance-fallback",
                operation_id="selector-assurance-fallback-1",
            )
        finally:
            service.close()

        self.assertEqual(backend.assurance_calls, [False, True])
        self.assertEqual(receipt["consistency"]["classification"], "partial")
        self.assertIn(
            "Object0",
            receipt["results"]["mdb"]["values"][0]["value"]["properties"],
        )
        self.assertTrue(
            any("did not improve" in gap for gap in receipt["gaps"]),
            receipt["gaps"],
        )

    def test_execute_hides_runtime_mechanics_and_records_terminal_outcome(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "repair the diagnosed source defect",
            },
            task_id="execute-task",
            operation_id="execute-1",
        )
        self.assertEqual(first["state"], "waiting_response")
        self.assertEqual(first["gate"]["name"], "developer.change")
        self.assertTrue(
            any(
                fact.get("kind") == "operation"
                and fact.get("name") == "debug_run"
                and fact.get("status") == "completed"
                for fact in first["facts"]
            ),
            first["facts"],
        )
        self.assertLessEqual(encoded_size(first), TURN_MAX_BYTES)
        rendered = json.dumps(first, ensure_ascii=False)
        for hidden in ("phase_record", "workflow.next", "offset"):
            self.assertNotIn(hidden, rendered)

        def keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from keys(item)
            elif isinstance(value, list):
                for item in value:
                    yield from keys(item)

        self.assertNotIn("revision", set(keys(first)))
        self.assertNotIn("attempt", set(keys(first)))

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                **gate_binding(first),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "abc123",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run regression tests"],
                    },
                },
            },
            task_id="execute-task",
            operation_id="execute-2",
        )
        self.assertEqual(final["state"], "completed")
        self.assertIsNotNone(final["outcome"])
        self.assertTrue(final["outcome_recorded"])
        self.assertLessEqual(encoded_size(final), TURN_MAX_BYTES)
        self.assertNotIn("phase_record", json.dumps(final, ensure_ascii=False))
        self.assertEqual(self.service.session_outcome_service.status()["outcome_count"], 0)
        replayed = self.service.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": first["run_id"]},
            task_id="execute-task-replay",
            operation_id="execute-3",
        )
        events = self.service._test.context_runtime.repository.events(first["run_id"])
        self.assertEqual(replayed["outcome"], final["outcome"])
        self.assertEqual(
            [event["kind"] for event in events].count("RunOutcomeRecorded"), 1
        )
        self.assertEqual(
            [event["kind"] for event in events].count("CloseoutRecorded"), 1
        )
        self.assertEqual(self.service.session_outcome_service.status()["outcome_count"], 0)

    def test_execute_blocks_generic_completion_without_evaluable_diagnosis(self) -> None:
        service = RuntimeMcpService(GenericCompletionBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "purpose": "identify the bounded root cause",
                    "entry_operation": "debug_run",
                },
                task_id="generic-diagnosis",
                operation_id="generic-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        typed_turn = RunTurn.from_public_dict(turn)
        self.assertIsInstance(typed_turn.diagnostic_receipt, DiagnosticReceipt)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(
            receipt["coverage"],
            {
                "requested": 1,
                "evaluable": 0,
                "unavailable": 0,
                "not_checked": 1,
                "complete": False,
            },
        )
        self.assertFalse(receipt["content_complete"])
        self.assertIn("diagnostic_result_not_visible", receipt["gaps"])
        self.assertIn("diagnostic_result_not_visible", turn["gaps"])
        diagnosis = next(
            check
            for check in turn["outcome"]["acceptance"]
            if check["requirement_id"] == "stage.diagnosis"
        )
        self.assertEqual(diagnosis["status"], "blocked")

    def test_execute_projects_bounded_results_and_truncation_metadata(self) -> None:
        service = RuntimeMcpService(BoundedDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "purpose": "build a bounded diagnostic timeline",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "logs": "app.log",
                        "tree_service": "bmc.kepler.devmon",
                        "mdb_queries": ["lsmc"],
                    },
                },
                task_id="bounded-diagnosis",
                operation_id="bounded-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["agent_acceptance"], "partial")
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(
            receipt["coverage"],
            {
                "requested": 4,
                "evaluable": 4,
                "unavailable": 0,
                "not_checked": 0,
                "complete": False,
            },
        )
        self.assertTrue(receipt["truncated"])
        self.assertFalse(receipt["content_complete"])
        self.assertEqual(receipt["freshness"]["status"], "partial")
        self.assertEqual(receipt["capabilities"]["ssh"], "available")
        self.assertEqual(receipt["capabilities"]["telnet"], "available")
        self.assertEqual(receipt["capabilities"]["mdbctl"], "available")
        self.assertEqual(receipt["capabilities"]["busctl"], "available")
        results = {item["result_id"]: item for item in receipt["results"]}
        self.assertEqual(
            set(results),
            {
                "target-clock",
                "logs",
                "service",
                "mdb-1",
            },
        )
        self.assertEqual(results["mdb-1"]["request"], "lsmc")
        self.assertEqual(
            results["target-clock"]["value"]["after"],
            "2026-08-25 04:42:55 +0000",
        )
        self.assertEqual(
            results["logs"]["value"]["entries"][0]["line_count"],
            31,
        )
        self.assertIn("content_truncated", receipt["gaps"])
        self.assertIn("content_truncated", turn["gaps"])
        self.assertLessEqual(encoded_size(turn), TURN_MAX_BYTES)

    def test_execute_mcp_text_keeps_diagnostic_identities_without_preview_values(
        self,
    ) -> None:
        service = RuntimeMcpService(BoundedDiagnosticBackend())
        try:
            response = JsonRpcMcpEndpoint(
                service,
                session_task_id="deduplicated-diagnostic-text",
            ).handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "execute",
                        "arguments": {
                            "kind": "start",
                            "target": "192.0.2.20",
                            "intent": "diagnosis-only",
                            "purpose": "build a bounded diagnostic timeline",
                            "entry_operation": "debug_run",
                            "entry_arguments": {
                                "logs": "app.log",
                                "tree_service": "bmc.kepler.devmon",
                                "mdb_queries": ["lsmc"],
                            },
                        },
                    },
                }
            )
        finally:
            service.close()

        result = response["result"]
        structured = result["structuredContent"]
        receipt = structured["diagnostic_receipt"]
        text = result["content"][0]["text"]
        self.assertEqual(
            receipt["results"][1]["value"]["entries"][0]["lines_preview"][0],
            "mctpd request timeout",
        )
        self.assertIn(f"receipt_id={receipt['receipt_id']}", text)
        self.assertIn("Outcome status=failed", text)
        self.assertIn("result_ids:", text)
        for result_id in ("target-clock", "logs", "service", "mdb-1"):
            self.assertIn(result_id, text)
        self.assertIn(
            "result[logs] status=available kind=bounded-logs request=app.log",
            text,
        )
        self.assertIn("evidence_ids:", text)
        self.assertIn("gaps:", text)
        self.assertEqual(text.count("content_truncated"), 1)
        for duplicated_value in (
            "mctpd request timeout",
            "storage PluginRequestEx timeout",
            "/bmc/kepler/devmon",
            "Drive_1_010102",
            "2026-08-25 04:42:55 +0000",
        ):
            self.assertNotIn(duplicated_value, text)

    def test_execute_mcp_text_preserves_oversized_terminal_semantics_and_guidance(
        self,
    ) -> None:
        class ExecuteOnlyService:
            def __init__(self) -> None:
                self.gateway = AgentGateway(RequiredIdentityPressureTurnRuntime())

            def call_exposed_tool(
                self,
                name,
                arguments,
                *,
                task_id,
                operation_id,
            ):
                self.assert_execute(name)
                return self.gateway.execute(
                    arguments,
                    task_id=task_id,
                    operation_id=operation_id,
                )

            @staticmethod
            def assert_execute(name) -> None:
                if name != "execute":
                    raise ValueError("only execute is supported")

        response = JsonRpcMcpEndpoint(
            ExecuteOnlyService(),
            session_task_id="oversized-terminal-text",
        ).handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.20",
                        "intent": "diagnosis-only",
                    },
                },
            }
        )

        result = response["result"]
        structured = result["structuredContent"]
        text = result["content"][0]["text"]
        self.assertEqual(structured["outcome"]["status"], "completed")
        self.assertEqual(
            structured["next"],
            "retain the Runtime-owned terminal instruction exactly "
            + "n" * 20_000,
        )
        self.assertTrue(structured["projection_target_exceeded"])
        self.assertEqual(
            structured["diagnostic_receipt"]["results"][0]["result_id"],
            "result-0",
        )
        self.assertLessEqual(
            len(text.encode("utf-8")),
            4 * 1024,
        )
        self.assertIn("Outcome status=completed", text)
        self.assertIn("terminal outcome remains Runtime-owned", text)
        self.assertIn(
            "next_guidance: retain the Runtime-owned terminal instruction exactly",
            text,
        )
        self.assertIn("gaps: required-gap-0-", text)
        self.assertIn("receipt_id=diagnostic-oversized", text)
        self.assertIn("result-0", text)
        shown_line = next(
            line for line in text.splitlines() if line.startswith("results_shown=")
        )
        shown = int(shown_line.split("=", 1)[1].split("/", 1)[0])
        self.assertEqual(shown, text.count("result["))
        self.assertIn("text_projection_compacted=true", text)
        self.assertNotIn("x" * 256, text)

    def test_execute_mcp_text_includes_persisted_compacted_result_identities(
        self,
    ) -> None:
        class CompactedReceiptService:
            @staticmethod
            def call_exposed_tool(
                name,
                arguments,
                *,
                task_id,
                operation_id,
            ):
                del arguments, task_id, operation_id
                if name != "execute":
                    raise ValueError("only execute is supported")
                return {
                    "state": "completed",
                    "diagnostic_receipt": {
                        "receipt_id": "diagnostic-compacted-identities",
                        "operation": "debug_run",
                        "status": "partial",
                        "coverage": {
                            "requested": 3,
                            "evaluable": 1,
                            "unavailable": 0,
                            "not_checked": 2,
                            "complete": False,
                            "visible_evaluable": 1,
                            "visible_unavailable": 0,
                            "visible_not_checked": 2,
                        },
                        "results": [
                            {
                                "result_id": "visible-result",
                                "status": "available",
                                "kind": "mdb",
                                "request": "lsmc",
                                "value": {"stdout_lines": ["hidden-value"]},
                            }
                        ],
                        "compacted_results": {
                            "result_ids": ["compacted-a", "compacted-b"],
                            "status": "not_checked",
                            "gap": "result_preview_compacted",
                        },
                        "freshness": {"status": "fresh"},
                        "capabilities": {"mdbctl": "available"},
                        "truncated": False,
                        "content_complete": True,
                        "evidence": [],
                        "gaps": ["result_preview_compacted"],
                    },
                }

        response = JsonRpcMcpEndpoint(
            CompactedReceiptService(),
            session_task_id="compacted-result-identities",
        ).handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "execute", "arguments": {}},
            }
        )

        result = response["result"]
        text = result["content"][0]["text"]
        self.assertEqual(
            result["structuredContent"]["diagnostic_receipt"]
            ["compacted_results"]["result_ids"],
            ["compacted-a", "compacted-b"],
        )
        self.assertIn("results_shown=3/3", text)
        self.assertIn("result[visible-result] status=available", text)
        self.assertIn("result[compacted-a] status=not_checked", text)
        self.assertIn("result[compacted-b] status=not_checked", text)
        self.assertNotIn("hidden-value", text)

    def test_execute_mcp_text_preserves_maximum_length_control_identities(
        self,
    ) -> None:
        run_id = "r" * 128
        gate_id = "g" * 128
        incident_id = "i" * 128
        receipt_id = "d" * 128
        next_action = {
            "kind": "control",
            "run_id": run_id,
            "command": "cancel",
            "incident_id": incident_id,
        }

        class MaximumIdentityService:
            @staticmethod
            def call_exposed_tool(
                name,
                arguments,
                *,
                task_id,
                operation_id,
            ):
                del arguments, task_id, operation_id
                if name != "execute":
                    raise ValueError("only execute is supported")
                return {
                    "run_id": run_id,
                    "state": "incident",
                    "gate": {
                        "gate_id": gate_id,
                        "gate_version": 1,
                        "schema_digest": "sha256:" + "a" * 64,
                    },
                    "incident": {
                        "incident_id": incident_id,
                        "code": "operator_cancel_required",
                        "message": "cancel the same Run",
                    },
                    "next_action": next_action,
                    "diagnostic_receipt": {
                        "receipt_id": receipt_id,
                        "status": "blocked",
                        "coverage": {
                            "requested": 1,
                            "evaluable": 0,
                            "unavailable": 0,
                            "not_checked": 1,
                            "complete": False,
                        },
                        "results": [],
                        "freshness": {"status": "unknown"},
                        "capabilities": {
                            "ssh": "available",
                            "telnet": "available",
                            "mdbctl": "available",
                            "busctl": "available",
                            "dbus": "not_checked",
                            "alarms": "unavailable",
                        },
                        "truncated": False,
                        "content_complete": False,
                        "evidence": [],
                        "gaps": ["diagnostic_result_not_visible"],
                    },
                }

        response = JsonRpcMcpEndpoint(
            MaximumIdentityService(),
            session_task_id="maximum-control-identities",
        ).handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "execute", "arguments": {}},
            }
        )

        result = response["result"]
        text = result["content"][0]["text"]
        self.assertEqual(result["structuredContent"]["run_id"], run_id)
        self.assertIn(f"run_id={run_id} ", text)
        self.assertIn(f"gate_id={gate_id} ", text)
        self.assertIn(f"incident_id={incident_id} ", text)
        self.assertIn(f"receipt_id={receipt_id} ", text)
        self.assertIn("capabilities_shown=6/6", text)
        self.assertIn("dbus=not_checked", text)
        self.assertIn("alarms=unavailable", text)
        action_line = next(
            line for line in text.splitlines() if line.startswith("next_action: ")
        )
        self.assertEqual(
            json.loads(action_line.removeprefix("next_action: ")),
            next_action,
        )

    def test_execute_mcp_text_handles_structured_gaps_without_changing_turn(
        self,
    ) -> None:
        structured_gaps = [
            {"code": "dependency_blocked", "detail": ["conan"]},
            ["nvme", "missing"],
            "plain-gap",
        ]

        class StructuredGapService:
            @staticmethod
            def call_exposed_tool(
                name,
                arguments,
                *,
                task_id,
                operation_id,
            ):
                del arguments, task_id, operation_id
                if name != "execute":
                    raise ValueError("only execute is supported")
                return {
                    "run_id": "run-structured-gaps",
                    "state": "completed",
                    "gaps": structured_gaps,
                    "outcome": {
                        "status": "completed",
                        "summary": "source delivery complete with external gaps",
                        "acceptance": [],
                    },
                }

        response = JsonRpcMcpEndpoint(
            StructuredGapService(),
            session_task_id="structured-gaps",
        ).handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "execute", "arguments": {}},
            }
        )

        result = response["result"]
        self.assertEqual(result["structuredContent"]["gaps"], structured_gaps)
        text = result["content"][0]["text"]
        self.assertIn('"code":"dependency_blocked"', text)
        self.assertIn('["nvme","missing"]', text)
        self.assertIn("plain-gap", text)

    def test_execute_completes_when_all_bounded_result_kinds_are_evaluable(self) -> None:
        service = RuntimeMcpService(CompleteBoundedDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "logs": "app.log",
                        "tree_service": "bmc.kepler.devmon",
                        "mdb_queries": ["lsmc"],
                    },
                },
                task_id="complete-bounded-diagnosis",
                operation_id="complete-bounded-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(turn["state"], "completed")
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["coverage"]["complete"])
        self.assertTrue(receipt["content_complete"])
        self.assertFalse(receipt["truncated"])
        target_clock = next(
            item for item in receipt["results"] if item["result_id"] == "target-clock"
        )
        self.assertEqual(
            target_clock["value"]["after"],
            "2026-08-25 04:42:55 +0000",
        )

    def test_execute_marks_the_runtime_target_clock_not_checked_when_missing(
        self,
    ) -> None:
        service = RuntimeMcpService(MissingTargetClockDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "logs": "app.log",
                        "tree_service": "bmc.kepler.devmon",
                        "mdb_queries": ["lsmc"],
                    },
                },
                task_id="missing-target-clock",
                operation_id="missing-target-clock-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        target_clock = next(
            item for item in receipt["results"] if item["result_id"] == "target-clock"
        )
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["coverage"]["requested"], 4)
        self.assertEqual(receipt["coverage"]["evaluable"], 3)
        self.assertEqual(receipt["coverage"]["not_checked"], 1)
        self.assertFalse(receipt["coverage"]["complete"])
        self.assertEqual(target_clock["status"], "not_checked")
        self.assertEqual(target_clock["gap"], "target_clock_not_returned")

    def test_execute_blocks_successful_tool_without_visible_result_content(self) -> None:
        service = RuntimeMcpService(EmptyStructuredDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "purpose": "verify empty tool output fails closed",
                    "entry_operation": "debug_run",
                },
                task_id="empty-structured-diagnosis",
                operation_id="empty-structured-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(turn["state"], "failed")
        self.assertEqual(receipt["agent_acceptance"], "blocked")
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["coverage"]["evaluable"], 0)
        self.assertEqual(receipt["results"][0]["status"], "not_checked")
        self.assertEqual(receipt["results"][0]["gap"], "result_not_visible")
        self.assertFalse(receipt["content_complete"])

    def test_execute_blocks_result_that_contains_only_completeness_metadata(self) -> None:
        service = RuntimeMcpService(MetadataOnlyDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="metadata-only-diagnosis",
                operation_id="metadata-only-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["coverage"]["evaluable"], 0)
        self.assertEqual(receipt["results"][0]["status"], "not_checked")
        self.assertEqual(
            receipt["results"][0]["gap"],
            "result_not_evaluable",
        )

    def test_execute_keeps_complete_source_content_complete_when_projection_compacts(self) -> None:
        service = RuntimeMcpService(ProjectionCompactedDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "purpose": "preserve projection truncation semantics",
                    "entry_operation": "debug_run",
                },
                task_id="projection-compacted-diagnosis",
                operation_id="projection-compacted-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(turn["state"], "completed")
        self.assertEqual(receipt["status"], "complete")
        self.assertFalse(receipt["truncated"])
        self.assertTrue(receipt["content_complete"])
        self.assertNotIn("content_truncated", receipt["gaps"])
        self.assertTrue(receipt["results"][0]["projection_truncated"])
        self.assertEqual(len(receipt["results"][0]["value"]["records"]), 16)

    def test_execute_requires_explicit_content_completeness_metadata(self) -> None:
        service = RuntimeMcpService(MissingContentCompleteDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="missing-content-complete",
                operation_id="missing-content-complete-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["coverage"]["evaluable"], 1)
        self.assertEqual(receipt["status"], "partial")
        self.assertFalse(receipt["content_complete"])
        self.assertIn("diagnostic_content_incomplete", receipt["gaps"])

    def test_execute_does_not_treat_a_timestamp_as_freshness_proof(self) -> None:
        service = RuntimeMcpService(TimestampOnlyDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "purpose": "require explicit freshness proof",
                    "entry_operation": "debug_run",
                },
                task_id="timestamp-only-diagnosis",
                operation_id="timestamp-only-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(turn["state"], "failed")
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["freshness"]["status"], "unknown")
        self.assertIn("freshness_unknown", receipt["gaps"])

    def test_execute_does_not_complete_when_freshness_contains_stale_evidence(self) -> None:
        service = RuntimeMcpService(StaleEvidenceDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="stale-evidence-diagnosis",
                operation_id="stale-evidence-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["freshness"]["status"], "stale")
        self.assertFalse(receipt["freshness"]["complete"])
        self.assertEqual(
            receipt["freshness"]["stale_evidence"][0]["evidence_id"],
            "evidence-old",
        )
        self.assertIn("freshness_stale", receipt["gaps"])

    def test_execute_reconciles_adapter_results_with_runtime_arguments(self) -> None:
        service = RuntimeMcpService(OmittedRequestDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {"logs": "app.log"},
                },
                task_id="omitted-request-diagnosis",
                operation_id="omitted-request-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["coverage"]["requested"], 2)
        results = {item["result_id"]: item for item in receipt["results"]}
        self.assertEqual(results["logs"]["status"], "not_checked")
        self.assertEqual(results["logs"]["gap"], "result_not_visible")
        self.assertEqual(results["target-clock"]["status"], "not_checked")
        self.assertEqual(
            results["target-clock"]["gap"],
            "target_clock_not_returned",
        )

    def test_execute_marks_skipped_source_correlation_not_checked(self) -> None:
        service = RuntimeMcpService(SkippedCorrelationDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {"source_correlation_requested": True},
                },
                task_id="skipped-correlation-diagnosis",
                operation_id="skipped-correlation-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        correlation = next(
            item for item in receipt["results"] if item["result_id"] == "correlation"
        )
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["coverage"]["requested"], 2)
        self.assertEqual(correlation["result_id"], "correlation")
        self.assertEqual(correlation["status"], "not_checked")
        self.assertIn("Source correlation disabled", correlation["gap"])

    def test_execute_projects_multi_target_results_and_comparison(self) -> None:
        service = RuntimeMcpService(MultiTargetDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {
                            "ip": "192.0.2.20",
                            "role": "reference",
                            "target_id": "reference",
                        },
                        {
                            "ip": "192.0.2.21",
                            "role": "candidate",
                            "target_id": "candidate",
                        },
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="multi-target-diagnosis",
                operation_id="multi-target-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        results = {item["result_id"]: item for item in receipt["results"]}
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["coverage"]["requested"], 3)
        self.assertEqual(receipt["coverage"]["evaluable"], 3)
        self.assertEqual(receipt["freshness"]["status"], "fresh")
        self.assertEqual(
            set(results),
            {"target-1-version", "target-2-version", "comparison"},
        )
        self.assertEqual(
            results["target-2-version"]["value"]["lines"],
            ["12.08.21.07"],
        )

    def test_decode_start_materializes_typed_runtime_targets(self) -> None:
        command = decode_run_command(
            {
                "kind": "start",
                "targets": [
                    {
                        "ip": "192.0.2.20",
                        "role": "reference",
                        "target_id": "reference",
                    },
                    {
                        "ip": "192.0.2.21",
                        "role": "candidate",
                        "target_id": "candidate",
                    },
                ],
                "intent": "diagnosis-only",
                "entry_operation": "debug_run",
                "entry_arguments": {"files": ["/etc/version.json"]},
            },
            operation_id="typed-target-start",
        )

        self.assertIsInstance(command, StartRun)
        self.assertTrue(
            all(isinstance(target, RunTarget) for target in command.targets)
        )
        self.assertEqual(
            [target.to_public_dict() for target in command.targets],
            [
                {
                    "ip": "192.0.2.20",
                    "role": "reference",
                    "target_id": "reference",
                },
                {
                    "ip": "192.0.2.21",
                    "role": "candidate",
                    "target_id": "candidate",
                },
            ],
        )

    def test_execute_rejects_diagnostic_scope_larger_than_durable_receipt(
        self,
    ) -> None:
        backend = MultiTargetDiagnosticBackend()
        service = RuntimeMcpService(backend)
        try:
            with self.assertRaisesRegex(
                AgentGatewayError,
                "diagnostic scope requests 16385 items; maximum is 1024",
            ):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "targets": [
                            {"ip": f"192.0.2.{index}"}
                            for index in range(1, 17)
                        ],
                        "intent": "diagnosis-only",
                        "entry_operation": "debug_run",
                        "entry_arguments": {
                            "files": [
                                f"/tmp/diagnostic-{index}.txt"
                                for index in range(1024)
                            ],
                            "mdb_only": True,
                        },
                    },
                    task_id="oversized-diagnostic-scope",
                    operation_id="oversized-diagnostic-scope-start",
                )
        finally:
            service.close()

        self.assertEqual(backend.calls, [])

    def test_execute_counts_the_runtime_target_clock_in_diagnostic_scope(self) -> None:
        backend = GenericCompletionBackend()
        service = RuntimeMcpService(backend)
        try:
            with self.assertRaisesRegex(
                AgentGatewayError,
                "diagnostic scope requests 1025 items; maximum is 1024",
            ):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.20",
                        "intent": "diagnosis-only",
                        "entry_operation": "debug_run",
                        "entry_arguments": {
                            "files": [
                                f"/tmp/diagnostic-{index}.txt"
                                for index in range(1024)
                            ],
                        },
                    },
                    task_id="target-clock-diagnostic-scope",
                    operation_id="target-clock-diagnostic-scope-start",
                )
        finally:
            service.close()

        self.assertEqual(backend.calls, [])

    def test_execute_rejects_adapter_owned_multi_target_scope(self) -> None:
        service = RuntimeMcpService(MultiTargetDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "comparison-scope",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="adapter-owned-multi-target-scope",
                operation_id="adapter-owned-multi-target-scope-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["coverage"]["evaluable"], 0)
        self.assertEqual(receipt["results"][0]["result_id"], "target-scope")
        self.assertEqual(
            receipt["results"][0]["gap"],
            "runtime_target_scope_not_visible",
        )

    def test_execute_marks_a_requested_but_missing_target_not_checked(self) -> None:
        service = RuntimeMcpService(MissingTargetDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {
                            "ip": "192.0.2.20",
                            "role": "reference",
                            "target_id": "reference",
                        },
                        {
                            "ip": "192.0.2.21",
                            "role": "candidate",
                            "target_id": "candidate",
                        },
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="missing-target-diagnosis",
                operation_id="missing-target-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        results = {item["result_id"]: item for item in receipt["results"]}
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["coverage"]["requested"], 3)
        self.assertEqual(receipt["coverage"]["evaluable"], 2)
        self.assertEqual(receipt["coverage"]["not_checked"], 1)
        self.assertEqual(results["target-1-version"]["status"], "not_checked")
        self.assertEqual(results["target-1-version"]["gap"], "result_not_visible")
        self.assertEqual(results["target-2-version"]["status"], "available")

    def test_execute_reconciles_symmetric_dual_target_identities(self) -> None:
        service = RuntimeMcpService(SymmetricMultiTargetDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {"ip": "192.0.2.20"},
                        {"ip": "192.0.2.21"},
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="symmetric-target-diagnosis",
                operation_id="symmetric-target-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["coverage"]["evaluable"], 3)

    def test_execute_marks_a_missing_multi_target_comparison_not_checked(self) -> None:
        service = RuntimeMcpService(MissingComparisonDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {
                            "ip": "192.0.2.20",
                            "role": "reference",
                            "target_id": "reference",
                        },
                        {
                            "ip": "192.0.2.21",
                            "role": "candidate",
                            "target_id": "candidate",
                        },
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="missing-comparison-diagnosis",
                operation_id="missing-comparison-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        results = {item["result_id"]: item for item in receipt["results"]}
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["coverage"]["requested"], 3)
        self.assertEqual(receipt["coverage"]["evaluable"], 2)
        self.assertEqual(receipt["coverage"]["not_checked"], 1)
        self.assertEqual(results["comparison"]["status"], "not_checked")
        self.assertEqual(results["comparison"]["gap"], "comparison_not_visible")

    def test_execute_marks_missing_target_freshness_partial(self) -> None:
        service = RuntimeMcpService(MissingTargetFreshnessDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {
                            "ip": "192.0.2.20",
                            "role": "reference",
                            "target_id": "reference",
                        },
                        {
                            "ip": "192.0.2.21",
                            "role": "candidate",
                            "target_id": "candidate",
                        },
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="missing-target-freshness",
                operation_id="missing-target-freshness-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["coverage"]["evaluable"], 3)
        self.assertEqual(receipt["freshness"]["status"], "partial")
        self.assertEqual(receipt["status"], "partial")
        self.assertIn("freshness_partial", receipt["gaps"])
        self.assertIn(
            {"target_id": "candidate", "dimension": "freshness"},
            receipt["freshness"]["unavailable_dimensions"],
        )

    def test_execute_does_not_allow_top_level_freshness_to_mask_a_target_gap(
        self,
    ) -> None:
        service = RuntimeMcpService(
            TopLevelFreshMissingTargetFreshnessDiagnosticBackend()
        )
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {
                            "ip": "192.0.2.20",
                            "role": "reference",
                            "target_id": "reference",
                        },
                        {
                            "ip": "192.0.2.21",
                            "role": "candidate",
                            "target_id": "candidate",
                        },
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="top-level-fresh-target-gap",
                operation_id="top-level-fresh-target-gap-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["freshness"]["status"], "partial")
        self.assertEqual(receipt["status"], "partial")

    def test_execute_does_not_aggregate_incomplete_target_freshness_to_fresh(
        self,
    ) -> None:
        service = RuntimeMcpService(IncompleteTargetFreshnessDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "targets": [
                        {
                            "ip": "192.0.2.20",
                            "role": "reference",
                            "target_id": "reference",
                        },
                        {
                            "ip": "192.0.2.21",
                            "role": "candidate",
                            "target_id": "candidate",
                        },
                    ],
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="incomplete-target-freshness",
                operation_id="incomplete-target-freshness-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["freshness"]["status"], "partial")
        self.assertFalse(receipt["freshness"]["complete"])
        self.assertIn(
            {
                "target_id": "candidate",
                "dimension": "freshness",
                "reason": "incomplete",
            },
            receipt["freshness"]["unavailable_dimensions"],
        )
        self.assertEqual(receipt["status"], "partial")

    def test_execute_fails_closed_when_redaction_removes_all_result_content(self) -> None:
        service = RuntimeMcpService(SecretOnlyDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="redacted-diagnosis",
                operation_id="redacted-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        encoded = json.dumps(turn, ensure_ascii=False)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["results"][0]["status"], "not_checked")
        self.assertIn("redaction", receipt["results"][0]["gap"])
        self.assertNotIn("must-not-leak", encoded)

    def test_execute_marks_bounded_diagnosis_text_partial_without_source_truncation(self) -> None:
        service = RuntimeMcpService(TruncatedDiagnosisTextBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="truncated-diagnosis-text",
                operation_id="truncated-diagnosis-text-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["results"][0]["projection_truncated"])
        self.assertFalse(receipt["truncated"])
        self.assertFalse(receipt["content_complete"])

    def test_execute_preserves_source_truncation_on_diagnosis_fallback(self) -> None:
        service = RuntimeMcpService(SourceTruncatedDiagnosisBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="source-truncated-diagnosis",
                operation_id="source-truncated-diagnosis-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "partial")
        self.assertTrue(receipt["truncated"])
        self.assertFalse(receipt["content_complete"])
        self.assertIn("content_truncated", receipt["gaps"])

    def test_persisted_diagnostic_receipt_has_an_aggregate_byte_budget(self) -> None:
        service = RuntimeMcpService(OversizedDurableDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="durable-receipt-budget",
                operation_id="durable-receipt-budget-start",
            )
            events = service._test.context_runtime.repository.events(
                turn["run_id"]
            )
        finally:
            service.close()

        terminal = next(
            event for event in events if event["kind"] == "OperationTerminal"
        )
        receipt = terminal["payload"]["diagnostic_receipt"]
        self.assertLessEqual(encoded_size(receipt), DIAGNOSTIC_RECEIPT_MAX_BYTES)
        self.assertEqual(receipt["status"], "partial")
        self.assertFalse(receipt["truncated"])
        self.assertTrue(receipt["content_compacted"])
        self.assertFalse(receipt["content_complete"])

    def test_durable_receipt_bounds_extreme_requested_item_counts(self) -> None:
        receipt = DiagnosticReceipt.from_public_dict(
            {
                "receipt_id": "diagnostic-extreme-count",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": 1024,
                    "evaluable": 1024,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": f"mdb-{index}",
                        "kind": "mdb",
                        "request": f"lsprop Object{index}",
                        "status": "available",
                        "value": {"stdout_lines": ["x" * 1024]},
                    }
                    for index in range(1024)
                ],
                "freshness": {
                    "status": "fresh",
                    "observed_at": "2026-08-25T00:00:00Z",
                },
                "capabilities": {"mdbctl": "available"},
                "truncated": False,
                "content_complete": True,
                "evidence": [],
                "gaps": [],
            }
        ).bounded_for_persistence()

        public = receipt.to_public_dict()
        self.assertLessEqual(encoded_size(public), DIAGNOSTIC_RECEIPT_MAX_BYTES)
        self.assertEqual(public["status"], "complete")
        self.assertEqual(public["coverage"]["requested"], 1024)
        self.assertTrue(public["coverage"]["complete"])
        self.assertEqual(public["coverage"]["visible_evaluable"], 64)
        self.assertEqual(public["coverage"]["visible_not_checked"], 960)
        self.assertEqual(public["coverage"]["compacted"], 1024)
        self.assertEqual(len(public["results"]), 64)
        compacted_results = public["compacted_results"]
        self.assertEqual(len(compacted_results["result_ids"]), 960)
        self.assertEqual(compacted_results["result_ids"][-1], "mdb-1023")
        self.assertEqual(compacted_results["status"], "not_checked")
        self.assertEqual(
            compacted_results["gap"],
            "result_preview_compacted",
        )
        self.assertFalse(public["truncated"])
        self.assertTrue(public["content_complete"])
        round_trip = DiagnosticReceipt.from_public_dict(public)
        self.assertEqual(round_trip.status.value, "complete")
        self.assertTrue(round_trip.coverage.complete)
        self.assertNotIn("diagnostic_receipt_invalid", round_trip.gaps)

    def test_durable_receipt_compaction_preserves_a_log_outcome_summary(self) -> None:
        receipt = DiagnosticReceipt.from_public_dict(
            {
                "receipt_id": "diagnostic-log-summary",
                "operation": "debug_run",
                "status": "complete",
                "coverage": {
                    "requested": 1,
                    "evaluable": 1,
                    "unavailable": 0,
                    "not_checked": 0,
                    "complete": True,
                },
                "results": [
                    {
                        "result_id": "logs",
                        "kind": "bounded-logs",
                        "request": "storage,hwproxy,request timeout",
                        "status": "available",
                        "value": {
                            **{
                                f"filler-{index}": "x" * 20_000
                                for index in range(4)
                            },
                            "entries": [
                                {
                                    "path": "app.log",
                                    "line_count": 0,
                                    "lines_preview": [],
                                    "empty": True,
                                    "empty_message": "No matching log lines",
                                    "truncated": False,
                                    "content_complete": True,
                                }
                            ],
                        },
                    }
                ],
                "freshness": {
                    "status": "fresh",
                    "observed_at": "2026-08-25T00:00:00Z",
                },
                "capabilities": {},
                "truncated": False,
                "content_complete": True,
                "evidence": [],
                "gaps": [],
            }
        ).bounded_for_persistence()

        public = receipt.to_public_dict()
        encoded_result = json.dumps(public["results"][0]["value"])
        self.assertLessEqual(encoded_size(public), DIAGNOSTIC_RECEIPT_MAX_BYTES)
        self.assertIn("No matching log lines", encoded_result)
        self.assertNotIn("<compacted>", encoded_result)

    def test_operator_projects_session_outcome_from_the_persisted_run_outcome(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.76",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="run-session-outcome-authority",
            operation_id="run-session-outcome-start",
        )

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "operator-projection-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run tests"],
                    },
                },
            },
            task_id="run-session-outcome-authority",
            operation_id="run-session-outcome-finish",
        )
        before = self.service._test.context_runtime.repository.events(waiting["run_id"])
        recorded = self.service.call_tool(
            "session_outcome_record",
            {"case_id": waiting["run_id"]},
            task_id="run-session-outcome-authority",
            operation_id="run-session-outcome-project",
        )
        after = self.service._test.context_runtime.repository.events(waiting["run_id"])

        self.assertEqual(recorded["case_id"], waiting["run_id"])
        self.assertEqual(recorded["outcome"], final["outcome"]["status"])
        self.assertEqual(after, before)
        self.assertEqual(
            self.service.session_outcome_service.status()["outcome_count"],
            1,
        )

    def test_existing_legacy_case_can_still_record_a_session_outcome(self) -> None:
        result = self.service.call_tool(
            "debug_run",
            {
                "case_id": "run-legacy-session-outcome",
                "ip": "192.0.2.77",
                "intent": "diagnosis-only",
                "final_purpose": "preserve legacy governance compatibility",
                "deadline": 10,
            },
            task_id="legacy-session-outcome",
            operation_id="legacy-session-outcome-debug",
        )
        case_id = str(result.envelope["case_id"])

        recorded = self.service.call_tool(
            "session_outcome_record",
            {
                "session_id": "legacy-session-outcome",
                "case_id": case_id,
                "replay_fingerprint": "sha256:" + "a" * 64,
                "workflow": "diagnosis-only",
                "domain": "debug",
                "outcome": "completed",
                "summary": "legacy diagnosis completed",
            },
            task_id="legacy-session-outcome",
            operation_id="legacy-session-outcome-record",
        )

        self.assertEqual(recorded["case_id"], case_id)

    def test_gate_submission_does_not_coerce_numeric_summary_to_text(self) -> None:
        waiting = self.service.semantic_runtime.execute(
            StartRun(
                target="192.0.2.81",
                intent="diagnose-and-fix",
                purpose="reject an invalid typed Gate response",
                delivery_strategy="source-only",
                command_id="typed-gate-schema-start",
                input_digest="",
            ),
            task_id="typed-gate-schema",
            operation_id="typed-gate-schema-start",
        )
        assert waiting.gate is not None

        with self.assertRaisesRegex(GateConflict, "summary has the wrong type"):
            self.service.semantic_runtime.execute(
                SubmitGate(
                    run_id=waiting.run_id,
                    gate_id=waiting.gate.gate_id,
                    gate_version=waiting.gate.version,
                    schema_digest=waiting.gate.schema_digest,
                    submission_id="typed-gate-schema-submit",
                    command_id="typed-gate-schema-submit",
                    input_digest="",
                    response={
                        "status": "completed",
                        "summary": 42,
                        "payload": {
                            "source_revision": "typed-gate-schema-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["run regression tests"],
                        },
                    },
                ),
                task_id="typed-gate-schema",
                operation_id="typed-gate-schema-submit",
            )

    def test_typed_runtime_normalizes_gate_input_before_command_replay(self) -> None:
        start = StartRun(
            target="192.0.2.78",
            intent="diagnose-and-fix",
            purpose="normalize a typed Gate submission",
            delivery_strategy="source-only",
            command_id="typed-normalized-start",
            input_digest="",
        )
        waiting = self.service.semantic_runtime.execute(
            start,
            task_id="typed-normalized-start",
            operation_id="typed-normalized-start",
        )
        self.assertIsInstance(waiting, RunTurn)
        assert waiting.gate is not None
        gate = waiting.gate
        first_command = SubmitGate(
            run_id=waiting.run_id,
            response={
                "status": "completed",
                "summary": " source repair completed ",
                "payload": {
                    "source_revision": "typed-normalized-source",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["run regression tests"],
                },
            },
            gate_id=gate.gate_id,
            gate_version=gate.version,
            schema_digest=gate.schema_digest,
            submission_id="typed-normalized-submission",
            command_id="typed-normalized-submission",
            input_digest="",
        )
        first = self.service.semantic_runtime.execute(
            first_command,
            task_id="typed-normalized-first",
            operation_id="typed-normalized-first",
        )
        replayed = self.service.semantic_runtime.execute(
            SubmitGate(
                **{
                    **first_command.__dict__,
                    "response": {
                        **dict(first_command.response),
                        "summary": "source repair completed",
                    },
                }
            ),
            task_id="typed-normalized-replay",
            operation_id="typed-normalized-replay",
        )

        self.assertIsInstance(first, RunTurn)
        self.assertEqual(first.state, "completed")
        replayed_public = replayed.to_public_dict()
        self.assertEqual(
            replayed_public.pop("progress"),
            {"status": "no_progress", "reason": "unchanged_command_replayed"},
        )
        self.assertEqual(replayed_public, first.to_public_dict())
        projection = self.service._test.context_runtime.read_case(waiting.run_id)
        self.assertEqual(
            sum(
                item.get("command_id") == "typed-normalized-submission"
                for item in projection["run_decisions"]
            ),
            1,
        )

    def test_duplicate_respond_reports_no_progress_without_a_new_transition(
        self,
    ) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.79",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="duplicate-respond",
            operation_id="duplicate-respond-start",
        )
        response = {
            "kind": "respond",
            "run_id": waiting["run_id"],
            **gate_binding(waiting),
            "submission_id": "duplicate-respond-submission",
            "response": {
                "status": "completed",
                "summary": "source repair completed",
                "payload": {
                    "source_revision": "duplicate-respond-source",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["run regression tests"],
                },
            },
        }
        completed = self.service.call_exposed_tool(
            "execute",
            response,
            task_id="duplicate-respond",
            operation_id="duplicate-respond-first",
        )
        before = self.service._test.context_runtime.read_case(waiting["run_id"])

        replayed = self.service.call_exposed_tool(
            "execute",
            response,
            task_id="duplicate-respond-replay",
            operation_id="duplicate-respond-second",
        )
        after = self.service._test.context_runtime.read_case(waiting["run_id"])

        self.assertEqual(completed["state"], "completed")
        self.assertIsNone(completed["next_action"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(
            replayed["progress"],
            {"status": "no_progress", "reason": "unchanged_command_replayed"},
        )
        self.assertTrue(
            replayed["interaction_telemetry"]["no_progress_retry"]
        )
        self.assertIsNone(replayed["next_action"])

    def test_replaying_an_old_start_command_returns_the_current_turn(self) -> None:
        action = {
            "kind": "start",
            "target": "192.0.2.80",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
        }
        waiting = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="current-turn-start",
            operation_id="current-turn-start-command",
        )
        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "submission_id": "current-turn-submission",
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "current-turn-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run regression tests"],
                    },
                },
            },
            task_id="current-turn-submit",
            operation_id="current-turn-submit",
        )

        replayed_start = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="current-turn-replay",
            operation_id="current-turn-start-command",
        )

        self.assertEqual(final["state"], "completed")
        self.assertEqual(replayed_start["state"], "completed")
        self.assertEqual(replayed_start["outcome"], final["outcome"])
        self.assertIsNone(replayed_start["gate"])

    def test_source_only_gate_response_is_carried_by_the_gate_submission(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.68",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="native-source-phase",
            operation_id="native-source-phase-start",
        )

        self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "submission_id": "native-source-phase-submission",
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "native-source-phase",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run regression tests"],
                    },
                },
            },
            task_id="native-source-phase",
            operation_id="native-source-phase-respond",
        )

        events = self.service._test.context_runtime.repository.events(waiting["run_id"])
        submission = next(
            event for event in events if event["kind"] == "RunGateSubmitted"
        )
        self.assertEqual(submission["payload"]["actor"], "openubmc-developer")
        self.assertEqual(
            submission["payload"]["phase"]["phase_type"],
            "developer.change",
        )
        self.assertEqual(
            sum(event["kind"] == "RunPhaseRecorded" for event in events),
            0,
        )
        self.assertFalse(
            any(
                event["kind"] == "OperationProgressed"
                and isinstance(event["payload"].get("phase_record"), dict)
                for event in events
            )
        )
        self.assertFalse(
            any(
                event["kind"] == "OperationAccepted"
                and event["payload"].get("operation")
                in {"phase_record", "workflow.next"}
                for event in events
            )
        )
        self.assertEqual(
            self.service.session_outcome_service.status()["outcome_count"],
            0,
        )

    def test_source_only_terminal_response_is_one_complete_run_decision(self) -> None:
        for response_status in ("completed", "failed"):
            with self.subTest(response_status=response_status):
                repository = RecordingCommitRepository()
                service = RuntimeMcpService(
                    SemanticBackend(),
                    context_repository=repository,
                )
                try:
                    waiting = service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "start",
                            "target": "192.0.2.69",
                            "intent": "diagnose-and-fix",
                            "delivery_strategy": "source-only",
                        },
                        task_id=f"atomic-source-{response_status}",
                        operation_id=f"atomic-source-{response_status}-start",
                    )
                    payload = (
                        {
                            "source_revision": f"atomic-{response_status}",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["run regression tests"],
                        }
                        if response_status == "completed"
                        else {}
                    )
                    final = service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "respond",
                            "run_id": waiting["run_id"],
                            **gate_binding(waiting),
                            "submission_id": f"atomic-{response_status}-submission",
                            "response": {
                                "status": response_status,
                                "summary": f"source repair {response_status}",
                                "payload": payload,
                            },
                        },
                        task_id=f"atomic-source-{response_status}",
                        operation_id=f"atomic-source-{response_status}-respond",
                    )
                    events = repository.events(waiting["run_id"])
                finally:
                    service.close()

                decision = next(
                    event
                    for event in events
                    if event["kind"] == "RunDecisionCommitted"
                    and event["payload"].get("command_id")
                    == f"atomic-{response_status}-submission"
                )
                self.assertEqual(decision["payload"]["turn"]["state"], response_status)
                self.assertEqual(
                    decision["payload"]["turn"]["outcome"]["status"],
                    response_status,
                )
                self.assertEqual(
                    decision["payload"]["turn"]["facts"],
                    final["facts"],
                )
                self.assertEqual(
                    decision["payload"]["turn"]["gaps"],
                    final["gaps"],
                )
                self.assertEqual(final["state"], response_status)

    def test_cancellation_is_one_complete_run_decision(self) -> None:
        repository = RecordingCommitRepository()
        service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
        )
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.70",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="atomic-cancel",
                operation_id="atomic-cancel-start",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "control",
                    "run_id": waiting["run_id"],
                    "command": "cancel",
                    "submission_id": "atomic-cancel-submission",
                    **gate_binding(waiting),
                },
                task_id="atomic-cancel",
                operation_id="atomic-cancel-control",
            )
            events = repository.events(waiting["run_id"])
        finally:
            service.close()

        decision = next(
            event
            for event in events
            if event["kind"] == "RunDecisionCommitted"
            and event["payload"].get("command_id") == "atomic-cancel-submission"
        )
        self.assertEqual(decision["payload"]["turn"]["state"], "cancelled")
        self.assertEqual(decision["payload"]["turn"]["outcome"]["status"], "cancelled")
        self.assertEqual(decision["payload"]["turn"]["facts"], final["facts"])
        self.assertEqual(decision["payload"]["turn"]["gaps"], final["gaps"])
        self.assertEqual(final["state"], "cancelled")

    def test_start_persists_effect_scheduling_then_returns_the_next_gate(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.71",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="atomic-start",
            operation_id="atomic-start-command",
        )

        projection = self.service._test.context_runtime.read_case(waiting["run_id"])
        decisions = [
            item
            for item in projection["run_decisions"]
            if item.get("command_id") == "atomic-start-command"
        ]

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["turn"]["state"], "running")
        self.assertEqual(
            decisions[0]["effect_intent"]["operation"],
            "debug_run",
        )
        self.assertEqual(projection["current_turn"]["state"], "waiting_response")
        projected_gate = dict(waiting["gate"])
        projected_gate.pop("submission_id", None)
        self.assertEqual(projection["current_turn"]["gate"], projected_gate)

    def test_build_source_submission_decision_contains_the_next_gate_turn(self) -> None:
        developer_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.72",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id="atomic-build-gate",
            operation_id="atomic-build-start",
        )

        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(developer_gate),
                "submission_id": "atomic-build-source-submission",
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "atomic-build-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and verify"],
                    },
                },
            },
            task_id="atomic-build-gate",
            operation_id="atomic-build-source",
        )

        projection = self.service._test.context_runtime.read_case(developer_gate["run_id"])
        decision = next(
            item
            for item in projection["run_decisions"]
            if item.get("command_id") == "atomic-build-source-submission"
        )

        self.assertEqual(decision["turn"]["state"], "waiting_response")
        projected_gate = dict(build_gate["gate"])
        projected_gate.pop("submission_id", None)
        self.assertEqual(decision["turn"]["gate"], projected_gate)
        self.assertEqual(decision["turn"]["gate"]["name"], "build.artifact")

    def test_live_patch_submission_replays_after_internal_effect_decisions(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.73",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
            },
            task_id="atomic-live-patch",
            operation_id="atomic-live-patch-start",
        )
        patch_file = self.artifact_root / "atomic-live-patch.lua"
        patch_file.write_bytes(b"return 'atomic-live-patch'\n")
        response = {
            "kind": "respond",
            "run_id": waiting["run_id"],
            **gate_binding(waiting),
            "submission_id": "atomic-live-patch-submission",
            "response": {
                "status": "completed",
                "summary": "live patch source is ready",
                "payload": {
                    "source_revision": "atomic-live-patch-source",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["fresh target verification"],
                    "artifact_ref": artifact_ref(
                        patch_file,
                        kind="openubmc-live-patch",
                        target="192.0.2.73",
                        run_id=waiting["run_id"],
                    ),
                    "remote_path": "/opt/bmc/apps/fix.lua",
                    "restart_scope": "skynet",
                },
            },
        }

        final = self.service.call_exposed_tool(
            "execute",
            response,
            task_id="atomic-live-patch",
            operation_id="atomic-live-patch-response",
        )
        replayed = self.service.call_exposed_tool(
            "execute",
            response,
            task_id="atomic-live-patch-replay",
            operation_id="atomic-live-patch-retry",
        )
        projection = self.service._test.context_runtime.read_case(waiting["run_id"])
        decisions = [
            item
            for item in projection["run_decisions"]
            if item.get("command_id") == "atomic-live-patch-submission"
        ]

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["turn"]["state"], "running")
        self.assertEqual(
            decisions[0]["effect_intent"]["operation"],
            "live_patch_run",
        )
        self.assertEqual(projection["current_turn"]["outcome"], final["outcome"])
        self.assertEqual(replayed["outcome"], final["outcome"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls].count("live_patch_run"),
            1,
        )

    def test_repeated_resume_with_no_progress_never_records_a_decision(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.74",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="atomic-resume",
            operation_id="atomic-resume-start",
        )
        action = {"kind": "resume", "run_id": waiting["run_id"]}

        before = self.service._test.context_runtime.read_case(waiting["run_id"])
        first = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="atomic-resume-first",
            operation_id="atomic-resume-first-command",
        )
        replayed = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="atomic-resume-replay",
            operation_id="atomic-resume-second-command",
        )
        projection = self.service._test.context_runtime.read_case(waiting["run_id"])
        decisions = [
            item
            for item in projection["run_decisions"]
            if item.get("command_id")
            in {"atomic-resume-first-command", "atomic-resume-second-command"}
        ]

        self.assertEqual(decisions, [])
        self.assertEqual(first["gate"], waiting["gate"])
        self.assertEqual(replayed["gate"], first["gate"])
        self.assertEqual(first["next_action"], waiting["next_action"])
        self.assertEqual(replayed["next_action"], first["next_action"])
        self.assertEqual(
            first["progress"],
            {"status": "no_progress", "reason": "response_required"},
        )
        self.assertEqual(replayed["progress"], first["progress"])
        self.assertEqual(projection["revision"], before["revision"])

    def test_reconcile_command_replays_without_repeating_the_effect(self) -> None:
        class FailOnceLivePatchSemanticBackend(SemanticBackend):
            def __init__(self) -> None:
                super().__init__()
                self.live_patch_attempts = 0

            def live_patch_run(self, task, arguments, context):
                self.live_patch_attempts += 1
                if self.live_patch_attempts <= 2:
                    self.calls.append(("live_patch_run", dict(arguments)))
                    raise OSError("live patch connection lost")
                return super().live_patch_run(task, arguments, context)

        backend = FailOnceLivePatchSemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "atomic-reconcile-live-patch.lua"
        patch_file.write_bytes(b"return 'atomic-reconcile'\n")
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.75",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="atomic-reconcile",
                operation_id="atomic-reconcile-start",
            )
            incident = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "source repair is ready",
                        "payload": {
                            "source_revision": "atomic-reconcile-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.75",
                                run_id=waiting["run_id"],
                            ),
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                    },
                },
                task_id="atomic-reconcile",
                operation_id="atomic-reconcile-response",
            )
            self.assertEqual(incident["state"], "incident")
            action = {
                "kind": "control",
                "run_id": waiting["run_id"],
                "command": "reconcile",
            }
            reconciled = service.call_exposed_tool(
                "execute",
                action,
                task_id="atomic-reconcile-first",
                operation_id="atomic-reconcile-command",
            )
            effect_calls = [
                name for name, _arguments in backend.calls
            ].count("live_patch_run")
            replayed = service.call_exposed_tool(
                "execute",
                action,
                task_id="atomic-reconcile-replay",
                operation_id="atomic-reconcile-retry-after-disconnect",
            )
        finally:
            service.close()

        self.assertEqual(reconciled["state"], "completed")
        self.assertEqual(replayed["outcome"], reconciled["outcome"])
        self.assertEqual(
            replayed["progress"],
            {"status": "no_progress", "reason": "unchanged_command_replayed"},
        )
        self.assertEqual(
            [name for name, _arguments in backend.calls].count("live_patch_run"),
            effect_calls,
        )

    def test_start_command_identity_reattaches_across_task_ids(self) -> None:
        action = {
            "kind": "start",
            "target": "192.0.2.59",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
            "purpose": "repair one source defect",
        }
        first = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="start-command-first-task",
            operation_id="start-command-shared-id",
        )
        replay = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="start-command-second-task",
            operation_id="start-command-shared-id",
        )
        projection = self.service._test.context_runtime.read_case(first["run_id"])
        events = self.service._test.context_runtime.repository.events(first["run_id"])

        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(replay["gate"], first["gate"])
        self.assertEqual(projection["start_command_id"], "start-command-shared-id")
        self.assertEqual(len(projection["start_input_digest"]), 64)
        self.assertEqual(sum(event["kind"] == "CaseOpened" for event in events), 1)
        self.assertEqual(
            [name for name, _arguments in self.backend.calls].count("debug_run"),
            1,
        )

    def test_start_command_identity_rejects_conflicting_input_without_mutating_the_run(self) -> None:
        action = {
            "kind": "start",
            "target": "192.0.2.60",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
            "purpose": "repair the original source defect",
        }
        first = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="start-command-conflict",
            operation_id="start-command-conflict-id",
        )
        before = self.service._test.context_runtime.read_case(first["run_id"])

        conflicting = dict(action)
        conflicting["target"] = "192.0.2.61"
        conflicting["purpose"] = "replace the original command input"
        with self.assertRaises(CommandConflict):
            self.service.call_exposed_tool(
                "execute",
                conflicting,
                task_id="start-command-conflict",
                operation_id="start-command-conflict-id",
            )

        after = self.service._test.context_runtime.read_case(first["run_id"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["targets"], before["targets"])
        self.assertEqual(after["final_purpose"], before["final_purpose"])

    def test_start_command_identity_reattaches_after_sqlite_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "start-command.sqlite3"
            blobs = root / "start-command-blobs"
            action = {
                "kind": "start",
                "target": "192.0.2.66",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "persist the Run command identity",
            }
            first_backend = SemanticBackend()
            first_service = RuntimeMcpService(
                first_backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                first = first_service.call_exposed_tool(
                    "execute",
                    action,
                    task_id="start-command-sqlite-first",
                    operation_id="start-command-sqlite-id",
                )
            finally:
                first_service.close()

            second_backend = SemanticBackend()
            second_service = RuntimeMcpService(
                second_backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                replay = second_service.call_exposed_tool(
                    "execute",
                    action,
                    task_id="start-command-sqlite-second",
                    operation_id="start-command-sqlite-id",
                )
                events = second_service._test.context_runtime.repository.events(
                    first["run_id"]
                )
            finally:
                second_service.close()

        self.assertEqual(replay["run_id"], first["run_id"])
        self.assertEqual(replay["gate"], first["gate"])
        self.assertEqual(replay["next_action"], first["next_action"])
        self.assertEqual(sum(event["kind"] == "CaseOpened" for event in events), 1)
        self.assertEqual(second_backend.calls, [])

    def test_start_command_conflict_survives_sqlite_restart_without_run_update(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "start-conflict.sqlite3"
            blobs = root / "start-conflict-blobs"
            action = {
                "kind": "start",
                "target": "192.0.2.67",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "preserve the original persisted input",
            }
            first_service = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                first = first_service.call_exposed_tool(
                    "execute",
                    action,
                    task_id="start-conflict-sqlite-first",
                    operation_id="start-conflict-sqlite-id",
                )
                before = first_service._test.context_runtime.read_case(first["run_id"])
            finally:
                first_service.close()

            second_service = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            conflicting = dict(action)
            conflicting["target"] = "192.0.2.68"
            conflicting["purpose"] = "replace the persisted input"
            try:
                with self.assertRaises(CommandConflict):
                    second_service.call_exposed_tool(
                        "execute",
                        conflicting,
                        task_id="start-conflict-sqlite-second",
                        operation_id="start-conflict-sqlite-id",
                    )
                after = second_service._test.context_runtime.read_case(first["run_id"])
            finally:
                second_service.close()

        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["targets"], before["targets"])
        self.assertEqual(after["final_purpose"], before["final_purpose"])

    def test_different_start_command_on_the_same_task_creates_an_independent_run(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.62",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="start-command-two-runs",
            operation_id="start-command-first-id",
        )
        first_before = self.service._test.context_runtime.read_case(first["run_id"])
        second = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.63",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="start-command-two-runs",
            operation_id="start-command-second-id",
        )
        first_after = self.service._test.context_runtime.read_case(first["run_id"])

        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(first_after["revision"], first_before["revision"])
        self.assertEqual(first_after["targets"], first_before["targets"])

    def test_execute_turn_soft_target_preserves_runtime_control_semantics(self) -> None:
        turn = AgentGateway(OversizedTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-turn",
            operation_id="oversized-turn-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["run_id"], "case-oversized-turn")
        self.assertEqual(turn["state"], "blocked")
        self.assertEqual(
            turn["gate"],
            {
                "kind": "blocker",
                "name": "oversized-blocker",
                "message": "message-" + "m" * 20_000,
            },
        )
        self.assertEqual(turn["next"], "next-" + "n" * 20_000)
        self.assertFalse(turn["content_compacted"])
        self.assertTrue(turn["projection_target_exceeded"])

    def test_execute_turn_soft_gate_target_preserves_runtime_gate_semantics(self) -> None:
        turn = AgentGateway(OversizedGateTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-gate",
            operation_id="oversized-gate-1",
        )

        self.assertEqual(turn["state"], "waiting_response")
        self.assertEqual(turn["gate"]["gate_id"], "gate-oversized")
        self.assertEqual(
            turn["gate"]["input_schema"]["description"],
            "x" * 5_000,
        )
        self.assertLess(encoded_size(turn), TURN_MAX_BYTES)
        self.assertTrue(turn["gate_projection_target_exceeded"])
        self.assertFalse(turn.get("projection_target_exceeded", False))
        self.assertFalse(turn["manual_narrowing_required"])
        self.assertFalse(turn["budget_blocker"])
        gate_metrics = turn["projection_metrics"]["gate_schema_soft_target"]
        self.assertEqual(
            gate_metrics["target_exceeded_causes"][0]["field"],
            "gate.input_schema",
        )

    def test_large_gate_compaction_preserves_no_progress_semantics(self) -> None:
        turn = AgentGateway(OversizedNoProgressGateTurnRuntime()).execute(
            {"kind": "resume", "run_id": "case-oversized-no-progress-gate"},
            task_id="oversized-no-progress-gate",
            operation_id="oversized-no-progress-gate-1",
        )

        self.assertTrue(turn["response_required"])
        self.assertEqual(
            turn["progress"],
            {"status": "no_progress", "reason": "response_required"},
        )
        self.assertEqual(turn["gate"]["gate_id"], "gate-oversized-no-progress")
        self.assertTrue(turn["projection_target_exceeded"])
        self.assertEqual(
            turn["interaction_telemetry"],
            {
                "classification": "no_progress_retry",
                "preflight_failure": False,
                "no_progress_retry": True,
                "incident_present": False,
                "operator_attention_required": False,
                "projection_target_exceeded": True,
                "budget_blocker": False,
            },
        )

    def test_turn_telemetry_reaches_a_fixed_point_at_the_soft_target(self) -> None:
        projector = ResultProjector()

        def projected(padding: int) -> dict[str, object]:
            return projector.turn(
                RunTurn(
                    run_id="run-telemetry-fixed-point",
                    state="waiting_response",
                    gate={"kind": "blocker", "message": "x" * padding},
                    response_required=True,
                    progress={"status": "no_progress", "reason": "response_required"},
                )
            )

        low = 1
        high = TURN_MAX_BYTES * 2
        while low < high:
            midpoint = (low + high) // 2
            if encoded_size(projected(midpoint)) > TURN_MAX_BYTES:
                high = midpoint
            else:
                low = midpoint + 1

        turn = projected(low)
        self.assertTrue(turn["projection_target_exceeded"])
        self.assertTrue(
            turn["interaction_telemetry"]["projection_target_exceeded"]
        )

    def test_execute_turn_soft_target_preserves_runtime_incident_semantics(self) -> None:
        turn = AgentGateway(OversizedIncidentTurnRuntime()).execute(
            {
                "kind": "resume",
                "run_id": "case-oversized-incident",
            },
            task_id="oversized-incident",
            operation_id="oversized-incident-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["state"], "incident")
        self.assertEqual(
            turn["incident"],
            {
                "incident_id": "incident-oversized",
                "code": "artifact_reference_invalid",
                "message": "incident-" + "i" * 20_000,
                "effect_id": "effect-oversized",
                "recovery_path": "correction_then_resume",
                "allowed_commands": ["resume", "cancel"],
                "operator_action": (
                    "restore the digest-bound artifact content, then resume the Run"
                ),
                "recoverable": True,
            },
        )
        self.assertTrue(turn["projection_target_exceeded"])
        self.assertFalse(turn["manual_narrowing_required"])
        self.assertFalse(turn["budget_blocker"])
        self.assertEqual(
            turn["projection_metrics"]["soft_target"]
            ["target_exceeded_causes"][0]["field"],
            "incident",
        )
        self.assertEqual(
            turn["interaction_telemetry"],
            {
                "classification": "incident",
                "preflight_failure": False,
                "no_progress_retry": False,
                "incident_present": True,
                "operator_attention_required": True,
                "projection_target_exceeded": True,
                "budget_blocker": False,
            },
        )

    def test_execute_turn_budget_preserves_diagnostic_receipt_semantics(self) -> None:
        turn = AgentGateway(OversizedDiagnosticTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-diagnostic",
            operation_id="oversized-diagnostic-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["state"], "completed")
        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["agent_acceptance"], "complete")
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["coverage"]["requested"], 20)
        self.assertEqual(receipt["coverage"]["evaluable"], 20)
        self.assertEqual(receipt["coverage"]["not_checked"], 0)
        self.assertFalse(receipt["truncated"])
        self.assertTrue(receipt["content_complete"])
        self.assertEqual(
            [item["result_id"] for item in receipt["results"]],
            [f"result-{index}" for index in range(20)],
        )
        self.assertTrue(all(item["status"] == "available" for item in receipt["results"]))
        self.assertTrue(
            all(
                item.get("projection_truncated") is not True
                for item in receipt["results"]
            )
        )
        self.assertTrue(all("value" in item for item in receipt["results"]))
        self.assertTrue(
            all(
                len(item["value"]["field-0"]) == 20_000
                for item in receipt["results"]
            )
        )
        self.assertEqual(len(receipt["evidence"]), 8)
        self.assertTrue(
            all(
                item["evidence_id"].startswith("evidence-")
                for item in receipt["evidence"]
            )
        )
        self.assertNotIn("diagnostic_receipt_compacted", receipt["gaps"])
        self.assertNotIn("content_truncated", receipt["gaps"])
        self.assertFalse(turn["content_compacted"])
        self.assertTrue(turn["projection_target_exceeded"])
        target_metrics = turn["projection_metrics"]["soft_target"]
        self.assertEqual(target_metrics["target_bytes"], TURN_MAX_BYTES)
        self.assertGreater(target_metrics["full_bytes"], TURN_MAX_BYTES)
        self.assertEqual(
            target_metrics["overage_bytes"],
            target_metrics["full_bytes"] - target_metrics["target_bytes"],
        )
        self.assertEqual(
            target_metrics["target_exceeded_causes"][0]["field"],
            "diagnostic_receipt",
        )

    def test_terminal_turn_references_an_unchanged_previously_presented_receipt(
        self,
    ) -> None:
        gateway = AgentGateway(RepeatedAcceptedDiagnosticTurnRuntime())
        initial = gateway.execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
            },
            task_id="repeated-accepted-diagnostic",
            operation_id="repeated-accepted-diagnostic-start",
        )
        terminal = gateway.execute(
            {
                "kind": "respond",
                "run_id": "case-repeated-accepted-diagnostic",
                "gate_id": "gate-developer-change",
                "gate_version": 1,
                "schema_digest": "sha256:" + "a" * 64,
                "submission_id": "accepted-diagnostic",
                "response": {
                    "status": "completed",
                    "summary": "developer change completed",
                    "payload": {},
                },
            },
            task_id="repeated-accepted-diagnostic",
            operation_id="repeated-accepted-diagnostic-resume",
        )

        self.assertIn("diagnostic_receipt", initial)
        self.assertNotIn("diagnostic_receipt_ref", initial)
        self.assertNotIn("diagnostic_receipt", terminal)
        reference = terminal["diagnostic_receipt_ref"]
        self.assertEqual(reference["receipt_id"], "diagnostic-oversized")
        self.assertTrue(reference["digest"].startswith("sha256:"))
        self.assertEqual(reference["agent_acceptance"], "complete")
        self.assertEqual(reference["coverage"]["requested"], 20)
        self.assertEqual(reference["coverage"]["evaluable"], 20)
        self.assertEqual(reference["result_ids"], [f"result-{index}" for index in range(20)])
        self.assertEqual(len(reference["evidence_ids"]), 8)
        self.assertEqual(
            reference["reconstruction"],
            {
                "authority": "runtime-core",
                "run_id": "case-repeated-accepted-diagnostic",
                "field": "diagnostic_receipt",
                "digest": reference["digest"],
            },
        )
        self.assertEqual(terminal["outcome"]["status"], "completed")
        self.assertEqual(
            terminal["outcome"]["acceptance"],
            [{"requirement_id": "runtime", "status": "passed"}],
        )
        metrics = terminal["projection_metrics"]["diagnostic_receipt"]
        self.assertTrue(metrics["repeated_reference"])
        self.assertEqual(metrics["repeated_fields"], ["diagnostic_receipt"])
        self.assertGreater(metrics["full_bytes"], metrics["reference_bytes"])
        self.assertEqual(
            metrics["saved_bytes"],
            metrics["full_bytes"] - metrics["reference_bytes"],
        )
        self.assertEqual(
            metrics["target_exceeded_causes"],
            [{"field": "diagnostic_receipt", "bytes": metrics["full_bytes"]}],
        )
        self.assertTrue(terminal["projection_compacted"])
        self.assertFalse(terminal["manual_narrowing_required"])
        self.assertFalse(terminal["budget_blocker"])
        rendered = render_execute_turn_text(terminal)
        self.assertIn("DiagnosticReceiptRef", rendered)
        self.assertIn("receipt_id=diagnostic-oversized", rendered)
        self.assertIn(reference["digest"], rendered)
        self.assertIn("Outcome status=completed", rendered)

    def test_retried_one_shot_terminal_turn_keeps_the_complete_receipt(self) -> None:
        gateway = AgentGateway(OneShotAcceptedDiagnosticTurnRuntime())
        action = {
            "kind": "start",
            "target": "192.0.2.20",
            "intent": "diagnosis-only",
        }
        first = gateway.execute(
            action,
            task_id="one-shot-diagnostic",
            operation_id="one-shot-diagnostic-start",
        )
        retry = gateway.execute(
            action,
            task_id="one-shot-diagnostic",
            operation_id="one-shot-diagnostic-start",
        )

        self.assertIn("diagnostic_receipt", first)
        self.assertIn("diagnostic_receipt", retry)
        self.assertNotIn("diagnostic_receipt_ref", first)
        self.assertNotIn("diagnostic_receipt_ref", retry)

    def test_terminal_turn_does_not_reference_receipt_across_task_ownership(self) -> None:
        gateway = AgentGateway(RepeatedAcceptedDiagnosticTurnRuntime())
        gateway.execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
            },
            task_id="task-a",
            operation_id="task-a-start",
        )
        terminal = gateway.execute(
            {"kind": "resume", "run_id": "case-repeated-accepted-diagnostic"},
            task_id="task-b",
            operation_id="task-b-resume",
        )

        self.assertIn("diagnostic_receipt", terminal)
        self.assertNotIn("diagnostic_receipt_ref", terminal)

    def test_terminal_turn_does_not_reference_a_stale_presentation_record(self) -> None:
        runtime = RepeatedAcceptedDiagnosticTurnRuntime()
        AgentGateway(runtime).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
            },
            task_id="stale-presentation",
            operation_id="stale-presentation-start",
        )

        terminal = AgentGateway(runtime).execute(
            {
                "kind": "respond",
                "run_id": "case-repeated-accepted-diagnostic",
                "gate_id": "gate-developer-change",
                "gate_version": 1,
                "schema_digest": "sha256:" + "a" * 64,
                "submission_id": "stale-presentation",
                "response": {
                    "status": "completed",
                    "summary": "developer change completed",
                    "payload": {},
                },
            },
            task_id="stale-presentation",
            operation_id="stale-presentation-resume",
        )

        self.assertIn("diagnostic_receipt", terminal)
        self.assertNotIn("diagnostic_receipt_ref", terminal)

    def test_terminal_turn_never_references_an_incomplete_receipt(self) -> None:
        gateway = AgentGateway(
            RepeatedAcceptedDiagnosticTurnRuntime(incomplete_receipt=True)
        )
        gateway.execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
            },
            task_id="incomplete-receipt",
            operation_id="incomplete-receipt-start",
        )
        terminal = gateway.execute(
            {
                "kind": "respond",
                "run_id": "case-repeated-accepted-diagnostic",
                "gate_id": "gate-developer-change",
                "gate_version": 1,
                "schema_digest": "sha256:" + "a" * 64,
                "submission_id": "incomplete-receipt",
                "response": {
                    "status": "completed",
                    "summary": "developer change completed",
                    "payload": {},
                },
            },
            task_id="incomplete-receipt",
            operation_id="incomplete-receipt-resume",
        )

        self.assertNotEqual(terminal["diagnostic_receipt"]["status"], "complete")
        self.assertNotEqual(
            terminal["diagnostic_receipt"]["agent_acceptance"], "complete"
        )
        self.assertNotIn("diagnostic_receipt_ref", terminal)

    def test_terminal_turn_preserves_a_changed_diagnostic_receipt(self) -> None:
        gateway = AgentGateway(
            RepeatedAcceptedDiagnosticTurnRuntime(change_receipt=True)
        )
        gateway.execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
            },
            task_id="changed-diagnostic",
            operation_id="changed-diagnostic-start",
        )
        terminal = gateway.execute(
            {"kind": "resume", "run_id": "case-repeated-accepted-diagnostic"},
            task_id="changed-diagnostic",
            operation_id="changed-diagnostic-resume",
        )

        self.assertEqual(
            terminal["diagnostic_receipt"]["receipt_id"],
            "diagnostic-oversized-new-cycle",
        )
        self.assertNotIn("diagnostic_receipt_ref", terminal)

    def test_retried_changed_terminal_turn_keeps_the_complete_receipt(self) -> None:
        gateway = AgentGateway(
            RepeatedAcceptedDiagnosticTurnRuntime(change_receipt=True)
        )
        gateway.execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnose-and-fix",
            },
            task_id="changed-terminal-retry",
            operation_id="changed-terminal-retry-start",
        )
        action = {
            "kind": "respond",
            "run_id": "case-repeated-accepted-diagnostic",
            "gate_id": "gate-developer-change",
            "gate_version": 1,
            "schema_digest": "sha256:" + "a" * 64,
            "submission_id": "changed-terminal-retry",
            "response": {
                "status": "completed",
                "summary": "developer change completed",
                "payload": {},
            },
        }
        first = gateway.execute(
            action,
            task_id="changed-terminal-retry",
            operation_id="changed-terminal-retry-respond",
        )
        retry = gateway.execute(
            action,
            task_id="changed-terminal-retry",
            operation_id="changed-terminal-retry-respond",
        )

        self.assertIn("diagnostic_receipt", first)
        self.assertIn("diagnostic_receipt", retry)
        self.assertNotIn("diagnostic_receipt_ref", first)
        self.assertNotIn("diagnostic_receipt_ref", retry)

    def test_adapter_cannot_expand_diagnostic_scope_beyond_the_durable_contract(
        self,
    ) -> None:
        service = RuntimeMcpService(AdapterExpandedDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                },
                task_id="adapter-expanded-diagnostic-scope",
                operation_id="adapter-expanded-diagnostic-scope-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["coverage"]["requested"], 1)
        self.assertEqual(len(receipt["results"]), 1)
        self.assertEqual(receipt["results"][0]["kind"], "diagnostic-scope")
        self.assertEqual(
            receipt["results"][0]["gap"],
            "runtime_diagnostic_scope_exceeds_1024_result_identities",
        )

    def test_multi_target_adapter_defaults_cannot_expand_the_durable_scope(
        self,
    ) -> None:
        targets = [
            {
                "ip": "192.0.2.20",
                "role": "reference",
                "target_id": "reference",
            },
            {
                "ip": "192.0.2.21",
                "role": "candidate",
                "target_id": "candidate",
            },
        ]
        value = {
            "targets": [
                {
                    "target_id": target["target_id"],
                    "result": {
                        "request": {
                            "files": [
                                f"/tmp/{target['target_id']}-{index}.txt"
                                for index in range(600)
                            ],
                            "mdb_only": True,
                        }
                    },
                }
                for target in targets
            ]
        }

        receipt = build_diagnostic_receipt(
            "debug_run",
            value,
            {"targets": targets},
            (),
            closeout_stage="diagnosis",
        )

        self.assertIsNotNone(receipt)
        public = receipt.to_public_dict()
        self.assertEqual(public["status"], "blocked")
        self.assertEqual(public["coverage"]["requested"], 1)
        self.assertEqual(public["results"][0]["kind"], "diagnostic-scope")

    def test_duplicate_special_file_requests_receive_unique_result_identities(
        self,
    ) -> None:
        service = RuntimeMcpService(BoundedDiagnosticBackend())
        try:
            turn = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "intent": "diagnosis-only",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "files": ["/etc/version.json", "/etc/version.json"],
                        "mdb_only": True,
                    },
                },
                task_id="duplicate-special-file-identities",
                operation_id="duplicate-special-file-identities-start",
            )
        finally:
            service.close()

        receipt = turn["diagnostic_receipt"]
        result_ids = [item["result_id"] for item in receipt["results"]]
        self.assertEqual(result_ids, ["version", "version-2"])
        self.assertNotIn("diagnostic_result_identity_invalid", receipt["gaps"])

    def test_execute_turn_soft_target_preserves_evaluable_result_values(self) -> None:
        turn = AgentGateway(DeeplyNestedDiagnosticTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
            },
            task_id="deeply-nested-diagnostic",
            operation_id="deeply-nested-diagnostic-1",
        )

        receipt = turn["diagnostic_receipt"]
        encoded_results = json.dumps(receipt["results"])
        self.assertEqual(receipt["coverage"]["evaluable"], 4)
        self.assertNotIn("visible_evaluable", receipt["coverage"])
        self.assertIn("12.08.21.06", encoded_results)
        self.assertIn("mctpd request timeout", encoded_results)
        self.assertIn("bmc.kepler.mctpd service visible", encoded_results)
        self.assertIn("No matching log lines", encoded_results)
        self.assertNotIn("<compacted>", encoded_results)

    def test_execute_turn_preserves_a_persisted_diagnostic_summary(self) -> None:
        turn = AgentGateway(PersistedDiagnosticSummaryTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
            },
            task_id="persisted-diagnostic-summary",
            operation_id="persisted-diagnostic-summary-1",
        )

        encoded_results = json.dumps(turn["diagnostic_receipt"]["results"])
        self.assertIn("PluginRequestEx", encoded_results)
        self.assertIn("bmc.kepler.hwproxy", encoded_results)
        self.assertNotIn("<compacted>", encoded_results)

    def test_execute_turn_fails_closed_on_a_non_evaluable_runtime_receipt(
        self,
    ) -> None:
        turn = AgentGateway(NonEvaluableDiagnosticTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
            },
            task_id="non-evaluable-diagnostic",
            operation_id="non-evaluable-diagnostic-1",
        )

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "blocked")
        self.assertFalse(receipt["coverage"]["complete"])
        self.assertEqual(receipt["coverage"]["evaluable"], 0)
        self.assertEqual(receipt["coverage"]["not_checked"], 1)
        self.assertEqual(receipt["results"][0]["status"], "not_checked")
        self.assertIn("diagnostic_receipt_invalid", receipt["gaps"])

    def test_execute_turn_exceeds_the_soft_target_instead_of_rewriting_completion(
        self,
    ) -> None:
        turn = AgentGateway(UncompactableDiagnosticTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
            },
            task_id="uncompactable-diagnostic",
            operation_id="uncompactable-diagnostic-1",
        )

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "complete")
        self.assertTrue(receipt["coverage"]["complete"])
        self.assertEqual(receipt["results"][0]["status"], "available")
        self.assertIn(
            "substantive diagnostic evidence",
            json.dumps(receipt["results"]),
        )
        self.assertTrue(turn["projection_target_exceeded"])

    def test_execute_turn_soft_target_preserves_large_diagnostic_previews(self) -> None:
        turn = AgentGateway(OversizedDiagnosticTurnRuntime(128)).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
                "delivery_strategy": "source-only",
            },
            task_id="unrepresentable-diagnostic",
            operation_id="unrepresentable-diagnostic-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["state"], "completed")
        self.assertEqual(turn["diagnostic_receipt"]["status"], "complete")
        self.assertNotIn("content_compacted", turn["diagnostic_receipt"])
        self.assertEqual(len(turn["diagnostic_receipt"]["results"]), 128)
        self.assertEqual(
            len(
                turn["diagnostic_receipt"]["results"][0]["value"]["field-0"]
            ),
            20_000,
        )
        self.assertTrue(turn["projection_target_exceeded"])

    def test_gate_construction_preserves_schema_above_the_projection_target(self) -> None:
        oversized_schema = {"type": "object", "description": "x" * 5_000}

        gate = Gate(
            gate_id="gate-oversized-construction",
            version=1,
            name="developer.change",
            owner="openubmc-developer",
            input_schema=oversized_schema,
            schema_digest=hashlib.sha256(
                json.dumps(
                    oversized_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )

        self.assertEqual(gate.input_schema, oversized_schema)

    def test_execute_turn_soft_budget_never_rewrites_terminal_outcome(self) -> None:
        turn = AgentGateway(OversizedTerminalTurnRuntime(128)).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "intent": "diagnosis-only",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-terminal-outcome",
            operation_id="oversized-terminal-outcome-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(
            turn["outcome"],
            {
                "status": "completed",
                "summary": "terminal outcome remains Runtime-owned",
                "acceptance": OversizedTerminalTurnRuntime.acceptance,
            },
        )
        self.assertTrue(turn["projection_target_exceeded"])
        self.assertFalse(turn["budget_blocker"])
        self.assertNotIn("turn_exceeds_8kb_budget", turn["gaps"])
        self.assertEqual(
            turn["next"],
            "retain the Runtime-owned terminal instruction exactly",
        )

    def test_execute_start_reuses_observation_evidence_before_diagnosis(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.40",
                "selectors": [
                    {"id": "facts", "kind": "mdb", "queries": ["lsprop Object0"]}
                ],
            },
            task_id="receipt-reuse",
            operation_id="receipt-observe",
        )

        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.40",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "repair using the grounded observation",
                "observation_ref": receipt["observation_ref"],
            },
            task_id="receipt-reuse",
            operation_id="receipt-execute",
        )

        self.assertEqual(first["state"], "waiting_response")
        self.assertEqual(first["gate"]["name"], "diagnosis.acceptance")
        self.assertEqual(first["observation_ref"], receipt["observation_ref"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_collect"],
        )

    def test_read_only_effect_retries_same_identity_after_evidence_failure(self) -> None:
        backend = SemanticBackend()
        service = RuntimeMcpService(
            backend,
            blob_repository=FailOnceBlobRepository(),
        )
        action = {
            "kind": "start",
            "target": "192.0.2.84",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
        }
        try:
            waiting = service.call_exposed_tool(
                "execute",
                action,
                task_id="read-only-evidence-retry",
                operation_id="read-only-evidence-retry-start",
            )
            replayed = service.call_exposed_tool(
                "execute",
                action,
                task_id="read-only-evidence-retry-replay",
                operation_id="read-only-evidence-retry-start",
            )
            events = service._test.context_runtime.repository.events(waiting["run_id"])
            projection = service._test.context_runtime.read_case(waiting["run_id"])
        finally:
            service.close()

        debug_calls = [
            arguments
            for name, arguments in backend.calls
            if name == "debug_run"
        ]
        self.assertEqual(waiting["state"], "waiting_response")
        self.assertEqual(replayed["state"], "waiting_response")
        self.assertEqual(len(debug_calls), 2)
        operation_ids = {
            event["operation_id"]
            for event in events
            if event["kind"] == "OperationProgressed"
            and event["payload"].get("canonical_error", {}).get("code")
            == "evidence_not_persisted"
        }
        self.assertEqual(len(operation_ids), 1)
        effect_id = next(iter(operation_ids))
        self.assertTrue(
            any(
                event["kind"] == "EvidenceAttached"
                and event["operation_id"] == effect_id
                for event in events
            )
        )
        self.assertEqual(
            sum(
                event["kind"] == "EvidenceAttached"
                and event["operation_id"] == effect_id
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event["kind"] == "OperationTerminal"
                and event["operation_id"] == effect_id
                for event in events
            ),
            1,
        )
        operation = next(
            item
            for item in projection["operations"]
            if item["operation_id"] == effect_id
        )
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(operation["evidence_retry_generation"], 1)
        self.assertNotIn("canonical_error", operation)

    def test_concurrent_read_only_settlement_commits_one_terminal_decision(self) -> None:
        backend = BlockingOnceDebugSemanticBackend()
        service = RuntimeMcpService(backend)
        action = {
            "kind": "start",
            "target": "192.0.2.85",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
        }
        turns: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def execute(task_id: str) -> None:
            try:
                turns.append(
                    service.call_exposed_tool(
                        "execute",
                        action,
                        task_id=task_id,
                        operation_id="concurrent-read-only-settlement-start",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        first = threading.Thread(target=execute, args=("concurrent-read-only-a",))
        second = threading.Thread(target=execute, args=("concurrent-read-only-b",))
        try:
            first.start()
            self.assertTrue(backend.started.wait(timeout=1))
            second.start()
            time.sleep(0.05)
            backend.release.set()
            first.join(timeout=3)
            second.join(timeout=3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(turns), 2)
            run_id = turns[0]["run_id"]
            events = service._test.context_runtime.repository.events(run_id)
        finally:
            backend.release.set()
            first.join(timeout=1)
            second.join(timeout=1)
            service.close()

        terminal_effect_ids = [
            event["operation_id"]
            for event in events
            if event["kind"] == "OperationTerminal"
            and event["payload"].get("status") == "completed"
        ]
        self.assertEqual(len(terminal_effect_ids), 1)
        effect_id = terminal_effect_ids[0]
        self.assertEqual(
            sum(
                event["kind"] == "EvidenceAttached"
                and event["operation_id"] == effect_id
                for event in events
            ),
            1,
        )

    def test_stale_waiter_cannot_settle_as_a_new_evidence_retry_generation(
        self,
    ) -> None:
        backend = SequencedEvidenceRetryBackend()
        service = RuntimeMcpService(
            backend,
            blob_repository=FailOnceBlobRepository(),
        )
        engine = service._test.run_engine
        original_commit = engine._commit_effect_result  # noqa: SLF001
        original_ensure = service._test.effect_runner.ensure
        original_acknowledge = service._test.effect_runner.acknowledge
        scheduling_lock = threading.Lock()
        old_execution: list[object | None] = [None]
        old_waiters = [0]
        stale_thread_ident: list[int | None] = [None]
        both_old_waiters = threading.Event()
        allow_stale_waiter = threading.Event()
        stale_waiter_done = threading.Event()
        stale_before_ensure = threading.Event()
        allow_stale_ensure = threading.Event()
        new_execution_acknowledged = threading.Event()

        def ordered_commit(intent, execution, *, settlement_mode):
            leader = False
            stale_waiter = False
            with scheduling_lock:
                if old_execution[0] is None:
                    old_execution[0] = execution
                if execution is old_execution[0]:
                    old_waiters[0] += 1
                    leader = old_waiters[0] == 1
                    stale_waiter = old_waiters[0] == 2
                    if stale_waiter:
                        stale_thread_ident[0] = threading.get_ident()
                        both_old_waiters.set()
            if leader:
                self.assertTrue(both_old_waiters.wait(timeout=3))
            if stale_waiter:
                self.assertTrue(allow_stale_waiter.wait(timeout=3))
            committed = original_commit(
                intent,
                execution,
                settlement_mode=settlement_mode,
            )
            if stale_waiter:
                stale_waiter_done.set()
            return committed

        def ordered_ensure(
            intent,
            *,
            mode,
            settlement_generation=0,
            claim=None,
        ):
            if (
                threading.get_ident() == stale_thread_ident[0]
                and settlement_generation == 1
            ):
                stale_before_ensure.set()
                self.assertTrue(allow_stale_ensure.wait(timeout=3))
            return original_ensure(
                intent,
                mode=mode,
                settlement_generation=settlement_generation,
                claim=claim,
            )

        def ordered_acknowledge(
            intent,
            execution,
            *,
            retain_for_reattach,
        ) -> None:
            original_acknowledge(
                intent,
                execution,
                retain_for_reattach=retain_for_reattach,
            )
            if (
                execution is not old_execution[0]
                and not retain_for_reattach
            ):
                new_execution_acknowledged.set()

        engine._commit_effect_result = ordered_commit  # type: ignore[method-assign]
        service._test.effect_runner.ensure = ordered_ensure  # type: ignore[method-assign]
        service._test.effect_runner.acknowledge = (  # type: ignore[method-assign]
            ordered_acknowledge
        )
        action = {
            "kind": "start",
            "target": "192.0.2.86",
            "intent": "diagnose-and-fix",
            "delivery_strategy": "source-only",
            "deadline": 2.5,
        }
        turns: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def execute(task_id: str) -> None:
            try:
                turns.append(
                    service.call_exposed_tool(
                        "execute",
                        action,
                        task_id=task_id,
                        operation_id="stale-generation-start",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        first = threading.Thread(target=execute, args=("stale-generation-a",))
        second = threading.Thread(target=execute, args=("stale-generation-b",))
        try:
            first.start()
            self.assertTrue(backend.first_started.wait(timeout=1))
            second.start()
            backend.first_release.set()
            self.assertTrue(backend.second_started.wait(timeout=2))
            allow_stale_waiter.set()
            self.assertTrue(stale_waiter_done.wait(timeout=2))
            self.assertTrue(stale_before_ensure.wait(timeout=2))
            backend.second_release.set()
            self.assertTrue(new_execution_acknowledged.wait(timeout=2))
            allow_stale_ensure.set()
            first.join(timeout=4)
            second.join(timeout=4)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(turns), 2)
            events = service._test.context_runtime.repository.events(turns[0]["run_id"])
        finally:
            backend.first_release.set()
            backend.second_release.set()
            allow_stale_waiter.set()
            allow_stale_ensure.set()
            first.join(timeout=1)
            second.join(timeout=1)
            service.close()

        terminal_effect_ids = [
            event["operation_id"]
            for event in events
            if event["kind"] == "OperationTerminal"
            and event["payload"].get("status") == "completed"
        ]
        self.assertEqual(backend._attempt, 2)
        self.assertEqual(len(terminal_effect_ids), 1)
        effect_id = terminal_effect_ids[0]
        self.assertEqual(
            sum(
                event["kind"] == "EvidenceAttached"
                and event["operation_id"] == effect_id
                for event in events
            ),
            1,
        )

    def test_observation_ref_rejects_digest_tamper_and_target_mismatch(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.41",
                "selectors": [
                    {"id": "facts", "kind": "mdb", "queries": ["lsprop Object0"]}
                ],
            },
            task_id="observation-ref-validation",
            operation_id="observation-ref-observe",
        )
        tampered_ref = dict(receipt["observation_ref"])
        tampered_ref["digest"] = "sha256:" + "0" * 64

        with self.assertRaises(EvidenceUnavailable):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.41",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "observation_ref": tampered_ref,
                },
                task_id="observation-ref-validation",
                operation_id="observation-ref-tampered",
            )
        self.assertIsNone(
            self.service._test.context_runtime.repository.case_for_task(
                "observation-ref-validation"
            )
        )

        metadata_tampering = {
            "scope_digest": "sha256:" + "1" * 64,
            "observed_at": "2026-08-20T23:59:59Z",
            "target_fingerprint": "wrong-target-fingerprint",
            "target_epoch": 99,
        }
        for field, value in metadata_tampering.items():
            with self.subTest(field=field):
                changed = dict(receipt["observation_ref"])
                changed[field] = value
                task_id = f"observation-ref-{field}"
                with self.assertRaises(EvidenceUnavailable):
                    self.service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "start",
                            "target": "192.0.2.41",
                            "intent": "diagnose-and-fix",
                            "delivery_strategy": "source-only",
                            "observation_ref": changed,
                        },
                        task_id=task_id,
                        operation_id=f"{task_id}-start",
                    )
                self.assertIsNone(
                    self.service._test.context_runtime.repository.case_for_task(task_id)
                )

        with self.assertRaisesRegex(ValueError, "target does not match"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.99",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "observation_ref": receipt["observation_ref"],
                },
                task_id="observation-ref-validation",
                operation_id="observation-ref-wrong-target",
            )
        self.assertIsNone(
            self.service._test.context_runtime.repository.case_for_task(
                "observation-ref-validation"
            )
        )

    def test_expired_observation_ref_is_rejected_before_a_run_is_opened(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.42",
                "selectors": [
                    {"id": "facts", "kind": "mdb", "queries": ["lsprop Object0"]}
                ],
            },
            task_id="observation-expiry-observe",
            operation_id="observation-expiry-observe-1",
        )
        observed_clock = self.service._test.context_runtime.clock()
        self.service._test.context_runtime.clock = lambda: observed_clock + 16 * 60

        with self.assertRaisesRegex(ValueError, "older than"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.42",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "observation_ref": receipt["observation_ref"],
                },
                task_id="observation-expiry-run",
                operation_id="observation-expiry-run-1",
            )
        self.assertIsNone(
            self.service._test.context_runtime.repository.case_for_task(
                "observation-expiry-run"
            )
        )

    def test_gate_identity_and_submission_idempotency_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "gate.sqlite3"
            blobs = root / "gate-blobs"
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.43",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id="gate-restart",
                    operation_id="gate-restart-start",
                )
            finally:
                first.close()

            second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                recovered = second.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id="gate-restart-resumed",
                    operation_id="gate-restart-resume",
                )
                for field in ("gate_id", "gate_version", "schema_digest"):
                    self.assertEqual(recovered["gate"][field], waiting["gate"][field])

                with self.assertRaises(GateConflict):
                    second.call_exposed_tool(
                        "execute",
                        {
                            "kind": "respond",
                            "run_id": waiting["run_id"],
                            "gate_id": recovered["gate"]["gate_id"],
                            "gate_version": recovered["gate"]["gate_version"] + 1,
                            "schema_digest": recovered["gate"]["schema_digest"],
                            "submission_id": "source-repair-1",
                            "response": {
                                "status": "completed",
                                "summary": "source repair completed",
                                "payload": {
                                    "source_revision": "gate-restart",
                                    "authored_files": ["src/fix.lua"],
                                    "verification_plan": ["run regression tests"],
                                },
                            },
                        },
                        task_id="gate-restart-resumed",
                        operation_id="gate-restart-stale",
                    )

                response = {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    "gate_id": recovered["gate"]["gate_id"],
                    "gate_version": recovered["gate"]["gate_version"],
                    "schema_digest": recovered["gate"]["schema_digest"],
                    "submission_id": "source-repair-1",
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "gate-restart",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["run regression tests"],
                        },
                    },
                }
                final = second.call_exposed_tool(
                    "execute",
                    response,
                    task_id="gate-restart-resumed",
                    operation_id="gate-restart-submit",
                )
                duplicate = second.call_exposed_tool(
                    "execute",
                    response,
                    task_id="gate-restart-resumed",
                    operation_id="gate-restart-duplicate",
                )
                wrong_gate = json.loads(json.dumps(response))
                wrong_gate["gate_id"] = "gate-wrong-replay"
                with self.assertRaises(GateConflict):
                    second.call_exposed_tool(
                        "execute",
                        wrong_gate,
                        task_id="gate-restart-resumed",
                        operation_id="gate-restart-wrong-gate",
                    )
                conflicting = json.loads(json.dumps(response))
                conflicting["response"]["summary"] = "different source result"
                with self.assertRaises(CommandConflict):
                    second.call_exposed_tool(
                        "execute",
                        conflicting,
                        task_id="gate-restart-resumed",
                        operation_id="gate-restart-conflict",
                    )
                projection = second._test.context_runtime.read_case(waiting["run_id"])
            finally:
                second.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(duplicate["state"], "completed")
        self.assertEqual(final["outcome"], duplicate["outcome"])
        self.assertEqual(
            len(
                [
                    record
                    for record in projection["phase_records"]
                    if record.get("submission_id") == "source-repair-1"
                ]
            ),
            1,
        )

    def test_gate_submission_reattaches_after_a_concurrent_commit(self) -> None:
        repository = CommitThenConflictRepository("RunGateSubmitted")
        service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
        )
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.47",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="gate-concurrent-commit",
                operation_id="gate-concurrent-start",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "submission_id": "gate-concurrent-submission",
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "concurrent-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["run tests"],
                        },
                    },
                },
                task_id="gate-concurrent-commit",
                operation_id="gate-concurrent-response",
            )
        finally:
            service.close()

        self.assertTrue(repository.conflicted)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(
            sum(
                event["kind"] == "RunGateSubmitted"
                for event in repository.events(waiting["run_id"])
            ),
            1,
        )

    def test_gate_submission_reattaches_when_an_equivalent_request_wins_the_race(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.57",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="gate-race-window",
                operation_id="gate-race-window-start",
            )
            response = {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "submission_id": "gate-race-window-submission",
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "gate-race-window-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run tests"],
                    },
                },
            }
            final = service.call_exposed_tool(
                "execute",
                response,
                task_id="gate-race-window",
                operation_id="gate-race-window-response",
            )
            events = service._test.context_runtime.repository.events(waiting["run_id"])
        finally:
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(
            sum(event["kind"] == "RunGateSubmitted" for event in events),
            1,
        )

    def test_gate_open_reattaches_after_a_concurrent_commit(self) -> None:
        repository = CommitThenConflictRepository("RunGateOpened")
        service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
        )
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.50",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="gate-open-concurrent-commit",
                operation_id="gate-open-concurrent-start",
            )
            resumed = service.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": waiting["run_id"]},
                task_id="gate-open-concurrent-commit",
                operation_id="gate-open-concurrent-resume",
            )
        finally:
            service.close()

        self.assertTrue(repository.conflicted)
        self.assertEqual(waiting["state"], "waiting_response")
        self.assertEqual(resumed["gate"], waiting["gate"])
        self.assertEqual(
            sum(
                event["kind"] == "RunGateOpened"
                for event in repository.events(waiting["run_id"])
            ),
            1,
        )

    def test_terminal_outcome_reattaches_after_a_concurrent_commit(self) -> None:
        repository = CommitThenConflictRepository("RunOutcomeRecorded")
        service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
        )
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.48",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="outcome-concurrent-commit",
                operation_id="outcome-concurrent-start",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "submission_id": "outcome-concurrent-submission",
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "concurrent-outcome-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["run tests"],
                        },
                    },
                },
                task_id="outcome-concurrent-commit",
                operation_id="outcome-concurrent-response",
            )
        finally:
            service.close()

        self.assertTrue(repository.conflicted)
        self.assertEqual(final["state"], "completed")
        self.assertEqual(
            sum(
                event["kind"] == "RunOutcomeRecorded"
                for event in repository.events(waiting["run_id"])
            ),
            1,
        )

    def test_gate_submission_requires_the_complete_persisted_binding(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.45",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="gate-binding",
            operation_id="gate-binding-start",
        )
        response = {
            "kind": "respond",
            "run_id": waiting["run_id"],
            **gate_binding(waiting),
            "response": {
                "status": "completed",
                "summary": "source repair completed",
                "payload": {
                    "source_revision": "gate-binding",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["run regression tests"],
                },
            },
        }

        for field in ("gate_id", "gate_version", "schema_digest"):
            with self.subTest(missing=field):
                incomplete = dict(response)
                incomplete.pop(field)
                with self.assertRaises(AgentGatewayError):
                    self.service.call_exposed_tool(
                        "execute",
                        incomplete,
                        task_id="gate-binding",
                        operation_id=f"gate-binding-missing-{field}",
                    )

        wrong_digest = dict(response)
        wrong_digest["schema_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(GateConflict, "schema digest"):
            self.service.call_exposed_tool(
                "execute",
                wrong_digest,
                task_id="gate-binding",
                operation_id="gate-binding-wrong-digest",
            )

        undeclared = json.loads(json.dumps(response))
        undeclared["response"]["payload"]["unexpected"] = True
        with self.assertRaisesRegex(GateConflict, "undeclared fields"):
            self.service.call_exposed_tool(
                "execute",
                undeclared,
                task_id="gate-binding",
                operation_id="gate-binding-undeclared",
            )

    def test_persisted_gate_schema_survives_restart_and_code_schema_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "gate-schema.sqlite3"
            blobs = root / "gate-schema-blobs"
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.46",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id="gate-schema-restart",
                    operation_id="gate-schema-start",
                )
            finally:
                first.close()

            second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                with patch(
                    "openubmc_target_runtime.run_engine.gate_input_schema",
                    side_effect=AssertionError(
                        "persisted Gate must not be rebuilt from current code"
                    ),
                ):
                    recovered = second.call_exposed_tool(
                        "execute",
                        {"kind": "resume", "run_id": waiting["run_id"]},
                        task_id="gate-schema-restart-resume",
                        operation_id="gate-schema-resume",
                    )
            finally:
                second.close()

        self.assertEqual(recovered["gate"], waiting["gate"])

    def test_artifact_ref_is_bound_to_content_kind_target_run_and_provenance(self) -> None:
        developer_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.44",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id="artifact-ref-validation",
            operation_id="artifact-ref-start",
        )
        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(developer_gate),
                "submission_id": "artifact-source-1",
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "artifact-ref-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and verify"],
                    },
                },
            },
            task_id="artifact-ref-validation",
            operation_id="artifact-ref-source",
        )

        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product.hpm"
            artifact.write_bytes(b"verified firmware bytes")
            valid = artifact_ref(
                artifact,
                kind="openubmc-hpm",
                target="192.0.2.44",
                run_id=developer_gate["run_id"],
                version="2.0.0",
            )
            tampered = dict(valid)
            tampered["digest"] = "sha256:" + "0" * 64
            missing = dict(valid)
            missing["handle"] = str(Path(raw) / "missing.hpm")
            wrong_kind = dict(valid)
            wrong_kind["kind"] = "openubmc-live-patch"
            wrong_target = dict(valid)
            wrong_target["target"] = "192.0.2.99"
            wrong_run = dict(valid)
            wrong_run["run_id"] = "run-other"
            wrong_size = dict(valid)
            wrong_size["size"] = int(valid["size"]) + 1
            wrong_version = dict(valid)
            wrong_version["version"] = "9.9.9"
            unbound_artifact = Path(raw) / "unbound-product.hpm"
            unbound_artifact.write_bytes(b"unbound firmware bytes")
            missing_metadata = artifact_ref(
                unbound_artifact,
                kind="openubmc-hpm",
                target="192.0.2.44",
                run_id=developer_gate["run_id"],
            )
            missing_metadata["version"] = "2.0.0"
            missing_target = dict(valid)
            missing_target.pop("target")
            missing_run = dict(valid)
            missing_run.pop("run_id")
            missing_provenance = dict(valid)
            missing_provenance["provenance"] = ""
            wrong_provenance = dict(valid)
            wrong_provenance["provenance"] = "forged-unverified-producer"

            invalid_cases = (
                (
                    "missing-content",
                    missing,
                    ReferenceViolation,
                    "content is unavailable",
                ),
                ("wrong-kind", wrong_kind, GateConflict, "allowed value"),
                (
                    "wrong-target",
                    wrong_target,
                    ReferenceViolation,
                    "target does not match",
                ),
                (
                    "wrong-run",
                    wrong_run,
                    ReferenceViolation,
                    "run_id does not match",
                ),
                (
                    "wrong-size",
                    wrong_size,
                    ReferenceViolation,
                    "size does not match stored content",
                ),
                (
                    "tampered-content",
                    tampered,
                    ReferenceViolation,
                    "digest does not match stored content",
                ),
                (
                    "wrong-version",
                    wrong_version,
                    ReferenceViolation,
                    "version does not match artifact metadata",
                ),
                (
                    "missing-metadata",
                    missing_metadata,
                    ReferenceViolation,
                    "requires build artifact metadata",
                ),
                ("missing-target", missing_target, GateConflict, "required fields"),
                ("missing-run", missing_run, GateConflict, "required fields"),
                (
                    "missing-provenance",
                    missing_provenance,
                    GateConflict,
                    "must not be empty",
                ),
                (
                    "wrong-provenance",
                    wrong_provenance,
                    ReferenceViolation,
                    "provenance does not match artifact metadata",
                ),
            )
            for name, reference, error, message in invalid_cases:
                with self.subTest(name=name), self.assertRaisesRegex(error, message):
                    self.service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "respond",
                            "run_id": developer_gate["run_id"],
                            **gate_binding(build_gate),
                            "submission_id": f"artifact-build-{name}",
                            "response": {
                                "status": "completed",
                                "summary": "artifact built",
                                "payload": {
                                    "source_revision": "artifact-ref-source",
                                    "artifact_ref": reference,
                                },
                            },
                        },
                        task_id="artifact-ref-validation",
                        operation_id=f"artifact-ref-{name}",
                    )

            self.assertNotIn(
                "upgrade_run",
                [operation for operation, _arguments in self.backend.calls],
            )

            final = self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(build_gate),
                    "submission_id": "artifact-build-valid",
                    "response": {
                        "status": "completed",
                        "summary": "artifact built",
                        "payload": {
                            "source_revision": "artifact-ref-source",
                            "artifact_ref": valid,
                            **compiled_validation_payload("artifact-ref-valid"),
                        },
                    },
                },
                task_id="artifact-ref-validation",
                operation_id="artifact-ref-valid",
            )

        self.assertEqual(final["state"], "completed")

    def test_file_uri_artifact_uses_the_verified_decoded_path(self) -> None:
        developer_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.49",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id="artifact-file-uri",
            operation_id="artifact-file-uri-start",
        )
        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(developer_gate),
                "submission_id": "artifact-file-uri-source",
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "artifact-file-uri-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and verify"],
                    },
                },
            },
            task_id="artifact-file-uri",
            operation_id="artifact-file-uri-source",
        )

        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "product image.hpm"
            artifact.write_bytes(b"verified firmware bytes")
            reference = artifact_ref(
                artifact,
                kind="openubmc-hpm",
                target="192.0.2.49",
                run_id=developer_gate["run_id"],
                version="2.0.0",
            )
            reference["handle"] = artifact.as_uri()
            final = self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(build_gate),
                    "submission_id": "artifact-file-uri-build",
                    "response": {
                        "status": "completed",
                        "summary": "artifact built",
                        "payload": {
                            "source_revision": "artifact-file-uri-source",
                            "artifact_ref": reference,
                            **compiled_validation_payload("artifact-file-uri"),
                        },
                    },
                },
                task_id="artifact-file-uri",
                operation_id="artifact-file-uri-build",
            )

            upgrade_arguments = next(
                arguments
                for name, arguments in reversed(self.backend.calls)
                if name == "upgrade_run"
            )
            self.assertEqual(upgrade_arguments["artifact_path"], str(artifact))

        self.assertEqual(final["state"], "completed")

    def test_live_patch_artifact_is_revalidated_immediately_before_effect_dispatch(self) -> None:
        backend = SemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "dispatch-boundary-fix.lua"
        patch_file.write_bytes(b"return 'validated-content'\n")
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.58",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="artifact-dispatch-boundary",
                operation_id="artifact-dispatch-boundary-start",
            )
            transactions = service._test.context_runtime.repository
            original = transactions.stage

            def replace_after_persist(*args, **kwargs):
                result = original(*args, **kwargs)
                patch_file.write_bytes(b"return 'replaced-after-gate'\n")
                return result

            with patch.object(
                transactions,
                "stage",
                side_effect=replace_after_persist,
            ):
                blocked = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "artifact-dispatch-boundary-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.58",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                    task_id="artifact-dispatch-boundary",
                    operation_id="artifact-dispatch-boundary-response",
                )
            projection = service._test.context_runtime.read_case(waiting["run_id"])
        finally:
            service.close()

        developer = next(
            record
            for record in projection["phase_records"]
            if record.get("phase_type") == "developer.change"
        )
        self.assertTrue(developer["artifact_ref"])
        self.assertEqual(
            developer["artifact_sha256"],
            str(developer["artifact_ref"]["digest"]).removeprefix("sha256:"),
        )
        self.assertEqual(blocked["state"], "incident")
        self.assertEqual(blocked["incident"]["code"], "artifact_reference_invalid")
        self.assertNotIn(
            "live_patch_run",
            [name for name, _arguments in backend.calls],
        )

    def test_control_cancel_terminates_a_run_waiting_at_an_incident(self) -> None:
        backend, service, blocked, _patch_file, _original_body = (
            self.open_tampered_live_patch_incident(
                scenario="cancel-incident",
                target="192.0.2.91",
                restart_scope="skynet",
            )
        )
        try:
            with self.assertRaisesRegex(
                CommandConflict,
                "incident_id does not match the current Incident",
            ):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "control",
                        "run_id": blocked["run_id"],
                        "command": "cancel",
                        "incident_id": "incident-wrong-binding",
                    },
                    task_id="cancel-incident",
                    operation_id="cancel-incident-wrong-binding",
                )
            still_blocked = service._test.context_runtime.read_case(blocked["run_id"])

            cancellation = {
                "kind": "control",
                "run_id": blocked["run_id"],
                "command": "cancel",
                "incident_id": blocked["incident"]["incident_id"],
            }
            cancelled = service.call_exposed_tool(
                "execute",
                cancellation,
                task_id="cancel-incident",
                operation_id="cancel-incident-control",
            )
            replayed = service.call_exposed_tool(
                "execute",
                cancellation,
                task_id="cancel-incident-replay",
                operation_id="cancel-incident-retry-after-disconnect",
            )
            projection = service._test.context_runtime.read_case(blocked["run_id"])
            events = service._test.context_runtime.repository.events(blocked["run_id"])
        finally:
            service.close()

        self.assertEqual(blocked["state"], "incident")
        self.assertEqual(
            still_blocked["current_incident"]["incident_id"],
            blocked["incident"]["incident_id"],
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(cancelled["next_action"])
        self.assertEqual(cancelled["outcome"]["status"], "cancelled")
        self.assertIn("diagnostic_receipt_ref", cancelled)
        self.assertIn("diagnostic_receipt", replayed)
        cancelled_semantics = {
            key: value
            for key, value in cancelled.items()
            if key
            not in {
                "budget_blocker",
                "content_compacted",
                "diagnostic_receipt_ref",
                "manual_narrowing_required",
                "projection_compacted",
                "projection_metrics",
                "projection_target_exceeded",
            }
        }
        replayed_semantics = {
            key: value
            for key, value in replayed.items()
            if key
            not in {
                "diagnostic_receipt",
                "interaction_telemetry",
                "progress",
                "projection_target_exceeded",
            }
        }
        self.assertEqual(replayed_semantics, cancelled_semantics)
        self.assertEqual(
            replayed["progress"],
            {"status": "no_progress", "reason": "unchanged_command_replayed"},
        )
        self.assertTrue(
            replayed["interaction_telemetry"]["no_progress_retry"]
        )
        self.assertIsNone(replayed["next_action"])
        self.assertEqual(
            replayed["diagnostic_receipt"]["receipt_id"],
            cancelled["diagnostic_receipt_ref"]["receipt_id"],
        )
        self.assertEqual(projection["current_incident"], {})
        self.assertEqual(projection["incidents"][-1]["status"], "cancelled")
        self.assertEqual(projection["incidents"][-1]["resolution"], "cancelled")
        self.assertEqual(
            sum(event["kind"] == "RunOutcomeRecorded" for event in events),
            1,
        )
        self.assertNotIn(
            "live_patch_run",
            [name for name, _arguments in backend.calls],
        )

    def test_resume_revalidates_and_recovers_an_artifact_incident(self) -> None:
        backend, service, blocked, patch_file, original_body = (
            self.open_tampered_live_patch_incident(
                scenario="resume-artifact-incident",
                target="192.0.2.92",
                restart_scope="skynet",
            )
        )
        try:
            patch_file.write_bytes(original_body)
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": blocked["run_id"],
                },
                task_id="resume-artifact-incident",
                operation_id="resume-artifact-incident-resume",
            )
            projection = service._test.context_runtime.read_case(blocked["run_id"])
        finally:
            service.close()

        self.assertEqual(blocked["state"], "incident")
        self.assertEqual(final["state"], "completed")
        self.assertEqual(projection["current_incident"], {})
        self.assertEqual(projection["incidents"][-1]["status"], "resolved")
        self.assertEqual(
            [name for name, _arguments in backend.calls].count("live_patch_run"),
            1,
        )

    def test_resume_retries_a_domain_preparation_incident(self) -> None:
        backend = SemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "resume-domain-incident-fix.lua"
        patch_file.write_bytes(b"return 'domain-retry'\n")
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.93",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="resume-domain-incident",
                operation_id="resume-domain-incident-start",
            )
            driver = service._test.run_engine.driver
            original_prepare = driver.prepare_step
            prepared_operations: list[str] = []

            def fail_once(*args, **kwargs):
                operation = str(kwargs.get("operation", ""))
                prepared_operations.append(operation)
                if prepared_operations.count("live_patch_run") == 1:
                    raise OSError("domain preparation temporarily unavailable")
                return original_prepare(*args, **kwargs)

            with patch.object(driver, "prepare_step", side_effect=fail_once):
                blocked = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "resume-domain-incident-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.93",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "none",
                            },
                        },
                    },
                    task_id="resume-domain-incident",
                    operation_id="resume-domain-incident-response",
                )
                final = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": waiting["run_id"],
                    },
                    task_id="resume-domain-incident",
                    operation_id="resume-domain-incident-resume",
                )
            projection = service._test.context_runtime.read_case(waiting["run_id"])
        finally:
            service.close()

        self.assertEqual(blocked["state"], "incident")
        self.assertEqual(blocked["incident"]["code"], "domain_execution_failed")
        self.assertEqual(final["state"], "completed")
        self.assertEqual(prepared_operations.count("live_patch_run"), 2)
        self.assertEqual(prepared_operations.count("debug_collect"), 1)
        self.assertEqual(projection["incidents"][-1]["status"], "resolved")
        self.assertEqual(
            projection["incidents"][-1]["resolution"],
            "retrying domain preparation",
        )

    def test_observation_ref_reconstructs_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "receipt.sqlite3"
            blobs = root / "receipt-blobs"
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                receipt = first.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.42",
                        "selectors": [
                            {"id": "facts", "kind": "mdb", "queries": ["lsprop Object0"]}
                        ],
                    },
                    task_id="receipt-restart-observe",
                    operation_id="receipt-restart-observe-1",
                )
            finally:
                first.close()

            resumed_backend = SemanticBackend()
            second = RuntimeMcpService(
                resumed_backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                turn = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.42",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                        "observation_ref": receipt["observation_ref"],
                    },
                    task_id="receipt-restart-execute",
                    operation_id="receipt-restart-execute-1",
                )
            finally:
                second.close()

        self.assertEqual(turn["gate"]["name"], "diagnosis.acceptance")
        self.assertEqual(resumed_backend.calls, [])

    def test_observation_ref_with_blocked_diagnosis_yields_diagnosis_gate(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.43",
                "selectors": [
                    {
                        "id": "drive-facts",
                        "kind": "mdb",
                        "queries": ["lsprop Drive_1_010102"],
                    }
                ],
                "freshness": {"mode": "live", "max_age_seconds": 0},
            },
            task_id="diagnosis-gate-observe",
            operation_id="diagnosis-gate-observe-1",
        )

        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.43",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "repair the diagnosed source defect",
                "observation_ref": receipt["observation_ref"],
            },
            task_id="diagnosis-gate-run",
            operation_id="diagnosis-gate-start",
        )

        self.assertEqual(waiting["state"], "waiting_response")
        self.assertEqual(waiting["diagnostic_receipt"]["status"], "blocked")
        self.assertIn(
            "diagnostic_result_not_visible",
            waiting["diagnostic_receipt"]["gaps"],
        )
        self.assertEqual(waiting["gate"]["name"], "diagnosis.acceptance")
        self.assertNotEqual(waiting["gate"]["name"], "developer.change")

        resumed = self.service.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": waiting["run_id"]},
            task_id="diagnosis-gate-run",
            operation_id="diagnosis-gate-resume",
        )

        self.assertEqual(resumed["gate"]["gate_id"], waiting["gate"]["gate_id"])
        self.assertEqual(resumed["gate"]["name"], "diagnosis.acceptance")
        self.assertTrue(resumed["response_required"])
        self.assertEqual(
            resumed["progress"],
            {"status": "no_progress", "reason": "response_required"},
        )
        self.assertEqual(
            resumed["next_action"],
            waiting["next_action"],
        )
        self.assertFalse(
            any(
                fact.get("kind") == "phase"
                and fact.get("name") == "developer.change"
                for fact in resumed["facts"]
            )
        )

    def test_diagnosis_gate_response_allows_successful_source_delivery(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.44",
                "selectors": [
                    {
                        "id": "drive-facts",
                        "kind": "mdb",
                        "queries": ["lsprop Drive_1_010102"],
                    }
                ],
                "freshness": {"mode": "live", "max_age_seconds": 0},
            },
            task_id="diagnosis-response-observe",
            operation_id="diagnosis-response-observe-1",
        )
        diagnosis = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.44",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "purpose": "repair the diagnosed source defect",
                "observation_ref": receipt["observation_ref"],
            },
            task_id="diagnosis-response-run",
            operation_id="diagnosis-response-start",
        )
        evidence_ids = [
            item["evidence_id"]
            for item in diagnosis["diagnostic_receipt"]["evidence"]
        ]

        development = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": diagnosis["run_id"],
                **gate_binding(diagnosis),
                "response": {
                    "status": "completed",
                    "summary": "the drive identity is derived from the wrong scope",
                    "payload": {
                        "root_cause": (
                            "socket-local SlotID was treated as a global drive slot"
                        ),
                        "evidence_ids": evidence_ids,
                        "known_gaps": ["NVMe hardware verification is pending"],
                    },
                },
            },
            task_id="diagnosis-response-run",
            operation_id="diagnosis-response-accept",
        )

        self.assertEqual(development["state"], "waiting_response")
        self.assertEqual(development["gate"]["name"], "developer.change")
        self.assertEqual(development["diagnostic_receipt"]["status"], "complete")
        self.assertEqual(development["diagnostic_receipt"]["gaps"], [])

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": development["run_id"],
                **gate_binding(development),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "abc123",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["run focused regression tests"],
                        "known_gaps": ["official build dependency unavailable"],
                    },
                },
            },
            task_id="diagnosis-response-run",
            operation_id="diagnosis-response-development",
        )

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["outcome"]["status"], "completed")
        acceptance = {
            item["requirement_id"]: item["status"]
            for item in final["outcome"]["acceptance"]
        }
        self.assertEqual(acceptance["stage.diagnosis"], "passed")
        self.assertEqual(acceptance["stage.development"], "passed")

    def test_source_only_reports_official_validation_and_build_classifications(
        self,
    ) -> None:
        cases = (
            ("official_ut", "passed", "ready", "available", "passed"),
            (
                "official_ut",
                "failed_after_start",
                "ready",
                "available",
                "failed",
            ),
            (
                "official_ut",
                "dependency_blocked_before_start",
                "blocked",
                "blocked_external",
                "blocked",
            ),
            ("build", "compiled", "ready", "available", "passed"),
            (
                "build",
                "compile_failed",
                "ready",
                "available",
                "failed",
            ),
            (
                "build",
                "dependency_graph_blocked",
                "blocked",
                "blocked_external",
                "blocked",
            ),
        )

        for index, (kind, status, readiness, resolution, acceptance) in enumerate(
            cases,
            start=1,
        ):
            with self.subTest(kind=kind, status=status):
                waiting = self.service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": f"192.0.2.{120 + index}",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id=f"validation-classification-{index}",
                    operation_id=f"validation-classification-{index}-start",
                )
                readiness_id = f"dependency-check-{index}"
                final = self.service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "source change completed",
                            "payload": {
                                "source_revision": f"revision-{index}",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["run official validation"],
                                "dependency_readiness": {
                                    "readiness_id": readiness_id,
                                    "status": readiness,
                                    "resolution": resolution,
                                    "summary": "dependency graph checked once",
                                    "check_commands": ["conan graph info ."],
                                    "evidence_ids": [f"evidence-readiness-{index}"],
                                    "attempt_count": 1,
                                    "reused_by": [kind],
                                },
                                "validation_results": [
                                    {
                                        "kind": kind,
                                        "status": status,
                                        "summary": f"{kind} classified as {status}",
                                        "commands": [f"run-{kind}"],
                                        "evidence_ids": [f"evidence-{kind}-{index}"],
                                        "dependency_readiness_id": readiness_id,
                                    }
                                ],
                            },
                        },
                    },
                    task_id=f"validation-classification-{index}",
                    operation_id=f"validation-classification-{index}-complete",
                )

                self.assertEqual(final["outcome"]["status"], "completed")
                development = next(
                    fact
                    for fact in final["facts"]
                    if fact.get("name") == "developer.change"
                )
                summary = development["validation_summary"]
                self.assertEqual(summary[kind]["status"], status)
                self.assertEqual(summary[kind]["acceptance"], acceptance)
                self.assertFalse(summary["claims"]["package"])
                self.assertFalse(summary["claims"]["firmware"])
                self.assertFalse(summary["claims"]["upgrade"])
                self.assertFalse(summary["claims"]["hardware_repair_validated"])

    def test_source_only_without_validation_fields_reports_not_run_gaps(
        self,
    ) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.139",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="source-only-unreported-validation",
            operation_id="source-only-unreported-validation-start",
        )
        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "revision-without-validation",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["official validation pending"],
                    },
                },
            },
            task_id="source-only-unreported-validation",
            operation_id="source-only-unreported-validation-complete",
        )

        development = next(
            fact
            for fact in final["facts"]
            if fact.get("name") == "developer.change"
        )
        self.assertEqual(
            development["validation_summary"]["official_ut"]["status"],
            "not_run",
        )
        self.assertEqual(
            development["validation_summary"]["build"]["status"],
            "not_run",
        )
        self.assertEqual(
            development["hardware_coverage"]["status"],
            "not_reported",
        )
        self.assertIn("official_ut=not_run", final["gaps"])
        self.assertIn("build=not_run", final["gaps"])
        self.assertIn("hardware_coverage=not_reported", final["gaps"])

    def test_source_only_keeps_dependency_and_nvme_coverage_gaps_visible(
        self,
    ) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.140",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="source-only-validation-gaps",
            operation_id="source-only-validation-gaps-start",
        )
        readiness_id = "dependency-check-storage"
        hardware_evidence_ids = [
            item["evidence_id"]
            for item in waiting["diagnostic_receipt"]["evidence"]
        ]
        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "response": {
                    "status": "completed",
                    "summary": "NVMe source repair completed",
                    "payload": {
                        "source_revision": "revision-nvme-fix",
                        "authored_files": ["src/storage/fix.lua"],
                        "verification_plan": [
                            "run official UT and compile after dependency recovery",
                            "verify on representative NVMe hardware",
                        ],
                        "dependency_readiness": {
                            "readiness_id": readiness_id,
                            "status": "blocked",
                            "resolution": "blocked_external",
                            "summary": "libmc4lua is unavailable from configured Conan remotes",
                            "check_commands": ["conan graph info ."],
                            "evidence_ids": ["evidence-conan-graph"],
                            "attempt_count": 1,
                            "reused_by": ["official_ut", "build"],
                        },
                        "validation_results": [
                            {
                                "kind": "official_ut",
                                "status": "dependency_blocked_before_start",
                                "summary": "official UT did not start",
                                "commands": ["bingo test"],
                                "evidence_ids": ["evidence-ut-dependency"],
                                "dependency_readiness_id": readiness_id,
                            },
                            {
                                "kind": "build",
                                "status": "dependency_graph_blocked",
                                "summary": "build stopped before compilation",
                                "commands": ["bmcgo build -bt debug --stage dev"],
                                "evidence_ids": ["evidence-build-dependency"],
                                "dependency_readiness_id": readiness_id,
                            },
                            {
                                "kind": "supplementary",
                                "status": "passed",
                                "summary": "pure-logic regression tests passed",
                                "commands": ["python -m unittest test_slot_mapping.py"],
                                "evidence_ids": ["evidence-supplementary-tests"],
                            },
                        ],
                        "hardware_coverage": {
                            "status": "blocked",
                            "required_protocols": ["NVMe"],
                            "devices": [
                                {"device_id": "Disk23", "protocol": "SATA"},
                                {"device_id": "Disk24", "protocol": "SAS"},
                            ],
                            "evidence_ids": hardware_evidence_ids,
                            "gaps": ["representative NVMe target is unavailable"],
                        },
                        "known_gaps": [
                            "official dependency validation is blocked",
                            "representative NVMe hardware validation is blocked",
                        ],
                    },
                },
            },
            task_id="source-only-validation-gaps",
            operation_id="source-only-validation-gaps-complete",
        )

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["outcome"]["status"], "completed")
        development = next(
            fact
            for fact in final["facts"]
            if fact.get("name") == "developer.change"
        )
        summary = development["validation_summary"]
        self.assertEqual(
            summary["official_ut"]["status"],
            "dependency_blocked_before_start",
        )
        self.assertEqual(
            summary["build"]["status"],
            "dependency_graph_blocked",
        )
        self.assertEqual(summary["supplementary"]["status"], "passed")
        self.assertFalse(summary["supplementary"]["counts_as_official_ut"])
        coverage = development["hardware_coverage"]
        self.assertEqual(coverage["observed_protocols"], ["SAS", "SATA"])
        self.assertFalse(coverage["proves_required_protocols"])
        self.assertIn(
            "representative NVMe target is unavailable",
            final["gaps"],
        )
        case = self.service._test.context_runtime.read_case(final["run_id"])
        closeout = case["closeout"]
        self.assertEqual(closeout["claim_level"], "source_changed")
        self.assertEqual(
            closeout["validation_summary"]["official_ut"]["acceptance"],
            "blocked",
        )
        self.assertFalse(
            closeout["hardware_coverage"]["proves_required_protocols"]
        )

    def test_validation_evidence_rejects_false_success_and_repeated_preflight(
        self,
    ) -> None:
        invalid_payloads = (
            {
                "dependency_readiness": {
                    "readiness_id": "dependency-repeated",
                    "status": "blocked",
                    "resolution": "blocked_external",
                    "summary": "dependency check repeated",
                    "check_commands": ["conan graph info ."],
                    "evidence_ids": ["evidence-dependency"],
                    "attempt_count": 2,
                    "reused_by": ["official_ut"],
                },
                "validation_results": [
                    {
                        "kind": "official_ut",
                        "status": "dependency_blocked_before_start",
                        "summary": "tests did not start",
                        "commands": ["bingo test"],
                        "evidence_ids": ["evidence-ut"],
                        "dependency_readiness_id": "dependency-repeated",
                    }
                ],
            },
            {
                "dependency_readiness": {
                    "readiness_id": "dependency-fabricated",
                    "status": "ready",
                    "resolution": "vendored",
                    "summary": "a local package was fabricated",
                    "check_commands": ["conan graph info ."],
                    "evidence_ids": ["evidence-fabricated"],
                    "attempt_count": 1,
                    "reused_by": ["build"],
                },
                "validation_results": [
                    {
                        "kind": "build",
                        "status": "compiled",
                        "summary": "compile claimed",
                        "commands": ["bmcgo build"],
                        "evidence_ids": ["evidence-build"],
                        "dependency_readiness_id": "dependency-fabricated",
                    }
                ],
            },
            {
                "dependency_readiness": {
                    "readiness_id": "dependency-blocked",
                    "status": "blocked",
                    "resolution": "blocked_external",
                    "summary": "dependency graph is blocked",
                    "check_commands": ["conan graph info ."],
                    "evidence_ids": ["evidence-blocked"],
                    "attempt_count": 1,
                    "reused_by": ["official_ut"],
                },
                "validation_results": [
                    {
                        "kind": "official_ut",
                        "status": "passed",
                        "summary": "false official success",
                        "commands": ["bingo test"],
                        "evidence_ids": ["evidence-false-pass"],
                        "dependency_readiness_id": "dependency-blocked",
                    }
                ],
            },
            {
                "dependency_readiness": {
                    "readiness_id": "dependency-blocked-unbound",
                    "status": "blocked",
                    "resolution": "blocked_external",
                    "summary": "dependency graph is blocked",
                    "check_commands": ["conan graph info ."],
                    "evidence_ids": ["evidence-blocked-unbound"],
                    "attempt_count": 1,
                    "reused_by": ["official_ut"],
                },
                "validation_results": [
                    {
                        "kind": "official_ut",
                        "status": "passed",
                        "summary": "false official success without readiness binding",
                        "commands": ["bingo test"],
                        "evidence_ids": ["evidence-false-pass-unbound"],
                    }
                ],
            },
            {
                "hardware_coverage": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [
                        {"device_id": "Disk23", "protocol": "SATA"},
                        {"device_id": "Disk24", "protocol": "SAS"},
                    ],
                    "evidence_ids": ["evidence-sata-sas"],
                    "gaps": [],
                }
            },
            {
                "hardware_coverage": {
                    "status": "covered",
                    "required_protocols": ["NVMe"],
                    "devices": [
                        {"device_id": "Disk23", "protocol": "NVMe"},
                    ],
                    "evidence_ids": ["unknown-target-evidence"],
                    "gaps": [],
                }
            },
        )

        for index, invalid in enumerate(invalid_payloads, start=1):
            with self.subTest(index=index):
                waiting = self.service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": f"192.0.2.{150 + index}",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id=f"invalid-validation-evidence-{index}",
                    operation_id=f"invalid-validation-evidence-{index}-start",
                )
                with self.assertRaises(GateConflict):
                    self.service.call_exposed_tool(
                        "execute",
                        {
                            "kind": "respond",
                            "run_id": waiting["run_id"],
                            **gate_binding(waiting),
                            "response": {
                                "status": "completed",
                                "summary": "source change completed",
                                "payload": {
                                    "source_revision": f"invalid-{index}",
                                    "authored_files": ["src/fix.lua"],
                                    "verification_plan": ["validate evidence"],
                                    **invalid,
                                },
                            },
                        },
                        task_id=f"invalid-validation-evidence-{index}",
                        operation_id=f"invalid-validation-evidence-{index}-submit",
                    )
                resumed = self.service.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id=f"invalid-validation-evidence-{index}",
                    operation_id=f"invalid-validation-evidence-{index}-resume",
                )
                self.assertEqual(
                    resumed["gate"]["gate_id"],
                    waiting["gate"]["gate_id"],
                )

    def test_hardware_coverage_rejects_unrelated_current_evidence(self) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.159",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="hardware-evidence-semantics",
            operation_id="hardware-evidence-semantics-start",
        )
        evidence_id = waiting["diagnostic_receipt"]["evidence"][0]["evidence_id"]

        with self.assertRaisesRegex(
            GateConflict,
            "does not support device Disk23 protocol NVMe",
        ):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "false NVMe coverage",
                        "payload": {
                            "source_revision": "false-nvme-coverage",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["verify NVMe"],
                            "hardware_coverage": {
                                "status": "covered",
                                "required_protocols": ["NVMe"],
                                "devices": [
                                    {"device_id": "Disk23", "protocol": "NVMe"}
                                ],
                                "evidence_ids": [evidence_id],
                                "gaps": [],
                            },
                        },
                    },
                },
                task_id="hardware-evidence-semantics",
                operation_id="hardware-evidence-semantics-submit",
            )

    def test_completed_failed_build_does_not_advance_to_upgrade(self) -> None:
        developer_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.160",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id="failed-build-does-not-upgrade",
            operation_id="failed-build-does-not-upgrade-start",
        )
        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(developer_gate),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "failed-build-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and verify"],
                    },
                },
            },
            task_id="failed-build-does-not-upgrade",
            operation_id="failed-build-does-not-upgrade-source",
        )
        product = self.artifact_root / "failed-build.hpm"
        product.write_bytes(b"incomplete firmware")

        with self.assertRaises(GateConflict):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(build_gate),
                    "response": {
                        "status": "completed",
                        "summary": "compile failed",
                        "payload": {
                            "source_revision": "failed-build-source",
                            "artifact_ref": artifact_ref(
                                product,
                                kind="openubmc-hpm",
                                target="192.0.2.160",
                                run_id=developer_gate["run_id"],
                                version="1.2.3",
                            ),
                            "dependency_readiness": {
                                "readiness_id": "failed-build-readiness",
                                "status": "ready",
                                "resolution": "available",
                                "summary": "dependencies resolved",
                                "check_commands": ["conan graph info ."],
                                "evidence_ids": ["dependency-ready"],
                                "attempt_count": 1,
                                "reused_by": ["build"],
                            },
                            "validation_results": [
                                {
                                    "kind": "build",
                                    "status": "compile_failed",
                                    "summary": "compiler returned failure",
                                    "commands": ["bmcgo build"],
                                    "evidence_ids": ["compile-failure-log"],
                                    "dependency_readiness_id": (
                                        "failed-build-readiness"
                                    ),
                                }
                            ],
                        },
                    },
                },
                task_id="failed-build-does-not-upgrade",
                operation_id="failed-build-does-not-upgrade-build",
            )

        with self.assertRaisesRegex(
            GateConflict,
            "failed build.artifact requires dependency_readiness and validation_results",
        ):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(build_gate),
                    "response": {
                        "status": "failed",
                        "summary": "build failed without classification",
                        "payload": {},
                    },
                },
                task_id="failed-build-does-not-upgrade",
                operation_id="failed-build-does-not-upgrade-unclassified",
            )

        failed = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(build_gate),
                "response": {
                    "status": "failed",
                    "summary": "compiler returned failure",
                    "payload": {
                        "dependency_readiness": {
                            "readiness_id": "failed-build-readiness",
                            "status": "ready",
                            "resolution": "available",
                            "summary": "dependencies resolved",
                            "check_commands": ["conan graph info ."],
                            "evidence_ids": ["dependency-ready"],
                            "attempt_count": 1,
                            "reused_by": ["build"],
                        },
                        "validation_results": [
                            {
                                "kind": "build",
                                "status": "compile_failed",
                                "summary": "compiler returned failure",
                                "commands": ["bmcgo build"],
                                "evidence_ids": ["compile-failure-log"],
                                "dependency_readiness_id": "failed-build-readiness",
                            }
                        ],
                    },
                },
            },
            task_id="failed-build-does-not-upgrade",
            operation_id="failed-build-does-not-upgrade-classified",
        )

        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            [name for name, _arguments in self.backend.calls].count("upgrade_run"),
            0,
        )

    def test_closeout_merges_development_and_build_validation_dimensions(
        self,
    ) -> None:
        developer_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.161",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
            },
            task_id="validation-closeout-merge",
            operation_id="validation-closeout-merge-start",
        )
        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(developer_gate),
                "response": {
                    "status": "completed",
                    "summary": "source and official UT completed",
                    "payload": {
                        "source_revision": "validation-closeout-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and verify"],
                        "dependency_readiness": {
                            "readiness_id": "developer-ut-readiness",
                            "status": "ready",
                            "resolution": "available",
                            "summary": "official UT dependencies resolved",
                            "check_commands": ["conan graph info ."],
                            "evidence_ids": ["developer-dependency-log"],
                            "attempt_count": 1,
                            "reused_by": ["official_ut"],
                        },
                        "validation_results": [
                            {
                                "kind": "official_ut",
                                "status": "passed",
                                "summary": "official UT passed",
                                "commands": ["bingo test"],
                                "evidence_ids": ["official-ut-log"],
                                "dependency_readiness_id": "developer-ut-readiness",
                            }
                        ],
                        "hardware_coverage": {
                            "status": "blocked",
                            "required_protocols": ["NVMe"],
                            "devices": [],
                            "evidence_ids": [],
                            "gaps": ["representative NVMe target is unavailable"],
                        },
                    },
                },
            },
            task_id="validation-closeout-merge",
            operation_id="validation-closeout-merge-source",
        )
        product = self.artifact_root / "validation-closeout-merge.hpm"
        product.write_bytes(b"firmware-4.0.0")
        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": developer_gate["run_id"],
                **gate_binding(build_gate),
                "response": {
                    "status": "completed",
                    "summary": "firmware compiled",
                    "payload": {
                        "source_revision": "validation-closeout-source",
                        "artifact_ref": artifact_ref(
                            product,
                            kind="openubmc-hpm",
                            target="192.0.2.161",
                            run_id=developer_gate["run_id"],
                            version="4.0.0",
                        ),
                        "dependency_readiness": {
                            "readiness_id": "build-readiness",
                            "status": "ready",
                            "resolution": "available",
                            "summary": "build dependencies resolved",
                            "check_commands": ["conan graph info ."],
                            "evidence_ids": ["build-dependency-log"],
                            "attempt_count": 1,
                            "reused_by": ["build"],
                        },
                        "validation_results": [
                            {
                                "kind": "build",
                                "status": "compiled",
                                "summary": "firmware compiled",
                                "commands": ["bmcgo build"],
                                "evidence_ids": ["build-log"],
                                "dependency_readiness_id": "build-readiness",
                            }
                        ],
                    },
                },
            },
            task_id="validation-closeout-merge",
            operation_id="validation-closeout-merge-build",
        )
        case = self.service._test.context_runtime.read_case(final["run_id"])

        self.assertEqual(
            case["closeout"]["validation_summary"]["official_ut"]["status"],
            "passed",
        )
        self.assertEqual(
            case["closeout"]["validation_summary"]["build"]["status"],
            "compiled",
        )
        readiness = case["closeout"]["validation_summary"][
            "dependency_readiness"
        ]
        self.assertEqual(
            readiness["official_ut"]["readiness_id"],
            "developer-ut-readiness",
        )
        self.assertEqual(
            readiness["build"]["readiness_id"],
            "build-readiness",
        )
        self.assertEqual(
            case["closeout"]["hardware_coverage"]["status"],
            "blocked",
        )
        self.assertIn(
            "representative NVMe target is unavailable",
            case["closeout"]["reasons"],
        )

    def test_failed_or_cancelled_diagnosis_never_opens_development(self) -> None:
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                receipt = self.service.call_exposed_tool(
                    "observe",
                    {
                        "target": f"192.0.2.{45 if status == 'failed' else 46}",
                        "selectors": [
                            {
                                "id": "drive-facts",
                                "kind": "mdb",
                                "queries": ["lsprop Drive_1_010102"],
                            }
                        ],
                    },
                    task_id=f"diagnosis-{status}-observe",
                    operation_id=f"diagnosis-{status}-observe-1",
                )
                waiting = self.service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": f"192.0.2.{45 if status == 'failed' else 46}",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                        "observation_ref": receipt["observation_ref"],
                    },
                    task_id=f"diagnosis-{status}-run",
                    operation_id=f"diagnosis-{status}-start",
                )

                final = self.service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": status,
                            "summary": f"diagnosis {status}",
                            "payload": {},
                        },
                    },
                    task_id=f"diagnosis-{status}-run",
                    operation_id=f"diagnosis-{status}-response",
                )

                self.assertEqual(final["state"], status)
                self.assertEqual(final["outcome"]["status"], status)
                self.assertIsNone(final["gate"])
                self.assertFalse(
                    any(
                        fact.get("kind") == "phase"
                        and fact.get("name") == "developer.change"
                        for fact in final["facts"]
                    )
                )

    def test_diagnosis_gate_rejects_unbound_evidence_and_keeps_same_gate(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.47",
                "selectors": [
                    {
                        "id": "drive-facts",
                        "kind": "mdb",
                        "queries": ["lsprop Drive_1_010102"],
                    }
                ],
            },
            task_id="diagnosis-evidence-observe",
            operation_id="diagnosis-evidence-observe-1",
        )
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.47",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "observation_ref": receipt["observation_ref"],
            },
            task_id="diagnosis-evidence-run",
            operation_id="diagnosis-evidence-start",
        )

        with self.assertRaisesRegex(GateConflict, "not bound"):
            self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "diagnosis is grounded",
                        "payload": {
                            "root_cause": "wrong slot scope",
                            "evidence_ids": ["evidence-not-in-this-run"],
                            "known_gaps": [],
                        },
                    },
                },
                task_id="diagnosis-evidence-run",
                operation_id="diagnosis-evidence-invalid",
            )

        resumed = self.service.call_exposed_tool(
            "execute",
            {"kind": "resume", "run_id": waiting["run_id"]},
            task_id="diagnosis-evidence-run",
            operation_id="diagnosis-evidence-resume",
        )
        self.assertEqual(resumed["gate"]["gate_id"], waiting["gate"]["gate_id"])
        self.assertEqual(resumed["gate"]["name"], "diagnosis.acceptance")

    def test_diagnosis_gate_accepts_run_bound_operator_diagnosis_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repository = SQLiteRuntimeRepository(root / "runtime.sqlite3")
            blobs = InMemoryBlobRepository()
            agent = RuntimeMcpService(
                SemanticBackend(),
                context_repository=repository,
                blob_repository=blobs,
            )
            diagnosis_path = root / "diagnosis.md"
            diagnosis_path.write_text(
                "The component-global slot was compared with a local slot.\n",
                encoding="utf-8",
            )
            try:
                observation = agent.call_exposed_tool(
                    "observe",
                    {
                        "target": "192.0.2.47",
                        "selectors": [
                            {
                                "id": "drive-facts",
                                "kind": "mdb",
                                "queries": ["lsprop Drive_1_010102"],
                            }
                        ],
                    },
                    task_id="diagnosis-attachment-observe",
                    operation_id="diagnosis-attachment-observe-1",
                )
                waiting = agent.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.47",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                        "observation_ref": observation["observation_ref"],
                    },
                    task_id="diagnosis-attachment-run",
                    operation_id="diagnosis-attachment-start",
                )
                operator = RuntimeMcpService(
                    SemanticBackend(),
                    context_repository=repository,
                    blob_repository=blobs,
                    interface_profile="operator",
                )
                try:
                    attached = operator.call_exposed_tool(
                        "evidence_attach",
                        {
                            "run_id": waiting["run_id"],
                            "target": "192.0.2.47",
                            "path": str(diagnosis_path),
                            "sha256": hashlib.sha256(
                                diagnosis_path.read_bytes()
                            ).hexdigest(),
                            "evidence_type": "workflow-diagnosis-record",
                        },
                        task_id="diagnosis-attachment-operator",
                        operation_id="diagnosis-attachment-attach",
                    )
                    unrelated = operator.call_exposed_tool(
                        "evidence_attach",
                        {
                            "run_id": waiting["run_id"],
                            "target": "192.0.2.47",
                            "path": str(diagnosis_path),
                            "sha256": hashlib.sha256(
                                diagnosis_path.read_bytes()
                            ).hexdigest(),
                            "evidence_type": "workflow-official-ut-record",
                        },
                        task_id="diagnosis-attachment-operator",
                        operation_id="diagnosis-attachment-unrelated",
                    )
                finally:
                    operator.close()
                evidence_ids = [
                    item["evidence_id"]
                    for item in waiting["diagnostic_receipt"]["evidence"]
                ]
                evidence_ids.append(attached["evidence"]["evidence_id"])

                with self.assertRaisesRegex(GateConflict, "not bound"):
                    agent.call_exposed_tool(
                        "execute",
                        {
                            "kind": "respond",
                            "run_id": waiting["run_id"],
                            **gate_binding(waiting),
                            "response": {
                                "status": "completed",
                                "summary": "an unrelated record is not diagnosis proof",
                                "payload": {
                                    "root_cause": "wrong slot scope",
                                    "evidence_ids": [
                                        unrelated["evidence"]["evidence_id"]
                                    ],
                                    "known_gaps": [],
                                },
                            },
                        },
                        task_id="diagnosis-attachment-run",
                        operation_id="diagnosis-attachment-reject-unrelated",
                    )

                development = agent.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "the drive identity mismatch is diagnosed",
                            "payload": {
                                "root_cause": (
                                    "component-global slot was compared with a local slot"
                                ),
                                "evidence_ids": evidence_ids,
                                "known_gaps": [],
                            },
                        },
                    },
                    task_id="diagnosis-attachment-run",
                    operation_id="diagnosis-attachment-accept",
                )
            finally:
                agent.close()

        self.assertEqual(development["gate"]["name"], "developer.change")
        self.assertEqual(
            [
                item["evidence_id"]
                for item in development["diagnostic_receipt"]["evidence"]
            ],
            evidence_ids,
        )

    def test_diagnosis_gate_cannot_reclassify_stale_runtime_evidence_as_fresh(
        self,
    ) -> None:
        service = RuntimeMcpService(StaleEvidenceDiagnosticBackend())
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.48",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="diagnosis-stale-run",
                operation_id="diagnosis-stale-start",
            )
            evidence_ids = [
                item["evidence_id"]
                for item in waiting["diagnostic_receipt"]["evidence"]
            ]

            with self.assertRaisesRegex(GateConflict, "complete evaluable"):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "the connector timeout was localized",
                            "payload": {
                                "root_cause": "connector timeout",
                                "evidence_ids": evidence_ids,
                                "known_gaps": ["fresh target evidence is required"],
                            },
                        },
                    },
                    task_id="diagnosis-stale-run",
                    operation_id="diagnosis-stale-accept",
                )

            resumed = service.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": waiting["run_id"]},
                task_id="diagnosis-stale-run",
                operation_id="diagnosis-stale-resume",
            )
        finally:
            service.close()

        self.assertEqual(resumed["gate"]["gate_id"], waiting["gate"]["gate_id"])
        self.assertEqual(resumed["diagnostic_receipt"]["freshness"]["status"], "stale")
        self.assertEqual(resumed["gate"]["name"], "diagnosis.acceptance")

    def test_diagnosis_gate_cannot_reclassify_partial_runtime_evidence_as_fresh(
        self,
    ) -> None:
        service = RuntimeMcpService(BoundedDiagnosticBackend())
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.49",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="diagnosis-partial-run",
                operation_id="diagnosis-partial-start",
            )
            evidence_ids = [
                item["evidence_id"]
                for item in waiting["diagnostic_receipt"]["evidence"]
            ]

            with self.assertRaisesRegex(GateConflict, "complete evaluable"):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "the timeout path was localized",
                            "payload": {
                                "root_cause": "the request waits on incomplete data",
                                "evidence_ids": evidence_ids,
                                "known_gaps": ["active alarm evidence is incomplete"],
                            },
                        },
                    },
                    task_id="diagnosis-partial-run",
                    operation_id="diagnosis-partial-accept",
                )

            resumed = service.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": waiting["run_id"]},
                task_id="diagnosis-partial-run",
                operation_id="diagnosis-partial-resume",
            )
        finally:
            service.close()

        self.assertEqual(resumed["gate"]["gate_id"], waiting["gate"]["gate_id"])
        self.assertEqual(
            resumed["diagnostic_receipt"]["freshness"]["status"],
            "partial",
        )
        self.assertEqual(resumed["gate"]["name"], "diagnosis.acceptance")

    def test_legacy_v1_run_fails_closed_before_development(self) -> None:
        repository = InMemoryRuntimeRepository(clock=lambda: 1.0)
        definition = WorkflowDefinition(
            definition_id="diagnose-and-fix.debug.source-only",
            version=1,
            intent="diagnose-and-fix",
            entry_domain="debug",
            entry_operation="",
            delivery_strategy="source-only",
            steps=(
                WorkflowStepDefinition(
                    "step-01-debug-run",
                    "operation",
                    "debug_run",
                    "openubmc-debug",
                ),
                WorkflowStepDefinition(
                    "step-02-developer-change",
                    "phase",
                    "developer.change",
                    "openubmc-developer",
                    "openubmc-agent-workflow/developer-change-receipt-v1",
                ),
            ),
        ).to_public_dict()
        blocked = build_diagnostic_receipt(
            "debug_run",
            {"ok": True, "summary": "Domain operation completed"},
            {},
            (),
            closeout_stage="diagnosis",
        )
        assert blocked is not None
        run_id = "run-legacy-v1-blocked-diagnosis"
        developer_schema = gate_input_schema(
            "developer.change",
            delivery_strategy="source-only",
            artifact_metadata={},
        )
        developer_gate = Gate(
            gate_id="gate-legacy-v1-developer",
            version=1,
            name="developer.change",
            owner="openubmc-developer",
            input_schema=developer_schema,
            schema_digest=hashlib.sha256(
                json.dumps(
                    developer_schema,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        repository.commit(
            run_id,
            expected_revision=0,
            events=(
                PendingCaseEvent(
                    "CaseOpened",
                    {
                        "intent": "diagnose-and-fix",
                        "entry_domain": "debug",
                        "entry_operation": "",
                        "final_purpose": "resume a persisted v1 Run",
                        "delivery_strategy": "source-only",
                        "acceptance_plan": {},
                        "targets": [
                            {
                                "target_id": "target-1",
                                "role": "candidate",
                                "address": "192.0.2.49",
                            }
                        ],
                        "target_version": 1,
                        "workflow_cycle_id": "cycle-1",
                        "workflow_cycle_number": 1,
                        "workflow_definition": definition,
                        "workflow_inputs": {},
                        "start_input": {},
                    },
                    "legacy-v1-open",
                ),
                PendingCaseEvent(
                    "OperationAccepted",
                    {
                        "operation": "debug_run",
                        "workflow_cycle_id": "cycle-1",
                        "workflow_step_id": "step-01-debug-run",
                        "workflow_step_kind": "operation",
                        "workflow_definition_id": definition["definition_id"],
                        "workflow_definition_version": 1,
                        "workflow_definition_fingerprint": definition["fingerprint"],
                        "workflow_execution_id": "step-legacy-v1-debug",
                        "workflow_attempt": 1,
                        "workflow_input_fingerprint": "a" * 64,
                        "workflow_target_epoch": 0,
                        "target_version": 1,
                        "target_id": "target-1",
                    },
                    "legacy-v1-debug",
                ),
                PendingCaseEvent("OperationStarted", {}, "legacy-v1-debug"),
                PendingCaseEvent(
                    "OperationTerminal",
                    {
                        "status": "completed",
                        "summary": "Domain completed without accepted diagnosis",
                        "case_status": "open",
                        "target_epoch": 0,
                        "diagnostic_receipt": blocked.to_public_dict(),
                    },
                    "legacy-v1-debug",
                ),
                PendingCaseEvent(
                    "RunGateOpened",
                    {
                        "gate": {
                            **developer_gate.to_public_dict(),
                            "run_id": run_id,
                            "workflow_cycle_id": "cycle-1",
                            "workflow_step_id": "step-02-developer-change",
                        }
                    },
                    "legacy-v1-developer-gate",
                ),
            ),
        )
        service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
        )
        try:
            turn = service.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": run_id},
                task_id="legacy-v1-task",
                operation_id="legacy-v1-resume",
            )
            with self.assertRaisesRegex(GateConflict, "Incident"):
                service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": run_id,
                        "gate_id": developer_gate.gate_id,
                        "gate_version": developer_gate.version,
                        "schema_digest": developer_gate.schema_digest,
                        "response": {
                            "status": "completed",
                            "summary": "must not be accepted",
                            "payload": {
                                "source_revision": "legacy-source",
                                "authored_files": ["src/legacy.lua"],
                                "verification_plan": ["run tests"],
                            },
                        },
                    },
                    task_id="legacy-v1-task",
                    operation_id="legacy-v1-invalid-submit",
                )
        finally:
            service.close()

        self.assertEqual(turn["state"], "incident")
        self.assertEqual(turn["incident"]["code"], "diagnosis_not_accepted")
        self.assertIsNone(turn["gate"])
        self.assertFalse(
            any(
                fact.get("kind") == "phase"
                and fact.get("name") == "developer.change"
                for fact in turn["facts"]
            )
        )
        self.assertFalse(repository.load(run_id)["phase_records"])

    def test_source_only_failed_phase_never_produces_a_success_outcome(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.41",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="source-failure",
            operation_id="source-failure-start",
        )
        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                **gate_binding(first),
                "response": {
                    "status": "failed",
                    "summary": "source repair validation failed",
                    "payload": {},
                },
            },
            task_id="source-failure",
            operation_id="source-failure-respond",
        )

        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["outcome"]["status"], "failed")
        self.assertTrue(final["outcome_recorded"])

    def test_control_cancel_without_response_terminates_the_run(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.61",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="cancel-run",
            operation_id="cancel-run-start",
        )

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "control",
                "run_id": first["run_id"],
                "command": "cancel",
                **gate_binding(first),
            },
            task_id="cancel-run",
            operation_id="cancel-run-control",
        )

        self.assertEqual(final["state"], "cancelled")
        self.assertIsNone(final["gate"])
        self.assertEqual(final["outcome"]["status"], "cancelled")
        self.assertTrue(final["outcome_recorded"])
        events = self.service._test.context_runtime.repository.events(first["run_id"])
        self.assertEqual(
            [event["kind"] for event in events].count("RunCancelled"), 1
        )
        cancelled = next(
            event for event in events if event["kind"] == "RunCancelled"
        )
        self.assertEqual(cancelled["payload"]["actor"], "runtime")
        self.assertFalse(
            any(
                event["kind"] == "OperationProgressed"
                and isinstance(event["payload"].get("phase_record"), dict)
                and event["payload"]["phase_record"].get("status")
                == "cancelled"
                for event in events
            )
        )

    def test_cancel_reattaches_after_a_concurrent_commit(self) -> None:
        repository = CommitThenConflictRepository("RunCancelled")
        service = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
        )
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.64",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                },
                task_id="cancel-concurrent-commit",
                operation_id="cancel-concurrent-start",
            )
            cancellation = {
                "kind": "control",
                "run_id": waiting["run_id"],
                "command": "cancel",
                "submission_id": "cancel-concurrent-submission",
                **gate_binding(waiting),
            }
            final = service.call_exposed_tool(
                "execute",
                cancellation,
                task_id="cancel-concurrent-commit",
                operation_id="cancel-concurrent-control",
            )
            replayed = service.call_exposed_tool(
                "execute",
                cancellation,
                task_id="cancel-concurrent-replay",
                operation_id="cancel-concurrent-replay",
            )
            events = repository.events(waiting["run_id"])
        finally:
            service.close()

        self.assertTrue(repository.conflicted)
        self.assertEqual(final["state"], "cancelled")
        self.assertEqual(replayed["outcome"], final["outcome"])
        self.assertEqual(
            sum(event["kind"] == "RunCancelled" for event in events),
            1,
        )

    def test_duplicate_control_cancel_reports_no_progress_without_a_new_transition(
        self,
    ) -> None:
        waiting = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.69",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            task_id="duplicate-control",
            operation_id="duplicate-control-start",
        )
        cancellation = {
            "kind": "control",
            "run_id": waiting["run_id"],
            "command": "cancel",
            "submission_id": "duplicate-control-submission",
            **gate_binding(waiting),
        }
        cancelled = self.service.call_exposed_tool(
            "execute",
            cancellation,
            task_id="duplicate-control",
            operation_id="duplicate-control-first",
        )
        before = self.service._test.context_runtime.read_case(waiting["run_id"])

        replayed = self.service.call_exposed_tool(
            "execute",
            cancellation,
            task_id="duplicate-control-replay",
            operation_id="duplicate-control-second",
        )
        after = self.service._test.context_runtime.read_case(waiting["run_id"])

        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(
            replayed["progress"],
            {"status": "no_progress", "reason": "unchanged_command_replayed"},
        )
        self.assertTrue(
            replayed["interaction_telemetry"]["no_progress_retry"]
        )

    def test_execute_live_patch_runs_diagnosis_mutation_and_fresh_verification(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.21",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "live-patch",
                "purpose": "repair and verify the running target",
            },
            task_id="execute-live-patch",
            operation_id="live-patch-start",
        )
        self.assertEqual(first["state"], "waiting_response")
        self.assertEqual(first["gate"]["name"], "developer.change")
        patch_file = self.artifact_root / "execute-live-patch-fix.lua"
        patch_file.write_bytes(b"return 'fixed'\n")

        with patch.object(
            self.service._test.context_runtime,
            "invoke_domain",
            side_effect=AssertionError(
                "typed Effect execution must not let ContextRuntime write transitions"
            ),
        ):
            final = self.service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": first["run_id"],
                    **gate_binding(first),
                    "response": {
                        "status": "completed",
                        "summary": "source repair is ready for live patching",
                        "payload": {
                            "source_revision": "live-patch-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh target verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.21",
                                run_id=first["run_id"],
                            ),
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                    },
                },
                task_id="execute-live-patch",
                operation_id="live-patch-respond",
            )

        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_run", "live_patch_run", "debug_collect"],
        )
        live_patch_arguments = self.backend.calls[1][1]
        self.assertEqual(
            live_patch_arguments["artifact_sha256"],
            hashlib.sha256(patch_file.read_bytes()).hexdigest(),
        )
        projection = self.service._test.context_runtime.read_case(first["run_id"])
        intent_arguments = next(
            intent["arguments"]
            for intent in projection["effect_intents"]
            if intent.get("operation") == "live_patch_run"
        )
        effect_id = next(
            intent["effect_id"]
            for intent in projection["effect_intents"]
            if intent.get("operation") == "live_patch_run"
        )
        effect_event_kinds = [
            event["kind"]
            for event in self.service._test.context_runtime.repository.events(
                first["run_id"]
            )
            if event.get("operation_id") == effect_id
        ]
        self.assertIn("OperationTerminal", effect_event_kinds)
        self.assertNotIn("OperationReconciled", effect_event_kinds)
        self.assertEqual(
            intent_arguments["artifact_ref"]["kind"],
            "openubmc-live-patch",
        )
        self.assertEqual(intent_arguments["artifact_ref"]["run_id"], first["run_id"])
        self.assertNotIn("local_path", intent_arguments)
        self.assertNotIn("artifact_path", intent_arguments)
        verification_arguments = self.backend.calls[-1][1]
        self.assertEqual(verification_arguments["profile"], "standard")
        self.assertFalse(verification_arguments["no_freshness"])

    def test_incomplete_live_patch_acceptance_cannot_report_completed_success(self) -> None:
        backend = IncompleteAcceptanceSemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "incomplete-acceptance-fix.lua"
        patch_file.write_bytes(b"return 'incomplete-acceptance'\n")
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.67",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="incomplete-live-patch-acceptance",
                operation_id="incomplete-live-patch-start",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "source repair ready",
                        "payload": {
                            "source_revision": "incomplete-acceptance-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh target verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.67",
                                run_id=waiting["run_id"],
                            ),
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                    },
                },
                task_id="incomplete-live-patch-acceptance",
                operation_id="incomplete-live-patch-response",
            )
            projection = service._test.context_runtime.read_case(waiting["run_id"])
            replayed = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": waiting["run_id"],
                },
                task_id="incomplete-live-patch-acceptance-replay",
                operation_id="incomplete-live-patch-outcome-replay",
            )
            events = service._test.context_runtime.repository.events(waiting["run_id"])
        finally:
            service.close()

        closeout = projection["closeout"]
        integrity = next(
            check
            for check in closeout["checks"]
            if check["requirement_id"] == "acceptance.live-patch.integrity"
        )
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["outcome"]["status"], "failed")
        self.assertEqual(projection["run_outcome"]["status"], "failed")
        self.assertEqual(replayed["outcome"], final["outcome"])
        self.assertEqual(closeout["closure_status"], "partial")
        self.assertEqual(closeout["business_acceptance"], "unverified")
        self.assertEqual(integrity["status"], "not_run")
        self.assertEqual(
            sum(event["kind"] == "RunOutcomeRecorded" for event in events),
            1,
        )

    def test_post_mutation_verification_requires_an_observed_target_epoch(self) -> None:
        backend = MissingFreshEpochSemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "missing-fresh-epoch.lua"
        patch_file.write_bytes(b"return 'missing-epoch'\n")
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.69",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="missing-fresh-epoch",
                operation_id="missing-fresh-epoch-start",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "source repair ready",
                        "payload": {
                            "source_revision": "missing-fresh-epoch-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh target verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.69",
                                run_id=waiting["run_id"],
                            ),
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                    },
                },
                task_id="missing-fresh-epoch",
                operation_id="missing-fresh-epoch-response",
            )
            for attempt in range(1, 4):
                if final["state"] != "running":
                    break
                final = service.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": waiting["run_id"]},
                    task_id="missing-fresh-epoch",
                    operation_id=f"missing-fresh-epoch-resume-{attempt}",
                )
            projection = service._test.context_runtime.read_case(waiting["run_id"])
        finally:
            service.close()

        self.assertEqual(final["state"], "running")
        self.assertIsNone(final["outcome"])
        self.assertFalse(projection.get("run_outcome"))
        self.assertIn("fresh target verification", final["next"])

    def test_conflicting_acceptance_evidence_fails_closed(self) -> None:
        backend = ConflictingAcceptanceSemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "conflicting-acceptance.lua"
        patch_file.write_bytes(b"return 'conflicting-acceptance'\n")
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.72",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="conflicting-acceptance",
                operation_id="conflicting-acceptance-start",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "source repair ready",
                        "payload": {
                            "source_revision": "conflicting-acceptance-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh target verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.72",
                                run_id=waiting["run_id"],
                            ),
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                    },
                },
                task_id="conflicting-acceptance",
                operation_id="conflicting-acceptance-response",
            )
            projection = service._test.context_runtime.read_case(waiting["run_id"])
            events = service._test.context_runtime.repository.events(waiting["run_id"])
        finally:
            service.close()

        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["outcome"]["status"], "failed")
        self.assertEqual(projection["closeout"]["business_acceptance"], "failed")
        self.assertNotEqual(projection["closeout"]["closure_status"], "verified")
        self.assertEqual(
            sum(event["kind"] == "RunOutcomeRecorded" for event in events),
            1,
        )

    def test_execute_build_upgrade_rejects_missing_requested_drive_expansion(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.22",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "entry_operation": "debug_run",
                "entry_arguments": {
                    "mdb_expand_classes": ["Drive"],
                    "no_freshness": True,
                },
                "purpose": "build, deploy, and verify a firmware repair",
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-start",
        )
        self.assertEqual(first["gate"]["name"], "diagnosis.acceptance")
        evidence_ids = [
            item["evidence_id"]
            for item in first["diagnostic_receipt"]["evidence"]
        ]
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                **gate_binding(first),
                "response": {
                    "status": "completed",
                    "summary": "Drive evidence defines the repair scope",
                    "payload": {
                        "root_cause": "Drive state requires a source repair",
                        "evidence_ids": evidence_ids,
                        "known_gaps": [],
                    },
                },
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-diagnosis",
        )
        self.assertEqual(first["gate"]["name"], "developer.change")

        build_gate = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                **gate_binding(first),
                "response": {
                    "status": "completed",
                    "summary": "source repair completed",
                    "payload": {
                        "source_revision": "upgrade-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["build and target verification"],
                    },
                },
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-developer",
        )
        self.assertEqual(build_gate["state"], "waiting_response")
        self.assertEqual(build_gate["gate"]["name"], "build.artifact")
        product = self.artifact_root / "execute-build-upgrade-product.hpm"
        product.write_bytes(b"firmware-1.2.3")

        final = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "respond",
                "run_id": first["run_id"],
                **gate_binding(build_gate),
                "response": {
                    "status": "completed",
                    "summary": "firmware artifact completed",
                    "payload": {
                        "source_revision": "upgrade-source",
                        "artifact_ref": artifact_ref(
                            product,
                            kind="openubmc-hpm",
                            target="192.0.2.22",
                            run_id=first["run_id"],
                            version="1.2.3",
                        ),
                        **compiled_validation_payload("execute-build-upgrade"),
                    },
                },
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-build",
        )

        self.assertEqual(final["state"], "failed")
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_run", "upgrade_run", "debug_collect"],
        )
        verification_arguments = self.backend.calls[-1][1]
        self.assertEqual(verification_arguments["profile"], "standard")
        self.assertFalse(verification_arguments["no_freshness"])
        self.assertEqual(
            verification_arguments["mdb_expand_classes"],
            ["Drive"],
        )

    def test_execute_build_upgrade_runs_both_gates_and_fresh_verification(self) -> None:
        backend = CompleteDriveVerificationSemanticBackend()
        repository = InMemoryRuntimeRepository()
        blob_repository = InMemoryBlobRepository()
        service = RuntimeMcpService(
            backend,
            context_repository=repository,
            blob_repository=blob_repository,
        )
        operator = RuntimeMcpService(
            backend,
            context_repository=repository,
            blob_repository=blob_repository,
            interface_profile="operator",
        )
        product = self.artifact_root / "complete-drive-verification-product.hpm"
        product.write_bytes(b"firmware-1.2.4")
        try:
            diagnosis = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.23",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "entry_operation": "debug_run",
                    "entry_arguments": {
                        "mdb_expand_classes": ["Drive"],
                        "hardware_acceptance": {
                            "devices": [
                                {
                                    "device_id": "Drive20",
                                    "protocol": "NVMe",
                                    "resource_id": "positive",
                                }
                            ]
                        },
                    },
                    "purpose": "build, deploy, and verify a Drive repair",
                },
                task_id="complete-drive-verification",
                operation_id="complete-drive-verification-start",
            )
            evidence_ids = [
                item["evidence_id"]
                for item in diagnosis["diagnostic_receipt"]["evidence"]
            ]
            developer_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": diagnosis["run_id"],
                    **gate_binding(diagnosis),
                    "response": {
                        "status": "completed",
                        "summary": "Drive evidence defines the repair scope",
                        "payload": {
                            "root_cause": "Drive state requires a source repair",
                            "evidence_ids": evidence_ids,
                            "known_gaps": [],
                        },
                    },
                },
                task_id="complete-drive-verification",
                operation_id="complete-drive-verification-diagnosis",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": diagnosis["run_id"],
                    **gate_binding(developer_gate),
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "upgrade-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["build and Drive verification"],
                        },
                    },
                },
                task_id="complete-drive-verification",
                operation_id="complete-drive-verification-developer",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": diagnosis["run_id"],
                    **gate_binding(build_gate),
                    "response": {
                        "status": "completed",
                        "summary": "firmware artifact completed",
                        "payload": {
                            "source_revision": "upgrade-source",
                            "artifact_ref": artifact_ref(
                                product,
                                kind="openubmc-hpm",
                                target="192.0.2.23",
                                run_id=diagnosis["run_id"],
                                version="1.2.4",
                            ),
                            **compiled_validation_payload(
                                "complete-drive-verification"
                            ),
                        },
                    },
                },
                task_id="complete-drive-verification",
                operation_id="complete-drive-verification-build",
            )
            projection = operator.call_exposed_tool(
                "case_read",
                {"case_id": diagnosis["run_id"]},
                task_id="complete-drive-verification-operator",
                operation_id="complete-drive-verification-case-read",
            )
            verification_operation = next(
                operation
                for operation in projection["operations"]
                if operation.get("operation") == "debug_collect"
            )
            verification_evidence = operator.call_exposed_tool(
                "evidence_read",
                {
                    "case_id": diagnosis["run_id"],
                    "evidence_id": verification_operation["evidence_ids"][-1],
                    "target_id": "target-1",
                },
                task_id="complete-drive-verification-operator",
                operation_id="complete-drive-verification-evidence-read",
            )
        finally:
            operator.close()
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["outcome"]["status"], "completed")
        self.assertEqual(
            [name for name, _arguments in backend.calls],
            ["debug_run", "upgrade_run", "debug_collect"],
        )
        verification_arguments = backend.calls[-1][1]
        self.assertEqual(verification_arguments["mdb_expand_classes"], ["Drive"])
        self.assertEqual(
            verification_arguments["hardware_acceptance"],
            {
                "devices": [
                    {
                        "device_id": "Drive20",
                        "protocol": "NVMe",
                        "resource_id": "positive",
                    }
                ]
            },
        )
        self.assertEqual(verification_arguments["_minimum_target_epoch"], 1)
        self.assertEqual(
            verification_operation["diagnostic_receipt"]["status"],
            "complete",
        )
        self.assertEqual(
            json.loads(verification_evidence["body"])["target_epoch"],
            1,
        )

    def test_build_upgrade_closes_from_runtime_adapter_receipts(self) -> None:
        backend = AdapterProjectionBuildUpgradeBackend()
        service = RuntimeMcpService(backend)
        product = self.artifact_root / "adapter-projection-product.hpm"
        product.write_bytes(b"firmware-3.0.0")
        try:
            developer_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.70",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="adapter-projection-build-upgrade",
                operation_id="adapter-projection-start",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(developer_gate),
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "adapter-projection-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["build and target verification"],
                        },
                    },
                },
                task_id="adapter-projection-build-upgrade",
                operation_id="adapter-projection-source",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(build_gate),
                    "response": {
                        "status": "completed",
                        "summary": "firmware artifact completed",
                        "payload": {
                            "source_revision": "adapter-projection-source",
                            "artifact_ref": artifact_ref(
                                product,
                                kind="openubmc-hpm",
                                target="192.0.2.70",
                                run_id=developer_gate["run_id"],
                                version="3.0.0",
                            ),
                            **compiled_validation_payload(
                                "adapter-projection-build-upgrade"
                            ),
                        },
                    },
                },
                task_id="adapter-projection-build-upgrade",
                operation_id="adapter-projection-build",
            )
            projection = service._test.context_runtime.read_case(
                developer_gate["run_id"]
            )
            replayed = service.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": developer_gate["run_id"]},
                task_id="adapter-projection-replay",
                operation_id="adapter-projection-replay",
            )
            events = service._test.context_runtime.repository.events(
                developer_gate["run_id"]
            )
        finally:
            service.close()

        self.assertEqual(
            final["state"],
            "completed",
            json.dumps(projection["closeout"], ensure_ascii=False, indent=2),
        )
        self.assertEqual(final["outcome"]["status"], "completed")
        self.assertEqual(replayed["outcome"], final["outcome"])
        self.assertEqual(projection["closeout"]["closure_status"], "verified")
        self.assertEqual(projection["closeout"]["business_acceptance"], "passed")
        self.assertEqual(projection["closeout"]["identity_status"], "matched")
        self.assertEqual(projection["closeout"]["freshness_status"], "fresh")
        verification_arguments = backend.calls[-1][1]
        self.assertEqual(verification_arguments["_minimum_target_epoch"], 1)
        verification_state = next(
            state
            for state in projection["workflow_step_states"].values()
            if state.get("name") == "debug_collect"
        )
        self.assertEqual(verification_state["target_epoch"], 1)
        self.assertEqual(
            [name for name, _arguments in backend.calls].count("upgrade_run"),
            1,
        )
        self.assertEqual(
            sum(event["kind"] == "CloseoutRecorded" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["kind"] == "RunOutcomeRecorded" for event in events),
            1,
        )

    def test_deferred_build_upgrade_verification_resumes_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "deferred-build-upgrade.sqlite3"
            blobs = root / "deferred-build-upgrade-blobs"
            product = root / "deferred-build-upgrade.hpm"
            product.write_bytes(b"firmware-3.1.0")
            backend = DeferredAdapterProjectionBuildUpgradeBackend()
            first_service = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                developer_gate = first_service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.71",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "build-upgrade",
                    },
                    task_id="deferred-build-upgrade",
                    operation_id="deferred-build-upgrade-start",
                )
                build_gate = first_service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer_gate["run_id"],
                        **gate_binding(developer_gate),
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed",
                            "payload": {
                                "source_revision": "deferred-build-upgrade-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["build and target verification"],
                            },
                        },
                    },
                    task_id="deferred-build-upgrade",
                    operation_id="deferred-build-upgrade-source",
                )
                running = first_service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer_gate["run_id"],
                        **gate_binding(build_gate),
                        "response": {
                            "status": "completed",
                            "summary": "firmware artifact completed",
                            "payload": {
                                "source_revision": "deferred-build-upgrade-source",
                                "artifact_ref": artifact_ref(
                                    product,
                                    kind="openubmc-hpm",
                                    target="192.0.2.71",
                                    run_id=developer_gate["run_id"],
                                    version="3.1.0",
                                ),
                                **compiled_validation_payload(
                                    "deferred-build-upgrade"
                                ),
                            },
                        },
                    },
                    task_id="deferred-build-upgrade",
                    operation_id="deferred-build-upgrade-build",
                )
            finally:
                first_service.close()

            self.assertEqual(running["state"], "running")
            self.assertEqual(
                [name for name, _arguments in backend.calls].count("upgrade_run"),
                1,
            )

            second_service = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                final = second_service.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": developer_gate["run_id"]},
                    task_id="deferred-build-upgrade-resume",
                    operation_id="deferred-build-upgrade-resume",
                )
                projection = second_service._test.context_runtime.read_case(
                    developer_gate["run_id"]
                )
                events = second_service._test.context_runtime.repository.events(
                    developer_gate["run_id"]
                )
            finally:
                second_service.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["outcome"]["status"], "completed")
        self.assertEqual(backend.verification_attempts, 3)
        self.assertEqual(
            [name for name, _arguments in backend.calls].count("upgrade_run"),
            1,
        )
        self.assertEqual(projection["closeout"]["closure_status"], "verified")
        self.assertEqual(
            sum(event["kind"] == "RunVerificationDeferred" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["kind"] == "CloseoutRecorded" for event in events),
            1,
        )
        self.assertEqual(
            sum(event["kind"] == "RunOutcomeRecorded" for event in events),
            1,
        )

    def test_unknown_mutation_without_a_durable_effect_stays_incident(self) -> None:
        repository = InMemoryRuntimeRepository()
        transactions = BufferedRuntimeRepository(repository)
        driver = PersistentUnknownRunDriver(transactions)
        engine = RunEngine(
            driver,
            run_store=EventRunStore(
                repository,
                draft_buffer=transactions,
            ),
        )
        turn = engine.execute(
            ResumeRun("run-persistent-unknown"),
            task_id="persistent-unknown",
            operation_id="persistent-unknown-resume",
        )
        resumed = engine.execute(
            ResumeRun("run-persistent-unknown"),
            task_id="persistent-unknown",
            operation_id="persistent-unknown-resume-again",
        )

        self.assertEqual(turn.state, "incident")
        self.assertIsNotNone(turn.incident)
        self.assertEqual(turn.incident.code, "mutation_outcome_unknown")
        self.assertEqual(resumed.state, "incident")

    def test_reconcile_identity_tracks_the_current_unknown_effect(self) -> None:
        repository = InMemoryRuntimeRepository()
        transactions = BufferedRuntimeRepository(repository)
        driver = PersistentUnknownRunDriver(transactions)
        engine = RunEngine(
            driver,
            run_store=EventRunStore(
                repository,
                draft_buffer=transactions,
            ),
        )

        first = engine.execute(
            ReconcileRun("run-persistent-unknown"),
            task_id="reconcile-first-effect",
            operation_id="reconcile-first-request",
        )
        driver.effect_id = "mutation-unknown-2"
        second = engine.execute(
            ReconcileRun("run-persistent-unknown"),
            task_id="reconcile-second-effect",
            operation_id="reconcile-second-request",
        )
        projection = repository.load("run-persistent-unknown")
        assert projection is not None
        reconcile_decisions = [
            item
            for item in projection["run_decisions"]
            if str(item.get("command_id", "")).startswith("reconcile-")
        ]

        self.assertEqual(first.state, "incident")
        self.assertEqual(second.state, "incident")
        self.assertEqual(len(reconcile_decisions), 2)
        self.assertNotEqual(
            reconcile_decisions[0]["command_id"],
            reconcile_decisions[1]["command_id"],
        )

    def test_run_engine_requires_durable_command_dependencies(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "RunStore is required",
        ):
            RunEngine(PersistentUnknownRunDriver())

    def test_run_engine_rejects_unknown_transition_kinds(self) -> None:
        repository = InMemoryRuntimeRepository()
        engine = RunEngine(
            PersistentUnknownRunDriver(),
            run_store=EventRunStore(
                repository,
                draft_buffer=BufferedRuntimeRepository(repository),
            ),
        )

        with self.assertRaisesRegex(ValueError, "unsupported Run transition kind"):
            engine._apply_transition(  # noqa: SLF001 - architecture contract test
                "run-persistent-unknown",
                "outcome_typo",
                {"status": "completed"},
                operation_id="transition-typo",
            )

    def test_execute_automatically_reconciles_an_unknown_mutation(self) -> None:
        backend = FailOnceUpgradeSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.30",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                    "purpose": "repair and reconcile an interrupted upgrade",
                },
                task_id="execute-reconcile",
                operation_id="reconcile-start",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": first["run_id"],
                    **gate_binding(first),
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "reconcile-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["build and verify"],
                        },
                    },
                },
                task_id="execute-reconcile",
                operation_id="reconcile-developer",
            )
            self.assertEqual(build_gate["gate"]["name"], "build.artifact")
            product = self.artifact_root / "execute-reconcile-product.hpm"
            product.write_bytes(b"firmware-2.0.0")
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": first["run_id"],
                    **gate_binding(build_gate),
                    "response": {
                        "status": "completed",
                        "summary": "firmware artifact completed",
                        "payload": {
                            "source_revision": "reconcile-source",
                            "artifact_ref": artifact_ref(
                                product,
                                kind="openubmc-hpm",
                                target="192.0.2.30",
                                run_id=first["run_id"],
                                version="2.0.0",
                            ),
                            **compiled_validation_payload("execute-reconcile"),
                        },
                    },
                },
                task_id="execute-reconcile",
                operation_id="reconcile-build",
            )
        finally:
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertIsNone(final["incident"])
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(backend.upgrade_attempts, 2)

    def test_automatic_reconcile_returns_running_at_the_caller_deadline(self) -> None:
        backend = BlockingUnknownRecoveryUpgradeBackend()
        service = RuntimeMcpService(backend)
        product = self.artifact_root / "bounded-reconcile-product.hpm"
        product.write_bytes(b"firmware-2.1.0")
        try:
            developer_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.80",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="bounded-reconcile",
                operation_id="bounded-reconcile-start",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(developer_gate),
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "bounded-reconcile-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["build and verify"],
                        },
                    },
                },
                task_id="bounded-reconcile",
                operation_id="bounded-reconcile-source",
            )
            self.assert_bounded_running_turn(
                lambda: service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": developer_gate["run_id"],
                        **gate_binding(build_gate),
                        "response": {
                            "status": "completed",
                            "summary": "firmware artifact completed",
                            "payload": {
                                "source_revision": "bounded-reconcile-source",
                                "artifact_ref": artifact_ref(
                                    product,
                                    kind="openubmc-hpm",
                                    target="192.0.2.80",
                                    run_id=developer_gate["run_id"],
                                    version="2.1.0",
                                ),
                                **compiled_validation_payload(
                                    "bounded-reconcile"
                                ),
                            },
                        },
                        "deadline": 0.05,
                    },
                    task_id="bounded-reconcile",
                    operation_id="bounded-reconcile-build",
                ),
                started=backend.recovery_started,
            )
            self.assertEqual(backend.apply_calls, 1)
            self.assertEqual(backend.recovery_calls, 1)
            self.assertEqual(len(set(backend.operation_ids)), 1)

            backend.release_recovery.set()
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": developer_gate["run_id"],
                    "deadline": 1,
                },
                task_id="bounded-reconcile-final",
                operation_id="bounded-reconcile-final",
            )
            self.assertEqual(final["state"], "completed")
            self.assertEqual(backend.apply_calls, 1)
            self.assertEqual(backend.recovery_calls, 1)
            self.assertEqual(len(set(backend.operation_ids)), 1)
        finally:
            backend.release_recovery.set()
            service.close()

    def test_short_running_effect_reattaches_without_a_model_polling_turn(self) -> None:
        backend = RunningUpgradeSemanticBackend()
        service = RuntimeMcpService(backend)
        product = self.artifact_root / "running-upgrade-product.hpm"
        product.write_bytes(b"running-upgrade-firmware")
        try:
            developer_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.31",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "build-upgrade",
                },
                task_id="running-upgrade",
                operation_id="running-upgrade-start",
            )
            build_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(developer_gate),
                    "response": {
                        "status": "completed",
                        "summary": "source repair completed",
                        "payload": {
                            "source_revision": "running-upgrade-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["build and verify"],
                        },
                    },
                },
                task_id="running-upgrade",
                operation_id="running-upgrade-source",
            )
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(build_gate),
                    "response": {
                        "status": "completed",
                        "summary": "firmware artifact completed",
                        "payload": {
                            "source_revision": "running-upgrade-source",
                            "artifact_ref": artifact_ref(
                                product,
                                kind="openubmc-hpm",
                                target="192.0.2.31",
                                run_id=developer_gate["run_id"],
                                version="3.0.0",
                            ),
                            **compiled_validation_payload("running-upgrade"),
                        },
                    },
                },
                task_id="running-upgrade",
                operation_id="running-upgrade-build",
            )
            running_projection = service._test.context_runtime.read_case(
                developer_gate["run_id"]
            )
        finally:
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(backend.upgrade_attempts, 2)
        self.assertEqual(len(backend.upgrade_operation_ids), 2)
        self.assertEqual(
            len(set(backend.upgrade_operation_ids)),
            1,
            (backend.upgrade_operation_ids, running_projection["operations"]),
        )

    def test_execute_deadline_returns_running_after_persisting_effect_intent(self) -> None:
        backend = BlockingLivePatchSemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "bounded-live-patch.lua"
        patch_file.write_bytes(b"return 'bounded'\n")
        timer = threading.Timer(1, backend.release.set)
        timer.start()
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.73",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="bounded-live-patch",
                operation_id="bounded-live-patch-start",
            )

            def persisted(effect_id: str) -> bool:
                for event in service._test.context_runtime.repository.events(
                    waiting["run_id"]
                ):
                    intent = event["payload"].get("effect_intent")
                    if (
                        event["kind"] == "RunDecisionCommitted"
                        and isinstance(intent, dict)
                        and intent.get("effect_id") == effect_id
                    ):
                        return True
                return False

            backend.inspect_persisted_intent = persisted
            started_at = time.monotonic()
            response_action = {
                "kind": "respond",
                "run_id": waiting["run_id"],
                **gate_binding(waiting),
                "submission_id": "bounded-live-patch-submission",
                "response": {
                    "status": "completed",
                    "summary": "patch is ready",
                    "payload": {
                        "source_revision": "bounded-source",
                        "authored_files": ["src/fix.lua"],
                        "verification_plan": ["verify replacement"],
                        "artifact_ref": artifact_ref(
                            patch_file,
                            kind="openubmc-live-patch",
                            target="192.0.2.73",
                            run_id=waiting["run_id"],
                        ),
                        "remote_path": "/tmp/bounded-live-patch.lua",
                        "restart_scope": "none",
                    },
                },
                "deadline": 0.05,
            }
            running = service.call_exposed_tool(
                "execute",
                response_action,
                task_id="bounded-live-patch",
                operation_id="bounded-live-patch-submit",
            )
            elapsed = time.monotonic() - started_at

            self.assertTrue(backend.started.wait(timeout=0.5))
            self.assertLess(elapsed, 0.5)
            self.assertEqual(running["state"], "running")
            self.assertTrue(backend.intent_was_persisted)

            replayed = service.call_exposed_tool(
                "execute",
                {**response_action, "deadline": 0.02},
                task_id="bounded-live-patch-replay",
                operation_id="bounded-live-patch-replay",
            )
            resumed = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": waiting["run_id"],
                    "deadline": 0.02,
                },
                task_id="bounded-live-patch-resume",
                operation_id="bounded-live-patch-resume",
            )
            self.assertEqual(replayed["state"], "running")
            self.assertEqual(resumed["state"], "running")
            self.assertEqual(len(backend.operation_ids), 1)

            backend.release.set()
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": waiting["run_id"],
                    "deadline": 1,
                },
                task_id="bounded-live-patch-final",
                operation_id="bounded-live-patch-final",
            )
            self.assertEqual(final["state"], "completed")
            self.assertEqual(len(backend.operation_ids), 1)
            self.assertEqual(len(set(backend.operation_ids)), 1)
        finally:
            backend.release.set()
            timer.cancel()
            service.close()

    def test_sqlite_restart_reexecutes_a_persisted_read_only_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "read-restart.sqlite3"
            blobs = root / "read-restart-blobs"
            first_backend = SemanticBackend()
            first = RuntimeMcpService(
                first_backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            dormant = DormantEffectRunner()
            first._test.effect_runner.close()
            first._test.effect_runner = dormant
            first._test.run_engine.effect_runner = dormant
            try:
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.74",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                        "deadline": 0.01,
                    },
                    task_id="read-restart",
                    operation_id="read-restart-start",
                )
            finally:
                first.close()

            self.assertEqual(running["state"], "running")
            self.assertEqual(first_backend.calls, [])
            persisted_effect_id = first._test.context_runtime.read_case(
                running["run_id"]
            )["effect_intents"][-1]["effect_id"]

            second_backend = SemanticBackend()
            second = RuntimeMcpService(
                second_backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                waiting = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": running["run_id"],
                        "deadline": 1,
                    },
                    task_id="read-restart-resume",
                    operation_id="read-restart-resume",
                )
                operations = second._test.context_runtime.read_case(
                    running["run_id"]
                )["operations"]
            finally:
                second.close()

            self.assertEqual(waiting["state"], "waiting_response")
            self.assertEqual(
                [name for name, _arguments in second_backend.calls],
                ["debug_run"],
            )
            self.assertEqual(
                [
                    item["operation_id"]
                    for item in operations
                    if item.get("operation") == "debug_run"
                ],
                [persisted_effect_id],
            )

    def test_effect_identity_is_unique_across_concurrent_runs(self) -> None:
        backend = BlockingDebugSemanticBackend()
        service = RuntimeMcpService(backend)
        try:
            first = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.76",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "deadline": 0.02,
                },
                task_id="effect-identity-first",
                operation_id="effect-identity-first",
            )
            second = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.76",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "deadline": 0.02,
                },
                task_id="effect-identity-second",
                operation_id="effect-identity-second",
            )

            self.assertTrue(backend.started.wait(timeout=0.5))
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(len(backend.operation_ids), 2)
            self.assertEqual(len(set(backend.operation_ids)), 2)
        finally:
            backend.release.set()
            service.close()

    def test_running_effect_reattach_uses_bounded_internal_backoff(self) -> None:
        backend = PersistentlyRunningDebugBackend()
        service = RuntimeMcpService(backend)
        try:
            started_at = time.monotonic()
            running = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.77",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "deadline": 0.65,
                },
                task_id="running-backoff",
                operation_id="running-backoff-start",
            )
            elapsed = time.monotonic() - started_at
        finally:
            service.close()

        self.assertEqual(running["state"], "running")
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLessEqual(backend.debug_attempts, 3)

    def test_sqlite_restart_reconciles_a_persisted_mutation_without_reapply(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "mutation-restart.sqlite3"
            blobs = root / "mutation-restart-blobs"
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            patch_file = root / "mutation-restart.lua"
            patch_file.write_bytes(b"return 'restart-safe'\n")
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.75",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="mutation-restart",
                    operation_id="mutation-restart-start",
                )
                first._test.effect_runner.close()
                dormant = DormantEffectRunner()
                first._test.effect_runner = dormant
                first._test.run_engine.effect_runner = dormant
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "mutation is durably scheduled",
                            "payload": {
                                "source_revision": "mutation-restart-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["verify after reconcile"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.75",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/tmp/mutation-restart.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 0.01,
                    },
                    task_id="mutation-restart",
                    operation_id="mutation-restart-submit",
                )
            finally:
                first.close()

            persisted_effect_id = first._test.context_runtime.read_case(
                running["run_id"]
            )["effect_intents"][-1]["effect_id"]
            backend = RecoveryAwareLivePatchBackend()
            second = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": running["run_id"],
                        "deadline": 1,
                    },
                    task_id="mutation-restart-resume",
                    operation_id="mutation-restart-resume",
                )
            finally:
                second.close()

            self.assertEqual(final["state"], "completed")
            self.assertEqual(backend.apply_calls, 0)
            self.assertEqual(backend.reconcile_calls, 1)
            self.assertEqual(backend.operation_ids, [persisted_effect_id])

    def test_restart_recovery_without_journal_stays_incident_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "missing-journal.sqlite3"
            blobs = root / "missing-journal-blobs"
            patch_file = root / "missing-journal.lua"
            patch_file.write_bytes(b"return 'never-reapply'\n")
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.78",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="missing-journal",
                    operation_id="missing-journal-start",
                )
                first._test.effect_runner.close()
                dormant = DormantEffectRunner()
                first._test.effect_runner = dormant
                first._test.run_engine.effect_runner = dormant
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "mutation is durably scheduled",
                            "payload": {
                                "source_revision": "missing-journal-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.78",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/tmp/missing-journal.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 0.01,
                    },
                    task_id="missing-journal",
                    operation_id="missing-journal-submit",
                )
            finally:
                first.close()

            backend = MissingJournalRecoveryBackend()
            second = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                incident = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": running["run_id"],
                        "deadline": 1,
                    },
                    task_id="missing-journal-recovery",
                    operation_id="missing-journal-recovery",
                )
                repeated = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": running["run_id"],
                        "deadline": 1,
                    },
                    task_id="missing-journal-repeated",
                    operation_id="missing-journal-repeated",
                )
            finally:
                second.close()

        self.assertEqual(incident["state"], "incident")
        self.assertEqual(repeated["state"], "incident")
        self.assertEqual(backend.apply_calls, 0)
        self.assertEqual(backend.recovery_calls, 1)

    def test_explicit_reconcile_returns_running_at_the_caller_deadline(self) -> None:
        patch_file = self.artifact_root / "explicit-reconcile.lua"
        patch_file.write_bytes(b"return 'explicit-reconcile'\n")
        backend = FailThenBlockRecoveryLivePatchBackend()
        service = RuntimeMcpService(backend)
        try:
            waiting = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.81",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="explicit-reconcile",
                operation_id="explicit-reconcile-start",
            )
            incident = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": waiting["run_id"],
                    **gate_binding(waiting),
                    "response": {
                        "status": "completed",
                        "summary": "mutation is ready",
                        "payload": {
                            "source_revision": "explicit-reconcile-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.81",
                                run_id=waiting["run_id"],
                            ),
                            "remote_path": "/tmp/explicit-reconcile.lua",
                            "restart_scope": "none",
                        },
                    },
                    "deadline": 1,
                },
                task_id="explicit-reconcile",
                operation_id="explicit-reconcile-submit",
            )
            self.assertEqual(incident["state"], "incident")

            self.assert_bounded_running_turn(
                lambda: service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "control",
                        "command": "reconcile",
                        "run_id": waiting["run_id"],
                        "deadline": 0.05,
                    },
                    task_id="explicit-reconcile-control",
                    operation_id="explicit-reconcile-control",
                ),
                started=backend.recovery_started,
            )

            backend.release_recovery.set()
            final = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": waiting["run_id"],
                    "deadline": 1,
                },
                task_id="explicit-reconcile-final",
                operation_id="explicit-reconcile-final",
            )
        finally:
            backend.release_recovery.set()
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(backend.apply_calls, 1)
        self.assertEqual(backend.recovery_calls, 2)
        self.assertEqual(len(set(backend.operation_ids)), 1)

    def test_recovery_boundary_converges_after_a_concurrent_run_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "recovery-race.sqlite3"
            blobs = root / "recovery-race-blobs"
            patch_file = root / "recovery-race.lua"
            patch_file.write_bytes(b"return 'recover-once'\n")
            first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                waiting = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.79",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="recovery-race",
                    operation_id="recovery-race-start",
                )
                first._test.effect_runner.close()
                dormant = DormantEffectRunner()
                first._test.effect_runner = dormant
                first._test.run_engine.effect_runner = dormant
                running = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": waiting["run_id"],
                        **gate_binding(waiting),
                        "response": {
                            "status": "completed",
                            "summary": "mutation is durably scheduled",
                            "payload": {
                                "source_revision": "recovery-race-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.79",
                                    run_id=waiting["run_id"],
                                ),
                                "remote_path": "/tmp/recovery-race.lua",
                                "restart_scope": "none",
                            },
                        },
                        "deadline": 0.01,
                    },
                    task_id="recovery-race",
                    operation_id="recovery-race-submit",
                )
            finally:
                first.close()

            persisted_effect_id = first._test.context_runtime.read_case(
                running["run_id"]
            )["effect_intents"][-1]["effect_id"]
            backend = RecoveryAwareLivePatchBackend()
            second = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            run_store = RecoveryBoundaryConflictOnceStore(
                second._test.run_engine.run_store
            )
            second._test.run_engine.run_store = run_store
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "resume",
                        "run_id": running["run_id"],
                        "deadline": 1,
                    },
                    task_id="recovery-race-resume",
                    operation_id="recovery-race-resume",
                )
                events = second._test.context_runtime.repository.events(
                    running["run_id"]
                )
            finally:
                second.close()

        self.assertEqual(final["state"], "completed")
        self.assertTrue(run_store.raced)
        self.assertEqual(backend.apply_calls, 0)
        self.assertEqual(backend.reconcile_calls, 1)
        self.assertTrue(
            any(
                event["kind"] == "OperationReconciled"
                and event.get("operation_id") == persisted_effect_id
                for event in events
            )
        )
        self.assertEqual(
            sum(
                event["kind"] == "OperationTerminal"
                and event["payload"].get("status")
                == "mutation_outcome_unknown"
                for event in events
            ),
            0,
        )
        recovery_decisions = [
            event["payload"]
            for event in events
            if event["kind"] == "RunDecisionCommitted"
            and str(event["payload"].get("command_id", "")).startswith(
                "recover-"
            )
        ]
        self.assertEqual(len(recovery_decisions), 1)
        self.assertEqual(
            recovery_decisions[0]["effect_intent"]["effect_id"],
            persisted_effect_id,
        )

    def test_verification_failure_is_deferred_and_resumed_without_reapplying(self) -> None:
        backend = DeferredVerificationSemanticBackend()
        service = RuntimeMcpService(backend)
        patch_file = self.artifact_root / "deferred-verification-fix.lua"
        patch_file.write_bytes(b"return 'verified-later'\n")
        try:
            developer_gate = service.call_exposed_tool(
                "execute",
                {
                    "kind": "start",
                    "target": "192.0.2.32",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "live-patch",
                },
                task_id="deferred-verification",
                operation_id="deferred-verification-start",
            )
            running = service.call_exposed_tool(
                "execute",
                {
                    "kind": "respond",
                    "run_id": developer_gate["run_id"],
                    **gate_binding(developer_gate),
                    "response": {
                        "status": "completed",
                        "summary": "source repair ready",
                        "payload": {
                            "source_revision": "deferred-verification-source",
                            "authored_files": ["src/fix.lua"],
                            "verification_plan": ["fresh verification"],
                            "artifact_ref": artifact_ref(
                                patch_file,
                                kind="openubmc-live-patch",
                                target="192.0.2.32",
                                run_id=developer_gate["run_id"],
                            ),
                            "remote_path": "/opt/bmc/apps/fix.lua",
                            "restart_scope": "skynet",
                        },
                    },
                },
                task_id="deferred-verification",
                operation_id="deferred-verification-source",
            )
            projection = service._test.context_runtime.read_case(developer_gate["run_id"])
            deferred_events = [
                event
                for event in service._test.context_runtime.repository.events(
                    developer_gate["run_id"]
                )
                if event["kind"] == "RunVerificationDeferred"
            ]
            self.assertEqual(running["state"], "running")
            self.assertIn("retry fresh target verification", running["next"])
            self.assertEqual(len(deferred_events), 1)
            self.assertTrue(
                any(
                    state.get("name") == "debug_collect"
                    and state.get("status") == "pending_retry"
                    for state in projection["workflow_step_states"].values()
                )
            )

            final = service.call_exposed_tool(
                "execute",
                {"kind": "resume", "run_id": developer_gate["run_id"]},
                task_id="deferred-verification-resume",
                operation_id="deferred-verification-resume-1",
            )
        finally:
            service.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(backend.verification_attempts, 3)
        self.assertEqual(
            [name for name, _arguments in backend.calls].count("live_patch_run"),
            1,
        )

    def test_execute_workflows_resume_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            source_database = root / "source.sqlite3"
            source_blobs = root / "source-blobs"
            source_first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(source_database),
                blob_repository=FilesystemBlobRepository(source_blobs),
            )
            try:
                source_gate = source_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.51",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                    task_id="restart-source",
                    operation_id="restart-source-start",
                )
            finally:
                source_first.close()
            source_second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(source_database),
                blob_repository=FilesystemBlobRepository(source_blobs),
            )
            try:
                source_final = source_second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": source_gate["run_id"],
                        **gate_binding(source_gate),
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed after restart",
                            "payload": {
                                "source_revision": "restart-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["run regression tests"],
                            },
                        },
                    },
                    task_id="restart-source-resumed",
                    operation_id="restart-source-respond",
                )
            finally:
                source_second.close()
            self.assertEqual(source_final["state"], "completed")

            live_database = root / "live.sqlite3"
            live_blobs = root / "live-blobs"
            live_first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(live_database),
                blob_repository=FilesystemBlobRepository(live_blobs),
            )
            try:
                live_gate = live_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.52",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="restart-live",
                    operation_id="restart-live-start",
                )
            finally:
                live_first.close()
            live_second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(live_database),
                blob_repository=FilesystemBlobRepository(live_blobs),
            )
            live_patch = root / "restart-live-fix.lua"
            live_patch.write_bytes(b"return 'restart-fixed'\n")
            try:
                live_final = live_second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": live_gate["run_id"],
                        **gate_binding(live_gate),
                        "response": {
                            "status": "completed",
                            "summary": "source repair restored after restart",
                            "payload": {
                                "source_revision": "restart-live",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    live_patch,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.52",
                                    run_id=live_gate["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                    task_id="restart-live-resumed",
                    operation_id="restart-live-respond",
                )
            finally:
                live_second.close()
            self.assertEqual(live_final["state"], "completed")

            build_database = root / "build.sqlite3"
            build_blobs = root / "build-blobs"
            build_first = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(build_database),
                blob_repository=FilesystemBlobRepository(build_blobs),
            )
            try:
                build_developer = build_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.53",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "build-upgrade",
                    },
                    task_id="restart-build",
                    operation_id="restart-build-start",
                )
                build_gate = build_first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": build_developer["run_id"],
                        **gate_binding(build_developer),
                        "response": {
                            "status": "completed",
                            "summary": "source repair completed",
                            "payload": {
                                "source_revision": "restart-build",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["build and verify"],
                            },
                        },
                    },
                    task_id="restart-build",
                    operation_id="restart-build-developer",
                )
            finally:
                build_first.close()
            build_second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(build_database),
                blob_repository=FilesystemBlobRepository(build_blobs),
            )
            build_product = root / "restart-build-product.hpm"
            build_product.write_bytes(b"restart-firmware-2.0.0")
            try:
                build_final = build_second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": build_gate["run_id"],
                        **gate_binding(build_gate),
                        "response": {
                            "status": "completed",
                            "summary": "artifact restored after restart",
                            "payload": {
                                "source_revision": "restart-build",
                                "artifact_ref": artifact_ref(
                                    build_product,
                                    kind="openubmc-hpm",
                                    target="192.0.2.53",
                                    run_id=build_gate["run_id"],
                                    version="2.0.0",
                                ),
                                **compiled_validation_payload("restart-build"),
                            },
                        },
                    },
                    task_id="restart-build-resumed",
                    operation_id="restart-build-respond",
                )
            finally:
                build_second.close()
            self.assertEqual(build_final["state"], "completed")

    def test_live_patch_unknown_mutation_reconciles_after_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            database = root / "live-failure.sqlite3"
            blobs = root / "live-failure-blobs"
            first = RuntimeMcpService(
                FailLivePatchSemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                gate = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.54",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="restart-live-failure",
                    operation_id="restart-live-failure-start",
                )
                patch = root / "restart-live-failure-fix.lua"
                patch.write_bytes(b"return 'unknown-outcome'\n")
                blocked = first.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": gate["run_id"],
                        **gate_binding(gate),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "restart-live-failure",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh verification"],
                                "artifact_ref": artifact_ref(
                                    patch,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.54",
                                    run_id=gate["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                    task_id="restart-live-failure",
                    operation_id="restart-live-failure-respond",
                )
            finally:
                first.close()
            self.assertEqual(blocked["state"], "incident")
            self.assertEqual(
                blocked["incident"]["code"], "mutation_outcome_unknown"
            )

            second = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            try:
                final = second.call_exposed_tool(
                    "execute",
                    {
                        "kind": "control",
                        "run_id": gate["run_id"],
                        "command": "reconcile",
                    },
                    task_id="restart-live-failure-reconcile",
                    operation_id="restart-live-failure-control",
                )
            finally:
                second.close()
        self.assertEqual(final["state"], "completed")

    def test_agent_and_operator_profiles_are_disjoint(self) -> None:
        agent = RuntimeMcpService(SemanticBackend())
        operator = RuntimeMcpService(SemanticBackend(), interface_profile="operator")
        try:
            self.assertEqual(agent.interface_catalog.names(), ("observe", "execute"))
            self.assertIn("evidence_attach", operator.interface_catalog.names())
            self.assertIn("evidence_query", operator.interface_catalog.names())
            self.assertIn("evidence_read", operator.interface_catalog.names())
            self.assertNotIn("debug_run", operator.interface_catalog.names())
        finally:
            agent.close()
            operator.close()

    def test_retired_writers_do_not_record_new_compatibility_telemetry(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            service.call_tool(
                "debug_run",
                {
                    "ip": "192.0.2.78",
                    "intent": "diagnosis-only",
                    "final_purpose": "internal domain dispatch",
                },
                task_id="internal-domain-task",
                operation_id="internal-domain-call",
            )
            status = service.call_tool(
                "runtime_status",
                {},
                task_id="internal-domain-status",
                operation_id="internal-domain-status",
            )
        finally:
            service.close()

        telemetry = status["compatibility_telemetry"]
        self.assertEqual(telemetry["total_calls"], 0)
        self.assertEqual(telemetry["operation_counts"], {})
        self.assertEqual(telemetry["total_features"], 0)
        self.assertEqual(telemetry["feature_counts"], {})

    def test_operator_status_preserves_persisted_compatibility_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "compatibility-telemetry.sqlite3"
            telemetry_repository = SQLiteCompatibilityTelemetryRepository(database)
            seed_compatibility_history(
                database,
                (
                    ("operation", "debug_run", 1, 1234.5),
                    ("feature", "observe.assurance", 1, 1234.5),
                ),
            )
            self.assertFalse(hasattr(telemetry_repository, "increment"))
            service = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                interface_profile="operator",
            )
            try:
                initial_status = service.call_exposed_tool(
                    "runtime_status",
                    {},
                    task_id="compatibility-persistent-shared-status",
                    operation_id="compatibility-persistent-shared-status",
                )
            finally:
                service.close()

            reopened = RuntimeMcpService(
                SemanticBackend(),
                context_repository=SQLiteRuntimeRepository(database),
                interface_profile="operator",
            )
            try:
                restarted_status = reopened.call_exposed_tool(
                    "runtime_status",
                    {},
                    task_id="compatibility-persistent-restarted-status",
                    operation_id="compatibility-persistent-restarted-status",
                )
            finally:
                reopened.close()

        expected = {"debug_run": 1}
        self.assertEqual(
            initial_status["compatibility_telemetry"]["operation_counts"],
            expected,
        )
        self.assertEqual(
            restarted_status["compatibility_telemetry"]["operation_counts"],
            expected,
        )
        for status in (initial_status, restarted_status):
            self.assertEqual(
                status["compatibility_telemetry"]["feature_counts"],
                {"observe.assurance": 1},
            )
        self.assertEqual(
            restarted_status["compatibility_telemetry"]["last_seen_at"],
            initial_status["compatibility_telemetry"]["last_seen_at"],
        )



    def test_gateway_preserves_mutation_journal_owned_identity(self) -> None:
        class JournalIdentityBackend(SemanticBackend):
            def live_patch_run(self, task, arguments, context):
                value = super().live_patch_run(task, arguments, context)
                value["journal"] = {
                    **value["journal"],
                    "operation_id": "backend-owned-effect",
                }
                return value

        service = RuntimeMcpService(JournalIdentityBackend())
        try:
            value = service._invoke_registered_domain_adapter(
                "live_patch_run",
                RuntimeSDKContext(
                    task_id="journal-identity",
                    operation_id="runtime-effect",
                    timeout_seconds=5,
                ),
                {
                    "ip": "192.0.2.82",
                    "intent": "live-patch",
                    "local_path": "/tmp/fix.lua",
                    "artifact_sha256": "a" * 64,
                    "remote_path": "/opt/bmc/apps/fix.lua",
                },
            )
        finally:
            service.close()

        self.assertEqual(value["journal"]["operation_id"], "backend-owned-effect")
        self.assertNotIn("backend_operation_id", value["journal"])

    def test_agent_endpoint_rejects_raw_evidence_tool(self) -> None:
        endpoint = JsonRpcMcpEndpoint(self.service, session_task_id="agent-session")
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "evidence_read",
                    "arguments": {"case_id": "case-x", "evidence_id": "evidence-x"},
                },
            }
        )
        self.assertTrue(response["result"]["isError"])

    def test_json_rpc_request_ids_are_scoped_to_the_mcp_session(self) -> None:
        first_endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="start-client-a",
        )
        second_endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="start-client-b",
        )
        first = first_endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.64",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                },
            }
        )
        second = second_endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "execute",
                    "arguments": {
                        "kind": "start",
                        "target": "192.0.2.65",
                        "intent": "diagnose-and-fix",
                        "delivery_strategy": "source-only",
                    },
                },
            }
        )

        self.assertFalse(first["result"]["isError"])
        self.assertFalse(second["result"]["isError"])
        self.assertNotEqual(
            first["result"]["structuredContent"]["run_id"],
            second["result"]["structuredContent"]["run_id"],
        )

    def test_explicit_operation_identity_reattaches_across_mcp_sessions(self) -> None:
        first_endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="reattach-client-a",
        )
        second_endpoint = JsonRpcMcpEndpoint(
            self.service,
            session_task_id="reattach-client-b",
        )
        action = {
            "name": "execute",
            "arguments": {
                "kind": "start",
                "target": "192.0.2.66",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
            },
            "_meta": {"openubmc/operationId": "mcp-stable-start-1"},
        }
        first = first_endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": action,
            }
        )
        replayed = second_endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": action,
            }
        )

        self.assertFalse(first["result"]["isError"])
        self.assertFalse(replayed["result"]["isError"])
        self.assertEqual(
            replayed["result"]["structuredContent"]["run_id"],
            first["result"]["structuredContent"]["run_id"],
        )
        first_structured = first["result"]["structuredContent"]
        first_text = first["result"]["content"][0]["text"]
        replayed_text = replayed["result"]["content"][0]["text"]
        gate = first_structured["gate"]
        self.assertEqual(replayed_text, first_text)
        self.assertIsNone(first_structured["next_action"])
        self.assertTrue(first_structured["response_required"])
        self.assertTrue(gate["submission_id"].startswith("gate-submit-"))
        self.assertIn(f"gate_id={gate['gate_id']}", first_text)
        self.assertIn(f"gate_version={gate['gate_version']}", first_text)
        self.assertIn(f"schema_digest={gate['schema_digest']}", first_text)
        self.assertIn(f"submission_id={gate['submission_id']}", first_text)

    def test_agent_endpoint_renders_observation_values_in_bounded_text_content(self) -> None:
        endpoint = JsonRpcMcpEndpoint(self.service, session_task_id="observe-session")
        response = endpoint.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "observe",
                    "arguments": {
                        "target": "192.0.2.10",
                        "selectors": [
                            {"kind": "mdb", "queries": ["lsprop Object0"]}
                        ],
                    },
                },
            }
        )

        text = response["result"]["content"][0]["text"]
        self.assertIn("lsprop Object0", text)
        self.assertIn("Value", text)
        self.assertLessEqual(len(text.encode("utf-8")), OBSERVATION_MAX_BYTES)

    def test_stdio_rejects_an_oversized_frame_and_processes_the_next_request(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="stdio-session")
        server = StdioMcpServer(endpoint)
        oversized = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "padding": "x" * STDIO_FRAME_MAX_BYTES}
        )
        valid = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        output = io.StringIO()

        server.serve(io.StringIO(oversized + "\n" + valid + "\n"), output)

        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["id"], 2)
        self.assertEqual(
            [tool["name"] for tool in responses[1]["result"]["tools"]],
            ["observe", "execute"],
        )

    def test_stdio_cancellation_uses_the_same_derived_operation_identity(self) -> None:
        started = threading.Event()
        released = threading.Event()
        cancellations: list[tuple[str, str]] = []

        class FakeService:
            def cancel_operation(self, task_id: str, operation_id: str) -> bool:
                cancellations.append((task_id, operation_id))
                released.set()
                return True

            @staticmethod
            def close() -> None:
                return None

        class FakeEndpoint:
            service = FakeService()

            @staticmethod
            def task_id_for_params(_params) -> str:
                return "stdio-cancel-task"

            @staticmethod
            def operation_id_for_params(_params, _request_id) -> str:
                return "stable-operation-id"

            @staticmethod
            def handle(message):
                if message.get("method") == "notifications/cancelled":
                    raise AssertionError(
                        "stdio cancellation should use the tracked operation"
                    )
                started.set()
                if not released.wait(timeout=2):
                    raise AssertionError("stdio cancellation did not release the call")
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {"cancelled": True},
                }

        class CancellationReader(io.StringIO):
            def __init__(self) -> None:
                call = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {
                            "_meta": {
                                "openubmc/operationId": "stable-operation-id"
                            },
                            "name": "execute",
                            "arguments": {"kind": "resume", "run_id": "run-x"},
                        },
                    }
                )
                cancelled = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 7},
                    }
                )
                super().__init__(call + "\n" + cancelled + "\n")
                self._reads = 0

            def readline(self, size: int = -1) -> str:
                self._reads += 1
                if self._reads == 2 and not started.wait(timeout=2):
                    raise AssertionError("stdio tool call did not start")
                return super().readline(size)

        output = io.StringIO()
        StdioMcpServer(FakeEndpoint()).serve(CancellationReader(), output)

        self.assertEqual(
            cancellations,
            [("stdio-cancel-task", "stable-operation-id")],
        )
        self.assertTrue(json.loads(output.getvalue())["result"]["cancelled"])

    def test_stdio_reader_never_uses_an_unbounded_readline(self) -> None:
        class BoundedReader(io.StringIO):
            def __init__(self, value: str) -> None:
                super().__init__(value)
                self.readline_limits: list[int] = []

            def readline(self, size: int = -1) -> str:
                self.readline_limits.append(size)
                if size < 0:
                    raise AssertionError("stdio reader attempted an unbounded readline")
                return super().readline(size)

        service = RuntimeMcpService(SemanticBackend())
        try:
            endpoint = JsonRpcMcpEndpoint(
                service, session_task_id="stdio-bounded-session"
            )
            server = StdioMcpServer(endpoint, max_frame_bytes=2048)
            reader = BoundedReader(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                )
                + "\n"
            )
            output = io.StringIO()

            server.serve(reader, output)
        finally:
            service.close()

        self.assertTrue(reader.readline_limits)
        self.assertEqual(set(reader.readline_limits), {2049})
        self.assertEqual(json.loads(output.getvalue())["id"], 1)

    def test_stdio_recursion_error_does_not_stop_the_next_request(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        endpoint = JsonRpcMcpEndpoint(service, session_task_id="stdio-depth-session")
        server = StdioMcpServer(endpoint)
        pathological = '{"a":' * 10_000 + "0" + "}" * 10_000
        valid = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )
        output = io.StringIO()

        server.serve(io.StringIO(pathological + "\n" + valid + "\n"), output)

        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], 2)

    def test_legacy_freshness_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "profile"):
            self.service.call_tool(
                "debug_collect",
                {"ip": "192.0.2.10", "profile": "freshness"},
                task_id="legacy-task",
                operation_id="legacy-1",
            )


if __name__ == "__main__":
    unittest.main()
