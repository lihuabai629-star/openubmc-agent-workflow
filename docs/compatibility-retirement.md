# Compatibility retirement

The compatibility writers and MCP profile are retired. The Runtime now accepts Agent writes only
through `observe` and `execute`; the Operator / CI Plane remains separate. Historical old-event
upcasters and anonymous telemetry are retained as read-only migration evidence.

## Telemetry

Operator `runtime_status.compatibility_telemetry` retains:

- `operation_counts`: compatibility operation calls, including `phase_record` and `workflow.next`;
- `feature_counts`: legacy semantic inputs such as `observe.assurance`,
  `execute.observation_receipt`, `execute.control_continue`, `phase_record`, and `workflow.next`;
- `total_calls` and `total_features`: sums of the corresponding counters.
- `tracking_started_at` and per-operation/per-feature `last_seen_at`: the persisted timestamps used
  with count snapshots to evaluate a no-new-use window.

When Runtime uses SQLite, counters remain stored in the same database and are visible across
Runtime instances and process restarts. Current Agent and internal Domain calls do not increment
them.

## Retired inputs

- `observe.assurance`: rejected; Runtime selects assurance automatically.
- `execute.control_continue`: rejected; use `execute(kind=resume)`.
- `execute.observation_receipt`: rejected; use `ObservationRef`.
- `phase_record`: not exposed; answer a Gate with `execute(kind=respond)`.
- `workflow.next` and `workflow.advance`: not exposed; use `execute` start/respond/resume.
- `compatibility` interface profile: rejected; use `agent` or `operator`.

Old-event upcasters remain until all supported persisted Runs pass their retention or migration
window. They are readers only and cannot create new Run transitions.

## Retirement evidence

`scripts/compatibility_retirement.py` turns a pre-retirement Operator `runtime_status` projection
into three digest-bound records:

- `baseline` captures one telemetry identity, count snapshot, source commit and capture time;
- `increment` compares a later snapshot, rejects counter rollback or tracking-identity changes, and
  counts distinct commit dates on canonical `github/main` first-parent history;
- `evaluate` binds that zero-use evidence to a complete promotable Release Gate from the same source
  commit and reports readiness separately for each writer and for the whole compatibility profile.

An active development day is a distinct committer date (`%cs`) among canonical first-parent commits
after both the baseline source and the baseline capture instant, through the current source.
Side-branch activity and main commits that already existed when the baseline was captured do not
shorten the window. Any count increase requires a new baseline after callers have been migrated.

```bash
python scripts/compatibility_retirement.py baseline \
  --runtime-status runtime-status.json \
  --source-ref github/main \
  --output compatibility-baseline.json

python scripts/compatibility_retirement.py increment \
  --baseline compatibility-baseline.json \
  --runtime-status runtime-status-current.json \
  --source-ref github/main \
  --main-ref github/main \
  --output compatibility-increment.json

python scripts/compatibility_retirement.py evaluate \
  --increment compatibility-increment.json \
  --release-gate release-gate.json \
  --writer execute.control_continue \
  --output compatibility-decision.json
```

The evaluate command returns exit status `0` only when the selected historical writer decision is
ready. Without `--writer`, it evaluates the whole retired profile. Old-event upcasters are always
reported as preserved readers and are outside writer-removal readiness. The tool remains for audit
and release evidence; it does not re-enable retired inputs.
