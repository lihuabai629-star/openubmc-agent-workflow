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
    PlanResolver,
    PlanningInput,
    PlanningRequest,
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


def corpus() -> tuple[tuple[str, bool, Mapping[str, object]], ...]:
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
        ("valid-sequence", True, valid_sequence),
        ("valid-bounded-repeat", True, valid_bounded_repeat),
        ("unknown-action", False, unknown_action),
        ("invalid-reference", False, invalid_reference),
        ("unbounded-repeat", False, unbounded_repeat),
        ("terminal-claim", False, terminal_claim),
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
    model_calls = 0
    for index, (name, expected_valid, raw_proposal) in enumerate(corpus()):
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
        candidate_accepted += int(accepted)
        candidate_false_accepts += int(accepted and not expected_valid)
        candidate_false_rejects += int(not accepted and expected_valid)
        model_calls += adapter.invoke_calls
        pairs.append(
            {
                "name": name,
                "expected_valid": expected_valid,
                "candidate_status": decision.status,
                "candidate_error_code": decision.record.error_code,
            }
        )

    pair_count = len(pairs)
    static_agent_turns = pair_count * 2
    candidate_agent_turns = pair_count * 2
    agent_tools = [descriptor.name for descriptor in agent_operation_descriptors()]
    invariant_passed = (
        candidate_false_accepts == 0
        and candidate_false_rejects == 0
        and agent_tools == ["observe", "execute"]
    )
    demonstrated_leverage = candidate_agent_turns < static_agent_turns
    verdict = "advance" if invariant_passed and demonstrated_leverage else "isolate"
    return {
        "schema": EVALUATION_SCHEMA,
        "hypothesis": (
            "Runtime-internal model planning reduces Agent turns or improves "
            "validity beyond the static workflow path"
        ),
        "pairs": pairs,
        "static_workflow": {
            "agent_turns": static_agent_turns,
            "model_calls": 0,
            "valid_plan_rate": 1.0,
        },
        "isolated_candidate": {
            "agent_turns": candidate_agent_turns,
            "model_calls": model_calls,
            "accepted": candidate_accepted,
            "rejected": pair_count - candidate_accepted,
            "false_accepts": candidate_false_accepts,
            "false_rejects": candidate_false_rejects,
            "accepted_plan_validity": (
                1.0 if candidate_accepted and candidate_false_accepts == 0 else 0.0
            ),
        },
        "agent_interface": agent_tools,
        "invariants_passed": invariant_passed,
        "demonstrated_leverage": demonstrated_leverage,
        "verdict": verdict,
        "reason": (
            "The bounded validator contains invalid proposals and preserves the "
            "two-operation Agent Interface, but this isolated prototype does not "
            "reduce Agent turns or outperform pinned static WorkflowDefinitions."
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
