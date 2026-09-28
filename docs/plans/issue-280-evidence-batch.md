# Issue #280: local evidence batch diagnosis

## Specification card

- **Behavior:** turn already captured Debug alarm and log results into a bounded, pointer-preserving batch; compare only compatible snapshots; keep up to three evidence-bound Drive candidates and avoid repeating an unanswered observation suggestion.
- **Repository:** this `openubmc-agent-workflow` worktree, branch `codex/issue-280`.
- **Contract:** the Runtime `observe`/`execute`, Evidence, Run, Gate, and Outcome contracts stay authoritative. The existing `diagnostic_advice` output remains compatible with Runtime validation. Local batch and state results are advisory only.
- **Standards:** `CONTEXT.md`, ADR-0001 and ADR-0007, `openubmc-debug/references/evidence-workflow.md`, and the captured-evidence bounds in `docs/issues/evidence-batch-diagnosis.md`. No MDB, northbound, permission, or generated interface changes; no Interface SIG review risk.
- **Acceptance:** synthetic captured results prove duplicate reduction with every raw pointer retained, explicit unknown time/sequence and partial states, incompatible comparison rejection, bounded time correlation, candidate contradiction and suggestion suppression; existing Debug and package tests remain green.
- **Risk and rollback:** compact Debug presentation gains grouped line entries with all original IDs; consumers must resolve any grouped ID through `ids`. Revert this branch commit to restore the old presentation. No persistent state or target access is introduced.

## Files and checks

1. Add a pure Debug batch processor in `openubmc-debug/scripts/_evidence_batch.py` and use it from `_workflow_correlation.py`; normalize only explicit time zones and retain raw pointers.
2. Group exact duplicate compact log lines in `_workflow_runtime.py`. Keep full captured results untouched.
3. Add an optional local state mode to `diagnostic_advice.py`; leave its Runtime-attach shape unchanged. Select the lowest declared observation cost and carry prior suggestion identities as caller-supplied advisory state.
4. Document the local API and run focused tests, Debug tests, and package validation. Use synthetic fixtures only. Report measured JSON bytes and suggestion counts.

The local processor does not contact a BMC. Product release, QEMU, and live-target validation remain separate gates.

## Synthetic verification result

- Fixed fixture: 48 identical long alarm-log lines and two identical alarm events. The selected compact log-line pool fell from 33,466 bytes when each line was displayed separately to 3,290 bytes when grouped, a reduction of 30,176 bytes (90.2%). All 48 log IDs, physical line numbers and raw-line pointers remained present.
- Replaying an equivalent captured Drive request twice produced two proposed `observe` queries without caller-carried state and one with the new state. The fixture made zero real device calls; this measures proposed calls, not live model or transport traffic.
- Validation: 208 Debug tests passed (two platform skips) with Python 3.12, `TMPDIR=/private/tmp` and `jsonschema` in a temporary environment; nine plugin packaging tests and seven Runtime advice projection tests passed. The Runtime projection tests are compatibility checks only; no Runtime source changed.
