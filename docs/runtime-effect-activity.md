# Runtime Effect activity

A running `execute` Turn includes a bounded `progress` projection for its existing
Effect. Use `execute(kind=resume, run_id=...)` to attach to that Effect. The
projection does not authorize another operation and is not an Outcome.

| Field | Source and meaning |
| --- | --- |
| `effect_id`, `operation`, `phase` | The durable Effect and pinned workflow step. |
| `owner` | Registered Domain owner, with the pinned workflow step as fallback. |
| `started_at`, `deadline_at` | Runtime scheduling timestamps persisted when the Effect is admitted. |
| `last_progress_at` | The timestamp of the latest persisted operation or Evidence event. Reads do not change it. |
| `retry_generation`, `retry_requested_at` | A persisted read-only Evidence retry request. Absent before such a request. |
| `reconcile_requested_at` | A committed request to recover the same Effect. |
| `reconcile_count`, `reconciled_at` | Recorded reconciliation results and their latest event time. They do not count unrecorded attempts. |
| `settled_at`, `reason` | The recorded settlement time or current error/Incident code, when available. |

Timestamps are Unix seconds. Fields without a corresponding recorded fact are
omitted. Reconstruction uses event timestamps, so replay does not create new
progress. An `OperationProgressed` payload cannot override `last_progress_at`.

The optional `heartbeat` describes only the current local supervisor:
`owner_pid`, `admitted_at`, `observed_at`, `worker_state`, execution mode, and
settlement generation. A running worker may be waiting on a target. Its heartbeat
does not prove target responsiveness, business progress, or a successful mutation.
Heartbeat state disappears on process restart.

The caller's `execute.deadline` bounds how long that call waits. The Effect's
persisted `deadline_at` is separate. When the Effect deadline expires before a
result is available, RunEngine records `effect_deadline_exceeded`. The Incident
does not fabricate failure, success, cancellation, or mutation truth. Before
committing it, RunEngine rechecks that the same Effect remains active and that a
late completion or another Incident has not superseded the timeout.

A late result can settle the existing Effect. A `running`, `blocked`, or unknown
result does not resolve the timeout. If no local execution remains, recovery uses
the same Effect identity: mutations reconcile their existing MutationJournal
without applying another mutation; read-only Effects may safely repeat. A proven
terminal result resolves the timeout and allows the workflow to continue. The
MutationJournal remains the authority for target execution and verification.

These semantics follow [ADR-0002](adr/0002-single-run-authority-and-effect-recovery.md).
