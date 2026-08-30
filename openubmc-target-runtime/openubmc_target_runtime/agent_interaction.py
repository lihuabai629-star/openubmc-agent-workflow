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
    manual_action_required = document.get("state") == "incident"
    if preflight_failure:
        classification = "preflight_failure"
    elif no_progress_retry:
        classification = "no_progress_retry"
    elif manual_action_required:
        classification = "manual_action_required"
    else:
        return None
    return {
        "classification": classification,
        "preflight_failure": preflight_failure,
        "no_progress_retry": no_progress_retry,
        "manual_action_required": manual_action_required,
    }
