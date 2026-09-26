# Workflow roadmap execution plan (2026-09-27)

## Specification and boundary

- Goal: complete the user-approved 24-item workflow improvement roadmap against
  the `openubmc-agent-workflow` checkout, with independent issue branches and
  evidence-based acceptance. The Desktop client is an existing separate
  workstream; coordinate its contract and acceptance without editing its active
  checkout from this repository.
- Repository: `lihuabai629-star/openubmc-agent-workflow`, local Git root
  `/Users/liqinghua/Documents/Codex/2026-09-26/openubmc-workflow-continuity`.
  Snapshot `73eeba56dda909c41dfb66345aa15f0dcd067142` contains the current
  first-pass implementation of 03, 05, 06, 07, 08 and 10's source ownership.
- Contract: the Agent interface remains `observe`/`execute`; Runtime alone owns
  Run, Gate, Incident, Effect, Outcome, mutation identity, verification and
  rollback authorization. Host bookmarks, retrieval, UI, MCP transport and
  evaluation cannot become independent device-effect authorities.
- Standard gate: this phase changes host/runtime compatibility, local
  persistence, retrieval and qualification, not MDB, MDS or northbound BMC
  interface definitions. Public MCP result/schema changes require backwards
  compatibility checks and a documented interface review risk. No generated
  openUBMC component files or Conan caches are edited.
- User decisions: credential saving happens automatically after verified
  authentication. Do not add a new redaction approval gate; preserve existing
  secret handling and never print credentials. Classify community, internal
  and product repositories before aggregating search results.
- Validation: local tests and a 20-scenario stateful evaluation corpus, then
  Linux/WSL and installed-plugin acceptance. A release claim requires passing
  the existing Runtime stability budget and release gates.
- Rollback: each issue uses a dedicated branch/worktree from the snapshot.
  Reverting an issue commit must leave the snapshot's Runtime ledger, source
  identities and existing credentials intact. No production deployment is a
  prerequisite for local implementation.

## Dependency order and ownership

| Stream | Roadmap items | Primary files and contracts | Validation and completion |
| --- | --- | --- | --- |
| Performance and evaluation | 01, 19, 20, 23 | `scripts/tests/test_runtime_stability.py`, qualification/evaluation scripts, CI metadata | Controlled base/candidate timing comparison; existing 40 s/30 s budgets pass without relaxing them; 20 scenarios × 3 stateful trials report correctness, interruption, repeat work and cost. Linux/WSL results are separate from macOS. |
| Windows structured routing | 05, 16 and GitHub #247 | `scripts/execution_router.py`, packaged mirror, host probes and fallback receipts | Typed Runtime preferred on healthy MCP; bounded, attributable fallback; Windows/WSL simulations and native acceptance. No shell text satisfies a typed mutation gate. |
| Terminal delivery | 06–08 and GitHub #250 | Host continuity, MCP adapter, packaged hooks, terminal-answer qualification | Installed hook end-to-end readback, restart/cancel, partial/blocked answers, duplicate/task mismatch; no repeated device call. |
| Input and credentials | 02, 03, 04 | Runtime request decoding, credential resolver, local configuration and packaging | Default and nondefault ports, legacy config migration, verified autosave, malformed/ambiguous target handling and compatibility; no new approval gate. |
| Source navigation and fusion | 09, 10 | Debug source catalog/trace/search, index store and optional LightRAG client | Incremental commit/dirty-aware index; exact symbol/error lookup; source/KB fusion retains product/repository provenance; optional service failure leaves local results usable. |
| Evidence and diagnosis | 11–13 | Debug evidence processing and hypothesis advice | Bounded local batch processing with raw evidence pointers; hypothesis/support/refutation; compare one-domain graph prototype with fusion baseline before adoption. |
| Host and protocol trials | 14–18, 24 | MCP compatibility, conditional Tasks/helper/reviewer/kernel adapters and Desktop contract | Controlled A/B evidence for optional features; old client path works; same Run is shown in Desktop/plugin; no new Runtime owner. |
| Operations | 21–23 | Bounded telemetry, package/release provenance, docs and issue reconciliation | Traces tie to Run/Effect without secrets; disabling telemetry is behavior-neutral; package identity/SBOM/qualification are linked; every closed issue cites tests and commit/PR. |

GitHub #253 is the final offline regression synthesis after its behavior-defining
dependencies, including #247 and #250, have landed. Existing #260/#263 are
specification parents whose implementation children #261/#264 are closed; audit
them against the accepted autosave behavior before making new changes.

## First execution wave

| Issue | Worktree / branch | Codex task | State |
| --- | --- | --- | --- |
| #247 | `/Users/liqinghua/Documents/Codex/2026-09-27/workflow-issue-247`, `codex/issue-247` | `01a0df10-3602-7b40-9af3-8a5c09fd9461` | Started |
| #250 | `/Users/liqinghua/Documents/Codex/2026-09-27/workflow-issue-250`, `codex/issue-250` | `01a0df10-30c6-7333-bb40-2854494b240e` | Started |
| #276 | `/Users/liqinghua/Documents/Codex/2026-09-27/workflow-issue-276`, `codex/issue-276` | `01a0df10-3c5b-7152-b4d0-e6ada1795350` | Started |
| #277 | `/Users/liqinghua/Documents/Codex/2026-09-27/workflow-issue-277`, `codex/issue-277` | `01a0df19-b7b3-7973-90c9-6ce98d0f5e56` | Started |

1. Preserve the current candidate in a local commit. Create separate worktrees
   and tasks for #247, #250, and source navigation/fusion; assign explicit file
   ownership and tests. New issue work starts from the snapshot commit.
2. In the coordinator worktree, reproduce the Runtime stability failure and
   compare the same test on `origin/main` under controlled host load. Diagnose
   before changing production timeouts or thresholds.
3. Review the incoming branches against the public behavior seams. Integrate
   them one at a time, rerun focused tests and then the full release qualifier.
4. Dispatch subsequent independent issues (input/credentials, evidence/
   diagnosis, telemetry) as slots and dependencies become available. Keep the
   issue map and verification record current.

The previous candidate finished 128/128 capacity and 64/64 restart Runs but
exceeded the existing time budgets. That is a failed release gate until a
controlled comparison and a passing candidate run establish otherwise.

## Performance comparison (2026-09-27, macOS)

The original `9799a04` and clean candidate `f6b8bdd` used the same Python
3.12.2 virtual environment and `scripts.runtime_stability.qualify_runtime_stability`
on the same host. Full original: capacity 33.381 s, restart soak 17.980 s,
`promotable=true`. Full candidate: capacity 25.696 s, restart soak 17.608 s,
`promotable=true`. This establishes a passing local run after the earlier
candidate failure; it does not erase the earlier failure or certify a future
integrated source.

The targeted 128-Run capacity check was repeated in ABBA order with process
CPU accounting. Original: 21.973 s wall / 21.038 s user+system CPU, then
33.247 s wall / 30.709 s CPU. Candidate: 29.009 s wall / 27.347 s CPU,
then 29.457 s wall / 27.416 s CPU. All four passed the 40 s limit. The original
also varied substantially while no source files changed, so these measurements
do not isolate a candidate-specific regression. macOS load averages ranged
above the host's 10 logical CPUs during this investigation. A quieter Linux/WSL
or controlled-host run and the final integrated candidate's full suite are
still required before release qualification.

## Integration update (2026-09-27)

- #247 policy commit `1d9f77f` is integrated and 38 routing/replay tests pass.
  Installed-path follow-up `f3320bd` is under review for argument-digest
  containment and a possible trusted Host `PreToolUse` shell guardrail; it is
  not integrated or accepted yet. Native Windows remains unverified.
- #250 terminal-delivery commit `76c5637` is integrated. The coordinator's
  14 terminal, 20 host-continuity and 17 evaluation/replay tests passed.
  Installed synthetic Codex does not prove real-model or Desktop delivery.
- #276 source-navigation commit `0b6cdd6` is integrated; 8 new navigation
  and 13 trace tests passed. [PR #281](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/281)
  is stacked against the snapshot branch, not yet `main`.
- #277 credential commit `b472773` is integrated. The coordinator reran 20
  credential-memory, 13 local-credential and 7 configuration tests, all
  passed. [PR #286](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/286)
  is likewise stacked. Installed-plugin and Linux/WSL acceptance remain.
- #279 input normalization is committed as `4b4377c` in its issue worktree,
  pending integration review. #280 evidence batching and #283 release
  provenance remain active. #282 tracing, #284 platform acceptance and #285
  stateful 20 × 3 evaluation are scoped, not implemented.

The coordinator's clean `76c5637` Runtime stability report was
`promotable=true`: 128/128 capacity Runs in 32.493 s (40 s limit) and 64/64
restart Runs in 17.105 s (30 s limit), with zero replay backend reads. A later
805-test Runtime run had two failures: one OpenSSH timeout under heavy load
passed on isolated rerun; the stability self-test first rejected a dirty
worktree during concurrent documentation edits. Its clean rerun failed the
performance gate while macOS load average was roughly 28 on 10 logical CPUs.
No timeout or release budget was relaxed. After #276, the full Debug suite
passed 207 tests with 2 platform skips. Final clean integrated-source and
Linux/WSL qualification are still required.

GitHub Actions for PRs #281 and #286 did not start: GitHub annotated failed
account payments or exceeded spending limit; downstream Linux/Windows jobs
were skipped. This is an account-side blocker, not CI success. The isolated
installer also exposes a historical-release-object requirement for superseded
lock commit `7dc350c`; #283 owns repair without weakening published-release
verification.
