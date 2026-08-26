# ADR-0007: Treat Agent display budgets as soft projection targets

- Status: Accepted
- Date: 2026-08-26
- Decision owners: openUBMC Agent Workflow maintainers
- Supersedes in part: the bounded Agent-projection wording in
  [ADR-0003](0003-turn-gate-artifact-and-distribution-boundaries.md)

## Context

ADR-0003 required bounded Agent projections so raw Evidence, logs, observations, and build
artifacts could not grow Run history or model context without limit. The first two-operation
Runtime implementation treated the 8 KiB Turn target as a hard control-flow boundary. When a
complete diagnostic result did not fit, projection pressure could remove evaluable evidence or
replace valid Runtime semantics with an output-budget blocker.

That behavior confused two responsibilities: the Runtime Core must preserve durable Gate,
Incident, Outcome, and diagnostic-completion truth, while the Agent Gateway should make that truth
economical to display. A fixed display target cannot safely override the authoritative result it
is projecting.

## Decision

Keep 4 KiB as the target size for an Agent-visible `ObservationReceipt` and Gate schema, and 8 KiB
as the target size for an Agent-visible Turn. None is a control-flow maximum. `AgentGateway`
compacts observation values, facts, and diagnostic previews first. If authoritative Observation,
Gate, Incident, Outcome, or `DiagnosticReceipt` semantics still do not fit, it returns the larger
projection and records projection telemetry rather than changing Runtime completion, removing a
reusable `ObservationRef`, or creating a budget blocker.

The following remain hard anti-runaway bounds:

- each MCP Agent request is at most 256 KiB;
- one `execute` call advances at most 64 internal steps;
- each durable `DiagnosticReceipt` is at most 32 KiB and an accepted diagnostic scope requires at
  most 1,024 result identities;
- raw Evidence, logs, observations, patches, and build artifacts remain outside Run state and are
  referenced by stable handles.

`AgentGateway` remains the sole decision point for final Agent projection. Runtime value objects
may provide pure bounded transformations of their own data, but those helpers do not select when a
Turn is projected, persist a second projection, or own Gate, Incident, Outcome, or diagnostic
completion semantics.

## Consequences

- Projection pressure cannot rewrite a complete Runtime source result; Agent acceptance still
  fails closed when durable compaction cannot retain complete visible evaluability.
- Ordinary ObservationReceipts and Gate schemas still target 4 KiB, and Turns target 8 KiB; all
  report compaction or target-exceeded telemetry separately from workflow state.
- A rare oversized semantic Turn is visible and measurable instead of becoming a retry loop that
  asks the Agent to guess a narrower diagnostic scope.
- Large source bytes remain externalized; this decision does not permit raw Evidence or logs in
  event history.
- Capacity qualification prioritizes substantive workflow completion and correctness before token
  or byte reduction.

## Rejected alternatives

- Restore an 8 KiB hard failure or output-budget blocker.
- Restore a 4 KiB ObservationReceipt or Gate-schema completeness/control-flow boundary.
- Drop Gate, Incident, Outcome, or diagnostic completion fields until the Turn fits.
- Add a third Agent-facing Evidence-read tool to recover information removed by projection.
- Remove the request, internal-step, durable-receipt, or ArtifactRef boundaries.

## Evidence and references

- [Issue #75](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/75)
- [Agent Semantic Gateway](../agent-semantic-gateway.md)
- [Execute DiagnosticReceipt qualification](../qualification/execute-diagnostic-receipt-20260825.md)
- [ADR-0001](0001-runtime-core-and-semantic-agent-interface.md)
- [ADR-0003](0003-turn-gate-artifact-and-distribution-boundaries.md)
