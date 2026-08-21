from __future__ import annotations

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
    EvidenceUnavailable,
    EventRunStore,
    GateConflict,
    InMemoryRuntimeRepository,
    Incident,
    OBSERVATION_MAX_BYTES,
    RevisionConflict,
    ReferenceViolation,
    ResumeRun,
    RunDecision,
    RunDecisionConflict,
    RunEngine,
    STDIO_FRAME_MAX_BYTES,
    TOOLS_LIST_MAX_BYTES,
    TURN_MAX_BYTES,
    JsonRpcMcpEndpoint,
    FilesystemBlobRepository,
    RunTurn,
    RuntimeMcpService,
    RuntimeSDKContext,
    SQLiteRuntimeRepository,
    ScopeContract,
    ScopeViolation,
    StartRun,
    StdioMcpServer,
    SubmitGate,
)
from openubmc_target_runtime.context_runtime import (  # noqa: E402
    BufferedRuntimeRepository,
)


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

    def ensure(self, intent, *, mode):
        self.intents.append((intent, mode))
        return SimpleNamespace(mode=mode, future=object())

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


class RecoveryBoundaryConflictOnceStore:
    def __init__(self, delegate: EventRunStore) -> None:
        self.delegate = delegate
        self.raced = False

    def load(self, run_id: str):
        return self.delegate.load(run_id)

    def replay(self, run_id: str, command_id: str, input_digest: str):
        return self.delegate.replay(run_id, command_id, input_digest)

    def commit(self, decision: RunDecision):
        if decision.command_id.startswith("recover-") and not self.raced:
            self.raced = True
            self.delegate.commit(
                RunDecision(
                    run_id=decision.run_id,
                    command_id=(
                        "concurrent-" + decision.command_id.removeprefix("recover-")
                    ),
                    input_digest="f" * 64,
                    expected_revision=decision.expected_revision,
                    events=(),
                    turn=decision.turn,
                )
            )
            raise RunDecisionConflict("simulated concurrent Run revision")
        return self.delegate.commit(decision)


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
        self.mdb_collections = 0

    def observe_query(self, task, arguments, context) -> dict[str, object]:
        context.raise_if_stopped()
        assured = bool(arguments.get("assured"))
        prior = arguments.get("prior_observation")
        self.assurance_calls.append(assured)
        if isinstance(prior, dict):
            value = json.loads(json.dumps(prior))
            value["result"]["capabilities"]["remote_log_file"] = True
            value["observed_at"] = "2026-08-19T00:00:01Z"
            return value
        self.mdb_collections += 1
        value = self.debug_collect(task, arguments, context)
        value["result"]["capabilities"].pop("remote_log_file", None)
        return value


class FailingAssuranceSemanticBackend(AutoAssuranceSemanticBackend):
    def observe_query(self, task, arguments, context) -> dict[str, object]:
        if arguments.get("assured"):
            self.assurance_calls.append(True)
            raise OSError("assurance transport is temporarily unavailable")
        return super().observe_query(task, arguments, context)


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


class PersistentUnknownRunDriver:
    def __init__(self) -> None:
        self.reconcile_calls = 0
        self.incident = None

    def _snapshot(self) -> dict[str, object]:
        projection: dict[str, object] = {
            "case_id": "run-persistent-unknown",
            "status": "incident" if self.incident is not None else "open",
            "operations": [
                {
                    "operation": "live_patch_run",
                    "operation_id": "mutation-unknown-1",
                    "status": "mutation_outcome_unknown",
                }
            ],
            "workflow_step_states": {},
            "current_incident": (
                self.incident.to_public_dict() if self.incident is not None else {}
            ),
        }
        return {"projection": projection, "continuation": {}}

    def run_snapshot(self, _run_id: str) -> dict[str, object]:
        return self._snapshot()

    def reconcile_run(self, _run_id: str, *, task_id: str, operation_id: str):
        del task_id, operation_id
        self.reconcile_calls += 1
        return self._snapshot()

    def apply_transition(self, _run_id: str, transition):
        if len(transition.events) != 1:
            raise AssertionError("unexpected transition event batch")
        event = transition.events[0]
        if event.kind != "RunIncidentRaised":
            raise AssertionError(f"unexpected transition: {event.kind}")
        value = event.payload["incident"]
        self.incident = Incident(
            incident_id=str(value["incident_id"]),
            code=str(value["code"]),
            message=str(value["message"]),
            effect_id=str(value.get("effect_id", "")),
            recoverable=bool(value.get("recoverable", True)),
        )
        return self._snapshot()

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
            self.service.context_runtime.repository.case_for_task("observe-task")
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
                    "assurance": "fast",
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

    def test_legacy_assurance_hint_uses_the_runtime_default_policy(self) -> None:
        receipt = self.service.call_exposed_tool(
            "observe",
            {
                "target": "192.0.2.10",
                "selectors": [
                    {"kind": "mdb", "queries": ["lsprop Object0"]}
                ],
                "assurance": "assured",
            },
            task_id="assured-without-adapter",
            operation_id="assured-without-adapter-1",
        )

        self.assertEqual(receipt["status"], "complete")
        self.assertNotIn("assurance", receipt)
        self.assertIn("observation_ref", receipt)

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
                    "assurance": "auto",
                },
                task_id="auto-assurance",
                operation_id="auto-assurance-1",
            )
        finally:
            service.close()

        self.assertEqual(receipt["status"], "complete")
        self.assertNotIn("assurance", receipt)
        self.assertEqual(backend.assurance_calls, [False, True])
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
        events = self.service.context_runtime.repository.events(first["run_id"])
        self.assertEqual(replayed["outcome"], final["outcome"])
        self.assertEqual(
            [event["kind"] for event in events].count("RunOutcomeRecorded"), 1
        )
        self.assertEqual(
            [event["kind"] for event in events].count("CloseoutRecorded"), 1
        )
        self.assertEqual(self.service.session_outcome_service.status()["outcome_count"], 0)

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
        before = self.service.context_runtime.repository.events(waiting["run_id"])
        recorded = self.service.call_tool(
            "session_outcome_record",
            {"case_id": waiting["run_id"]},
            task_id="run-session-outcome-authority",
            operation_id="run-session-outcome-project",
        )
        after = self.service.context_runtime.repository.events(waiting["run_id"])

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
        projection = self.service.context_runtime.read_case(waiting.run_id)
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

        events = self.service.context_runtime.repository.events(waiting["run_id"])
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

        projection = self.service.context_runtime.read_case(waiting["run_id"])
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

        projection = self.service.context_runtime.read_case(developer_gate["run_id"])
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
        projection = self.service.context_runtime.read_case(waiting["run_id"])
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
        projection = self.service.context_runtime.read_case(waiting["run_id"])
        decisions = [
            item
            for item in projection["run_decisions"]
            if item.get("command_id") == "atomic-resume-command"
        ]

        self.assertEqual(len(decisions), 1)
        self.assertEqual(first["gate"], waiting["gate"])
        self.assertEqual(replayed["gate"], first["gate"])

    def test_reconcile_persists_and_replays_the_incident_decision(self) -> None:
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
            projection = service.context_runtime.read_case(waiting["run_id"])
        finally:
            service.close()

        decisions = [
            item
            for item in projection["run_decisions"]
            if item.get("command_id") == "atomic-reconcile-command"
        ]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["turn"]["state"], "running")
        self.assertEqual(
            decisions[0]["effect_intent"]["operation"],
            "debug_collect",
        )
        self.assertEqual(projection["current_turn"]["state"], "completed")
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
        projection = self.service.context_runtime.read_case(first["run_id"])
        events = self.service.context_runtime.repository.events(first["run_id"])

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
        before = self.service.context_runtime.read_case(first["run_id"])

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

        after = self.service.context_runtime.read_case(first["run_id"])
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
                events = second_service.context_runtime.repository.events(
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
                before = first_service.context_runtime.read_case(first["run_id"])
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
                after = second_service.context_runtime.read_case(first["run_id"])
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
        first_before = self.service.context_runtime.read_case(first["run_id"])
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
        first_after = self.service.context_runtime.read_case(first["run_id"])

        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual(first_after["revision"], first_before["revision"])
        self.assertEqual(first_after["targets"], first_before["targets"])

    def test_execute_turn_hard_limit_survives_oversized_blocker_details(self) -> None:
        turn = AgentGateway(OversizedTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-turn",
            operation_id="oversized-turn-1",
        )

        self.assertLessEqual(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["run_id"], "case-oversized-turn")
        self.assertEqual(turn["state"], "blocked")
        self.assertEqual(turn["gate"]["kind"], "blocker")
        self.assertTrue(turn["content_compacted"])

    def test_execute_turn_replaces_an_oversized_gate_schema_with_a_blocker(self) -> None:
        turn = AgentGateway(OversizedGateTurnRuntime()).execute(
            {
                "kind": "start",
                "target": "192.0.2.20",
                "delivery_strategy": "source-only",
            },
            task_id="oversized-gate",
            operation_id="oversized-gate-1",
        )

        self.assertLessEqual(encoded_size(turn), TURN_MAX_BYTES)
        self.assertEqual(turn["run_id"], "case-oversized-gate")
        self.assertEqual(turn["gate"]["kind"], "blocker")
        self.assertEqual(turn["gate"]["name"], "gate_schema_exceeds_budget")
        self.assertTrue(turn["content_compacted"])

    def test_execute_start_reuses_a_complete_observation_receipt(self) -> None:
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
                "observation_receipt": receipt,
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
            self.service.context_runtime.repository.case_for_task(
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
                    self.service.context_runtime.repository.case_for_task(task_id)
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
            self.service.context_runtime.repository.case_for_task(
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
        observed_clock = self.service.context_runtime.clock()
        self.service.context_runtime.clock = lambda: observed_clock + 16 * 60

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
            self.service.context_runtime.repository.case_for_task(
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
                projection = second.context_runtime.read_case(waiting["run_id"])
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
            events = service.context_runtime.repository.events(waiting["run_id"])
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
            transactions = service.context_runtime.repository
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
            projection = service.context_runtime.read_case(waiting["run_id"])
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

    def test_observation_receipt_reconstructs_after_process_restart(self) -> None:
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
                        "observation_receipt": receipt,
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
        events = self.service.context_runtime.repository.events(first["run_id"])
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
            self.service.context_runtime,
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
        projection = self.service.context_runtime.read_case(first["run_id"])
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
            for event in self.service.context_runtime.repository.events(
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
            projection = service.context_runtime.read_case(waiting["run_id"])
            replayed = service.call_exposed_tool(
                "execute",
                {
                    "kind": "resume",
                    "run_id": waiting["run_id"],
                },
                task_id="incomplete-live-patch-acceptance-replay",
                operation_id="incomplete-live-patch-outcome-replay",
            )
            events = service.context_runtime.repository.events(waiting["run_id"])
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

    def test_automatic_reconcile_attempts_an_unknown_mutation_only_once(self) -> None:
        driver = PersistentUnknownRunDriver()
        repository = InMemoryRuntimeRepository()
        engine = RunEngine(
            driver,
            run_store=EventRunStore(repository),
            command_transactions=BufferedRuntimeRepository(repository),
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

        self.assertEqual(driver.reconcile_calls, 1)
        self.assertEqual(turn.state, "incident")
        self.assertIsNotNone(turn.incident)
        self.assertEqual(turn.incident.code, "mutation_outcome_unknown")
        self.assertEqual(resumed.state, "incident")

    def test_run_engine_requires_durable_command_dependencies(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "RunStore and command transactions are required",
        ):
            RunEngine(PersistentUnknownRunDriver())

    def test_run_engine_rejects_unknown_transition_kinds(self) -> None:
        repository = InMemoryRuntimeRepository()
        engine = RunEngine(
            PersistentUnknownRunDriver(),
            run_store=EventRunStore(repository),
            command_transactions=BufferedRuntimeRepository(repository),
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
            running_projection = service.context_runtime.read_case(
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
                for event in service.context_runtime.repository.events(
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
            first.effect_runner.close()
            first.effect_runner = dormant
            first.semantic_runtime.run_engine.effect_runner = dormant
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
            persisted_effect_id = first.context_runtime.read_case(
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
                operations = second.context_runtime.read_case(
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
                first.effect_runner.close()
                dormant = DormantEffectRunner()
                first.effect_runner = dormant
                first.semantic_runtime.run_engine.effect_runner = dormant
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

            persisted_effect_id = first.context_runtime.read_case(
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
                first.effect_runner.close()
                dormant = DormantEffectRunner()
                first.effect_runner = dormant
                first.semantic_runtime.run_engine.effect_runner = dormant
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
                first.effect_runner.close()
                dormant = DormantEffectRunner()
                first.effect_runner = dormant
                first.semantic_runtime.run_engine.effect_runner = dormant
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

            persisted_effect_id = first.context_runtime.read_case(
                running["run_id"]
            )["effect_intents"][-1]["effect_id"]
            backend = RecoveryAwareLivePatchBackend()
            second = RuntimeMcpService(
                backend,
                context_repository=SQLiteRuntimeRepository(database),
                blob_repository=FilesystemBlobRepository(blobs),
            )
            run_store = RecoveryBoundaryConflictOnceStore(
                second.semantic_runtime.run_engine.run_store
            )
            second.semantic_runtime.run_engine.run_store = run_store
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
                events = second.context_runtime.repository.events(
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
            projection = service.context_runtime.read_case(developer_gate["run_id"])
            deferred_events = [
                event
                for event in service.context_runtime.repository.events(
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

    def test_legacy_and_governance_operations_require_explicit_profiles(self) -> None:
        compatibility = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )
        operator = RuntimeMcpService(SemanticBackend(), interface_profile="operator")
        try:
            self.assertIn("debug_run", compatibility.interface_catalog.names())
            self.assertNotIn("evidence_read", compatibility.interface_catalog.names())
            self.assertNotIn("runtime_status", compatibility.interface_catalog.names())
            self.assertIn("evidence_read", operator.interface_catalog.names())
            self.assertNotIn("debug_run", operator.interface_catalog.names())
        finally:
            compatibility.close()
            operator.close()

    def test_compatibility_calls_publish_anonymous_operation_counts(self) -> None:
        compatibility = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )
        try:
            compatibility.call_exposed_tool(
                "debug_run",
                {
                    "ip": "192.0.2.78",
                    "intent": "diagnosis-only",
                    "final_purpose": "compatibility telemetry",
                },
                task_id="compatibility-telemetry-task",
                operation_id="compatibility-telemetry-call",
            )
            status = compatibility.call_tool(
                "runtime_status",
                {},
                task_id="compatibility-telemetry-status",
                operation_id="compatibility-telemetry-status",
            )
        finally:
            compatibility.close()

        telemetry = status["compatibility_telemetry"]
        self.assertEqual(telemetry["total_calls"], 1)
        self.assertEqual(telemetry["operation_counts"], {"debug_run": 1})
        self.assertNotIn("task", json.dumps(telemetry))
        self.assertNotIn("192.0.2.78", json.dumps(telemetry))

    def test_compatibility_controls_delegate_native_runs_to_typed_runtime(self) -> None:
        compatibility = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )
        try:
            waiting = compatibility.semantic_runtime.execute(
                StartRun(
                    target="192.0.2.79",
                    intent="diagnose-and-fix",
                    purpose="verify compatibility delegation",
                    delivery_strategy="source-only",
                    command_id="compatibility-native-start",
                    input_digest="",
                ),
                task_id="compatibility-native",
                operation_id="compatibility-native-start",
            )
            compatibility.call_exposed_tool(
                "phase_record",
                {
                    "case_id": waiting.run_id,
                    "expected_revision": compatibility.context_runtime.read_case(
                        waiting.run_id
                    )["revision"],
                    "idempotency_key": "compatibility-native-phase",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "summary": "source completed",
                    "source_revision": "compatibility-native-source",
                    "authored_files": ["src/fix.lua"],
                    "verification_plan": ["run tests"],
                },
                task_id="compatibility-native",
                operation_id="compatibility-native-phase",
            )
            final = compatibility.call_exposed_tool(
                "workflow.next",
                {"case_id": waiting.run_id},
                task_id="compatibility-native",
                operation_id="compatibility-native-next",
            )
            events = compatibility.context_runtime.repository.events(waiting.run_id)
        finally:
            compatibility.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(
            sum(event["kind"] == "RunGateSubmitted" for event in events),
            1,
        )
        self.assertFalse(
            any(
                event["kind"] == "OperationAccepted"
                and event["payload"].get("operation")
                in {"phase_record", "workflow.next"}
                for event in events
            )
        )

    def test_legacy_phase_writer_does_not_persist_a_native_gate_submission(self) -> None:
        opened = self.service.call_tool(
            "debug_run",
            {
                "ip": "192.0.2.82",
                "intent": "diagnose-and-fix",
                "delivery_strategy": "source-only",
                "final_purpose": "verify legacy phase isolation",
            },
            task_id="legacy-phase-isolation",
            operation_id="legacy-phase-isolation-start",
        )
        run_id = opened.envelope["case_id"]

        self.service.call_tool(
            "phase_record",
            {
                "case_id": run_id,
                "expected_revision": self.service.context_runtime.read_case(
                    run_id
                )["revision"],
                "idempotency_key": "legacy-phase-isolation-submission",
                "phase_type": "developer.change",
                "producer_identity": "openubmc-developer",
                "status": "completed",
                "summary": "legacy source completed",
                "source_revision": "legacy-phase-isolation-source",
                "authored_files": ["src/legacy.lua"],
                "verification_plan": ["run tests"],
            },
            task_id="legacy-phase-isolation",
            operation_id="legacy-phase-isolation-submission",
        )

        events = self.service.context_runtime.repository.events(run_id)
        projection = self.service.context_runtime.read_case(run_id)
        self.assertFalse(any(event["kind"] == "RunGateSubmitted" for event in events))
        self.assertTrue(
            any(
                event["kind"] == "OperationProgressed"
                and isinstance(event["payload"].get("legacy_phase_record"), dict)
                for event in events
            )
        )
        self.assertEqual(
            projection["phase_records"][-1]["source_revision"],
            "legacy-phase-isolation-source",
        )

    def test_legacy_phase_writer_rejects_native_runs(self) -> None:
        waiting = self.service.semantic_runtime.execute(
            StartRun(
                target="192.0.2.83",
                intent="diagnose-and-fix",
                purpose="verify native Gate authority",
                delivery_strategy="source-only",
                command_id="native-phase-authority-start",
                input_digest="",
            ),
            task_id="native-phase-authority",
            operation_id="native-phase-authority-start",
        )

        with self.assertRaisesRegex(
            GateConflict,
            "native Run phase transitions require RunEngine",
        ):
            self.service.call_tool(
                "phase_record",
                {
                    "case_id": waiting.run_id,
                    "expected_revision": self.service.context_runtime.read_case(
                        waiting.run_id
                    )["revision"],
                    "idempotency_key": "native-phase-authority-submission",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "summary": "must use SubmitGate",
                    "source_revision": "native-phase-authority-source",
                    "authored_files": ["src/native.lua"],
                    "verification_plan": ["run tests"],
                },
                task_id="native-phase-authority",
                operation_id="native-phase-authority-submission",
            )

    def test_compatibility_controls_upcast_legacy_runs_before_transitioning(self) -> None:
        compatibility = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )
        try:
            opened = compatibility.call_exposed_tool(
                "debug_run",
                {
                    "ip": "192.0.2.80",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "verify legacy compatibility delegation",
                },
                task_id="compatibility-legacy",
                operation_id="compatibility-legacy-debug",
            )
            run_id = opened.envelope["case_id"]
            compatibility.call_exposed_tool(
                "phase_record",
                {
                    "case_id": run_id,
                    "expected_revision": compatibility.context_runtime.read_case(
                        run_id
                    )["revision"],
                    "idempotency_key": "compatibility-legacy-phase",
                    "phase_type": "developer.change",
                    "producer_identity": "openubmc-developer",
                    "status": "completed",
                    "summary": "legacy source completed",
                    "source_revision": "compatibility-legacy-source",
                    "authored_files": ["src/legacy-fix.lua"],
                    "verification_plan": ["run tests"],
                },
                task_id="compatibility-legacy",
                operation_id="compatibility-legacy-phase",
            )
            final = compatibility.call_exposed_tool(
                "workflow.next",
                {"case_id": run_id},
                task_id="compatibility-legacy",
                operation_id="compatibility-legacy-next",
            )
            events = compatibility.context_runtime.repository.events(run_id)
        finally:
            compatibility.close()

        self.assertEqual(final["state"], "completed")
        self.assertFalse(
            any(
                event["kind"] == "OperationAccepted"
                and event["payload"].get("operation")
                in {"phase_record", "workflow.next"}
                for event in events
            )
        )
        self.assertTrue(any(event["kind"] == "RunDecisionCommitted" for event in events))

    def test_compatibility_workflow_advance_starts_a_typed_run(self) -> None:
        compatibility = RuntimeMcpService(
            SemanticBackend(), interface_profile="compatibility"
        )
        try:
            waiting = compatibility.call_exposed_tool(
                "workflow.advance",
                {
                    "ip": "192.0.2.81",
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "final_purpose": "verify typed compatibility start",
                },
                task_id="compatibility-advance",
                operation_id="compatibility-advance-start",
            )
            run_id = waiting["run_id"]
            events = compatibility.context_runtime.repository.events(run_id)
        finally:
            compatibility.close()

        self.assertEqual(waiting["state"], "waiting_response")
        self.assertTrue(any(event["kind"] == "RunDecisionCommitted" for event in events))
        self.assertFalse(
            any(
                event["kind"] == "OperationAccepted"
                and event["payload"].get("operation") == "workflow.advance"
                for event in events
            )
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
