from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from openubmc_target_runtime import (  # noqa: E402
    EventRunStore,
    InMemoryRuntimeRepository,
    RUN_DECISION_SCHEMA,
    RunDecision,
    RunDecisionConflict,
    RunEvent,
    RunEventSchemaError,
    RunTurn,
    SQLiteRuntimeRepository,
    WORKFLOW_DEFINITION_SCHEMA,
    project_case,
)


class RunDecisionContractTests(unittest.TestCase):
    def test_run_decision_is_a_versioned_typed_contract(self) -> None:
        decision = RunDecision(
            run_id="run-1",
            command_id="command-1",
            input_digest="a" * 64,
            expected_revision=3,
            events=(
                RunEvent(
                    kind="RunIncidentRaised",
                    payload={"incident": {"incident_id": "incident-1"}},
                    operation_id="command-1",
                ),
            ),
            turn=RunTurn(run_id="run-1", state="incident"),
            effect_intent={"effect_id": "effect-1", "action": "debug_run"},
        )

        public = decision.to_public_dict()

        self.assertEqual(public["schema"], RUN_DECISION_SCHEMA)
        self.assertEqual(public["version"], 1)
        self.assertEqual(public["run_id"], "run-1")
        self.assertEqual(public["command_id"], "command-1")
        self.assertEqual(public["input_digest"], "a" * 64)
        self.assertEqual(public["expected_revision"], 3)
        self.assertEqual(public["events"][0]["kind"], "RunIncidentRaised")
        self.assertEqual(public["turn"]["state"], "incident")
        self.assertEqual(public["effect_intent"]["effect_id"], "effect-1")

    def test_run_decision_requires_an_integer_expected_revision(self) -> None:
        with self.assertRaisesRegex(
            ValueError, "expected_revision must be a non-negative integer"
        ):
            RunDecision(
                run_id="run-invalid-revision",
                command_id="command-invalid-revision",
                input_digest="f" * 64,
                expected_revision=1.5,  # type: ignore[arg-type]
                events=(),
                turn=RunTurn(run_id="run-invalid-revision", state="running"),
            )

    def test_run_store_atomically_commits_and_replays_one_command_decision(self) -> None:
        class RecordingRepository(InMemoryRuntimeRepository):
            def __init__(self) -> None:
                super().__init__()
                self.commit_count = 0

            def commit(self, case_id, *, expected_revision, events):
                self.commit_count += 1
                return super().commit(
                    case_id,
                    expected_revision=expected_revision,
                    events=events,
                )

        repository = RecordingRepository()
        store = EventRunStore(repository)
        decision = RunDecision(
            run_id="run-atomic",
            command_id="command-atomic",
            input_digest="b" * 64,
            expected_revision=0,
            events=(
                RunEvent(
                    kind="RunIncidentRaised",
                    payload={
                        "incident": {
                            "incident_id": "incident-atomic",
                            "code": "test_incident",
                            "message": "stop here",
                        }
                    },
                    operation_id="command-atomic",
                ),
            ),
            turn=RunTurn(run_id="run-atomic", state="incident"),
        )

        committed = store.commit(decision)
        replayed = store.commit(decision)

        self.assertFalse(committed.replayed)
        self.assertTrue(replayed.replayed)
        self.assertEqual(repository.commit_count, 1)
        self.assertEqual(repository.current_revision("run-atomic"), 2)
        self.assertEqual(
            committed.projection["current_incident"]["incident_id"],
            "incident-atomic",
        )
        self.assertEqual(replayed.turn.state, "incident")

    def test_run_store_commits_the_complete_event_set_and_decision_marker_together(self) -> None:
        class RecordingRepository(InMemoryRuntimeRepository):
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

        repository = RecordingRepository()
        store = EventRunStore(repository)

        store.commit(
            RunDecision(
                run_id="run-complete-set",
                command_id="command-complete-set",
                input_digest="7" * 64,
                expected_revision=0,
                events=(
                    RunEvent(
                        kind="RunGateOpened",
                        payload={"gate": {"gate_id": "gate-complete-set"}},
                        operation_id="command-complete-set",
                    ),
                    RunEvent(
                        kind="RunIncidentRaised",
                        payload={
                            "incident": {
                                "incident_id": "incident-complete-set",
                                "code": "contract-test",
                                "message": "stop",
                            }
                        },
                        operation_id="command-complete-set",
                    ),
                ),
                turn=RunTurn(run_id="run-complete-set", state="incident"),
            )
        )

        self.assertEqual(
            repository.commits,
            [
                (
                    "RunGateOpened",
                    "RunIncidentRaised",
                    "RunDecisionCommitted",
                )
            ],
        )

    def test_run_store_rejects_same_command_identity_with_a_new_digest(self) -> None:
        repository = InMemoryRuntimeRepository()
        store = EventRunStore(repository)
        original = RunDecision(
            run_id="run-conflict",
            command_id="command-conflict",
            input_digest="c" * 64,
            expected_revision=0,
            events=(),
            turn=RunTurn(run_id="run-conflict", state="running"),
        )
        store.commit(original)
        before = repository.current_revision("run-conflict")

        with self.assertRaises(RunDecisionConflict):
            store.commit(
                RunDecision(
                    run_id="run-conflict",
                    command_id="command-conflict",
                    input_digest="d" * 64,
                    expected_revision=before or 0,
                    events=(),
                    turn=RunTurn(run_id="run-conflict", state="running"),
                )
            )

        self.assertEqual(repository.current_revision("run-conflict"), before)

    def test_run_store_rejects_a_stale_expected_revision(self) -> None:
        repository = InMemoryRuntimeRepository()
        store = EventRunStore(repository)
        store.commit(
            RunDecision(
                run_id="run-stale",
                command_id="command-first",
                input_digest="1" * 64,
                expected_revision=0,
                events=(),
                turn=RunTurn(run_id="run-stale", state="running"),
            )
        )

        with self.assertRaisesRegex(RunDecisionConflict, "revision"):
            store.commit(
                RunDecision(
                    run_id="run-stale",
                    command_id="command-stale",
                    input_digest="2" * 64,
                    expected_revision=0,
                    events=(),
                    turn=RunTurn(run_id="run-stale", state="running"),
                )
            )

    def test_sqlite_restart_replays_the_persisted_run_decision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            first_store = EventRunStore(SQLiteRuntimeRepository(database))
            decision = RunDecision(
                run_id="run-sqlite-replay",
                command_id="command-sqlite-replay",
                input_digest="3" * 64,
                expected_revision=0,
                events=(
                    RunEvent(
                        kind="RunIncidentRaised",
                        payload={
                            "incident": {
                                "incident_id": "incident-sqlite-replay",
                                "code": "restart_test",
                                "message": "persist this decision",
                            }
                        },
                        operation_id="command-sqlite-replay",
                    ),
                ),
                turn=RunTurn(run_id="run-sqlite-replay", state="incident"),
            )
            first_store.commit(decision)

            replayed = EventRunStore(SQLiteRuntimeRepository(database)).commit(decision)

        self.assertTrue(replayed.replayed)
        self.assertEqual(replayed.turn.state, "incident")
        self.assertEqual(
            replayed.projection["current_incident"]["incident_id"],
            "incident-sqlite-replay",
        )

    def test_legacy_run_events_are_explicitly_upcast_to_the_current_projection(self) -> None:
        legacy_definition = {
            "definition_id": "workflow-source-only",
            "version": 1,
            "intent": "diagnose-and-fix",
            "entry_domain": "debug",
            "entry_operation": "",
            "delivery_strategy": "source-only",
            "steps": [
                {
                    "step_id": "step-developer",
                    "kind": "phase",
                    "name": "developer.change",
                    "owner": "openubmc-developer",
                    "receipt_schema": "developer-change-v1",
                }
            ],
        }
        events = (
            {
                "revision": 1,
                "kind": "CaseOpened",
                "operation_id": "start-legacy",
                "payload": {
                    "intent": "diagnose-and-fix",
                    "delivery_strategy": "source-only",
                    "workflow_definition": legacy_definition,
                },
                "created_at": 1.0,
            },
            {
                "revision": 2,
                "kind": "RunGateOpened",
                "operation_id": "gate-legacy",
                "payload": {
                    "gate": {
                        "gate_id": "gate-legacy",
                        "gate_version": 1,
                        "gate_schema_digest": "e" * 64,
                        "workflow_cycle_id": "cycle-1",
                        "workflow_step_id": "step-developer",
                    }
                },
                "created_at": 2.0,
            },
            {
                "revision": 3,
                "kind": "OperationProgressed",
                "operation_id": "phase-legacy",
                "payload": {
                    "status": "completed",
                    "phase_record": {
                        "phase_type": "developer.change",
                        "status": "completed",
                        "summary": "legacy source completed",
                        "workflow_cycle_id": "cycle-1",
                        "workflow_step_id": "step-developer",
                        "operation_id": "phase-legacy",
                    },
                },
                "created_at": 3.0,
            },
            {
                "revision": 4,
                "kind": "RunOutcomeRecorded",
                "operation_id": "outcome-legacy",
                "payload": {
                    "status": "completed",
                    "summary": "legacy run completed",
                    "acceptance": [],
                },
                "created_at": 4.0,
            },
        )

        projection = project_case("run-legacy", events)

        self.assertEqual(
            projection["workflow_definition"]["schema"],
            WORKFLOW_DEFINITION_SCHEMA,
        )
        self.assertEqual(projection["current_gate"], {})
        self.assertEqual(projection["run_gates"][0]["schema_digest"], "e" * 64)
        self.assertEqual(
            projection["phase_records"][0]["summary"],
            "legacy source completed",
        )
        self.assertEqual(projection["run_outcome"]["status"], "completed")

    def test_unknown_persisted_run_event_schema_is_rejected(self) -> None:
        with self.assertRaises(RunEventSchemaError):
            project_case(
                "run-unknown-schema",
                (
                    {
                        "revision": 1,
                        "kind": "RunIncidentRaised",
                        "operation_id": "unknown-schema",
                        "payload": {
                            "_run_event_schema": "openubmc.target-runtime.v1/run-event-v99",
                            "_run_event_version": 99,
                            "incident": {"incident_id": "incident-unknown"},
                        },
                        "created_at": 1.0,
                    },
                ),
            )

    def test_incompatible_persisted_run_decision_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            RunEventSchemaError, "RunDecision version"
        ):
            project_case(
                "run-unknown-decision-version",
                (
                    {
                        "revision": 1,
                        "kind": "RunDecisionCommitted",
                        "operation_id": "unknown-decision-version",
                        "payload": {
                            "_run_event_schema": (
                                "openubmc.target-runtime.v1/run-event-v1"
                            ),
                            "_run_event_version": 1,
                            "schema": RUN_DECISION_SCHEMA,
                            "version": 99,
                            "command_id": "unknown-decision-version",
                            "input_digest": "8" * 64,
                            "turn": {
                                "run_id": "run-unknown-decision-version",
                                "state": "running",
                            },
                        },
                        "created_at": 1.0,
                    },
                ),
            )


if __name__ == "__main__":
    unittest.main()
