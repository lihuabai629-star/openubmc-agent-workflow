# ADR-0005: Retire compatibility writers and profile

- Status: Proposed; becomes Accepted only after the evidence-gated candidate is promoted to
  canonical `main`
- Date: 2026-08-24
- Decision owners: openUBMC Agent Workflow maintainers
- Supersedes in part: the temporary compatibility profile in [ADR-0001](0001-runtime-core-and-semantic-agent-interface.md)

## Context

The semantic Agent Interface has stabilized at `observe` and `execute`. Canonical Skills use
`ObservationRef`, `execute respond`, and `execute resume`; persisted compatibility telemetry and
qualification evidence provide the migration record. Keeping a second Agent-facing profile would
retain duplicate write paths and obsolete protocol vocabulary after its callers have moved.

Persisted Runs still require historical readers. Removing an input writer does not imply deleting
the event upcasters needed to reconstruct supported records.

## Decision

Remove the `compatibility` MCP profile and its translation Adapter. Reject the retired Agent inputs
`observe.assurance`, `execute.observation_receipt`, and `execute control=continue`. Do not expose
`phase_record`, `workflow.advance`, `workflow.next`, or Domain operation names through an Agent
profile.

Keep exactly two transport projections:

- `agent`: `observe` and `execute`;
- `operator`: Evidence, Replay, lifecycle, Runtime status, and Session Outcome governance.

Keep historical compatibility telemetry readable through operator Runtime status, but stop writing
new compatibility counters. Preserve explicit old-event upcasters for supported persisted Runs.

## Consequences

- Canonical Skills answer Gates with `execute(kind=respond)` and continue Runs with
  `execute(kind=resume)`.
- `ObservationRef` is the only supported way to seed a Run from an earlier observation.
- Automatic assurance remains Runtime policy and is not selectable by the Agent.
- `RunEngine` remains the sole Run, Gate, Incident, and Outcome transition authority.
- Historical qualification may execute a pinned pre-retirement source as its baseline; the current
  Runtime cannot enable the retired profile.
- Removing old-event readers requires a separate retention or migration decision.

## Rejected alternatives

- Keep the profile indefinitely as an undocumented escape hatch.
- Continue accepting retired fields while merely hiding them from `tools/list`.
- Delete persisted-event upcasters together with command writers.
- Add replacement Agent tools for phase submission, status polling, or direct Domain execution.

## Evidence and references

- [Compatibility retirement](../compatibility-retirement.md)
- [Agent Semantic Gateway](../agent-semantic-gateway.md)
- [Issue #66](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/66)
