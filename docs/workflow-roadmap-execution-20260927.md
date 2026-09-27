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

## Second integration update (2026-09-27)

- #279 input normalization, #280 bounded Debug evidence batching, #283 release
  provenance and #282 optional tracing have been integrated on the coordinator
  branch. The #282 trace-enabled checks passed with the optional OTel SDK, and
  the default-off path leaves Runtime responses and persisted events unchanged.
  Synthetic Host shell-hook checks do not prove a production shell budget hook.
- #285's 20-scenario manifest, 60-slot pinned trial plan and evidence scorer are
  integrated. Its deterministic offline fixtures scored 20/20 as expected, and
  the integrated plan was rebound at `69f3223` to source `ba78cf4`. Actual
  Agent trials remain **0/60**; no compatible fake-Runtime Host adapter or
  baseline rerun has been accepted. This is an evaluation harness, not item 19
  live acceptance.
- #284's fail-closed platform evidence collector is integrated at `c860890`.
  On macOS the relevant 18 tests and quick metadata check passed. The release
  matrix remains blocked: native Linux x86_64, Windows bootstrap, selected WSL
  Runtime, separate Desktop same-Run proof, and exact-source hosted CI have no
  passing evidence. A skipped GitHub job or emulated container cannot fill a
  native row. The prior GitHub Actions run belongs to an older commit and did
  not execute because of the account-side billing/spending limit.
- The MCP compatibility review is recorded in
  [mcp-compatibility-20260927.md](mcp-compatibility-20260927.md). The current
  Codex CLI 0.153.4 uses the legacy MCP path; its 2026 feature flag is off.
  The 36 legacy MCP contract tests pass. Modern `server/discover` and the Tasks
  extension are **not** advertised, adopted or claimed compatible.
- #243 credential containment remains in review on its isolated branch.
  Until its synthetic-secret tests and integrated regression pass, do not run
  actual model trials with credential-bearing work or claim production safety.

These updates do not replace a clean final-source stability run, real 20 × 3
Agent trials, native platform acceptance, GitHub CI or the separate Desktop
installer test. No source from this coordinator branch has been published as a
new release.

## Third integration update (2026-09-27)

- #243's credential-containment patch is integrated as `611122b`. It rejects
  inline secret-shaped MCP/Runtime input, keeps selected credentials local and
  task-bound, removes SSH passwords from child argv/environment, and sanitizes
  durable event, receipt, exception and Host-output boundaries. Synthetic
  containment tests passed 20/20 after integration; no real credential or BMC
  was used. The operator rotation runbook is in
  [credential-exposure-response.md](credential-exposure-response.md). It does
  not erase historical transcripts or rotate an account automatically.
- Adjacent macOS capacity measurements exposed CPU overhead in the new
  boundary checks. With the same Python 3.12 environment, original `main`
  used 10.82 s user CPU, the integrated candidate before optimization used
  16.37 s, and the optimized candidate used 11.66 s for 128/128 Runs. Commit
  `37447c9` skips static redaction regexes only when a field has no syntax
  marker and no registered secret value. The optimized function matched the
  prior implementation on 10,005 deterministic synthetic strings, and
  credential/MCP tests passed. No protection or release threshold was removed.
- The pre-optimization source `459b4a6` had one failed standalone stability
  report: capacity 53.066 s and restart 45.009 s exceeded the 40/30 s limits,
  despite 128/128 and 64/64 completed Runs. The same source later passed at
  30.966/16.659 s; the earlier failure remains evidence of load sensitivity.
  The clean optimized code commit `37447c9` passed its standalone qualifier
  (`promotable=true`): capacity 12.384 s, restart 7.435 s, zero failed calls
  and zero replay backend reads. These are macOS observations, not Linux
  release qualification.
- After optimization, the full Runtime suite passed 847 tests (3 skips), the
  full Debug suite passed 216 (2 skips), and 55 focused package, activation,
  replay, stateful-evaluation and platform tests passed. Immediately before
  the optimization, the full script suite passed 551 tests (35 skips). Four
  previously unguarded `/proc` and pinned-Linux-Codex tests are now marked
  Linux-only; their acceptance behavior was not replaced by macOS simulation.
  The full script suite has not been rerun on the final optimized source.
- The 20-scenario offline scorer remains 20/20 for its expected fixture
  verdicts, but actual independent Agent trials are still **0/60**. #247's
  global Host shell budget, #250's installed live final delivery, native
  Linux/Windows/WSL, Desktop same-Run readback and exact-source hosted CI
  remain unaccepted. The last observed hosted CI attempt had a zero-step
  failed preflight and skipped Linux/Windows jobs due to account billing or
  spending limits. Conditional helper/reviewer/Tasks/framework adoption is
  not justified without controlled trials; the one-domain graph prototype
  remains a local, unadopted experiment.

The coordinator branch is still a local candidate, not a production deployment
or release. Keep the parent and dependent issues open until their distinct
acceptance evidence exists.

## Fourth integration update (2026-09-27)

- Draft PR #287 now carries the coordinator candidate; it remains open and
  unmerged. Its hosted validation preflight failed before any steps ran, and
  Linux/Windows jobs were skipped. This does not replace native acceptance.
- #247's Host binding audit is integrated as `132a2fd`. A disposable native
  hook probe confirmed session identity and command-only `PreToolUse` input,
  but not a trustworthy target/operation or a global shell-call budget. No
  production hook or global trust setting was installed.
- #285's guarded single-slot fake-Runtime adapter is integrated as `4ab8cc4`.
  It is restricted to `diagnosis-complete`, uses a loopback relay with a
  per-run random marker, rejects non-loopback plaintext upstream, and checks
  that native shell snapshots are disabled. Twelve focused tests and quick
  workflow validation pass after integration. The attempted native pilot on
  Codex CLI 0.144.6 was cancelled before Runtime execution, so there is no
  persisted Outcome and actual verified Agent trials remain **0/60**. No
  additional model turn was run after the guard was added.
- The full script suite must be run on a clean integrated commit; the latest
  result is recorded on PR #287. The prior 551-test run and 35 platform skips
  above belong to an earlier source. Release and item 19 acceptance remain
  blocked by the missing live trials and platform evidence.

## Fifth integration update (2026-09-27)

- The clean `7d86c9a` candidate completed the full script suite with exit
  code 0. The guarded fake-Runtime Host path was then tested in a disposable
  local CLI 0.153.4 fixture under a read-only sandbox and a single-tool
  approval override. Start, cancel, same-session final and resume passed
  without a real model or target.
- One authenticated `diagnosis-complete` slot was attempted against source
  `611a125` through a temporary SSH loopback tunnel, retaining the read-only
  sandbox and fake backend. Runtime binding, terminal Outcome, native Host
  session and final answer verified. The native rollout recorded two MCP
  calls and 43.831 seconds. The corrected scorer in `fb9bc11` reads 55,847
  input plus 607 output tokens, exceeding the unchanged 10,000-token budget.
  Result: **1/60 actual trials, 0 accepted**; no prior-source comparison.
  The remaining 59 trials were not dispatched, and the tunnel was closed.
- The scorer now reads native cumulative token and completed-tool events,
  rejects missing usage, and retains legacy CLI-stream parsing. The two
  focused suites passed 14 tests after integration. A fresh full-suite and
  stability result must still be bound to a clean final commit. No release,
  merge or production deployment is authorized by this pilot.
