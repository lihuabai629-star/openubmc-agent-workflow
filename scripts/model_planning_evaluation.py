#!/usr/bin/env python3
"""Deterministically compare isolated model planning with the static workflow path."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
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
    PlanPolicy,
    PlanRevision,
    PlanResolver,
    PlanningInput,
    PlanningRequest,
)
from openubmc_target_runtime.workflow import (  # noqa: E402
    DEFAULT_WORKFLOW_REGISTRY,
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
    candidate_proposal: Mapping[str, object]
    static_request: Mapping[str, str]


@dataclass(frozen=True)
class ContainmentCase:
    name: str
    candidate_proposal: Mapping[str, object]


def paired_corpus() -> tuple[EvaluationCase, ...]:
    diagnosis = proposal(
        [{"node_id": "root", "kind": "action", "action": "inspect.target"}]
    )
    source_change = proposal(
        [
            {
                "node_id": "root",
                "kind": "sequence",
                "children": ["inspect", "approval", "verify"],
            },
            {"node_id": "inspect", "kind": "action", "action": "inspect.target"},
            {
                "node_id": "approval",
                "kind": "gate",
                "gate_schema": "upgrade-approval/v1",
            },
            {"node_id": "verify", "kind": "action", "action": "inspect.target"},
        ]
    )
    live_patch = proposal(
        [
            {
                "node_id": "root",
                "kind": "sequence",
                "children": ["inspect", "approval", "upgrade", "verify"],
            },
            {"node_id": "inspect", "kind": "action", "action": "inspect.target"},
            {
                "node_id": "approval",
                "kind": "gate",
                "gate_schema": "upgrade-approval/v1",
            },
            {
                "node_id": "upgrade",
                "kind": "action",
                "action": "upgrade.component",
            },
            {"node_id": "verify", "kind": "action", "action": "inspect.target"},
        ]
    )
    build_upgrade = proposal(
        [
            {
                "node_id": "root",
                "kind": "sequence",
                "children": ["diagnose", "approval", "upgrade", "verify"],
            },
            {
                "node_id": "diagnose",
                "kind": "subflow",
                "subflow": "diagnose",
                "subflow_version": "v1",
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
            },
            {"node_id": "verify", "kind": "action", "action": "inspect.target"},
        ]
    )
    upgrade_only = proposal(
        [
            {
                "node_id": "root",
                "kind": "repeat",
                "repeat_max": 2,
                "body": "inspect",
            },
            {"node_id": "inspect", "kind": "action", "action": "inspect.target"},
        ]
    )
    bundle_diagnosis = proposal(
        [
            {
                "node_id": "root",
                "kind": "parallel",
                "branches": ["inspect-a", "inspect-b"],
            },
            {"node_id": "inspect-a", "kind": "action", "action": "inspect.target"},
            {"node_id": "inspect-b", "kind": "action", "action": "inspect.target"},
        ]
    )
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
    )


def evaluation_policy() -> PlanPolicy:
    return PlanPolicy.freeze(
        allowed_actions={"inspect.target", "upgrade.component"},
        allowed_gate_schemas={"upgrade-approval/v1"},
        allowed_subflows={"diagnose": {"v1"}},
    )


def evaluate() -> dict[str, object]:
    policy = evaluation_policy()
    pairs: list[dict[str, object]] = []
    candidate_valid_revisions = 0
    static_valid = 0
    model_calls = 0
    for index, case in enumerate(paired_corpus()):
        static_definition = DEFAULT_WORKFLOW_REGISTRY.resolve(**case.static_request)
        restored_static = WorkflowDefinition.from_public_dict(
            static_definition.to_public_dict()
        )
        static_pair_valid = (
            restored_static.fingerprint == static_definition.fingerprint
            and bool(restored_static.steps)
        )
        static_valid += int(static_pair_valid)
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
                slot_id=f"pair-{index}",
                planning_input=PlanningInput(objective=case.objective),
            )
        )
        candidate_revision_valid = False
        if decision.revision is not None:
            restored_revision = PlanRevision.from_public_dict(
                decision.revision.to_public_dict()
            )
            candidate_revision_valid = (
                restored_revision.proposal_digest
                == decision.revision.proposal_digest
                and restored_revision.status == "pinned"
            )
        candidate_valid_revisions += int(candidate_revision_valid)
        model_calls += adapter.invoke_calls
        pairs.append(
            {
                "name": case.name,
                "objective": case.objective,
                "static_definition_id": static_definition.definition_id,
                "static_valid": static_pair_valid,
                "candidate_status": decision.status,
                "candidate_error_code": decision.record.error_code,
                "candidate_revision_valid": candidate_revision_valid,
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

    pair_count = len(pairs)
    agent_tools = [descriptor.name for descriptor in agent_operation_descriptors()]
    invariant_passed = (
        static_valid == pair_count
        and candidate_valid_revisions == pair_count
        and containment_false_accepts == 0
        and agent_tools == ["observe", "execute"]
    )
    static_valid_plan_rate = static_valid / pair_count
    candidate_valid_plan_rate = candidate_valid_revisions / pair_count
    demonstrated_leverage = candidate_valid_plan_rate > static_valid_plan_rate
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
        },
        "isolated_candidate": {
            "model_calls": model_calls,
            "accepted": candidate_valid_revisions,
            "rejected": pair_count - candidate_valid_revisions,
            "valid_revisions": candidate_valid_revisions,
            "valid_plan_rate": candidate_valid_plan_rate,
        },
        "containment": {
            "evaluated": len(containment_results),
            "rejected": len(containment_results) - containment_false_accepts,
            "false_accepts": containment_false_accepts,
            "model_calls": containment_model_calls,
            "cases": containment_results,
        },
        "agent_interface": agent_tools,
        "invariants_passed": invariant_passed,
        "demonstrated_leverage": demonstrated_leverage,
        "verdict": verdict,
        "reason": (
            "The static resolver and isolated candidate both produce valid pinned "
            "plans for all six equivalent tasks, while the candidate requires one "
            "model call per task; four separate invalid outputs are contained."
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
