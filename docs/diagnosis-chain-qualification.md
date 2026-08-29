# Diagnosis chain qualification

The hermetic diagnosis-chain qualification exercises the public `observe` and `execute` seam with
the semantic shape captured from ObservationReceipt
`observation-ba1a3b5277447c57cf972ee2`:

```bash
python scripts/diagnosis_chain_qualification.py
```

The command requires no BMC, credentials, Conan remote, or private network. It proves that a
complete live ObservationRef whose diagnostic content is not visible yields a durable
`diagnosis.acceptance` Gate, survives resume and subprocess restart through shared SQLite/blob
storage, accepts a grounded diagnosis, opens `developer.change`, and reaches one durably recorded
source-only terminal Outcome. Failed and cancelled diagnosis responses must terminate without any
Developer Gate or submission in their event ledgers. The historical source timestamp remains in
the fixture for traceability; each run generates a current qualification timestamp for live
freshness semantics.

The development response deliberately reproduces an incomplete validation environment. One Conan
readiness check is reused by official UT and build; official UT is
`dependency_blocked_before_start`, build is `dependency_graph_blocked`, and a passing pure-logic
check remains supplementary. Hardware coverage requires NVMe while the fixture observes only SATA
and SAS, so the report preserves an explicit blocked hardware result. The source-only Outcome may
complete in scope, but the Closeout claim remains `source_changed` and qualification fails if any
blocked result is promoted to official UT, compilation, package, firmware, upgrade, or NVMe
hardware success.

Correctness determines the exit code. Runtime calls and elapsed time are reported as secondary
evidence and never compensate for an incomplete Outcome or a false validation claim.

The historical regression can be reproduced from a local v2.0.1 source tree:

```bash
python scripts/diagnosis_chain_qualification.py \
  --runtime-root /path/to/v2.0.1/openubmc-target-runtime
```

That invocation exits non-zero because the blocked diagnosis exposes `developer.change`.
`python scripts/validate_workflow.py` runs the current qualification on every pull request and main
push through the repository validation workflow.
