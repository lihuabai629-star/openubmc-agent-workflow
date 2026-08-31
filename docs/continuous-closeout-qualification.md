# Continuous closeout evidence collector

The continuous closeout collector assembles repository-level Runtime, task, projection, and
lifecycle evidence. It is an internal input to the canonical
[Codex Adoption Qualification](codex-adoption-qualification.md), not a separate public CI or
Release Gate decision.

It can still be run directly for diagnostics or product-closeout evidence assembly:

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

- Codex installs a working Runtime MCP registration whose configured stdio command completes
  `initialize` and `tools/list`;
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
measurements remain secondary telemetry. Codex is the product client; DSH is a separate evaluation
harness and is never installed or evaluated as a product client.

The representative repeated DiagnosticReceipt is 15,516 bytes in full and 1,035 bytes as a
digest-bound terminal reference, saving 14,481 bytes. Initial actionable Turns, one-shot terminal
Turns, cross-task resumes, and changed receipts retain the complete evaluable receipt. Projection
size remains a soft display target and never becomes a 4 KiB or 8 KiB control-flow gate.

Its `maintenance_checkpoint_ready` and `fresh_product_promotable` fields are collector-level
evidence used by higher-level qualification. Product-client adoption decisions come from Codex
Adoption Qualification.
