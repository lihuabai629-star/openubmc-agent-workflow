from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from openubmc_target_runtime.context_runtime import (  # noqa: E402
    BufferedRuntimeRepository,
    InMemoryRuntimeRepository,
    PendingCaseEvent,
    SQLiteRuntimeRepository,
)
from openubmc_target_runtime.run_engine import RunEngine  # noqa: E402
from openubmc_target_runtime.run_store import EventRunStore  # noqa: E402
from openubmc_target_runtime.semantic_runtime import (  # noqa: E402
    ReconcileRun,
    ResumeRun,
    RunTurn,
)
from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from test_agent_gateway import (  # noqa: E402
    PersistentUnknownRunDriver,
    SemanticBackend,
)


class IncidentLifecycleTests(unittest.TestCase):
    @staticmethod
    def unknown_mutation_engine() -> tuple[InMemoryRuntimeRepository, RunEngine]:
        repository = InMemoryRuntimeRepository()
        transactions = BufferedRuntimeRepository(repository)
        return repository, RunEngine(
            PersistentUnknownRunDriver(transactions),
            run_store=EventRunStore(
                repository,
                draft_buffer=transactions,
            ),
        )

    @staticmethod
    def operator_status(repository) -> dict[str, object]:
        operator = RuntimeMcpService(
            SemanticBackend(),
            context_repository=repository,
            interface_profile="operator",
        )
        try:
            return operator.call_exposed_tool(
                "runtime_status",
                {},
                task_id="incident-metrics-operator",
                operation_id="incident-metrics-status",
            )
        finally:
            operator.close()

    def test_agent_incident_turn_exposes_the_bounded_recovery_path(self) -> None:
        _repository, engine = self.unknown_mutation_engine()

        turn = engine.execute(
            ResumeRun("run-persistent-unknown"),
            task_id="incident-recovery-turn",
            operation_id="incident-recovery-turn-resume",
        ).to_public_dict()

        incident = turn["incident"]
        self.assertEqual(incident["recovery_path"], "reconcile")
        self.assertEqual(incident["allowed_commands"], ["reconcile", "cancel"])
        self.assertIn("same Effect identity", incident["operator_action"])
        self.assertEqual(turn["next"], incident["operator_action"])

    def test_repeated_reconcile_reuses_the_open_unknown_mutation_incident(self) -> None:
        repository, engine = self.unknown_mutation_engine()

        engine.execute(
            ResumeRun("run-persistent-unknown"),
            task_id="incident-reconcile",
            operation_id="incident-reconcile-resume",
        )
        for attempt in range(2):
            turn = engine.execute(
                ReconcileRun("run-persistent-unknown"),
                task_id="incident-reconcile",
                operation_id=f"incident-reconcile-{attempt}",
            )
            self.assertEqual(turn.state, "incident")

        projection = repository.load("run-persistent-unknown")
        self.assertIsNotNone(projection)
        self.assertEqual(len(projection["incidents"]), 1)

    def test_old_terminal_incident_uses_the_current_recovery_policy(self) -> None:
        turn = RunTurn.from_public_dict(
            {
                "run_id": "run-old-terminal-incident",
                "state": "incident",
                "incident": {
                    "incident_id": "incident-old-terminal",
                    "code": "internal_step_limit",
                    "message": "legacy writer defaulted recoverable",
                    "recoverable": True,
                },
            }
        ).to_public_dict()

        self.assertFalse(turn["incident"]["recoverable"])
        self.assertEqual(turn["incident"]["recovery_path"], "cancel_terminal")
        self.assertEqual(turn["incident"]["allowed_commands"], ["cancel"])

    def test_operator_status_derives_incident_metrics_from_persisted_run_events(self) -> None:
        now = [100.0]
        repository = InMemoryRuntimeRepository(clock=lambda: now[0])
        repository.commit(
            "run-incident-metrics",
            expected_revision=0,
            events=(
                PendingCaseEvent(
                    kind="CaseOpened",
                    operation_id="incident-open",
                    payload={"intent": "diagnose-and-fix"},
                ),
                PendingCaseEvent(
                    kind="RunIncidentRaised",
                    operation_id="incident-raised",
                    payload={
                        "incident": {
                            "incident_id": "incident-domain-retry",
                            "code": "domain_execution_failed",
                            "message": "domain temporarily unavailable",
                            "effect_id": "live_patch_run",
                            "recoverable": True,
                        }
                    },
                ),
            ),
        )
        now[0] = 130.0
        repository.commit(
            "run-incident-metrics",
            expected_revision=2,
            events=(
                PendingCaseEvent(
                    kind="RunIncidentResolved",
                    operation_id="incident-resolved",
                    payload={
                        "incident_id": "incident-domain-retry",
                        "resolution": "retrying domain preparation",
                    },
                ),
            ),
        )
        status = self.operator_status(repository)

        metrics = status["incident_metrics"]
        self.assertEqual(metrics["total"], 1)
        self.assertEqual(metrics["open"], 0)
        self.assertEqual(metrics["resolved"], 1)
        self.assertEqual(metrics["cancelled"], 0)
        self.assertEqual(metrics["duplicate_raises"], 0)
        domain = metrics["by_code"]["domain_execution_failed"]
        self.assertEqual(domain["recovery_path"], "retry_resume")
        self.assertEqual(domain["resolved"], 1)
        self.assertEqual(domain["average_resolution_seconds"], 30.0)
        self.assertEqual(
            metrics["resolution_counts"],
            {"retrying domain preparation": 1},
        )

    def test_operator_metrics_cover_age_duplicates_cancel_and_unknown_codes(
        self,
    ) -> None:
        repository = InMemoryRuntimeRepository(clock=lambda: time.time() - 45.0)
        repository.commit(
            "run-open-incident",
            expected_revision=0,
            events=(
                PendingCaseEvent(
                    kind="CaseOpened",
                    operation_id="open-case",
                    payload={"intent": "diagnose"},
                ),
                PendingCaseEvent(
                    kind="RunIncidentRaised",
                    operation_id="open-incident",
                    payload={
                        "incident": {
                            "incident_id": "incident-open",
                            "code": "artifact_reference_invalid",
                            "message": "artifact missing",
                        }
                    },
                ),
            ),
        )
        repository.commit(
            "run-unknown-incident",
            expected_revision=0,
            events=(
                PendingCaseEvent(
                    kind="CaseOpened",
                    operation_id="unknown-case",
                    payload={"intent": "diagnose"},
                ),
                PendingCaseEvent(
                    kind="RunIncidentRaised",
                    operation_id="unknown-incident-first",
                    payload={
                        "incident": {
                            "incident_id": "incident-unknown",
                            "code": "extension_specific_failure",
                            "message": "extension failed",
                        }
                    },
                ),
                PendingCaseEvent(
                    kind="RunIncidentRaised",
                    operation_id="unknown-incident-duplicate",
                    payload={
                        "incident": {
                            "incident_id": "incident-unknown",
                            "code": "extension_specific_failure",
                            "message": "extension failed",
                        }
                    },
                ),
                PendingCaseEvent(
                    kind="RunIncidentResolved",
                    operation_id="unknown-incident-cancelled",
                    payload={
                        "incident_id": "incident-unknown",
                        "resolution": "cancelled",
                    },
                ),
            ),
        )

        metrics = self.operator_status(repository)["incident_metrics"]

        self.assertEqual(metrics["total"], 2)
        self.assertEqual(metrics["open"], 1)
        self.assertEqual(metrics["cancelled"], 1)
        self.assertEqual(metrics["duplicate_raises"], 1)
        self.assertEqual(
            metrics["unknown_policy_codes"],
            ["extension_specific_failure"],
        )
        self.assertGreaterEqual(
            metrics["by_code"]["artifact_reference_invalid"][
                "max_open_age_seconds"
            ],
            44.0,
        )
        unknown = metrics["by_code"]["extension_specific_failure"]
        self.assertEqual(unknown["recovery_path"], "operator_required")
        self.assertEqual(unknown["allowed_commands"], ["cancel"])

    def test_duplicate_raise_preserves_the_first_raise_for_resolution_time(
        self,
    ) -> None:
        now = [100.0]
        repository = InMemoryRuntimeRepository(clock=lambda: now[0])
        repository.commit(
            "run-duplicate-duration",
            expected_revision=0,
            events=(
                PendingCaseEvent(
                    kind="CaseOpened",
                    operation_id="duplicate-duration-case",
                    payload={"intent": "diagnose"},
                ),
                PendingCaseEvent(
                    kind="RunIncidentRaised",
                    operation_id="duplicate-duration-first",
                    payload={
                        "incident": {
                            "incident_id": "incident-duplicate-duration",
                            "code": "domain_execution_failed",
                            "message": "domain failed",
                        }
                    },
                ),
            ),
        )
        now[0] = 120.0
        repository.commit(
            "run-duplicate-duration",
            expected_revision=2,
            events=(
                PendingCaseEvent(
                    kind="RunIncidentRaised",
                    operation_id="duplicate-duration-second",
                    payload={
                        "incident": {
                            "incident_id": "incident-duplicate-duration",
                            "code": "domain_execution_failed",
                            "message": "domain failed",
                        }
                    },
                ),
            ),
        )
        now[0] = 160.0
        repository.commit(
            "run-duplicate-duration",
            expected_revision=3,
            events=(
                PendingCaseEvent(
                    kind="RunIncidentResolved",
                    operation_id="duplicate-duration-resolved",
                    payload={
                        "incident_id": "incident-duplicate-duration",
                        "resolution": "retrying domain preparation",
                    },
                ),
            ),
        )

        metrics = self.operator_status(repository)["incident_metrics"]

        self.assertEqual(metrics["duplicate_raises"], 1)
        self.assertEqual(
            metrics["by_code"]["domain_execution_failed"][
                "average_resolution_seconds"
            ],
            60.0,
        )

    def test_sqlite_restart_preserves_incident_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            first_repository = SQLiteRuntimeRepository(database)
            first_repository.commit(
                "run-sqlite-incident",
                expected_revision=0,
                events=(
                    PendingCaseEvent(
                        kind="CaseOpened",
                        operation_id="sqlite-case",
                        payload={"intent": "diagnose"},
                    ),
                    PendingCaseEvent(
                        kind="RunIncidentRaised",
                        operation_id="sqlite-incident",
                        payload={
                            "incident": {
                                "incident_id": "incident-sqlite",
                                "code": "domain_execution_failed",
                                "message": "domain failed",
                            }
                        },
                    ),
                    PendingCaseEvent(
                        kind="RunIncidentResolved",
                        operation_id="sqlite-incident-resolved",
                        payload={
                            "incident_id": "incident-sqlite",
                            "resolution": "retrying domain preparation",
                        },
                    ),
                ),
            )

            before_restart = self.operator_status(first_repository)[
                "incident_metrics"
            ]
            after_restart = self.operator_status(
                SQLiteRuntimeRepository(database)
            )["incident_metrics"]

        self.assertEqual(after_restart, before_restart)


if __name__ == "__main__":
    unittest.main()
