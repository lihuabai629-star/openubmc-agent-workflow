"""Pure classification of exceptional Agent Interface interactions."""

from __future__ import annotations

from collections.abc import Mapping


def interaction_telemetry(
    document: Mapping[str, object],
    *,
    preflight_failure: bool = False,
) -> dict[str, object] | None:
    progress = document.get("progress")
    no_progress_retry = (
        isinstance(progress, Mapping) and progress.get("status") == "no_progress"
    )
    incident_manual_action_required = document.get("state") == "incident"
    projection_target_exceeded = (
        document.get("projection_target_exceeded") is True
        or document.get("gate_projection_target_exceeded") is True
    )
    budget_blocker = document.get("budget_blocker") is True
    if preflight_failure:
        classification = "preflight_failure"
    elif no_progress_retry:
        classification = "no_progress_retry"
    elif incident_manual_action_required:
        classification = "manual_action_required"
    elif budget_blocker:
        classification = "budget_blocker"
    elif projection_target_exceeded:
        classification = "projection_target_exceeded"
    else:
        return None
    return {
        "classification": classification,
        "preflight_failure": preflight_failure,
        "no_progress_retry": no_progress_retry,
        "manual_action_required": incident_manual_action_required,
        "projection_target_exceeded": projection_target_exceeded,
        "budget_blocker": budget_blocker,
    }
