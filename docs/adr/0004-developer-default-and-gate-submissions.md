# ADR-0004: Developer-friendly default execution and Gate submissions

- Status: Accepted
- Date: 2026-08-19
- Decision owners: openUBMC Agent Workflow maintainers
- Supersedes: the one-time secret Gate token requirement in ADR-0003
- Superseded in part by: [ADR-0005](0005-retire-compatibility-writers-and-profile.md), which rejects
  the legacy assurance input after its evidence gate passed.

## Context

The Runtime is primarily used by openUBMC developers inside trusted engineering environments. Its
main correctness risks are duplicate or wrong-target mutation, wrong artifact deployment,
unrecoverable unknown effects, and false terminal success. Exposing multiple policy modes or
requiring a secret token for every expected development Gate would add Agent choices and failure
branches without addressing those risks.

The Runtime still needs durable submission identity because model and transport calls can be
repeated, delayed, or delivered after a process restart.

## Decision

Expose one developer-friendly default execution behavior. The Runtime automatically performs
read-only work, bounded read retries, deterministic progression, one read-first reconcile for an
unknown mutation, and fresh verification. It returns only a real external-input Gate, an Incident
that could not be resolved automatically, a running reattach point, or a terminal Outcome.

Do not expose `fast`, `assured`, `strict`, `safe`, or similar user-selectable execution policies.
The retired observation assurance input is rejected. Assurance remains automatic Runtime policy
and is not an Agent choice or returned control surface.

A Gate submission is bound by:

- `run_id`;
- `gate_id`;
- `gate_version`;
- Gate schema digest;
- `submission_id`;
- normalized submission input digest.

The Agent does not supply a one-time secret Gate token. If `submission_id` is omitted by a legacy
caller, the Adapter derives it from the persisted Run and Gate binding, independent of a transport
request ID. Actor and submission time are also derived by the Adapter or Runtime rather than
accepted as model-authored facts.

The same submission identity and input digest returns the current equivalent Turn without another
Gate transition. Reusing an identity with different input, a different Gate identity, or a stale
Gate version returns a conflict. A new submission cannot reopen a closed Gate.

## Consequences

- Ordinary development workflows have one behavior and one test matrix.
- Gate replay remains safe across retries and process restart without adding a credential
  lifecycle to the Agent protocol.
- Effect classes remain internal Runtime policy. They can select retry or reconcile behavior
  without becoming Agent-facing modes.
- Authorization outside a selected workflow, rollback, target identity conflicts, artifact
  mismatch, and unresolved mutation outcomes can still stop progression.
- A future untrusted or cross-tenant deployment may add authentication at the transport or
  operator boundary without changing Run, Gate, Submission, or Turn semantics.

## Rejected alternatives

- Expose separate developer-fast and strict execution modes.
- Require a one-time secret token for every internal developer Gate.
- Let the model author actor, timestamp, or idempotency facts.
- Retry an unknown mutation under a replacement Effect identity.
- Remove Gate versioning or input-digest conflict detection.

## Evidence and references

- [Issue #25 specification](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/25)
- [Agent Semantic Gateway](../agent-semantic-gateway.md)
- [Architecture arbitration](../workflow-architecture-arbitration.md)
- [ADR-0002](0002-single-run-authority-and-effect-recovery.md)
- [ADR-0003](0003-turn-gate-artifact-and-distribution-boundaries.md)
