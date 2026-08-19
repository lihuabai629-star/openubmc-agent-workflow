# ADR-0002: Single Run authority and external Effect recovery

- Status: Accepted
- Date: 2026-08-19
- Decision owners: openUBMC Agent Workflow maintainers

## Context

The semantic Agent Interface is small, but the current internal implementation still spreads Run
progression across the Gateway, `ContextRuntime.workflow_advance`, workflow continuation wrappers,
and terminal governance writes. A second state machine layered around the existing logic would
create conflicting authorities and ambiguous recovery.

External target operations have a different failure model from deterministic workflow transitions.
A process can lose the result after a target Effect has started, so workflow replay cannot prove
that repeating the Effect is safe.

## Decision

Introduce `RunEngine` as the only Module that commits Run, Gate, Incident, Effect-reference, and
Outcome transitions. Rename the current definition-oriented `WorkflowKernel` to
`WorkflowDefinitions`; it supplies versioned deterministic structure but performs no I/O and writes
no state.

Register Domain Adapters with a `DomainExecutor`. The executor invokes a typed Domain command using
a stable Effect identity and returns a typed result. Mutation truth remains in
`MutationJournal`, including effect-start status, result, verification, unknown recovery, and
rollback state.

External Effects use at-least-once semantics. Safety comes from stable identity, fingerprinting,
idempotency where the target supports it, target epoch or fencing, fail-closed unknown state, and
read-first reconcile. The Runtime does not claim exactly-once external mutation.

Migration follows move-and-delete: compatibility Adapters translate old calls into the new seam,
then old state-writing paths are removed. No permanent wrapper state machine is added.

## Consequences

- Gateway-to-Runtime traffic is decoded immediately into typed Query, Command, Result, and Turn
  objects rather than passed through as unrestricted mappings.
- Session Outcome becomes a governance projection of terminal Run Outcome, not an independent
  terminal write.
- Workflow definitions are pinned per Run and require compatible readers or upcasters for old
  records.
- Tests move to the `RunEngine` Interface and observable outcomes; source-string and function-body
  assertions are retired as behaviour coverage becomes available.
- A possible Effect start followed by an exception produces `unknown`, never success or an
  automatic replacement operation.
- Rollback remains explicit and authorized because many firmware and hardware operations are not
  generally reversible.

## Rejected alternatives

- Keep Gateway, workflow wrappers, and ContextRuntime as peer Run-state writers.
- Add a new orchestration layer without deleting the old transition authority.
- Treat deterministic workflow replay as proof of exactly-once target mutation.
- Retry an unknown mutation with a new operation identity.
- Make full Event Sourcing or CQRS a prerequisite for the local Runtime.

## Evidence and references

- [Architecture arbitration](../workflow-architecture-arbitration.md)
- [Market workflow design research](../workflow-design-market-research.md)
- [Evolution roadmap](../workflow-evolution-roadmap.md)
