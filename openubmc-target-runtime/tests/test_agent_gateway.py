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
    RevisionConflict,
    ReferenceViolation,
    ResumeRun,
    RunDecision,
    RunEngine,
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
    decode_run_command,
)
from openubmc_target_runtime.context_runtime import (  # noqa: E402
    BufferedRuntimeRepository,
)
from openubmc_target_runtime.diagnostic_receipt import (  # noqa: E402
    build_diagnostic_receipt,
)
from openubmc_target_runtime.run_store import RunCommitRequest  # noqa: E402
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
                "freshness": {"status": "fresh"},
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
                    "description": "x" * 20_000,
                },
            },
        )


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


class PersistentUnknownRunDriver:
    def __init__(self, repository=None) -> None:
        self.repository = repository

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
                    "operation_id": "mutation-unknown-1",
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
        self.assertNotIn("max_steps", execute_schema["properties"])

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

    def test_observe_compaction_is_incomplete_and_never_exceeds_four_kib(self) -> None:
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

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertFalse(receipt["coverage"]["complete"])
        self.assertTrue(receipt["content_compacted"])
        self.assertNotIn("observation_ref", receipt)

    def test_observe_hard_limit_survives_oversized_target_metadata(self) -> None:
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

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(receipt["content_compacted"])

    def test_observe_hard_limit_survives_maximum_legal_scope_and_large_result(self) -> None:
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
                            "queries": ["q" * 1024],
                        }
                    ],
                },
                task_id="observe-maximum-scope",
                operation_id="observe-maximum-scope-1",
            )
        finally:
            service.close()

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertTrue(receipt["content_compacted"])

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

        self.assertLessEqual(encoded_size(receipt), OBSERVATION_MAX_BYTES)
        self.assertTrue(receipt["content_compacted"])
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
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(
            receipt["coverage"],
            {
                "requested": 10,
                "evaluable": 10,
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
                "version",
                "uptime",
                "target-clock",
                "logs",
                "service",
                "mdb-1",
                "file-3",
                "mdb-expand-1",
                "active-alarms",
                "correlation",
            },
        )
        self.assertEqual(results["active-alarms"]["status"], "available")
        self.assertEqual(
            results["file-3"]["value"]["lines"],
            ["custom diagnostic value"],
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
        self.assertEqual(receipt["coverage"]["requested"], 1)
        self.assertEqual(receipt["results"][0]["result_id"], "logs")
        self.assertEqual(receipt["results"][0]["status"], "not_checked")
        self.assertEqual(receipt["results"][0]["gap"], "result_not_visible")

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
        correlation = receipt["results"][0]
        self.assertEqual(receipt["status"], "blocked")
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
                    "entry_arguments": {"files": ["/etc/version.json"]},
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
        self.assertEqual(public["status"], "partial")
        self.assertEqual(public["coverage"]["requested"], 1024)
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
        self.assertEqual(replayed.to_public_dict(), first.to_public_dict())
        projection = self.service._test.context_runtime.read_case(waiting.run_id)
        self.assertEqual(
            sum(
                item.get("command_id") == "typed-normalized-submission"
                for item in projection["run_decisions"]
            ),
            1,
        )

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
        self.assertEqual(projection["current_turn"]["gate"], waiting["gate"])

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
        self.assertEqual(decision["turn"]["gate"], build_gate["gate"])
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

    def test_resume_persists_and_replays_a_decision_by_operation_identity(self) -> None:
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

        first = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="atomic-resume-first",
            operation_id="atomic-resume-command",
        )
        replayed = self.service.call_exposed_tool(
            "execute",
            action,
            task_id="atomic-resume-replay",
            operation_id="atomic-resume-command",
        )
        projection = self.service._test.context_runtime.read_case(waiting["run_id"])
        decisions = [
            item
            for item in projection["run_decisions"]
            if item.get("command_id") == "atomic-resume-command"
        ]

        self.assertEqual(len(decisions), 1)
        self.assertEqual(first["gate"], waiting["gate"])
        self.assertEqual(replayed["gate"], first["gate"])

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
                operation_id="atomic-reconcile-command",
            )
        finally:
            service.close()

        self.assertEqual(reconciled["state"], "completed")
        self.assertEqual(replayed["outcome"], reconciled["outcome"])
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
        self.assertTrue(turn["content_compacted"])
        self.assertTrue(turn["projection_target_exceeded"])

    def test_execute_turn_rejects_a_gate_schema_above_its_hard_limit(self) -> None:
        with self.assertRaisesRegex(AgentGatewayError, "Gate schema.*4 KiB"):
            AgentGateway(OversizedGateTurnRuntime()).execute(
                {
                    "kind": "start",
                    "target": "192.0.2.20",
                    "delivery_strategy": "source-only",
                },
                task_id="oversized-gate",
                operation_id="oversized-gate-1",
            )

    def test_execute_turn_budget_preserves_diagnostic_receipt_semantics(self) -> None:
        turn = AgentGateway(OversizedDiagnosticTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-diagnostic",
            operation_id="oversized-diagnostic-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["state"], "completed")
        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["coverage"]["requested"], 20)
        self.assertEqual(receipt["coverage"]["evaluable"], 20)
        self.assertEqual(receipt["coverage"]["visible_evaluable"], 20)
        self.assertEqual(receipt["coverage"]["visible_not_checked"], 0)
        self.assertEqual(receipt["coverage"]["not_checked"], 0)
        self.assertEqual(receipt["coverage"]["compacted"], 20)
        self.assertFalse(receipt["truncated"])
        self.assertTrue(receipt["content_complete"])
        self.assertEqual(
            [item["result_id"] for item in receipt["results"]],
            [f"result-{index}" for index in range(20)],
        )
        self.assertTrue(all(item["status"] == "available" for item in receipt["results"]))
        self.assertTrue(
            all(item["projection_truncated"] for item in receipt["results"])
        )
        self.assertTrue(all("value" in item for item in receipt["results"]))
        self.assertEqual(len(receipt["evidence"]), 8)
        self.assertTrue(
            all(
                item["evidence_id"].startswith("evidence-")
                for item in receipt["evidence"]
            )
        )
        self.assertIn("diagnostic_receipt_compacted", receipt["gaps"])
        self.assertNotIn("content_truncated", receipt["gaps"])
        self.assertTrue(turn["content_compacted"])
        self.assertTrue(turn["projection_target_exceeded"])

    def test_execute_turn_compaction_preserves_an_evaluable_result_summary(self) -> None:
        turn = AgentGateway(DeeplyNestedDiagnosticTurnRuntime()).execute(
            {"kind": "start", "target": "192.0.2.20"},
            task_id="deeply-nested-diagnostic",
            operation_id="deeply-nested-diagnostic-1",
        )

        receipt = turn["diagnostic_receipt"]
        encoded_results = json.dumps(receipt["results"])
        self.assertEqual(receipt["coverage"]["visible_evaluable"], 4)
        self.assertIn("12.08.21.06", encoded_results)
        self.assertIn("mctpd request timeout", encoded_results)
        self.assertIn("bmc.kepler.mctpd service visible", encoded_results)
        self.assertIn("No matching log lines", encoded_results)
        self.assertNotIn("<compacted>", encoded_results)

    def test_execute_turn_preserves_a_persisted_diagnostic_summary(self) -> None:
        turn = AgentGateway(PersistedDiagnosticSummaryTurnRuntime()).execute(
            {"kind": "start", "target": "192.0.2.20"},
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
            {"kind": "start", "target": "192.0.2.20"},
            task_id="non-evaluable-diagnostic",
            operation_id="non-evaluable-diagnostic-1",
        )

        receipt = turn["diagnostic_receipt"]
        self.assertEqual(receipt["status"], "blocked")
        self.assertFalse(receipt["coverage"]["complete"])
        self.assertEqual(receipt["coverage"]["visible_evaluable"], 0)
        self.assertEqual(receipt["coverage"]["visible_not_checked"], 1)
        self.assertEqual(receipt["results"][0]["status"], "not_checked")
        self.assertIn("diagnostic_receipt_invalid", receipt["gaps"])

    def test_execute_turn_exceeds_the_soft_target_instead_of_rewriting_completion(
        self,
    ) -> None:
        turn = AgentGateway(UncompactableDiagnosticTurnRuntime()).execute(
            {"kind": "start", "target": "192.0.2.20"},
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

    def test_execute_turn_compacts_large_diagnostic_previews_to_the_target(self) -> None:
        turn = AgentGateway(OversizedDiagnosticTurnRuntime(128)).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "delivery_strategy": "source-only",
            },
            task_id="unrepresentable-diagnostic",
            operation_id="unrepresentable-diagnostic-1",
        )

        self.assertGreater(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["state"], "completed")
        self.assertEqual(turn["diagnostic_receipt"]["status"], "complete")
        self.assertTrue(turn["diagnostic_receipt"]["content_compacted"])
        self.assertEqual(len(turn["diagnostic_receipt"]["results"]), 128)
        self.assertTrue(turn["projection_target_exceeded"])

    def test_gate_schema_limit_is_enforced_when_the_gate_is_constructed(self) -> None:
        oversized_schema = {"type": "object", "description": "x" * 5_000}

        with self.assertRaisesRegex(GateConflict, "4 KiB"):
            Gate(
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

    def test_execute_turn_soft_budget_never_rewrites_terminal_outcome(self) -> None:
        turn = AgentGateway(OversizedTerminalTurnRuntime(128)).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
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

    def test_execute_start_reuses_a_complete_observation_ref(self) -> None:
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
        self.assertEqual(first["gate"]["name"], "developer.change")
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
        self.assertEqual(cancelled["outcome"]["status"], "cancelled")
        self.assertEqual(replayed, cancelled)
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

        self.assertEqual(turn["gate"]["name"], "developer.change")
        self.assertEqual(resumed_backend.calls, [])

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

    def test_execute_build_upgrade_runs_both_gates_and_fresh_verification(self) -> None:
        first = self.service.call_exposed_tool(
            "execute",
            {
                "kind": "start",
                "target": "192.0.2.22",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "build-upgrade",
                "purpose": "build, deploy, and verify a firmware repair",
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-start",
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
                    },
                },
            },
            task_id="execute-build-upgrade",
            operation_id="build-upgrade-build",
        )

        self.assertEqual(final["state"], "completed")
        self.assertTrue(final["outcome_recorded"])
        self.assertEqual(
            [name for name, _arguments in self.backend.calls],
            ["debug_run", "upgrade_run", "debug_collect"],
        )
        verification_arguments = self.backend.calls[-1][1]
        self.assertEqual(verification_arguments["profile"], "standard")
        self.assertFalse(verification_arguments["no_freshness"])

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
