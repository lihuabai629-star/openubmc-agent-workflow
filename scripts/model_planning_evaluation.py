#!/usr/bin/env python3
"""Deterministically compare isolated model planning with the static workflow path."""

from __future__ import annotations

import argparse
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


def proposal(nodes: list[dict[str, object]], *, root: str = "root") -> dict[str, object]:
    return {
        "schema": "openubmc.target-runtime.v1/plan-proposal-v1",
        "version": 1,
        "run_id": "run-evaluation",
        "root_node_id": root,
        "nodes": nodes,
    }


def corpus() -> tuple[
    tuple[str, bool, Mapping[str, object], Mapping[str, str]], ...
]:
    valid_sequence = proposal(
        [
            {
                "node_id": "root",
                "kind": "sequence",
                "children": ["inspect", "approval", "upgrade", "verify"],
            },
            {"node_id": "inspect", "kind": "action", "action": "inspect.target"},
            {"node_id": "approval", "kind": "gate", "gate_schema": "upgrade-approval/v1"},
            {"node_id": "upgrade", "kind": "action", "action": "upgrade.component"},
            {"node_id": "verify", "kind": "action", "action": "inspect.target"},
        ]
    )
    valid_bounded_repeat = proposal(
        [
            {"node_id": "root", "kind": "repeat", "repeat_max": 2, "body": "inspect"},
            {"node_id": "inspect", "kind": "action", "action": "inspect.target"},
        ]
    )
    unknown_action = proposal(
        [
            {"node_id": "root", "kind": "action", "action": "unknown.root-shell"},
        ]
    )
    invalid_reference = proposal(
        [
            {"node_id": "root", "kind": "sequence", "children": ["missing"]},
        ]
    )
    unbounded_repeat = proposal(
        [
            {"node_id": "root", "kind": "repeat", "repeat_max": 999, "body": "inspect"},
            {"node_id": "inspect", "kind": "action", "action": "inspect.target"},
        ]
    )
    terminal_claim = proposal(
        [
            {"node_id": "root", "kind": "outcome", "status": "success"},
        ]
    )
    return (
        ("valid-sequence", True, valid_sequence, {"intent": "diagnosis-only"}),
        (
            "valid-bounded-repeat",
            True,
            valid_bounded_repeat,
            {"intent": "diagnose-and-fix", "delivery_strategy": "source-only"},
        ),
        (
            "unknown-action",
            False,
            unknown_action,
            {"intent": "diagnose-and-fix", "delivery_strategy": "live-patch"},
        ),
        (
            "invalid-reference",
            False,
            invalid_reference,
            {"intent": "diagnose-and-fix", "delivery_strategy": "build-upgrade"},
        ),
        (
            "unbounded-repeat",
            False,
            unbounded_repeat,
            {"intent": "upgrade-and-verify", "entry_operation": "upgrade_run"},
        ),
        (
            "terminal-claim",
            False,
            terminal_claim,
            {"intent": "bundle-and-diagnose", "entry_operation": "log_bundle_collect"},
        ),
    )


def evaluate() -> dict[str, object]:
    policy = PlanPolicy.freeze(
        allowed_actions={"inspect.target", "upgrade.component"},
        allowed_gate_schemas={"upgrade-approval/v1"},
        allowed_subflows={"diagnose": {"v1"}},
    )
    pairs: list[dict[str, object]] = []
    candidate_false_accepts = 0
    candidate_false_rejects = 0
    candidate_accepted = 0
    candidate_valid_revisions = 0
    static_valid = 0
    model_calls = 0
    for index, (name, expected_valid, raw_proposal, static_request) in enumerate(
        corpus()
    ):
        static_definition = DEFAULT_WORKFLOW_REGISTRY.resolve(**static_request)
        restored_static = WorkflowDefinition.from_public_dict(
            static_definition.to_public_dict()
        )
        static_pair_valid = (
            restored_static.fingerprint == static_definition.fingerprint
            and bool(restored_static.steps)
        )
        static_valid += int(static_pair_valid)
        adapter = DeterministicFakeModelAdapter(
            invoke_results=(ModelAdapterResult.succeeded(raw_proposal),)
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
                planning_input=PlanningInput(objective=f"evaluate {name}"),
            )
        )
        accepted = decision.status == "accepted"
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
        candidate_accepted += int(accepted)
        candidate_valid_revisions += int(candidate_revision_valid)
        candidate_false_accepts += int(accepted and not expected_valid)
        candidate_false_rejects += int(not accepted and expected_valid)
        model_calls += adapter.invoke_calls
        pairs.append(
            {
                "name": name,
                "expected_valid": expected_valid,
                "static_definition_id": static_definition.definition_id,
                "static_valid": static_pair_valid,
                "candidate_status": decision.status,
                "candidate_error_code": decision.record.error_code,
                "candidate_revision_valid": candidate_revision_valid,
            }
        )

    pair_count = len(pairs)
    agent_tools = [descriptor.name for descriptor in agent_operation_descriptors()]
    invariant_passed = (
        static_valid == pair_count
        and candidate_false_accepts == 0
        and candidate_false_rejects == 0
        and candidate_valid_revisions == candidate_accepted
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
            "accepted": candidate_accepted,
            "rejected": pair_count - candidate_accepted,
            "valid_revisions": candidate_valid_revisions,
            "false_accepts": candidate_false_accepts,
            "false_rejects": candidate_false_rejects,
            "valid_plan_rate": candidate_valid_plan_rate,
            "accepted_plan_validity": (
                1.0 if candidate_accepted and candidate_false_accepts == 0 else 0.0
            ),
        },
        "agent_interface": agent_tools,
        "invariants_passed": invariant_passed,
        "demonstrated_leverage": demonstrated_leverage,
        "verdict": verdict,
        "reason": (
            "Both accepted candidate revisions round-trip as valid and all invalid "
            "model outputs are contained, but the executed static resolver produces "
            "a valid pinned definition for every paired case without a model call."
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
