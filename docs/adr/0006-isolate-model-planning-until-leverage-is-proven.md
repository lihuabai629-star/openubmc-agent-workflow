# ADR-0006: Isolate model planning until leverage is proven

- Status: Proposed
- Date: 2026-08-24
- Decision owners: openUBMC Agent Workflow maintainers

## Context

The stable Runtime supports deterministic, version-pinned workflows and keeps model calls outside
Runtime authority. Issue #65 tested whether a Runtime-managed model planning Effect and bounded
`PlanProposal -> PlanRevision` path could improve planning without widening the Agent Interface or
creating a second state authority.

The prototype demonstrates safe identity binding, unknown reconciliation, bounded validation, and
restart reuse. Its deterministic paired evaluation does not demonstrate fewer Agent turns or
better validity than pinned static `WorkflowDefinitions`; it adds model calls while preserving the
same estimated turns.

## Proposed decision

Keep the prototype isolated behind one internal `PlanResolver.resolve()` Interface. Do not compose
it into `RunEngine` or expose planning vocabulary to Agents until a real-task experiment proves
measurable leverage.

If later adopted, preserve these constraints:

- `RunEngine` remains the sole Run, Gate, Incident, Effect-reference, and Outcome transition
  authority;
- model output remains a proposal and cannot authorize or execute effects;
- the invocation identity is stable across retry and restart, while changed input conflicts;
- accepted revisions are immutable, version-pinned, bounded, and replayable without the model;
- the Agent Interface remains `observe` and `execute`;
- static `WorkflowDefinitions` remain the fallback;
- execution remains local and single-process until a real deployment seam proves otherwise.

## Consequences

- The prototype can continue to test Provider and IR behavior without affecting production Runs.
- There is no production latency, provider-cost, or availability regression.
- A future adoption proposal must bring real A/B evidence rather than architectural enthusiasm.
- The bounded IR and record readers can be removed cheaply while they remain uncomposed.

## Rejected alternatives

- Add a model-planning Agent tool or policy mode.
- Let a model write Run facts, answer Gates, invoke Domain Adapters, or declare Outcomes.
- Replace static workflows before demonstrating leverage.
- Add Broker, Outbox/Inbox, remote Workers, a worker fleet, or distributed scheduling for the
  single-process experiment.

## Evidence and references

- [Runtime-internal model planning prototype](../model-planning-prototype.md)
- [Evolution roadmap](../workflow-evolution-roadmap.md)
- [ADR-0001](0001-runtime-core-and-semantic-agent-interface.md)
- [ADR-0002](0002-single-run-authority-and-effect-recovery.md)
- [Issue #65](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/65)
