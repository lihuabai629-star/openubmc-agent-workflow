# Continuous closeout qualification

The continuous closeout checkpoint combines the repository-level evidence needed to pass a
Maintenance checkpoint without weakening product promotion semantics:

```bash
python3 scripts/continuous_closeout_qualification.py \
  --product-manifest /path/to/product-evidence.json \
  --runtime-repository /trusted/runtime-state/runtime.sqlite3 \
  --output continuous-closeout-report.json
```

The same checkpoint can assemble the manifest from trusted Operator / CI inputs:

```bash
python3 scripts/continuous_closeout_qualification.py \
  --product-ingestion /path/to/product-ingestion.json \
  --runtime-repository /trusted/runtime-state/runtime.sqlite3 \
  --output continuous-closeout-report.json
```

`--product-manifest` and `--product-ingestion` are mutually exclusive. Ingestion derives Run,
target, terminal Outcome, source, and artifact identity from the Runtime ledger, clean Git
repositories, and adjacent artifact metadata before the existing closeout verifier evaluates the
result.

Without `--product-manifest`, the hermetic repository checkpoint still runs and reports the fresh
product evidence as an external blocker. When a manifest is supplied, the product evidence is
re-read and re-hashed rather than trusting a previously rendered report.
`--runtime-repository` is required only for a fresh Runtime product manifest and is selected by
the Operator / CI Plane independently of manifest-authored paths.

The checkpoint verifies:

- Codex and Claude install working Runtime MCP registrations whose configured stdio command
  completes `initialize` and `tools/list`; OpenClaw remains an explicit Skills-only product client
  until a supported OpenClaw MCP configuration Adapter exists;
- DSH remains a disjoint, task-owned evaluation harness with isolated home, configuration,
  Runtime state, session, and MCP lifecycle roots;
- MCP lifecycle tests cover parent loss, active-request drain, identity-bound orphan cleanup, and
  zero live orphan closeout;
- product-closeout qualification fails closed on evidence, source, artifact, Runtime, and protocol
  mismatches;
- source-only, live-patch, build-upgrade, wide-observe, restart/crash, dependency-blocked, and
  hardware-blocked task classes finish with reproducible machine-readable results;
- repeated terminal diagnostic projection preserves Outcome and acceptance while measuring bytes
  saved as a secondary metric.

Task correctness and terminal completion are qualification gates. Token and projection byte
measurements remain secondary telemetry. Codex, Claude, and OpenClaw are product clients; DSH is a
separate evaluation harness and is never installed or evaluated as a fourth product client.

The representative repeated DiagnosticReceipt is 15,516 bytes in full and 1,035 bytes as a
digest-bound terminal reference, saving 14,481 bytes. Initial actionable Turns, one-shot terminal
Turns, cross-task resumes, and changed receipts retain the complete evaluable receipt. Projection
size remains a soft display target and never becomes a 4 KiB or 8 KiB control-flow gate.

`maintenance_checkpoint_ready` concerns repository correctness and qualification coverage.
`fresh_product_promotable` remains false until fresh Runtime-bound product evidence is supplied.
These are separate decisions.
