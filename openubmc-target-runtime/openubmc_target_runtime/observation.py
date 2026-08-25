"""Runtime-owned qualification of one bounded Observation attempt."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .capabilities import CAPABILITY_ALIASES
from .semantic_runtime import ObservationQuery


OBSERVATION_TIMING_FIELD = "observation_timing"
OBSERVATION_MAX_SELECTOR_SKEW_SECONDS = 5.0
_SELECTOR_STATUSES = frozenset({"observed", "missing", "stale"})
def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _instant(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def capability_selector_complete(
    capabilities: Mapping[str, object],
    names: tuple[str, ...] | list[str],
) -> bool:
    return all(
        CAPABILITY_ALIASES.get(name) in capabilities
        and (
            name != "alarms"
            or capabilities.get(CAPABILITY_ALIASES[name]) is True
        )
        for name in names
    )


def selected_scope_complete(
    raw: Mapping[str, object], query: ObservationQuery
) -> bool:
    result = _mapping(raw.get("result"))
    capabilities = _mapping(result.get("capabilities"))
    lanes = _mapping(result.get("lanes"))
    ssh = _mapping(lanes.get("ssh"))
    mdb_index = 0
    for selector in query.selectors:
        if selector.kind == "capability":
            if not capability_selector_complete(capabilities, selector.names):
                return False
            continue
        for _query in selector.queries:
            name = "mdbctl" if mdb_index == 0 else f"mdbctl_{mdb_index + 1}"
            mdb_index += 1
            child = _mapping(ssh.get(name))
            if not child or child.get("ok") is not True:
                return False
    return True


def qualify_observation(
    raw: Mapping[str, object],
    query: ObservationQuery,
    *,
    scope_complete: bool,
) -> dict[str, object]:
    """Return a copy with authoritative selector timing and consistency facts."""

    qualified = dict(raw)
    supplied = _mapping(raw.get(OBSERVATION_TIMING_FIELD))
    raw_selectors = supplied.get("selectors")
    expected = [(selector.selector_id, selector.kind) for selector in query.selectors]
    selector_facts: list[dict[str, object]] = []
    if isinstance(raw_selectors, list):
        actual = [
            (_text(_mapping(item).get("selector_id")), _text(_mapping(item).get("kind")))
            for item in raw_selectors
        ]
        if actual != expected:
            raise ValueError(
                "observation selector timing must match the declared selector order"
            )
        for item in raw_selectors:
            fact = _mapping(item)
            status = _text(fact.get("status"))
            if status not in _SELECTOR_STATUSES:
                status = "missing"
            selector_facts.append(
                {
                    "selector_id": _text(fact.get("selector_id")),
                    "kind": _text(fact.get("kind")),
                    "started_at": _text(fact.get("started_at")),
                    "completed_at": _text(fact.get("completed_at")),
                    "status": status,
                }
            )
    else:
        selector_facts = [
            {
                "selector_id": selector.selector_id,
                "kind": selector.kind,
                "started_at": "",
                "completed_at": "",
                "status": "missing",
            }
            for selector in query.selectors
        ]

    selector_starts = [_instant(fact["started_at"]) for fact in selector_facts]
    selector_completions = [_instant(fact["completed_at"]) for fact in selector_facts]
    valid_starts = [value for value in selector_starts if value is not None]
    valid_completions = [value for value in selector_completions if value is not None]
    supplied_start_text = _text(supplied.get("started_at"))
    supplied_complete_text = _text(supplied.get("completed_at"))
    attempt_start = _instant(supplied_start_text)
    attempt_complete = _instant(supplied_complete_text)
    if attempt_start is None and valid_starts:
        attempt_start = min(valid_starts)
        supplied_start_text = min(
            (fact["started_at"] for fact in selector_facts if _instant(fact["started_at"])),
            key=lambda value: _instant(value),
        )
    if attempt_complete is None and valid_completions:
        attempt_complete = max(valid_completions)
        supplied_complete_text = max(
            (
                fact["completed_at"]
                for fact in selector_facts
                if _instant(fact["completed_at"])
            ),
            key=lambda value: _instant(value),
        )

    gaps: list[str] = []
    for index, fact in enumerate(selector_facts):
        started = selector_starts[index]
        completed = selector_completions[index]
        if fact["status"] == "observed" and (
            started is None or completed is None or completed < started
        ):
            fact["status"] = "missing"
        if fact["status"] == "observed" and (
            attempt_start is None
            or attempt_complete is None
            or started < attempt_start
            or completed > attempt_complete
        ):
            fact["status"] = "stale"
        if fact["status"] != "observed":
            gaps.append(
                f"selector {fact['selector_id']} timing is {fact['status']}"
            )

    observed_completions = [
        _instant(fact["completed_at"])
        for fact in selector_facts
        if fact["status"] == "observed"
    ]
    observed_completions = [
        value for value in observed_completions if value is not None
    ]
    skew_seconds = (
        (max(observed_completions) - min(observed_completions)).total_seconds()
        if len(observed_completions) > 1
        else 0.0
    )
    over_skew = skew_seconds > OBSERVATION_MAX_SELECTOR_SKEW_SECONDS
    if over_skew:
        gaps.append(
            "selector completion skew exceeds the Runtime consistency window"
        )
    if not scope_complete:
        gaps.append("one or more selected facts were not observed")
    if attempt_start is None or attempt_complete is None or attempt_complete < attempt_start:
        gaps.append("observation attempt window is incomplete")

    if over_skew:
        classification = "inconsistent"
    elif gaps:
        classification = "partial"
    else:
        classification = "coherent"
    consistency = {
        "started_at": supplied_start_text,
        "completed_at": supplied_complete_text,
        "selectors": selector_facts,
        "classification": classification,
        "max_skew_seconds": OBSERVATION_MAX_SELECTOR_SKEW_SECONDS,
        "observed_skew_seconds": skew_seconds,
        "reusable": classification == "coherent",
        "gaps": list(dict.fromkeys(gaps))[:16],
    }
    qualified[OBSERVATION_TIMING_FIELD] = consistency
    return qualified


def observation_reusable(raw: Mapping[str, object]) -> bool:
    timing = _mapping(raw.get(OBSERVATION_TIMING_FIELD))
    return timing.get("classification") == "coherent" and timing.get("reusable") is True


def observation_consistency(raw: Mapping[str, object]) -> dict[str, object]:
    timing = _mapping(raw.get(OBSERVATION_TIMING_FIELD))
    return dict(timing)


def observation_improves(
    candidate: Mapping[str, object],
    baseline: Mapping[str, object],
) -> bool:
    candidate_timing = _mapping(candidate.get(OBSERVATION_TIMING_FIELD))
    baseline_timing = _mapping(baseline.get(OBSERVATION_TIMING_FIELD))
    ranks = {"inconsistent": 0, "partial": 1, "coherent": 2}
    candidate_selectors = candidate_timing.get("selectors", [])
    baseline_selectors = baseline_timing.get("selectors", [])
    candidate_observed = sum(
        _mapping(item).get("status") == "observed"
        for item in (
            candidate_selectors if isinstance(candidate_selectors, list) else []
        )
    )
    baseline_observed = sum(
        _mapping(item).get("status") == "observed"
        for item in (
            baseline_selectors if isinstance(baseline_selectors, list) else []
        )
    )
    candidate_rank = ranks.get(
        _text(candidate_timing.get("classification")), 0
    )
    baseline_rank = ranks.get(
        _text(baseline_timing.get("classification")), 0
    )
    return candidate_rank > baseline_rank or (
        candidate_rank == baseline_rank
        and candidate_observed > baseline_observed
    )
