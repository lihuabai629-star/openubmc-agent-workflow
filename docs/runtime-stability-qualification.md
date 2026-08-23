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
| restart soak | 64 terminal diagnosis Runs across four SQLite process lifecycles, each replayed | every replay returns the terminal Turn; one Outcome per Run; no open Incident or unsettled operation |
| concurrency matrix | simultaneous settlement and revision-race recovery | one terminal Decision and deterministic reattach |
| crash-cut matrix | journal and real Live Patch backend durable cuts | stable Effect identity and no repeated dangerous mutation |

The CI profile blocks promotion when any of these limits is exceeded:

- 30 seconds for the restart soak;
- 128 MiB peak Python allocations measured by `tracemalloc`;
- 32 MiB persisted SQLite and Artifact storage for 64 Runs;
- 16 persisted event revisions for a single diagnosis command;
- any duplicate Outcome, open Incident, incomplete operation, or same-key/different-input
  acceptance.

## Evidence

The report records the source commit, Python and platform fingerprint, all workload parameters,
raw scenario metrics, thresholds, pass/fail status, and a SHA-256 digest over the complete report.
Intermediate `running` Turns during a duplicate storm are valid reattach points; the decisive
condition is convergence under the same command identity to one terminal Outcome.
