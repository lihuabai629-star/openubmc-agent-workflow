# Compatibility retirement

This document describes the compatibility-retirement candidate. It removes the writers and MCP
profile so Agent writes enter only through `observe` and `execute`; the Operator / CI Plane remains
separate, and historical old-event upcasters plus anonymous telemetry remain read-only. The
candidate must remain unmerged until zero-use telemetry and a same-source promotable Release Gate
satisfy the readiness policy below.

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
- `increment.v3` binds the baseline telemetry, compares a strictly later snapshot, and rejects
  rewritten deltas, counter rollback, non-finite or inconsistent timestamps, and tracking-identity
  changes;
- `evaluate` binds that zero-use evidence to a complete promotable Release Gate from the same source
  commit and reports readiness separately for each writer and for the whole compatibility profile.

Earlier `increment.v1` and `increment.v2` evidence is not accepted by the retirement evaluator.
Evidence digests detect accidental drift inside the trusted internal qualification workspace; they
are not an authentication or hostile-tampering boundary.

Any count increase blocks the affected writer and the compatibility profile. Migrate the caller,
capture a fresh baseline, then generate a new same-source qualification before retrying retirement.

```bash
python scripts/compatibility_retirement.py baseline \
  --runtime-status runtime-status.json \
  --source-ref github/main \
  --output compatibility-baseline.json

python scripts/compatibility_retirement.py increment \
  --baseline compatibility-baseline.json \
  --runtime-status runtime-status-current.json \
  --source-ref github/main \
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
