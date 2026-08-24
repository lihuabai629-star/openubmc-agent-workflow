from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_ROOT = Path(__file__).resolve().parent
if str(TEST_ROOT) not in sys.path:
    sys.path.insert(0, str(TEST_ROOT))

from openubmc_target_runtime.model_planning import (  # noqa: E402
    InMemoryModelPlanningRepository,
    ModelAdapterResult,
    ModelConfiguration,
    ModelInvocationConflict,
    PlanResolver,
    PlanPolicy,
    PlanProposal,
    PlanningInput,
    PlanningRequest,
    SQLiteModelPlanningRepository,
)
from openubmc_target_runtime.mcp import RuntimeMcpService  # noqa: E402
from openubmc_target_runtime.semantic_runtime import ResumeRun, StartRun  # noqa: E402
from test_agent_gateway import SemanticBackend  # noqa: E402


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


class DeterministicModelAdapter:
    def __init__(
        self,
        result: ModelAdapterResult,
        *,
        reconcile_result: ModelAdapterResult | None = None,
    ) -> None:
        self.configuration = ModelConfiguration.freeze(
            provider="deterministic-fake",
            model="planner-v1",
            parameters={"temperature": 0, "seed": 7},
            timeout_seconds=3.0,
        )
        self.result = result
        self.reconcile_result = reconcile_result or result
        self.invoke_calls = 0
        self.reconcile_calls = 0

    def invoke(self, request):
        self.invoke_calls += 1
        return self.result

    def reconcile(self, record):
        self.reconcile_calls += 1
        return self.reconcile_result


class TimeoutThenRecoverAdapter(DeterministicModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelAdapterResult.failed("unused", "unused"),
            reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
        )

    def invoke(self, request):
        self.invoke_calls += 1
        raise TimeoutError("provider timed out after request dispatch")


class ConnectionFailureThenRecoverAdapter(DeterministicModelAdapter):
    def __init__(self) -> None:
        super().__init__(
            ModelAdapterResult.failed("unused", "unused"),
            reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
        )

    def invoke(self, request):
        self.invoke_calls += 1
        raise ConnectionError("provider connection ended after dispatch")


class ClaimObservingAdapter(DeterministicModelAdapter):
    def __init__(self, repository: InMemoryModelPlanningRepository) -> None:
        super().__init__(ModelAdapterResult.succeeded(valid_proposal()))
        self.repository = repository
        self.status_at_dispatch = ""

    def invoke(self, request):
        current = self.repository.load_invocation(request.invocation_id)
        self.status_at_dispatch = current.status if current is not None else "missing"
        return super().invoke(request)


class ModelPlanningRuntimeTests(unittest.TestCase):
    def test_valid_proposal_is_frozen_as_a_pinned_revision(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.succeeded(valid_proposal())
        )
        runtime = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
            clock=lambda: 100.0,
        )

        decision = runtime.resolve(
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
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
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
        runtime = PlanResolver(
            repository,
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
        )

        decision = runtime.resolve(
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

    def test_unknown_retry_reconciles_the_same_identity_without_reinvocation(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.unknown("provider timed out after dispatch"),
            reconcile_result=ModelAdapterResult.succeeded(valid_proposal()),
        )
        runtime = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="unknown-recovery",
            planning_input=PlanningInput(objective="recover one timed-out plan"),
        )

        first = runtime.resolve(request)
        second = runtime.resolve(request)

        self.assertEqual(first.status, "unknown")
        self.assertEqual(second.status, "accepted")
        self.assertTrue(second.reconciled)
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_timeout_exception_is_persisted_as_unknown_then_reconciled(self) -> None:
        adapter = TimeoutThenRecoverAdapter()
        runtime = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="timeout-exception",
            planning_input=PlanningInput(objective="recover the provider timeout"),
        )

        unknown = runtime.resolve(request)
        recovered = runtime.resolve(request)

        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(unknown.record.error_code, "model_outcome_unknown")
        self.assertEqual(recovered.status, "accepted")
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_post_dispatch_connection_failure_is_unknown_not_running(self) -> None:
        adapter = ConnectionFailureThenRecoverAdapter()
        runtime = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="connection-exception",
            planning_input=PlanningInput(objective="recover connection loss"),
        )

        unknown = runtime.resolve(request)
        recovered = runtime.resolve(request)

        self.assertEqual(unknown.status, "unknown")
        self.assertEqual(unknown.record.status, "unknown")
        self.assertEqual(recovered.status, "accepted")
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 1)

    def test_known_provider_failure_is_reused_without_another_call(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.failed("provider_rejected", "request was rejected")
        )
        runtime = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
        )
        request = PlanningRequest(
            run_id="run-plan-1",
            slot_id="known-failure",
            planning_input=PlanningInput(objective="reuse a known failure"),
        )

        failed = runtime.resolve(request)
        replay = runtime.resolve(request)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(replay.status, "failed")
        self.assertTrue(replay.reused)
        self.assertEqual(adapter.invoke_calls, 1)
        self.assertEqual(adapter.reconcile_calls, 0)

    def test_same_identity_reuses_same_input_and_conflicts_on_new_input(self) -> None:
        adapter = DeterministicModelAdapter(
            ModelAdapterResult.succeeded(valid_proposal())
        )
        runtime = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
        )
        original = PlanningRequest(
            run_id="run-plan-1",
            slot_id="stable-identity",
            planning_input=PlanningInput(objective="plan the upgrade"),
        )

        first = runtime.resolve(original)
        replay = runtime.resolve(original)

        self.assertEqual(first.revision, replay.revision)
        self.assertTrue(replay.reused)
        self.assertEqual(adapter.invoke_calls, 1)
        with self.assertRaisesRegex(
            ModelInvocationConflict,
            "different input or configuration",
        ):
            runtime.resolve(
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
            policy = PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            )
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

    def test_sqlite_restart_reconciles_an_unknown_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            database = Path(raw) / "runtime.sqlite3"
            policy = PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            )
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
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
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
                    policy=PlanPolicy.freeze(
                        allowed_actions={"inspect.target", "upgrade.component"},
                        allowed_gate_schemas={"upgrade-approval/v1"},
                        allowed_subflows={"diagnose": {"v1"}},
                    ),
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
            policy=PlanPolicy.freeze(
                allowed_actions={"inspect.target", "upgrade.component"},
                allowed_gate_schemas={"upgrade-approval/v1"},
                allowed_subflows={"diagnose": {"v1"}},
            ),
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

    def test_plan_revision_cannot_advance_a_gate_or_declare_terminal_success(self) -> None:
        service = RuntimeMcpService(SemanticBackend())
        try:
            self.assertEqual(
                [item["name"] for item in service.tool_definitions()],
                ["observe", "execute"],
            )
            before = service.semantic_runtime.execute(
                StartRun(
                    target="192.0.2.200",
                    intent="diagnose-and-fix",
                    purpose="prove planning has no Run authority",
                    delivery_strategy="source-only",
                    command_id="model-plan-authority-start",
                    input_digest="",
                ),
                task_id="model-plan-authority",
                operation_id="model-plan-authority-start",
            )
            decision = PlanResolver(
                InMemoryModelPlanningRepository(),
                DeterministicModelAdapter(
                    ModelAdapterResult.succeeded(complete_ir_proposal(before.run_id))
                ),
                policy=PlanPolicy.freeze(
                    allowed_actions={"inspect.target", "upgrade.component"},
                    allowed_gate_schemas={"upgrade-approval/v1"},
                    allowed_subflows={"diagnose": {"v1"}},
                ),
            ).resolve(
                PlanningRequest(
                    run_id=before.run_id,
                    slot_id="authority-proof",
                    planning_input=PlanningInput(
                        objective="propose work without changing Run facts"
                    ),
                )
            )
            after = service.semantic_runtime.execute(
                ResumeRun(before.run_id),
                task_id="model-plan-authority",
                operation_id="model-plan-authority-resume",
            )

            self.assertEqual(decision.status, "accepted")
            self.assertEqual(after.state, "waiting_response")
            self.assertEqual(after.gate.gate_id, before.gate.gate_id)
            self.assertIsNone(after.outcome)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
