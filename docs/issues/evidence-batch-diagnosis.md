Part of #278, roadmap items 11 and 12.

## Problem

The Debug workflow can collect bounded alarm/log/observation evidence and has
conservative Drive hypotheses, but common tasks still send repeated raw lines
to the model and re-investigate eliminated explanations. Add deterministic
local processing and a small, evidence-bound diagnosis state.

## Scope

- Process only evidence already captured by Runtime or the Debug adapters.
  A processor does not connect to the device, issue shell commands or create
  an alternate Evidence/Run authority.
- Normalize timestamps only with an explicit source timezone or offset.
  Keep unknown time and failed parsing visible. Collapse exact duplicates
  while retaining counts, target/source identity and raw line/event pointers.
- Compare snapshots only when target, source version and epoch are compatible;
  otherwise return an explicit incomparable result. Correlate logs/alarms with
  bounded time windows and distinguish correlation from causal proof.
- Maintain at most three candidate hypotheses with support, contradiction,
  ruled-out state and remaining gap references. Recommend the cheapest
  read-only observation that distinguishes the remaining candidates, using
  the existing Runtime `observe` contract for any actual device query.
- When evidence cannot support one root cause, deliver the candidates and
  unresolved gap. A processor failure leaves the original evidence usable.

## Acceptance

- Fixed synthetic fixtures preserve every key evidence pointer while reducing
  repeated model-facing bytes and calls. Report the measured difference.
- Timezone unknown, missing sequence, stale epoch, conflicting source version
  and partial collection remain explicitly uncertain.
- A new observation that contradicts a candidate changes its state; repeated
  equivalent evidence does not trigger the same suggestion again.
- Existing Drive advice and packaged Debug entrypoints remain compatible.
  Tests use captured fixtures and do not contact a BMC.

## Ownership

Own Debug local evidence processors, `_workflow_correlation.py`,
`diagnostic_advice.py` and their focused tests/docs. Avoid Runtime MCP,
Windows routing, credential and source-index implementations in concurrent
issue branches. Start from snapshot `73eeba56dda909c41dfb66345aa15f0dcd067142`.
