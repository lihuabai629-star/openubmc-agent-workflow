# ADR-0008: Bounded observation continuity

- Status: Accepted
- Date: 2026-09-05
- Extends: [ADR-0005](0005-retire-compatibility-writers-and-profile.md), which requires ObservationRef as the only Run seed mechanism

## Decision

Runtime may select an `ObservationRef` automatically for a single-target debug diagnosis when
the same task owns exactly one durable, complete source for that target. The content-addressed
source records its task, selector scope, freshness deadline, target fingerprint, identity, and
epoch. A scan of durable blobs reconstructs the task and target index after restart. Ambiguous
sources remain available through explicit references.

Automatic selection has a 30-second age limit and requires a fresh, bounded capability probe to
confirm target identity and epoch. Unknown identity, changed fingerprint or epoch, expired data,
cross-task or cross-target scope, and multiple candidates decline reuse. The normal collection
path then runs. Explicit references retain their existing 15-minute window and take precedence.

The selected reference is saved in `start_input`; the caller's original command ID and input
digest remain unchanged. Duplicate starts reattach the original decision without discovering a
new reference. `ObservationRef` remains the only mechanism that seeds a Run, and `RunEngine`
remains the sole Run, Gate, Incident, and Outcome transition writer.

An extended observation plan may reuse exact values from existing selector IDs and collect only
missing values. The combined scope must preserve requested order, target identity, epoch, and
the five-second selector completion window. An invalid combination falls back to a fresh
collection of the whole requested scope. Collection completeness still requires explicit
`DiagnosisRecord` acceptance before a diagnosis workflow can succeed.

Standalone observation sources survive garbage collection until their reference window expires;
they do not require a synthetic Case merely to become discoverable.

## Validation

`test_observation_continuity.py` covers automatic seeding and replay, task and target isolation,
expiry, ambiguity, fingerprint and epoch changes, restart reconstruction, source retention,
missing-value collection, requested value ordering, and time-window failures.
