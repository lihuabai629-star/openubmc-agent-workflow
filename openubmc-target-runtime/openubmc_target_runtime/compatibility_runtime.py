"""Translate legacy workflow operations onto the typed Runtime seam."""
from __future__ import annotations

from collections.abc import Mapping

from .context_runtime import CaseNotFound, ContextRuntime, ContextToolResult
from .semantic_runtime import (
    Gate,
    GateConflict,
    ResumeRun,
    SemanticRuntimePort,
    StartRun,
    SubmitGate,
)


class CompatibilityRuntimeAdapter:
    """Keep legacy workflow vocabulary outside the MCP transport Adapter."""

    def __init__(
        self,
        context_runtime: ContextRuntime,
        semantic_runtime: SemanticRuntimePort,
        *,
        interface_profile: str,
    ) -> None:
        self._context_runtime = context_runtime
        self._semantic_runtime = semantic_runtime
        self._interface_profile = interface_profile

    def translate(
        self,
        operation: str,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult | None:
        translators = {
            "phase_record": self._phase_record,
            "workflow.advance": self._workflow_advance,
            "workflow.next": self._workflow_next,
        }
        translator = translators.get(operation)
        if translator is None:
            return None
        return translator(
            arguments,
            task_id=task_id,
            operation_id=operation_id,
        )

    def _bound_case_id(
        self,
        arguments: Mapping[str, object],
        *,
        task_id: str,
    ) -> str:
        return str(arguments.get("case_id", "")).strip() or (
            self._context_runtime.repository.case_for_task(task_id) or ""
        )

    def _load_projection(self, case_id: str) -> Mapping[str, object]:
        projection = self._context_runtime.repository.load(case_id)
        if not isinstance(projection, Mapping):
            raise CaseNotFound(case_id)
        return projection

    def _phase_record(
        self,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult | None:
        case_id = str(arguments.get("case_id", "")).strip()
        if not case_id:
            return None
        projection = self._load_projection(case_id)
        if (
            not str(projection.get("start_command_id", ""))
            and self._interface_profile != "compatibility"
        ):
            return None
        raw_gate = projection.get("current_gate")
        if not isinstance(raw_gate, Mapping) or not raw_gate:
            self._semantic_runtime.execute(
                ResumeRun(
                    run_id=case_id,
                    command_id=f"{operation_id}-open-gate",
                ),
                task_id=task_id,
                operation_id=f"{operation_id}-open-gate",
            )
            projection = self._load_projection(case_id)
            raw_gate = projection.get("current_gate")
        if not isinstance(raw_gate, Mapping) or not raw_gate:
            raise GateConflict("Run is not waiting at a compatibility phase Gate")
        gate = Gate.from_public_dict(raw_gate)
        if str(arguments.get("phase_type", "")) != gate.name:
            raise GateConflict("phase_record targets a different Run Gate")
        payload = {
            key: value
            for key, value in arguments.items()
            if key
            not in {
                "case_id",
                "expected_revision",
                "idempotency_key",
                "phase_type",
                "producer_identity",
                "status",
                "summary",
                "gate_id",
                "gate_version",
                "gate_schema_digest",
                "submission_id",
                "submission_digest",
            }
        }
        submission_id = str(
            arguments.get("idempotency_key") or operation_id
        ).strip()
        turn = self._semantic_runtime.execute(
            SubmitGate(
                run_id=case_id,
                response={
                    "status": str(arguments.get("status", "completed")),
                    "summary": str(arguments.get("summary", "")),
                    "payload": payload,
                },
                gate_id=gate.gate_id,
                gate_version=gate.version,
                schema_digest=gate.schema_digest,
                submission_id=submission_id,
                command_id=submission_id,
            ),
            task_id=task_id,
            operation_id=operation_id,
        )
        updated = self._context_runtime.read_case(case_id)
        record = next(
            (
                item
                for item in reversed(updated.get("phase_records", []))
                if isinstance(item, Mapping)
                and str(item.get("submission_id", "")) == submission_id
            ),
            None,
        )
        if not isinstance(record, Mapping):
            raise RuntimeError("typed Runtime did not project the submitted phase")
        return self._context_runtime.wrap_read(
            {**dict(record), "run_turn": turn.to_public_dict()},
            operation="phase_record",
            operation_id=operation_id,
            case_id=case_id,
            status=str(record.get("status", "completed")),
        )

    def _workflow_advance(
        self,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult | None:
        case_id = self._bound_case_id(arguments, task_id=task_id)
        if case_id:
            projection = self._load_projection(case_id)
            command = ResumeRun(run_id=case_id, command_id=operation_id)
        else:
            target = str(arguments.get("ip", "")).strip()
            if not target:
                return None
            intent = str(arguments.get("intent", "diagnosis-only")).strip().lower()
            delivery = str(arguments.get("delivery_strategy", "")).strip().lower()
            if not delivery and intent == "diagnose-and-fix":
                delivery = "source-only"
            command = StartRun(
                target=target,
                intent=intent,
                purpose=str(
                    arguments.get(
                        "final_purpose",
                        "complete the requested workflow",
                    )
                ).strip(),
                delivery_strategy=delivery,
                command_id=operation_id,
                input_digest="",
            )
        turn = self._semantic_runtime.execute(
            command,
            task_id=task_id,
            operation_id=operation_id,
        )
        projection = self._context_runtime.read_case(turn.run_id)
        continuation = self._context_runtime.continuation_for(projection)
        value = {
            **turn.to_public_dict(),
            **continuation,
            "case_id": turn.run_id,
            "revision": int(projection.get("revision", 0)),
            "completed": turn.state == "completed",
        }
        if turn.state in {"completed", "failed", "cancelled"}:
            requested_bundle = bool(arguments.get("include_closeout_bundle", True))
            outcome = projection.get("run_outcome")
            terminal_status = (
                str(outcome.get("status", turn.state))
                if isinstance(outcome, Mapping)
                else turn.state
            )
            derived_closeout = self._context_runtime.derive_run_closeout(
                turn.run_id,
                terminal_status=terminal_status,
                include_bundle=requested_bundle,
            )
            value.update(derived_closeout)
            for name in ("closeout", "closeout_markdown"):
                if name in projection:
                    value[name] = projection[name]
        for name in ("closeout", "closeout_markdown", "closeout_bundle"):
            if name not in value and name in projection:
                value[name] = projection[name]
        return self._context_runtime.wrap_read(
            value,
            operation="workflow.advance",
            operation_id=operation_id,
            case_id=turn.run_id,
            status=turn.state,
        )

    def _workflow_next(
        self,
        arguments: Mapping[str, object],
        *,
        task_id: str,
        operation_id: str,
    ) -> ContextToolResult | None:
        case_id = self._bound_case_id(arguments, task_id=task_id)
        if not case_id:
            return None
        projection = self._load_projection(case_id)
        if (
            not str(projection.get("start_command_id", ""))
            and self._interface_profile != "compatibility"
        ):
            return None
        turn = self._semantic_runtime.execute(
            ResumeRun(run_id=case_id, command_id=operation_id),
            task_id=task_id,
            operation_id=operation_id,
        )
        return self._context_runtime.wrap_read(
            turn.to_public_dict(),
            operation="workflow.next",
            operation_id=operation_id,
            case_id=case_id,
            status=turn.state,
        )
