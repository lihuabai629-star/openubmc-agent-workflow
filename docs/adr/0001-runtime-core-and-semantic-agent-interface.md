# ADR-0001: Runtime Core and semantic Agent Interface

- Status: Accepted
- Date: 2026-08-19
- Decision owners: openUBMC Agent Workflow maintainers
- Superseded in part by: [ADR-0005](0005-retire-compatibility-writers-and-profile.md), which removed
  the temporary compatibility profile after its evidence gate passed.

## Context

The first Runtime release exposed many implementation-shaped operations directly to agents. That
surface improved evidence completeness and auditability, but required models to learn Runtime
sequencing and increased token and wall-time cost. The semantic-gateway experiment reduced the
default surface to two operations and passed the formal paired Observation qualification.

The Runtime also contains durable assets that cannot safely be delegated to a model or a Skill:
target identity, Evidence provenance, Run recovery, mutation identity, Replay, and governance.

## Decision

Keep one durable Runtime Core and expose only two operations in the default Agent profile:

- `observe(Query)` for bounded, exact, read-only target observations;
- `execute(Action)` for starting or continuing a durable Run to its next semantic yield.

MCP and CLI remain transport Adapters at the same seam. The temporary compatibility profile has
been retired under ADR-0005; raw Evidence, Replay, lifecycle, Runtime status, and Session Outcome
governance remain in a disjoint Operator / CI Plane.

The product is an openUBMC safety and execution substrate. It is not a general-purpose agent graph,
BPMN engine, or universal DAG platform.

## Consequences

- New target facts normally become selectors behind `observe`, not top-level Agent tools.
- New delivery flows normally become typed intents and Gate schemas behind `execute`.
- Runtime sequencing terms such as revision, attempt, phase record, Evidence offset, worker lease,
  and queue token stay outside the Agent Interface.
- Skills remain the human- and model-facing reasoning layer. They may choose intent and provide Gate
  input, but do not own durable execution truth.
- Historical compatibility telemetry and old-event upcasters remain readable after the retired
  writers and profile are removed.

## Rejected alternatives

- Restore the original many-tool Agent-facing MCP surface.
- Replace the Runtime Core with Skills or model-managed state.
- Make a general-purpose workflow framework the product-facing domain model.
- Merge operator governance operations into the default Agent profile.

## Evidence and references

- [Agent Semantic Gateway](../agent-semantic-gateway.md)
- [Market workflow design research](../workflow-design-market-research.md)
- [Architecture arbitration](../workflow-architecture-arbitration.md)
- [Evolution roadmap](../workflow-evolution-roadmap.md)
