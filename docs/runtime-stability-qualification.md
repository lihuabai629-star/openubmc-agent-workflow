# Runtime Stability Qualification

The stability qualification exercises the Agent and Operator interfaces with a hermetic read-only
Adapter. It does not require a BMC, credentials, private networking, or a compatibility operation.

Run it directly:

```bash
python scripts/runtime_stability.py --workspace . --output runtime-stability.json
```

`scripts/runtime_qualification.py` runs the same probe together with the mutation crash-cut,
concurrency, false-success, target binding, Artifact binding, and unknown-recovery suites. The
release gate consumes the resulting aggregate report.

## CI profile

| Scenario | Workload | Promotion invariant |
| --- | --- | --- |
| duplicate storm | 16 simultaneous deliveries plus terminal replay and a conflicting retry | one Run, one original command Decision, one Outcome, no Incident, conflicting input rejected |
| Gate concurrency | eight SQLite-backed Runtime instances submit the same Gate receipt | one Gate submission, one Outcome, and byte-equivalent terminal Turns for every caller |
| capacity | 128 hermetic diagnosis Runs measured in four batches | bounded linear event/storage growth, no failed call, duplicate Outcome, open Incident, or incomplete operation |
| restart soak | 64 terminal diagnosis Runs across four SQLite process lifecycles, each replayed only after reopening the Runtime | every cross-restart replay returns the terminal Turn without another backend call; one Outcome per Run; no open Incident or unsettled operation |
| crash-cut matrix | journal and real Live Patch backend durable cuts | stable Effect identity and no repeated dangerous mutation |

The CI profile blocks promotion when any of these limits is exceeded:

- 30 seconds for the restart soak;
- 30 seconds for the 128-Run capacity workload;
- 512 MiB total process peak RSS and 128 MiB peak Python allocations;
- 32 MiB persisted SQLite and Artifact storage for 64 Runs;
- 16 persisted events for a single diagnosis Run and bounded linear event growth per restart cycle;
- any duplicate Outcome, open Incident, incomplete operation, or same-key/different-input
  acceptance.

## Evidence

The report records the source commit, Python and platform fingerprint, all workload parameters,
Agent execute calls, failures, completions, per-cycle and cumulative event growth, storage growth,
total process RSS, Python allocations, thresholds, pass/fail status, and a SHA-256 digest over the complete report. The aggregate Runtime
qualification validates the child schema, source binding, canonical parameters, digest, raw metrics,
and every hard threshold before accepting it.
An explicit source commit must equal the tested workspace HEAD. The only exception is an immutable
release-lock child, where it must equal that commit's sole parent and the lock's recorded source.
Scenario assertions read projections and events through the public persistent repository contract;
they do not depend on Operator-only case tools or private test hooks.
Intermediate `running` Turns during a duplicate storm are valid reattach points; the decisive
condition is convergence under the same command identity to one terminal Outcome.
