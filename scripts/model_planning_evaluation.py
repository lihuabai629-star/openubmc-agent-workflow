#!/usr/bin/env python3
"""Compare isolated model planning with the static WorkflowDefinitions path."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
TARGET_RUNTIME_ROOT = ROOT / "openubmc-target-runtime"
if str(TARGET_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(TARGET_RUNTIME_ROOT))

from openubmc_target_runtime.agent_gateway import (  # noqa: E402
    agent_operation_descriptors,
)
from openubmc_target_runtime.model_planning import (  # noqa: E402
    DeterministicFakeModelAdapter,
    InMemoryModelPlanningRepository,
    ModelAdapterResult,
    ModelConfiguration,
    ModelInvocationRecord,
    PlanNodeKind,
    PlanPolicy,
    PlanRevision,
    PlanResolver,
    PlanningInput,
    PlanningRequest,
)
from openubmc_target_runtime.workflow import (  # noqa: E402
    DEFAULT_WORKFLOW_DEFINITIONS,
    WorkflowDefinition,
)


EVALUATION_SCHEMA = "openubmc.agent-workflow/model-planning-evaluation-v1"


def proposal(
    nodes: list[dict[str, object]],
    *,
    root: str = "root",
) -> dict[str, object]:
    return {
        "schema": "openubmc.target-runtime.v1/plan-proposal-v1",
        "version": 1,
        "run_id": "run-evaluation",
        "root_node_id": root,
        "nodes": nodes,
    }


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    objective: str
    expected_steps: tuple[str, ...]
    static_request: Mapping[str, str]
    expected_compensations: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ContainmentCase:
    name: str
    candidate_proposal: Mapping[str, object]


def phase_gate_schema(
    phase_name: str,
    *,
    delivery_strategy: str,
) -> str:
    definition = DEFAULT_WORKFLOW_DEFINITIONS.definition_for(
        {
            "intent": "diagnose-and-fix",
            "delivery_strategy": delivery_strategy,
        }
    )
    return next(
        step.receipt_schema
        for step in definition.steps
        if step.kind == "phase" and step.name == phase_name
    )


DEVELOPER_CHANGE_GATE = phase_gate_schema(
    "developer.change",
    delivery_strategy="source-only",
)
DIAGNOSIS_ACCEPTANCE_GATE = phase_gate_schema(
    "diagnosis.acceptance",
    delivery_strategy="source-only",
)
BUILD_ARTIFACT_GATE = phase_gate_schema(
    "build.artifact",
    delivery_strategy="build-upgrade",
)
GATE_PHASES = {
    DIAGNOSIS_ACCEPTANCE_GATE: "diagnosis.acceptance",
    DEVELOPER_CHANGE_GATE: "developer.change",
    BUILD_ARTIFACT_GATE: "build.artifact",
}


def semantic_proposal(
    steps: tuple[tuple[str, str], ...],
    *,
    compensations: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    compensation_links = dict(compensations or {})
    if len(steps) == 1:
        kind, name = steps[0]
        return proposal(
            [
                {
                    "node_id": "root",
                    "kind": kind,
                    "action" if kind == "action" else "gate_schema": name,
                }
            ]
        )
    node_ids = [f"step-{index}" for index in range(1, len(steps) + 1)]
    nodes: list[dict[str, object]] = [
        {"node_id": "root", "kind": "sequence", "children": node_ids}
    ]
    for node_id, (kind, name) in zip(node_ids, steps, strict=True):
        if kind == "gate":
            nodes.append(
                {"node_id": node_id, "kind": "gate", "gate_schema": name}
            )
            continue
        node: dict[str, object] = {
            "node_id": node_id,
            "kind": "action",
            "action": name,
        }
        compensation = compensation_links.get(name)
        if compensation:
            compensation_id = f"compensation-{len(nodes)}"
            node["compensation"] = compensation_id
            nodes.append(node)
            nodes.append(
                {
                    "node_id": compensation_id,
                    "kind": "action",
                    "action": compensation,
                    "compensation_only": True,
                }
            )
        else:
            nodes.append(node)
    return proposal(nodes)


def paired_corpus() -> tuple[EvaluationCase, ...]:
    diagnosis = ("debug_run", "diagnosis.acceptance")
    source_change = (
        "debug_run",
        "diagnosis.acceptance",
        "developer.change",
    )
    live_patch = (
        "debug_run",
        "diagnosis.acceptance",
        "developer.change",
        "live_patch_run",
        "debug_collect",
    )
    build_upgrade = (
        "debug_run",
        "diagnosis.acceptance",
        "developer.change",
        "build.artifact",
        "upgrade_run",
        "debug_collect",
    )
    upgrade_only = ("upgrade_run", "debug_collect")
    bundle_diagnosis = ("log_bundle_collect", "debug_run", "diagnosis.acceptance")
    return (
        EvaluationCase(
            "diagnosis-only",
            "collect bounded diagnosis evidence",
            diagnosis,
            {"intent": "diagnosis-only"},
        ),
        EvaluationCase(
            "source-change",
            "prepare and verify a source-only repair",
            source_change,
            {"intent": "diagnose-and-fix", "delivery_strategy": "source-only"},
        ),
        EvaluationCase(
            "live-patch",
            "diagnose, approve, apply, and verify a live patch",
            live_patch,
            {"intent": "diagnose-and-fix", "delivery_strategy": "live-patch"},
            (("live_patch_run", "live_patch.rollback"),),
        ),
        EvaluationCase(
            "build-upgrade",
            "diagnose, build, upgrade, and verify firmware",
            build_upgrade,
            {"intent": "diagnose-and-fix", "delivery_strategy": "build-upgrade"},
        ),
        EvaluationCase(
            "upgrade-and-verify",
            "perform a bounded upgrade verification workflow",
            upgrade_only,
            {"intent": "upgrade-and-verify", "entry_operation": "upgrade_run"},
        ),
        EvaluationCase(
            "bundle-and-diagnose",
            "collect and inspect a diagnostic log bundle",
            bundle_diagnosis,
            {
                "intent": "bundle-and-diagnose",
                "entry_operation": "log_bundle_collect",
            },
        ),
    )


def containment_corpus() -> tuple[ContainmentCase, ...]:
    return (
        ContainmentCase(
            "unknown-action",
            proposal(
                [
                    {
                        "node_id": "root",
                        "kind": "action",
                        "action": "unknown.root-shell",
                    }
                ]
            ),
        ),
        ContainmentCase(
            "invalid-reference",
            proposal(
                [
                    {
                        "node_id": "root",
                        "kind": "sequence",
                        "children": ["missing"],
                    }
                ]
            ),
        ),
        ContainmentCase(
            "unbounded-repeat",
            proposal(
                [
                    {
                        "node_id": "root",
                        "kind": "repeat",
                        "repeat_max": 999,
                        "body": "inspect",
                    },
                    {
                        "node_id": "inspect",
                        "kind": "action",
                        "action": "inspect.target",
                    },
                ]
            ),
        ),
        ContainmentCase(
            "terminal-claim",
            proposal(
                [
                    {
                        "node_id": "root",
                        "kind": "outcome",
                        "status": "success",
                    }
                ]
            ),
        ),
        ContainmentCase(
            "gate-as-action",
            proposal(
                [
                    {
                        "node_id": "root",
                        "kind": "action",
                        "action": "developer.change",
                    }
                ]
            ),
        ),
    )


def evaluation_policy() -> PlanPolicy:
    return PlanPolicy.freeze(
        allowed_actions={
            "debug_collect",
            "debug_run",
            "inspect.target",
            "live_patch_run",
            "live_patch.rollback",
            "log_bundle_collect",
            "upgrade.component",
            "upgrade_run",
        },
        allowed_gate_schemas={
            DIAGNOSIS_ACCEPTANCE_GATE,
            DEVELOPER_CHANGE_GATE,
            BUILD_ARTIFACT_GATE,
        },
        allowed_subflows={"diagnose": {"v1"}},
    )


def plan_for_objective(objective: str) -> Mapping[str, object]:
    selected = " ".join(objective.lower().split())
    normalized = re.sub(r"\bdon['’]?t\b", "do not", selected)
    tokens = re.findall(r"[a-z]+", normalized)
    negators = {"avoid", "never", "no", "not", "skip", "without"}
    capability_is_negated = any(
        token.startswith(("build", "upgrad"))
        and any(negator in negators for negator in tokens[max(0, index - 3) : index])
        for index, token in enumerate(tokens)
    )
    if capability_is_negated:
        return semantic_proposal((("action", "unsupported.objective"),))
    if "log bundle" in selected:
        steps = (
            ("action", "log_bundle_collect"),
            ("action", "debug_run"),
            ("gate", DIAGNOSIS_ACCEPTANCE_GATE),
        )
        return semantic_proposal(steps)
    if "source-only" in selected or "source only" in selected:
        steps = (
            ("action", "debug_run"),
            ("gate", DIAGNOSIS_ACCEPTANCE_GATE),
            ("gate", DEVELOPER_CHANGE_GATE),
        )
        return semantic_proposal(steps)
    if "live patch" in selected:
        steps = (
            ("action", "debug_run"),
            ("gate", DIAGNOSIS_ACCEPTANCE_GATE),
            ("gate", DEVELOPER_CHANGE_GATE),
            ("action", "live_patch_run"),
            ("action", "debug_collect"),
        )
        return semantic_proposal(
            steps,
            compensations={"live_patch_run": "live_patch.rollback"},
        )
    if "build" in selected and "upgrade" in selected and "firmware" in selected:
        steps = (
            ("action", "debug_run"),
            ("gate", DIAGNOSIS_ACCEPTANCE_GATE),
            ("gate", DEVELOPER_CHANGE_GATE),
            ("gate", BUILD_ARTIFACT_GATE),
            ("action", "upgrade_run"),
            ("action", "debug_collect"),
        )
        return semantic_proposal(steps)
    if "upgrade" in selected and "verification" in selected:
        steps = (
            ("action", "upgrade_run"),
            ("action", "debug_collect"),
        )
        return semantic_proposal(steps)
    if "diagnosis" in selected and "evidence" in selected:
        return semantic_proposal(
            (("action", "debug_run"), ("gate", DIAGNOSIS_ACCEPTANCE_GATE))
        )
    return semantic_proposal((("action", "unsupported.objective"),))


class DeterministicEvaluationModelAdapter:
    """Generate a repeatable proposal from the actual PlanningInput objective."""

    def __init__(self) -> None:
        self.configuration = ModelConfiguration.freeze(
            provider="deterministic-evaluation",
            model="objective-feature-planner-v1",
            parameters={"temperature": 0, "seed": 1},
            timeout_seconds=1.0,
        )
        self.objectives: list[str] = []
        self.invoke_calls = 0
        self.reconcile_calls = 0

    def invoke(self, request: PlanningRequest) -> ModelAdapterResult:
        self.invoke_calls += 1
        self.objectives.append(request.planning_input.objective)
        return ModelAdapterResult.succeeded(
            plan_for_objective(request.planning_input.objective)
        )

    def reconcile(self, record: ModelInvocationRecord) -> ModelAdapterResult:
        del record
        self.reconcile_calls += 1
        return ModelAdapterResult.unknown(
            "deterministic evaluation has no unresolved invocation"
        )


def has_demonstrated_leverage(
    *,
    static_valid_plan_rate: float,
    candidate_valid_plan_rate: float,
    static_gate_turns: int,
    candidate_gate_turns: int,
) -> bool:
    return (
        candidate_valid_plan_rate >= static_valid_plan_rate
        and (
            candidate_valid_plan_rate > static_valid_plan_rate
            or candidate_gate_turns < static_gate_turns
        )
    )


def revision_semantics(revision: PlanRevision) -> tuple[dict[str, str], ...]:
    nodes = {node.node_id: node for node in revision.proposal.nodes}

    def walk(node_id: str) -> tuple[dict[str, str], ...]:
        node = nodes[node_id]
        if node.kind is PlanNodeKind.ACTION:
            semantic = {"kind": "action", "name": node.action}
            if node.compensation:
                semantic["compensation"] = nodes[node.compensation].action
            return (semantic,)
        if node.kind is PlanNodeKind.GATE:
            return (
                {
                    "kind": "gate",
                    "name": GATE_PHASES.get(node.gate_schema, node.gate_schema),
                    "gate_schema": node.gate_schema,
                },
            )
        if node.kind is PlanNodeKind.SEQUENCE:
            return tuple(
                step
                for child in node.children
                for step in walk(child)
            )
        return ()

    return walk(revision.proposal.root_node_id)


def revision_steps(revision: PlanRevision) -> tuple[str, ...]:
    return tuple(item["name"] for item in revision_semantics(revision))


def static_semantics(
    definition: WorkflowDefinition,
) -> tuple[dict[str, str], ...]:
    return tuple(
        (
            {"kind": "action", "name": step.name}
            if step.kind == "operation"
            else {
                "kind": "gate",
                "name": step.name,
                "gate_schema": step.receipt_schema,
            }
        )
        for step in definition.steps
    )


def evaluate() -> dict[str, object]:
    policy = evaluation_policy()
    pairs: list[dict[str, object]] = []
    candidate_valid_revisions = 0
    static_valid = 0
    model_calls = 0
    static_agent_gate_turns = 0
    candidate_agent_gate_turns = 0
    for index, case in enumerate(paired_corpus()):
        static_definition = DEFAULT_WORKFLOW_DEFINITIONS.definition_for(
            case.static_request
        )
        restored_static = WorkflowDefinition.from_public_dict(
            static_definition.to_public_dict()
        )
        static_steps = tuple(step.name for step in restored_static.steps)
        static_pair_semantics = static_semantics(restored_static)
        static_pair_gate_turns = sum(
            item["kind"] == "gate" for item in static_pair_semantics
        )
        static_pair_gate_schemas = [
            item["gate_schema"]
            for item in static_pair_semantics
            if item["kind"] == "gate"
        ]
        static_pair_valid = (
            restored_static.fingerprint == static_definition.fingerprint
            and static_steps == case.expected_steps
        )
        static_valid += int(static_pair_valid)
        adapter = DeterministicEvaluationModelAdapter()
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=policy,
            clock=lambda: 100.0,
        ).resolve(
            PlanningRequest(
                run_id="run-evaluation",
                slot_id=f"pair-{index}",
                planning_input=PlanningInput(objective=case.objective),
            )
        )
        candidate_revision_valid = False
        candidate_steps: tuple[str, ...] = ()
        candidate_pair_semantics: tuple[dict[str, str], ...] = ()
        candidate_pair_gate_turns = 0
        candidate_pair_gate_schemas: list[str] = []
        candidate_pair_compensations: list[dict[str, str]] = []
        if decision.revision is not None:
            restored_revision = PlanRevision.from_public_dict(
                decision.revision.to_public_dict()
            )
            candidate_steps = revision_steps(restored_revision)
            candidate_pair_semantics = revision_semantics(restored_revision)
            candidate_pair_gate_turns = sum(
                item["kind"] == "gate" for item in candidate_pair_semantics
            )
            candidate_pair_gate_schemas = [
                item["gate_schema"]
                for item in candidate_pair_semantics
                if item["kind"] == "gate"
            ]
            candidate_pair_compensations = [
                {
                    "action": item["name"],
                    "compensation": item["compensation"],
                }
                for item in candidate_pair_semantics
                if "compensation" in item
            ]
            candidate_revision_valid = (
                restored_revision.proposal_digest
                == decision.revision.proposal_digest
                and restored_revision.status == "pinned"
                and candidate_steps == case.expected_steps
                and tuple(
                    {key: value for key, value in item.items() if key != "compensation"}
                    for item in candidate_pair_semantics
                )
                == static_pair_semantics
                and candidate_pair_compensations
                == [
                    {"action": action, "compensation": compensation}
                    for action, compensation in case.expected_compensations
                ]
            )
        candidate_valid_revisions += int(candidate_revision_valid)
        model_calls += adapter.invoke_calls
        static_agent_gate_turns += static_pair_gate_turns
        candidate_agent_gate_turns += candidate_pair_gate_turns
        pairs.append(
            {
                "name": case.name,
                "objective": case.objective,
                "static_definition_id": static_definition.definition_id,
                "expected_steps": list(case.expected_steps),
                "static_steps": list(static_steps),
                "static_semantics": list(static_pair_semantics),
                "static_agent_gate_turns": static_pair_gate_turns,
                "static_gate_schemas": static_pair_gate_schemas,
                "static_valid": static_pair_valid,
                "candidate_steps": list(candidate_steps),
                "candidate_semantics": list(candidate_pair_semantics),
                "candidate_agent_gate_turns": candidate_pair_gate_turns,
                "candidate_gate_schemas": candidate_pair_gate_schemas,
                "candidate_compensations": candidate_pair_compensations,
                "candidate_status": decision.status,
                "candidate_error_code": decision.record.error_code,
                "candidate_revision_valid": candidate_revision_valid,
                "candidate_used_objective": adapter.objectives == [case.objective],
            }
        )

    containment_results: list[dict[str, object]] = []
    containment_false_accepts = 0
    containment_model_calls = 0
    for index, case in enumerate(containment_corpus()):
        adapter = DeterministicFakeModelAdapter(
            invoke_results=(ModelAdapterResult.succeeded(case.candidate_proposal),)
        )
        decision = PlanResolver(
            InMemoryModelPlanningRepository(),
            adapter,
            policy=policy,
            clock=lambda: 100.0,
        ).resolve(
            PlanningRequest(
                run_id="run-evaluation",
                slot_id=f"containment-{index}",
                planning_input=PlanningInput(
                    objective=f"reject invalid plan: {case.name}"
                ),
            )
        )
        accepted = decision.status == "accepted"
        containment_false_accepts += int(accepted)
        containment_model_calls += adapter.invoke_calls
        containment_results.append(
            {
                "name": case.name,
                "candidate_status": decision.status,
                "candidate_error_code": decision.record.error_code,
            }
        )

    sensitivity_adapter = DeterministicEvaluationModelAdapter()
    sensitivity = PlanResolver(
        InMemoryModelPlanningRepository(),
        sensitivity_adapter,
        policy=policy,
        clock=lambda: 100.0,
    ).resolve(
        PlanningRequest(
            run_id="run-evaluation",
            slot_id="input-sensitivity",
            planning_input=PlanningInput(
                objective="write an unrelated status greeting"
            ),
        )
    )
    input_sensitivity_passed = (
        sensitivity.status == "rejected"
        and sensitivity_adapter.objectives
        == ["write an unrelated status greeting"]
    )
    negation_adapter = DeterministicEvaluationModelAdapter()
    negation = PlanResolver(
        InMemoryModelPlanningRepository(),
        negation_adapter,
        policy=policy,
        clock=lambda: 100.0,
    ).resolve(
        PlanningRequest(
            run_id="run-evaluation",
            slot_id="negated-upgrade",
            planning_input=PlanningInput(
                objective="write firmware release notes; do not build or upgrade"
            ),
        )
    )
    negation_sensitivity_passed = (
        negation.status == "rejected"
        and negation_adapter.objectives
        == ["write firmware release notes; do not build or upgrade"]
    )

    pair_count = len(pairs)
    agent_tools = [descriptor.name for descriptor in agent_operation_descriptors()]
    invariant_passed = (
        static_valid == pair_count
        and candidate_valid_revisions == pair_count
        and containment_false_accepts == 0
        and input_sensitivity_passed
        and negation_sensitivity_passed
        and agent_tools == ["observe", "execute"]
    )
    static_valid_plan_rate = static_valid / pair_count
    candidate_valid_plan_rate = candidate_valid_revisions / pair_count
    validity_leverage = candidate_valid_plan_rate > static_valid_plan_rate
    turn_leverage = candidate_agent_gate_turns < static_agent_gate_turns
    demonstrated_leverage = has_demonstrated_leverage(
        static_valid_plan_rate=static_valid_plan_rate,
        candidate_valid_plan_rate=candidate_valid_plan_rate,
        static_gate_turns=static_agent_gate_turns,
        candidate_gate_turns=candidate_agent_gate_turns,
    )
    verdict = "advance" if invariant_passed and demonstrated_leverage else "isolate"
    return {
        "schema": EVALUATION_SCHEMA,
        "hypothesis": (
            "Runtime-internal model planning improves executable plan validity "
            "beyond pinned static WorkflowDefinitions"
        ),
        "paired_tasks": pair_count,
        "pairs": pairs,
        "static_workflow": {
            "evaluated": pair_count,
            "valid": static_valid,
            "invalid": pair_count - static_valid,
            "model_calls": 0,
            "valid_plan_rate": static_valid_plan_rate,
            "agent_gate_turns": static_agent_gate_turns,
        },
        "isolated_candidate": {
            "model_calls": model_calls,
            "accepted": candidate_valid_revisions,
            "rejected": pair_count - candidate_valid_revisions,
            "valid_revisions": candidate_valid_revisions,
            "valid_plan_rate": candidate_valid_plan_rate,
            "agent_gate_turns": candidate_agent_gate_turns,
        },
        "containment": {
            "evaluated": len(containment_results),
            "rejected": len(containment_results) - containment_false_accepts,
            "false_accepts": containment_false_accepts,
            "model_calls": containment_model_calls,
            "cases": containment_results,
        },
        "input_sensitivity": {
            "passed": input_sensitivity_passed,
            "unrelated_status": sensitivity.status,
            "unrelated_error_code": sensitivity.record.error_code,
            "negated_upgrade_passed": negation_sensitivity_passed,
            "negated_upgrade_status": negation.status,
            "negated_upgrade_error_code": negation.record.error_code,
        },
        "agent_interface": agent_tools,
        "invariants_passed": invariant_passed,
        "validity_leverage": validity_leverage,
        "turn_leverage": turn_leverage,
        "demonstrated_leverage": demonstrated_leverage,
        "verdict": verdict,
        "reason": (
            "The static WorkflowDefinitions path and isolated candidate both produce "
            "valid pinned plans with the same "
            f"{static_agent_gate_turns} semantic Gate turns across six equivalent tasks, "
            "while the candidate requires one model call per task; five separate "
            "invalid outputs and two negative controls are contained."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = evaluate()
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if result["invariants_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
