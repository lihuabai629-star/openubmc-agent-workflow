# ADR-0005: Retire compatibility writers and profile

- Status: Accepted
- Date: 2026-08-24
- Accepted: 2026-08-25
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
- [Machine-readable roadmap completion evidence](../roadmap-completion.json)
- [Agent Semantic Gateway](../agent-semantic-gateway.md)
- [Issue #66](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/66)
- [PR #67](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/67), merged as
  `a28350d19fde5808626f3c73b4e070db440cfa83`
- Qualified source `e7dc74c052f3874d3d9214ce0cfae8949a397765` and lock-only commit
  `7dc350cd3ecf2ffab2d1d4db89d4bac81f1ccec4`
- Release Gate evidence digest
  `sha256:e18fdbfcbc04e84a5ba79f2160728cedc11d4a873a40e7091f2beaa35c9b2a67`
- [Successful canonical main validation](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32764011480)
