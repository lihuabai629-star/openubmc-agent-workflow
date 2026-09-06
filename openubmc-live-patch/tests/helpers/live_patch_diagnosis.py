"""Public diagnosis acceptance for Live Patch recovery fixtures."""
from collections.abc import Mapping

from openubmc_target_runtime import RuntimeMcpService


def accept_diagnosis(
    service: RuntimeMcpService,
    turn: Mapping[str, object],
    *,
    task_id: str,
    operation_id: str,
) -> dict[str, object]:
    """Advance the public workflow through its typed diagnosis Gate."""
    gate = turn.get("gate")
    assert isinstance(gate, Mapping) and gate.get("name") == "diagnosis.acceptance", turn
    receipt = turn.get("diagnostic_receipt")
    evidence = receipt.get("evidence", []) if isinstance(receipt, Mapping) else []
    evidence_ids = [
        item.get("evidence_id")
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
    ]
    developer = service.call_exposed_tool(
        "execute",
        {
            "kind": "respond",
            "run_id": turn["run_id"],
            **{name: gate[name] for name in ("gate_id", "gate_version", "schema_digest")},
            "response": {
                "status": "completed",
                "summary": "diagnosis accepted",
                "payload": {
                    "root_cause": "the bounded live-patch fault was isolated",
                    "evidence_ids": evidence_ids,
                    "causal_chain": [
                        "the target state was inconsistent",
                        "the live-patch fault was isolated",
                    ],
                    "code_owner": "openubmc-live-patch",
                    "contradictions": [],
                    "remaining_gaps": [],
                    "verification_status": "verified",
                },
            },
        },
        task_id=task_id,
        operation_id=operation_id,
    )

    assert developer["gate"]["name"] == "developer.change", developer
    return developer
