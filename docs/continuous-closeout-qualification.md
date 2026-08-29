# Continuous closeout qualification

The continuous closeout checkpoint combines the repository-level evidence needed to pass a
Maintenance checkpoint without weakening product promotion semantics:

```bash
python3 scripts/continuous_closeout_qualification.py \
  --product-manifest /path/to/product-evidence.json \
  --output continuous-closeout-report.json
```

Without `--product-manifest`, the hermetic repository checkpoint still runs and reports the fresh
product evidence as an external blocker. When a manifest is supplied, the product evidence is
re-read and re-hashed rather than trusting a previously rendered report.

The checkpoint verifies:

- Codex, Claude, and OpenClaw remain the supported product clients;
- DSH remains a disjoint, task-owned evaluation harness with isolated home, configuration,
  Runtime state, session, and MCP lifecycle roots;
- MCP lifecycle tests cover parent loss, active-request drain, identity-bound orphan cleanup, and
  zero live orphan closeout;
- product-closeout qualification fails closed on evidence, source, artifact, Runtime, and protocol
  mismatches;
- repeated terminal diagnostic projection preserves Outcome and acceptance while measuring bytes
  saved as a secondary metric.

The representative repeated DiagnosticReceipt is 15,516 bytes in full and 1,035 bytes as a
digest-bound terminal reference, saving 14,481 bytes. Initial actionable Turns, one-shot terminal
Turns, cross-task resumes, and changed receipts retain the complete evaluable receipt. Projection
size remains a soft display target and never becomes a 4 KiB or 8 KiB control-flow gate.

`maintenance_checkpoint_ready` concerns repository correctness and qualification coverage.
`fresh_product_promotable` remains false until fresh Runtime-bound product evidence is supplied.
These are separate decisions.
