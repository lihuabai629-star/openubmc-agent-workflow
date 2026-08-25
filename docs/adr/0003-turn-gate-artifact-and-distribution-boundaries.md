# ADR-0003: Turn, Gate, Artifact, and distribution boundaries

- Status: Accepted
- Date: 2026-08-19
- Decision owners: openUBMC Agent Workflow maintainers
- Superseded in part by: [ADR-0004](0004-developer-default-and-gate-submissions.md), which
  removes the one-time secret Gate token requirement for the internal developer Runtime.
- Superseded in part by: [ADR-0007](0007-soft-agent-projection-budget.md), which treats 8 KiB as a
  soft Turn projection target while preserving hard Gate, request, step, and durable-receipt bounds.

## Context

Agent-visible acknowledgements and polling add model turns without adding a new domain decision.
Unversioned Gate responses can be replayed against the wrong state. Embedding complete observation,
Evidence, log, or build payloads in commands and Run state makes transport and history growth
unbounded. Conversely, adopting queues, Outbox/Inbox, remote Workers, or an external durable engine
before there is a process seam would add state without solving a present dual-write problem.

## Decision

`execute(Action)` advances to and returns the next actionable `Turn`:

- a Gate requiring external input;
- an Incident requiring operator attention;
- a bounded running reattach point when the caller deadline is reached;
- a terminal Outcome.

Ordinary agents do not poll Run state through `observe`. Transport-level acknowledgement may exist
inside a future Adapter but is not an Agent domain concept.

Each Gate is a durable object with `gate_id`, `gate_version`, an opaque token, schema digest, and a
one-time submission identity. Duplicate submission is idempotent; stale, cross-Run, wrong-version,
or wrong-token submission fails closed.

Large or reusable content is stored outside Run state and passed as `ObservationRef` or
`ArtifactRef` containing a handle, digest, type, size, provenance, and policy metadata. Agent
projections carry only bounded summaries and references.

The v2 and v2.1 Runtime remains single-process with a local event-backed Run ledger and inline
dispatch. Outbox, Inbox, remote Workers, shared fencing, or an external durable backend become
mandatory only when their corresponding process or ownership seam actually exists.

## Distribution triggers

| Observed requirement | Required mechanism |
| --- | --- |
| Return a durable acknowledgement before work completes | Durable Outbox |
| Dispatch a WorkOrder to another process | Outbox plus Worker Inbox |
| Commit results delivered repeatedly across a process seam | Inbox plus idempotent result commit |
| Run mutation Workers on another host | Shared durable state plus monotonic fencing |
| Operate more than one active Runtime writer | External durable backend and explicit leadership/fencing |

## Consequences

- Performance is measured as time and tokens to the next actionable Turn, not time to an
  acknowledgement.
- Caller disconnect does not imply that a target Effect did not run. Retry or resume reattaches by
  stable command and Run identity.
- Observation, Evidence, logs, patches, and build products share content-addressed identity and
  lifecycle policy, while their bytes stay out of event history.
- Artifact retention, access control, redaction, and garbage collection become explicit Runtime
  capabilities.
- Temporal or another durable engine can later implement an internal Adapter only if deployment
  evidence justifies it; the Agent Interface and domain language remain unchanged.

## Rejected alternatives

- Return only `CommandAck` and require agents to poll with `observe` or a new status tool.
- Accept free-form Gate payloads without persistent identity and version binding.
- Inline complete ObservationReceipt, Evidence, logs, or build artifacts in every command and
  Turn.
- Introduce a broker, Worker fleet, Outbox/Inbox, or active-active Runtime without a real
  cross-process, availability, isolation, or capacity requirement.

## Evidence and references

- [Architecture arbitration](../workflow-architecture-arbitration.md)
- [Market workflow design research](../workflow-design-market-research.md)
- [Evolution roadmap](../workflow-evolution-roadmap.md)
