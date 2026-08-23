# Compatibility retirement

The compatibility profile exists to migrate callers, not as a second Runtime policy. Runtime
status publishes anonymous counters that make removal decisions observable without storing task,
target, payload, credential, or caller identity.

## Telemetry

`compatibility_telemetry` contains:

- `operation_counts`: compatibility operation calls, including `phase_record` and `workflow.next`;
- `feature_counts`: legacy semantic inputs such as `observe.assurance`,
  `execute.observation_receipt`, `execute.control_continue`, `phase_record`, and `workflow.next`;
- `total_calls` and `total_features`: sums of the corresponding counters.
- `tracking_started_at` and per-operation/per-feature `last_seen_at`: the persisted timestamps used
  with count snapshots to evaluate a no-new-use window.

When Context Runtime uses SQLite, counters are stored in the same database and are visible across
Runtime instances and process restarts. In-memory Runtime instances intentionally keep process-local
counters for tests and disposable development sessions.

## Burn-down order

1. Migrate `control=continue` to `resume` and full `observation_receipt` to `ObservationRef`.
2. Stop sending the legacy `assurance` field; Runtime already selects assurance automatically.
3. Migrate compatibility `phase_record` and `workflow.next` callers to `execute respond/resume`.
4. Persist a baseline Runtime-status snapshot. Remove legacy writers only when the relevant counts
   have not increased, their `last_seen_at` is older than 14 active development days (or no use has
   occurred since `tracking_started_at`), and one full release-qualification run has completed.
5. Retain old-event upcasters until all supported persisted Runs have passed their retention or
   migration window.

The removal sequence changes only compatibility Adapters. It must not add Agent operations, expose
another policy, or move Run transition ownership out of `RunEngine`.

## Retirement evidence

`scripts/compatibility_retirement.py` turns the Operator `runtime_status` projection into three
digest-bound records:

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

The evaluate command returns exit status `0` only when the selected writer is ready. Without
`--writer`, it evaluates retirement of the whole compatibility profile. Old-event upcasters are
always reported as preserved readers and are outside writer-removal readiness.
