from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from openubmc_target_runtime.model_planning import (  # noqa: E402
    DeterministicFakeModelAdapter,
    InMemoryModelPlanningRepository,
    ModelAdapterResult,
    ModelConfiguration,
    ModelInvocationConflict,
    ModelInvocationRecord,
    ModelPlanningError,
    PlanResolver,
    PlanPolicy,
    PlanProposal,
    PlanProposalRejected,
    PlanRevision,
    PlanningInput,
    PlanningRequest,
    SQLiteModelPlanningRepository,
)
from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from test_agent_gateway import (  # noqa: E402
    MissingFreshEpochSemanticBackend,
    SemanticBackend,
    artifact_ref,
    gate_binding,
    accept_diagnosis,
)


def proposal_mapping(
    nodes: list[dict[str, object]],
    *,
    root: str = "root",
    run_id: str = "run-plan-1",
) -> dict[str, object]:
    return {
        "schema": "openubmc.target-runtime.v1/plan-proposal-v1",
        "version": 1,
        "run_id": run_id,
        "root_node_id": root,
        "nodes": nodes,
    }


def valid_proposal(run_id: str = "run-plan-1") -> PlanProposal:
    return PlanProposal.from_mapping(
        {
            "schema": "openubmc.target-runtime.v1/plan-proposal-v1",
            "version": 1,
            "run_id": run_id,
            "root_node_id": "sequence-root",
            "nodes": [
                {
                    "node_id": "sequence-root",
                    "kind": "sequence",
                    "children": ["inspect", "approval", "upgrade", "verify"],
                },
                {
                    "node_id": "inspect",
                    "kind": "action",
                    "action": "inspect.target",
                },
                {
                    "node_id": "approval",
                    "kind": "gate",
                    "gate_schema": "upgrade-approval/v1",
                },
                {
                    "node_id": "upgrade",
                    "kind": "action",
                    "action": "upgrade.component",
                    "compensation": "rollback",
                },
                {
                    "node_id": "verify",
                    "kind": "repeat",
                    "repeat_max": 2,
                    "body": "verify-once",
                },
                {
                    "node_id": "verify-once",
                    "kind": "action",
                    "action": "inspect.target",
                },
                {
                    "node_id": "rollback",
                    "kind": "action",
                    "action": "upgrade.component",
                    "compensation_only": True,
                },
            ],
        }
    )


def complete_ir_proposal(run_id: str = "run-plan-1") -> PlanProposal:
    return PlanProposal.from_mapping(
        {
            "schema": "openubmc.target-runtime.v1/plan-proposal-v1",
            "version": 1,
            "run_id": run_id,
            "root_node_id": "root",
            "nodes": [
                {
                    "node_id": "root",
                    "kind": "sequence",
                    "children": [
                        "choose",
                        "parallel",
                        "repeat",
                        "timer",
                        "approval",
                        "subflow",
                        "upgrade",
                    ],
                },
                {
                    "node_id": "choose",
                    "kind": "choice",
                    "branches": ["choice-a", "choice-b"],
                },
                {"node_id": "choice-a", "kind": "action", "action": "inspect.target"},
                {"node_id": "choice-b", "kind": "action", "action": "inspect.target"},
                {
                    "node_id": "parallel",
                    "kind": "parallel",
                    "branches": ["parallel-a", "parallel-b"],
                },
                {"node_id": "parallel-a", "kind": "action", "action": "inspect.target"},
                {"node_id": "parallel-b", "kind": "action", "action": "inspect.target"},
                {"node_id": "repeat", "kind": "repeat", "repeat_max": 2, "body": "repeat-body"},
                {"node_id": "repeat-body", "kind": "action", "action": "inspect.target"},
                {"node_id": "timer", "kind": "timer", "timer_seconds": 30},
                {"node_id": "approval", "kind": "gate", "gate_schema": "upgrade-approval/v1"},
                {
                    "node_id": "subflow",
                    "kind": "subflow",
                    "subflow": "diagnose",
                    "subflow_version": "v1",
                },
                {
                    "node_id": "upgrade",
                    "kind": "action",
                    "action": "upgrade.component",
                    "compensation": "rollback",
                },
                {
                    "node_id": "rollback",
                    "kind": "action",
                    "action": "upgrade.component",
                    "compensation_only": True,
                },
            ],
        }
    )


def default_policy() -> PlanPolicy:
    return PlanPolicy.freeze(
        allowed_actions={"inspect.target", "upgrade.component"},
        allowed_gate_schemas={"upgrade-approval/v1"},
        allowed_subflows={"diagnose": {"v1"}},
    )


class DeterministicModelAdapter(DeterministicFakeModelAdapter):
    def __init__(
        self,
        result: ModelAdapterResult,
        *,
        reconcile_result: ModelAdapterResult | None = None,
    ) -> None:
        self.result = result
        selected_reconcile = reconcile_result or result
        super().__init__(
            invoke_results=(result,),
            reconcile_results=(selected_reconcile,),
            configuration=ModelConfiguration.freeze(
                provider="deterministic-fake",
                model="planner-v1",
                parameters={"temperature": 0, "seed": 7},
                timeout_seconds=3.0,
            ),
        )


class ExceptionThenRecoverAdapter(DeterministicModelAdapter):
    def __init__(self, error: Exception) -> None:
        super().__init__(
            ModelAdapterResult.failed("unused", "unused"),
            reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
        )
        self.error = error

    def invoke(self, request):
        del request
        self.invoke_calls += 1
        raise self.error


class OversizedFailureAdapter(DeterministicModelAdapter):
    def __init__(self) -> None:
        super().__init__(ModelAdapterResult.failed("unused", "unused"))

    def invoke(self, request):
        self.invoke_calls += 1
        raise ConnectionError("x" * 20_000)


class ClaimObservingAdapter(DeterministicModelAdapter):
    def __init__(self, repository: InMemoryModelPlanningRepository) -> None:
        super().__init__(ModelAdapterResult.succeeded(valid_proposal()))
        self.repository = repository
        self.status_at_dispatch = ""

    def invoke(self, request):
        current = self.repository.load_invocation(request.invocation_id)
        self.status_at_dispatch = current.status if current is not None else "missing"
        return super().invoke(request)


class BlockingUnknownAdapter(DeterministicModelAdapter):
    def __init__(self) -> None:
        super().__init__(ModelAdapterResult.unknown("late provider timeout"))
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(self, request):
        self.invoke_calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return self.result


class PlanResolverTests(unittest.TestCase):
    def test_valid_proposal_is_frozen_as_a_pinned_revision(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.succeeded(valid_proposal())
        )
        resolver = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
            clock=lambda: 100.0,
        )

        decision = resolver.resolve(
            PlanningRequest(
                run_id="run-plan-1",
                planning_input=PlanningInput(
                    objective="prepare a verified upgrade",
                    context_digests=("sha256:" + "a" * 64,),
                    constraints=("approval required",),
                ),
            )
        )

        self.assertEqual(decision.status, "accepted")
        self.assertEqual(decision.record.status, "succeeded")
        self.assertEqual(decision.record.effect_kind, "non_deterministic")
        self.assertEqual(decision.record.provider, "deterministic-fake")
        self.assertEqual(decision.record.model, "planner-v1")
        self.assertEqual(decision.record.provider_config["adapter_version"], "1")
        self.assertEqual(decision.record.provider_config["model_revision"], "pinned")
        self.assertIsNotNone(decision.revision)
        self.assertEqual(decision.revision.status, "pinned")
        self.assertEqual(decision.revision.run_id, "run-plan-1")
        self.assertEqual(decision.revision.ir_version, "bounded-plan-ir/v1")
        self.assertEqual(
            decision.revision.proposal.invocation_id,
            decision.record.invocation_id,
        )
        self.assertEqual(
            decision.revision.proposal.input_digest,
            decision.record.input_digest,
        )
        self.assertEqual(decision.revision.proposal.status, "proposed")
        self.assertEqual(decision.revision.error_code, "")
        self.assertEqual(adapter.invoke_calls, 1)

    def test_invocation_record_is_persisted_before_provider_dispatch(self) -> None:
        repository = InMemoryModelPlanningRepository()
        adapter = ClaimObservingAdapter(repository)

        decision = PlanResolver(
            repository,
            adapter,
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="claim-before-dispatch",
                planning_input=PlanningInput(objective="record before provider I/O"),
            )
        )

        self.assertEqual(decision.status, "accepted")
        self.assertEqual(adapter.status_at_dispatch, "running")

    def test_invalid_proposal_is_rejected_without_a_plan_revision(self) -> None:
        raw = valid_proposal().to_public_dict()
        raw["nodes"][1]["action"] = "unknown.root-shell"
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.succeeded(PlanProposal.from_mapping(raw))
        )
        repository = InMemoryModelPlanningRepository()
        resolver = PlanResolver(
            repository,
            adapter,
            policy=default_policy(),
        )

        decision = resolver.resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="invalid-proposal",
                planning_input=PlanningInput(objective="invent an unsafe action"),
            )
        )

        self.assertEqual(decision.status, "rejected")
        self.assertEqual(decision.record.status, "rejected")
        self.assertEqual(decision.record.error_code, "plan_proposal_rejected")
        self.assertIn("unknown Plan action", decision.record.error_message)
        self.assertIsNone(decision.revision)
        self.assertEqual(repository.list_revisions(), ())

    def test_rejection_message_is_bounded_before_terminal_persistence(self) -> None:
        raw = valid_proposal().to_public_dict()
        raw["nodes"][1]["action"] = "x" * 6_000
        repository = InMemoryModelPlanningRepository()

        decision = PlanResolver(
            repository,
            DeterministicModelAdapter(ModelAdapterResult.succeeded(raw)),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="bounded-rejection",
                planning_input=PlanningInput(objective="bound controlled rejection text"),
            )
        )

        self.assertEqual(decision.status, "rejected")
        self.assertLessEqual(
            len(decision.record.error_message.encode("utf-8")),
            4096,
        )
        self.assertEqual(
            repository.load_invocation(decision.record.invocation_id).status,
            "rejected",
        )

    def test_non_json_proposal_is_rejected_after_the_invocation_is_claimed(self) -> None:
        repository = InMemoryModelPlanningRepository()
        raw = valid_proposal().to_public_dict()
        raw["nodes"][0]["non_json"] = object()

        decision = PlanResolver(
            repository,
            DeterministicModelAdapter(ModelAdapterResult.succeeded(raw)),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="non-json-proposal",
                planning_input=PlanningInput(objective="reject non-json output"),
            )
        )

        self.assertEqual(decision.status, "rejected")
        self.assertEqual(decision.record.status, "rejected")
        self.assertEqual(
            repository.load_invocation(decision.record.invocation_id).status,
            "rejected",
        )

    def test_explicit_provider_configuration_must_match_the_invocation(self) -> None:
        configuration = ModelConfiguration.freeze(
            provider="deterministic-fake",
            model="planner-v1",
            parameters={"temperature": 0, "seed": 7},
            timeout_seconds=3.0,
        )
        raw = valid_proposal().to_public_dict()
        raw["provider_config"] = {
            **configuration.to_public_dict(),
            "model": "contradictory-model",
        }
        raw["provider_config_digest"] = configuration.digest
        adapter = DeterministicFakeModelAdapter(
            invoke_results=(ModelAdapterResult.succeeded(raw),),
            configuration=configuration,
        )

        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="provider-config-conflict",
                planning_input=PlanningInput(
                    objective="reject contradictory provider configuration"
                ),
            )
        )

        self.assertEqual(decision.status, "rejected")
        self.assertIn("provider_config", decision.record.error_message)

    def test_proposed_plan_cannot_contain_error_evidence(self) -> None:
        raw = valid_proposal().to_public_dict()
        raw["error_code"] = "provider_failed"
        raw["error_message"] = "contradictory error"

        with self.assertRaisesRegex(PlanProposalRejected, "error"):
            PlanProposal.from_mapping(raw)

    def test_unknown_retry_reconciles_the_same_identity_without_reinvocation(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.unknown("provider timed out after dispatch"),
            reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
        )
        resolver = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="unknown-recovery",
            planning_input=PlanningInput(objective="recover one timed-out plan"),
        )

        first = resolver.resolve(request)
        second = resolver.resolve(request)

        self.assertEqual(first.status, "unknown")
        self.assertEqual(second.status, "accepted")
        self.assertTrue(second.reconciled)
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_late_unknown_cannot_overwrite_a_concurrent_accepted_revision(self) -> None:
        repository = InMemoryModelPlanningRepository()
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="concurrent-settlement",
            planning_input=PlanningInput(objective="settle one provider identity"),
        )
        invoking_adapter = BlockingUnknownAdapter()
        invoking_resolver = PlanResolver(
            repository,
            invoking_adapter,
            policy=default_policy(),
        )
        decisions: list[object] = []

        worker = threading.Thread(
            target=lambda: decisions.append(invoking_resolver.resolve(request))
        )
        worker.start()
        self.assertTrue(invoking_adapter.started.wait(timeout=1))
        recovered = PlanResolver(
            repository,
            DeterministicModelAdapter(
                ModelAdapterResult.failed("unused", "unused"),
                reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
            ),
            policy=default_policy(),
        ).resolve(request)
        invoking_adapter.release.set()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(recovered.status, "accepted")
        self.assertEqual(decisions[0].status, "accepted")
        persisted = repository.load_invocation(recovered.record.invocation_id)
        self.assertEqual(persisted.status, "succeeded")
        self.assertEqual(persisted.plan_revision_id, recovered.revision.revision_id)
        self.assertEqual(repository.list_revisions(), (recovered.revision,))

    def test_sqlite_late_unknown_cannot_overwrite_an_accepted_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "concurrent-runtime.sqlite3"
            request = PlanningRequest(
                run_id="run-plan-1",
                slot_id="sqlite-concurrent-settlement",
                planning_input=PlanningInput(
                    objective="settle one persisted provider identity"
                ),
            )
            invoking_adapter = BlockingUnknownAdapter()
            decisions: list[object] = []
            worker = threading.Thread(
                target=lambda: decisions.append(
                    PlanResolver(
                        SQLiteModelPlanningRepository(database),
                        invoking_adapter,
                        policy=default_policy(),
                    ).resolve(request)
                )
            )
            worker.start()
            self.assertTrue(invoking_adapter.started.wait(timeout=1))
            repository = SQLiteModelPlanningRepository(database)
            recovered = PlanResolver(
                repository,
                DeterministicModelAdapter(
                    ModelAdapterResult.failed("unused", "unused"),
                    reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
                ),
                policy=default_policy(),
            ).resolve(request)
            invoking_adapter.release.set()
            worker.join(timeout=2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(recovered.status, "accepted")
            self.assertEqual(decisions[0].status, "accepted")
            persisted = repository.load_invocation(recovered.record.invocation_id)
            self.assertEqual(persisted.status, "succeeded")
            self.assertEqual(
                repository.load_revision(persisted.plan_revision_id),
                recovered.revision,
            )

    def test_timeout_exception_is_persisted_as_unknown_then_reconciled(self) -> None:
        adapter = ExceptionThenRecoverAdapter(
            TimeoutError("provider timed out after request dispatch")
        )
        resolver = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="timeout-exception",
            planning_input=PlanningInput(objective="recover the provider timeout"),
        )

        unknown = resolver.resolve(request)
        recovered = resolver.resolve(request)

        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(unknown.record.error_code, "model_outcome_unknown")
        self.assertEqual(recovered.status, "accepted")
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_post_dispatch_connection_failure_is_unknown_not_running(self) -> None:
        adapter = ExceptionThenRecoverAdapter(
            ConnectionError("provider connection ended after dispatch")
        )
        resolver = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="connection-exception",
            planning_input=PlanningInput(objective="recover connection loss"),
        )

        unknown = resolver.resolve(request)
        recovered = resolver.resolve(request)

        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(unknown.record.status, "unknown")
        self.assertEqual(recovered.status, "accepted")
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_provider_exception_is_bounded_before_persistence(self) -> None:
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            OversizedFailureAdapter(),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="bounded-provider-error",
                planning_input=PlanningInput(objective="bound provider failure"),
            )
        )

        self.assertEqual(decision.status, "unknown")
        self.assertLessEqual(
            len(decision.record.error_message.encode("utf-8")),
            4096,
        )

    def test_known_provider_failure_is_reused_without_another_call(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.failed("provider_rejected", "request was rejected")
        )
        resolver = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="known-failure",
            planning_input=PlanningInput(objective="reuse a known failure"),
        )

        failed = resolver.resolve(request)
        replay = resolver.resolve(request)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(replay.status, "failed")
        self.assertTrue(replay.reused)
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 0)

    def test_same_identity_reuses_same_input_and_conflicts_on_new_input(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.succeeded(valid_proposal())
        )
        resolver = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        )
        original = PlanningRequest(
            run_id="run-plan-1",
            slot_id="stable-identity",
            planning_input=PlanningInput(objective="plan the upgrade"),
        )

        first = resolver.resolve(original)
        replay = resolver.resolve(original)

        self.assertEqual(first.revision, replay.revision)
        self.assertTrue(replay.reused)
        self.assertEqual(adapter.invoke_calls, 1)
        with self.assertRaisesRegex(
            ModelInvocationConflict,
            "different input or configuration",
        ):
            resolver.resolve(
                PlanningRequest(
                    run_id="run-plan-1",
                    slot_id="stable-identity",
                    planning_input=PlanningInput(objective="plan a downgrade"),
                )
            )
        self.assertEqual(adapter.invoke_calls, 1)

    def test_sqlite_restart_reuses_the_pinned_revision_without_a_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            policy = default_policy()
            request = PlanningRequest(
                run_id="run-plan-1",
                slot_id="restart-replay",
                planning_input=PlanningInput(objective="persist the accepted plan"),
            )
            first_adapter = DeterministicModelAdapter(
                ModelAdapterResult.succeeded(valid_proposal())
            )
            first = PlanResolver(
                SQLiteModelPlanningRepository(database),
                first_adapter,
                policy=policy,
            ).resolve(request)

            restart_adapter = DeterministicModelAdapter(
                ModelAdapterResult.failed(
                    "unexpected_call",
                    "restart must not invoke the model",
                )
            )
            replay = PlanResolver(
                SQLiteModelPlanningRepository(database),
                restart_adapter,
                policy=policy,
            ).resolve(request)

            self.assertEqual(replay.status, "accepted")
            self.assertTrue(replay.reused)
            self.assertEqual(replay.revision, first.revision)
            self.assertEqual(restart_adapter.invoke_calls, 0)
            self.assertEqual(restart_adapter.reconcile_calls, 0)

    def test_sqlite_replay_revalidates_the_persisted_revision_against_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "policy-replay.sqlite3"
            request = PlanningRequest(
                run_id="run-plan-1",
                slot_id="policy-replay",
                planning_input=PlanningInput(objective="revalidate the pinned plan"),
            )
            first = PlanResolver(
                SQLiteModelPlanningRepository(database),
                DeterministicModelAdapter(
                    ModelAdapterResult.succeeded(valid_proposal())
                ),
                policy=default_policy(),
            ).resolve(request)
            tampered_revision = first.revision.to_public_dict()
            tampered_proposal = dict(tampered_revision["proposal"])
            tampered_nodes = [dict(node) for node in tampered_proposal["nodes"]]
            tampered_nodes[1]["action"] = "unknown.persisted-action"
            tampered_proposal["nodes"] = tampered_nodes
            tampered_proposal_digest = PlanProposal.from_mapping(
                tampered_proposal
            ).digest
            tampered_revision_id = (
                "plan-revision:"
                + tampered_proposal_digest.removeprefix("sha256:")[:48]
            )
            tampered_revision["proposal"] = tampered_proposal
            tampered_revision["proposal_digest"] = tampered_proposal_digest
            tampered_revision["revision_id"] = tampered_revision_id
            tampered_record = first.record.to_public_dict()
            tampered_record["result_digest"] = tampered_proposal_digest
            tampered_record["plan_revision_id"] = tampered_revision_id
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "DELETE FROM model_planning_revisions WHERE revision_id = ?",
                    (first.revision.revision_id,),
                )
                connection.execute(
                    "INSERT INTO model_planning_revisions "
                    "(revision_id, document_json, created_at) VALUES (?, ?, ?)",
                    (
                        tampered_revision_id,
                        json.dumps(
                            tampered_revision,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        first.revision.created_at,
                    ),
                )
                connection.execute(
                    "UPDATE model_planning_invocations SET document_json = ? "
                    "WHERE invocation_id = ?",
                    (
                        json.dumps(
                            tampered_record,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        first.record.invocation_id,
                    ),
                )
            replay_adapter = DeterministicModelAdapter(
                ModelAdapterResult.failed(
                    "unexpected_call",
                    "replay must not call the model",
                )
            )

            with self.assertRaisesRegex(
                PlanProposalRejected,
                "unknown Plan action",
            ):
                PlanResolver(
                    SQLiteModelPlanningRepository(database),
                    replay_adapter,
                    policy=default_policy(),
                ).resolve(request)

            self.assertEqual(replay_adapter.invoke_calls, 0)
            self.assertEqual(replay_adapter.reconcile_calls, 0)

    def test_sqlite_replay_rejects_a_revision_from_another_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "mismatched-settlement.sqlite3"
            repository = SQLiteModelPlanningRepository(database)
            first_request = PlanningRequest(
                run_id="run-plan-a",
                slot_id="mismatched-replay",
                planning_input=PlanningInput(objective="pin plan a"),
            )
            first = PlanResolver(
                repository,
                DeterministicModelAdapter(
                    ModelAdapterResult.succeeded(valid_proposal("run-plan-a"))
                ),
                policy=default_policy(),
            ).resolve(first_request)
            second = PlanResolver(
                repository,
                DeterministicModelAdapter(
                    ModelAdapterResult.succeeded(valid_proposal("run-plan-b"))
                ),
                policy=default_policy(),
            ).resolve(
                PlanningRequest(
                    run_id="run-plan-b",
                    slot_id="mismatched-replay",
                    planning_input=PlanningInput(objective="pin plan b"),
                )
            )
            mismatched = first.record.to_public_dict()
            mismatched["plan_revision_id"] = second.revision.revision_id
            mismatched["result_digest"] = second.revision.proposal_digest
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE model_planning_invocations SET document_json = ? "
                    "WHERE invocation_id = ?",
                    (
                        json.dumps(mismatched, sort_keys=True, separators=(",", ":")),
                        first.record.invocation_id,
                    ),
                )

            with self.assertRaisesRegex(
                ModelInvocationConflict,
                "settlement",
            ):
                PlanResolver(
                    SQLiteModelPlanningRepository(database),
                    DeterministicModelAdapter(
                        ModelAdapterResult.failed(
                            "unexpected_call",
                            "replay must not call the model",
                        )
                    ),
                    policy=default_policy(),
                ).resolve(first_request)

    def test_sqlite_restart_reconciles_an_unknown_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            policy = default_policy()
            request = PlanningRequest(
                run_id="run-plan-1",
                slot_id="restart-unknown",
                planning_input=PlanningInput(objective="recover after restart"),
            )
            first_adapter = DeterministicModelAdapter(
                ModelAdapterResult.unknown("connection ended after dispatch")
            )
            unknown = PlanResolver(
                SQLiteModelPlanningRepository(database),
                first_adapter,
                policy=policy,
            ).resolve(request)
            restart_adapter = DeterministicModelAdapter(
                ModelAdapterResult.failed("must_reconcile", "invoke is forbidden"),
                reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
            )

            recovered = PlanResolver(
                SQLiteModelPlanningRepository(database),
                restart_adapter,
                policy=policy,
            ).resolve(request)

            self.assertEqual(unknown.status, "unknown")
            self.assertEqual(recovered.status, "accepted")
            self.assertTrue(recovered.reconciled)
            self.assertEqual(restart_adapter.invoke_calls, 0)
            self.assertEqual(restart_adapter.reconcile_calls, 1)

    def test_complete_bounded_ir_is_accepted(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.succeeded(complete_ir_proposal())
        )
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="complete-ir",
                planning_input=PlanningInput(objective="exercise the bounded IR"),
            )
        )

        self.assertEqual(decision.status, "accepted")
        self.assertEqual(len(decision.revision.proposal.nodes), 14)

    def test_schema_reference_and_budget_violations_are_rejected(self) -> None:
        base = complete_ir_proposal().to_public_dict()
        cases = {
            "schema": {"schema": "unknown/plan", "version": 1},
            "reference": {
                **base,
                "nodes": [
                    {
                        **base["nodes"][0],
                        "children": ["missing"],
                    },
                    *base["nodes"][1:],
                ],
            },
            "unbounded_repeat": {
                **base,
                "nodes": [
                    {**node, "repeat_max": 99}
                    if node["node_id"] == "repeat"
                    else node
                    for node in base["nodes"]
                ],
            },
            "unbounded_parallel": {
                **base,
                "nodes": [
                    {
                        **node,
                        "branches": [
                            "parallel-a",
                            "parallel-b",
                            "choice-a",
                            "choice-b",
                            "repeat-body",
                        ],
                    }
                    if node["node_id"] == "parallel"
                    else node
                    for node in base["nodes"]
                ],
            },
            "unsupported_construct": {
                **base,
                "nodes": [
                    {**node, "kind": "outcome"}
                    if node["node_id"] == "timer"
                    else node
                    for node in base["nodes"]
                ],
            },
            "unknown_field": {
                **base,
                "nodes": [
                    {**node, "shell": "rm -rf /tmp/example"}
                    if node["node_id"] == "choice-a"
                    else node
                    for node in base["nodes"]
                ],
            },
            "node_budget": proposal_mapping(
                [
                    {
                        "node_id": "root",
                        "kind": "sequence",
                        "children": [f"step-{item}" for item in range(32)],
                    },
                    *[
                        {
                            "node_id": f"step-{item}",
                            "kind": "action",
                            "action": "inspect.target",
                        }
                        for item in range(32)
                    ],
                ]
            ),
            "expanded_step_budget": proposal_mapping(
                [
                    {
                        "node_id": "root",
                        "kind": "sequence",
                        "children": [f"repeat-{item}" for item in range(30)],
                    },
                    *[
                        {
                            "node_id": f"repeat-{item}",
                            "kind": "repeat",
                            "repeat_max": 3,
                            "body": "shared-body",
                        }
                        for item in range(30)
                    ],
                    {
                        "node_id": "shared-body",
                        "kind": "action",
                        "action": "inspect.target",
                    },
                ]
            ),
            "invalid_json_types": {
                **base,
                "version": True,
                "nodes": [
                    {**node, "repeat_max": "2"}
                    if node["node_id"] == "repeat"
                    else {**node, "timer_seconds": True}
                    if node["node_id"] == "timer"
                    else node
                    for node in base["nodes"]
                ],
            },
        }
        for index, (name, raw_proposal) in enumerate(cases.items()):
            with self.subTest(name=name):
                adapter = DeterministicModelAdapter(
                    ModelAdapterResult.succeeded(raw_proposal)
                )
                decision = PlanResolver(
                    InMemoryModelPlanningRepository(),
                    adapter,
                    policy=default_policy(),
                ).resolve(
                    PlanningRequest(
                        run_id="run-plan-1",
                        slot_id=f"invalid-{index}",
                        planning_input=PlanningInput(objective=f"reject {name}"),
                    )
                )
                self.assertEqual(decision.status, "rejected")
                self.assertEqual(decision.record.error_code, "plan_proposal_rejected")

    def test_pinned_revision_cannot_be_mutated_through_provider_config(self) -> None:
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            DeterministicModelAdapter(
                ModelAdapterResult.succeeded(valid_proposal())
            ),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="immutable-revision",
                planning_input=PlanningInput(objective="freeze provider configuration"),
            )
        )
        before = decision.revision.proposal.digest
        exposed = decision.revision.proposal.provider_config
        exposed["parameters"]["temperature"] = 99

        self.assertEqual(decision.revision.proposal.digest, before)
        self.assertEqual(
            decision.revision.proposal.provider_config["parameters"]["temperature"],
            0,
        )
        self.assertEqual(decision.revision.proposal_digest, before)

    def test_persisted_revision_rejects_contradictory_proposal_bindings(self) -> None:
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            DeterministicModelAdapter(
                ModelAdapterResult.succeeded(valid_proposal())
            ),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="contradictory-revision",
                planning_input=PlanningInput(objective="pin consistent bindings"),
            )
        )
        revision = decision.revision.to_public_dict()
        cases = {
            "invocation_id": "model-plan:" + "f" * 48,
            "input_digest": "sha256:" + "1" * 64,
            "provider_config_digest": "sha256:" + "2" * 64,
            "provider_config": {
                **revision["proposal"]["provider_config"],
                "model": "contradictory-model",
            },
        }

        for name, value in cases.items():
            with self.subTest(name=name):
                tampered = dict(revision)
                raw_proposal = dict(revision["proposal"])
                raw_proposal[name] = value
                tampered["proposal"] = raw_proposal
                tampered["proposal_digest"] = PlanProposal.from_mapping(
                    raw_proposal
                ).digest
                tampered["revision_id"] = (
                    "plan-revision:"
                    + tampered["proposal_digest"].removeprefix("sha256:")[:48]
                )
                with self.assertRaisesRegex(
                    ModelPlanningError,
                    f"proposal {name}",
                ):
                    PlanRevision.from_public_dict(tampered)

    def test_persisted_invocation_rejects_contradictory_status_evidence(self) -> None:
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            DeterministicModelAdapter(
                ModelAdapterResult.succeeded(valid_proposal())
            ),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="status-evidence",
                planning_input=PlanningInput(objective="pin status evidence"),
            )
        )
        succeeded = decision.record.to_public_dict()
        cases = {
            "running_with_result": {**succeeded, "status": "running"},
            "unknown_with_result": {
                **succeeded,
                "status": "unknown",
                "error_code": "model_outcome_unknown",
            },
            "failed_without_error": {
                **succeeded,
                "status": "failed",
                "result_digest": "",
                "plan_revision_id": "",
                "error_code": "",
            },
            "succeeded_with_error": {
                **succeeded,
                "error_code": "contradictory_error",
            },
            "forged_provider": {
                **succeeded,
                "provider": "contradictory-provider",
            },
            "forged_model": {
                **succeeded,
                "model": "contradictory-model",
            },
        }

        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaises(ModelPlanningError):
                ModelInvocationRecord.from_public_dict(raw)

    def test_persisted_records_reject_coerced_or_non_finite_scalar_types(self) -> None:
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            DeterministicModelAdapter(
                ModelAdapterResult.succeeded(valid_proposal())
            ),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="strict-record-types",
                planning_input=PlanningInput(objective="reject coerced record values"),
            )
        )
        invocation = decision.record.to_public_dict()
        revision = decision.revision.to_public_dict()
        invocation_cases = {
            "fractional_version": {**invocation, "version": 1.9},
            "boolean_run_id": {**invocation, "run_id": True},
            "nan_started_at": {**invocation, "started_at": float("nan")},
            "infinite_updated_at": {**invocation, "updated_at": float("inf")},
            "non_finite_provider_parameter": {
                **invocation,
                "provider_config": {
                    **invocation["provider_config"],
                    "parameters": {"temperature": float("nan")},
                },
            },
        }
        revision_cases = {
            "fractional_version": {**revision, "version": 1.9},
            "nan_created_at": {**revision, "created_at": float("nan")},
        }

        for name, raw in invocation_cases.items():
            with self.subTest(record=name), self.assertRaises(ModelPlanningError):
                ModelInvocationRecord.from_public_dict(raw)
        for name, raw in revision_cases.items():
            with self.subTest(revision=name), self.assertRaises(ModelPlanningError):
                PlanRevision.from_public_dict(raw)

    def test_sqlite_rejects_ambiguous_or_non_finite_json_documents(self) -> None:
        corruptions = {
            "duplicate_key": lambda document: document.replace(
                '"status":"succeeded"',
                '"status":"failed","status":"succeeded"',
                1,
            ),
            "non_finite_constant": lambda document: document.removesuffix("}")
            + ',"unknown_number":NaN}',
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw:
                database = Path(raw) / "strict-json.sqlite3"
                decision = PlanResolver(
                    SQLiteModelPlanningRepository(database),
                    DeterministicModelAdapter(
                        ModelAdapterResult.succeeded(valid_proposal())
                    ),
                    policy=default_policy(),
                ).resolve(
                    PlanningRequest(
                        run_id="run-plan-1",
                        slot_id=f"strict-json-{name}",
                        planning_input=PlanningInput(
                            objective="reject ambiguous persisted JSON"
                        ),
                    )
                )
                with sqlite3.connect(database) as connection:
                    document = connection.execute(
                        "SELECT document_json FROM model_planning_invocations "
                        "WHERE invocation_id = ?",
                        (decision.record.invocation_id,),
                    ).fetchone()[0]
                    connection.execute(
                        "UPDATE model_planning_invocations SET document_json = ? "
                        "WHERE invocation_id = ?",
                        (corrupt(document), decision.record.invocation_id),
                    )

                with self.assertRaisesRegex(ModelPlanningError, "strict JSON"):
                    SQLiteModelPlanningRepository(database).load_invocation(
                        decision.record.invocation_id
                    )

    def test_model_configuration_rejects_non_finite_json_and_timeout_values(self) -> None:
        cases = (
            {"parameters": {"temperature": float("nan")}},
            {"parameters": {"temperature": float("inf")}},
            {"timeout_seconds": "nan"},
            {"timeout_seconds": float("inf")},
        )

        for values in cases:
            with self.subTest(values=values), self.assertRaises(ModelPlanningError):
                ModelConfiguration.freeze(
                    provider="deterministic-fake",
                    model="planner-v1",
                    parameters=values.get("parameters", {"temperature": 0}),
                    timeout_seconds=values.get("timeout_seconds", 3.0),
                )

        with self.assertRaises(ModelPlanningError):
            ModelConfiguration(
                provider="deterministic-fake",
                model="planner-v1",
                parameters_json='{"temperature": NaN}',
                timeout_seconds=3.0,
            )

    def test_plan_revision_identity_is_derived_from_its_proposal_digest(self) -> None:
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            DeterministicModelAdapter(
                ModelAdapterResult.succeeded(valid_proposal())
            ),
            policy=default_policy(),
        ).resolve(
            PlanningRequest(
                run_id="run-plan-1",
                slot_id="revision-identity",
                planning_input=PlanningInput(objective="pin revision identity"),
            )
        )
        raw = decision.revision.to_public_dict()
        raw["revision_id"] = "plan-revision:arbitrary-safe-id"

        with self.assertRaisesRegex(ModelPlanningError, "revision_id"):
            PlanRevision.from_public_dict(raw)

    def test_plan_revision_cannot_advance_a_gate_or_declare_terminal_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = RuntimeMcpService(MissingFreshEpochSemanticBackend())
            try:
                self.assertEqual(
                    [item["name"] for item in service.tool_definitions()],
                    ["observe", "execute"],
                )
                before = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "start",
                        "target": "192.0.2.200",
                        "intent": "diagnose-and-fix",
                        "purpose": "prove planning has no Run authority",
                        "delivery_strategy": "live-patch",
                    },
                    task_id="model-plan-authority",
                    operation_id="model-plan-authority-start",
                )
                decision = PlanResolver(
                    InMemoryModelPlanningRepository(),
                    DeterministicModelAdapter(
                        ModelAdapterResult.succeeded(
                            complete_ir_proposal(before["run_id"])
                        )
                    ),
                    policy=default_policy(),
                ).resolve(
                    PlanningRequest(
                        run_id=before["run_id"],
                        slot_id="authority-proof",
                        planning_input=PlanningInput(
                            objective="propose work without changing Run facts"
                        ),
                    )
                )
                after = service.call_exposed_tool(
                    "execute",
                    {"kind": "resume", "run_id": before["run_id"]},
                    task_id="model-plan-authority",
                    operation_id="model-plan-authority-resume",
                )

                self.assertEqual(decision.status, "accepted")
                self.assertEqual(after["state"], "waiting_response")
                self.assertEqual(after["gate"]["gate_id"], before["gate"]["gate_id"])
                self.assertIsNone(after["outcome"])

                before = accept_diagnosis(service, before, task_id="model-plan-authority")
                patch_file = Path(raw) / "model-planning-authority.lua"
                patch_file.write_bytes(b"return 'authority-proof'\n")
                final = service.call_exposed_tool(
                    "execute",
                    {
                        "kind": "respond",
                        "run_id": before["run_id"],
                        **gate_binding(before),
                        "response": {
                            "status": "completed",
                            "summary": "source repair ready",
                            "payload": {
                                "source_revision": "authority-proof-source",
                                "authored_files": ["src/fix.lua"],
                                "verification_plan": ["fresh target verification"],
                                "artifact_ref": artifact_ref(
                                    patch_file,
                                    kind="openubmc-live-patch",
                                    target="192.0.2.200",
                                    run_id=before["run_id"],
                                ),
                                "remote_path": "/opt/bmc/apps/fix.lua",
                                "restart_scope": "skynet",
                            },
                        },
                    },
                    task_id="model-plan-authority",
                    operation_id="model-plan-authority-respond",
                )
                for attempt in range(1, 4):
                    if final["state"] != "running":
                        break
                    final = service.call_exposed_tool(
                        "execute",
                        {"kind": "resume", "run_id": before["run_id"]},
                        task_id="model-plan-authority",
                        operation_id=f"model-plan-authority-verify-{attempt}",
                    )
                projection = service._test.context_runtime.read_case(
                    before["run_id"]
                )

                self.assertEqual(final["state"], "running")
                self.assertIsNone(final["outcome"])
                self.assertFalse(projection.get("run_outcome"))
                self.assertIn("fresh target verification", final["next"])
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
