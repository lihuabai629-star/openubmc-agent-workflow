"""Persisted native diagnosis fixtures for repository lifecycle tests."""

from openubmc_target_runtime import DiagnosisRecord, PendingCaseEvent


def persist_terminal_diagnosis(repository, case_id: str) -> None:
    """Complete an existing collection fixture without relying on legacy receipts."""
    projection = repository.load(case_id)
    assert projection is not None
    cycle_id = projection.get("workflow_cycle_id", "cycle-1")
    target_version = projection.get("target_version", 1)
    evidence_ids = tuple(item["evidence_id"] for item in projection.get("evidence_refs", []))
    assert evidence_ids, "terminal diagnosis fixture needs collected evidence"
    source_receipt_id = next(
        item["diagnostic_receipt"]["receipt_id"] for item in reversed(projection["operations"])
        if item.get("diagnostic_receipt", {}).get("receipt_id")
    )
    record = DiagnosisRecord(
        root_cause="the fixture component consumed an outdated state",
        evidence_ids=evidence_ids,
        causal_chain=("the fixture state was outdated", "the component consumed that state"),
        code_owner="fixture/component.lua", contradictions=(), remaining_gaps=(),
        verification_status="verified", record_id=f"diagnosis-{case_id}",
    )
    repository.commit(
        case_id, expected_revision=projection["revision"],
        events=(
            PendingCaseEvent("RunGateSubmitted", {"phase": {
                "phase_type": "diagnosis.acceptance", "status": "completed",
                "native_run_fact": True, "workflow_cycle_id": cycle_id,
                "target_version": target_version,
                "diagnosis_record": {
                    **record.to_public_dict(), "run_id": case_id,
                    "workflow_cycle_id": cycle_id, "target_version": target_version,
                    "source_receipt_id": source_receipt_id,
                },
            }}),
            PendingCaseEvent("RunOutcomeRecorded", {"outcome": {"status": "completed"}}),
        ),
    )
