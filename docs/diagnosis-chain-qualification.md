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
successful terminal Outcome. Failed and cancelled diagnosis responses must terminate without any
Developer Gate or submission in their event ledgers. The historical source timestamp remains in
the fixture for traceability; each run generates a current qualification timestamp for live
freshness semantics.

Correctness determines the exit code. Runtime calls and elapsed time are reported as secondary
evidence and never compensate for an incomplete or failed Outcome.

The historical regression can be reproduced from a local v2.0.1 source tree:

```bash
python scripts/diagnosis_chain_qualification.py \
  --runtime-root /path/to/v2.0.1/openubmc-target-runtime
```

That invocation exits non-zero because the blocked diagnosis exposes `developer.change`.
`python scripts/validate_workflow.py` runs the current qualification on every pull request and main
push through the repository validation workflow.
