# Workflow continuity implementation

## Specification (2026-09-27)

Continue the accepted roadmap in `codex/workflow-continuity`. Preserve the first
batch (verified credential auto-save, Windows/WSL routing, mixed-source identity).
No extra credential confirmation or redaction gate is introduced.

This batch implements 06 (local recovery), 07 (terminal delivery), and 08 (task
handoff) at the existing host/Runtime seam. A host checkpoint is a bookmark and
reasoning aid, never a second Run, Gate, Effect, or Outcome authority.

- Capture Run references automatically after actual MCP execute results.
- Reconstruct handoff from fresh Runtime state, retaining goals, source identities,
  evidence references, hypotheses/contradictions, and outstanding questions as
  explicitly non-authoritative host notes.
- Prepare a durable minimal answer only from a Runtime terminal Outcome. A
  successful tool response is not a user-visible final-answer acknowledgement.
- Recover prepared answers without executing or resuming device operations.
- Audit real host final events before marking delivery confirmed.
- Continue independent diagnostic collection when optional source/KB processing
  fails; never blindly retry unknown writes or authentication failures.

## Standards and scope

Level 3: local persistence and additive host integration. Read `CONTEXT.md`,
ADR-0001 through ADR-0004, and the existing terminal-answer contract. No BMC
MDB/Redfish interface changes; no Interface SIG change. Agent tools remain
`observe` and `execute`. Host metadata is additive, not a new domain command.
Native Codex UI final injection requires host support; preparation and observed
delivery must stay distinct. No new generic orchestrator is introduced.

Only this isolated worktree is edited. The separately developed Desktop and the
original checkout are not changed. No remote push, release, billing settings, or
real-device operation is included.

## File plan and checks

1. `openubmc_target_runtime/host_continuity.py`: bounded durable bookmarks, fresh
   readback, task notes, terminal preparation/audit. Test multi-Run tasks, restart,
   stale summaries, cross-task isolation, concurrent writers and storage failure.
2. `mcp.py`, `target_runtime_mcp.py`: wire capture into production MCP execution;
   storage failures remain separate from successful Runtime work. Preserve tool
   schema and domain results.
3. `openubmc-debug/scripts/host_continuity.py`: local host/operator commands for
   handoff, notes and final-delivery audit; no target transport.
4. Diagnostic collection: isolate optional source-processing failures and retain
   partial evidence. Tests verify independent lanes still complete.
5. Documentation and packaging manifest: supported usage and exact remaining
   integration limits. Run focused unittest suites, packaging validation,
   compile checks and `git diff --check`.

## Risks and rollback

Host files can be unavailable/corrupt/contended; return a local checkpoint warning
without changing an already-completed Runtime result. Never infer terminal success
from host notes. Runtime data may expire; report missing authority instead of
using cached status. Disable the optional host-continuity constructor argument to
roll back integration; existing Runtime state and credentials remain unchanged.
Linux/WSL lifecycle checks and real native UI delivery are reported separately
from hermetic tests.

## Roadmap boundaries

Remaining retrieval/diagnosis work (09–12), experimental graph/reviewer/Tasks work
(13–17), and telemetry/qualification/release work (19–24) are not silently marked
complete by this batch. Desktop (18) belongs to the other task. Conditional
framework adoption remains conditional on a demonstrated benefit.

## Implemented in this batch

- Added `HostContinuity`: bounded SQLite bookmarks/notes; current ledger readback;
  per-task/Run terminal records using the existing `TerminalAnswerStore`.
- MCP captures after the authoritative result, with fail-soft transport metadata.
  Native Codex 0.153.4 supplies `_meta.threadId` (not a child-process thread-id
  environment variable); this observed field now binds MCP and hook identities.
- Added packaged SessionStart/Stop hooks and a local recovery CLI. The launcher
  reuses the existing immutable composition/entrypoint validation recipe. No
  hook trust, global config, release, or real device was changed.
- Source index exceptions no longer discard collected log/alarm lanes or prevent
  freshness checks. Their results stay explicitly incomplete.
- Fixed an existing migration path comparison exposed by macOS `/var` aliases;
  only canonically identical recorded targets are eligible, with raw spelling
  retained for configuration rollback.
- Packaging test fixtures now include untracked non-ignored candidate files,
  so a green archive test cannot silently omit a new feature. Published archives
  still build from immutable Git refs.

## Remaining roadmap status

These statuses are deliberately narrower than the original 24-item goals.

| Item | Current disposition |
| --- | --- |
| 01 interruption baseline | Existing evaluation retained; no new 20-task comparison baseline yet |
| 02 input normalization | Existing compatibility retained; proposed broader normalization not implemented here |
| 03 credentials | First-batch verified auto-save retained; legacy migration/nondefault-port branches remain |
| 04 redaction | Per user direction, no new redaction cleanup or gate; existing protection retained |
| 05 Windows/WSL routing | First-batch correction retained; physical Windows/WSL acceptance still required |
| 06 partial recovery | Optional source failure isolation added; no claim of universal recovery for every external service |
| 07 final delivery | Prepared records, trusted hooks, native synthetic recovery and packaged launcher implemented; installed live delivery not verified |
| 08 handoff | Durable bounded notes/current-state reconstruction and native same-session resume implemented; no automatic cross-session transfer |
| 09 incremental index | Existing source trace retained; new incremental index not implemented |
| 10 retrieval fusion | First-batch source ownership/provenance retained; LightRAG fusion still pending |
| 11 evidence batching | Existing log processing retained; proposed new processors still pending |
| 12 diagnosis strategy | Existing Drive advice retained; proposed broader hypothesis policy still pending |
| 13 small graph | Conditional experiment, not adopted |
| 14 read-only helpers | Conditional use, no always-on orchestration added |
| 15 counter-evidence reviewer | Conditional use, no new mandatory gate added |
| 16 MCP compatibility | Additive host metadata/native identity fixed; broader protocol upgrade not claimed |
| 17 MCP Tasks | Conditional experiment, not implemented |
| 18 Desktop | Separate workstream; not modified or certified here |
| 19 stateful evaluation | Native deterministic lifecycle probe added; not the planned 20 tasks × 3 live-agent trials |
| 20 platform/CI | macOS local regression expanded; Linux-specific checks/hosted CI/actual WSL not certified |
| 21 telemetry | No new OpenTelemetry exporter implemented |
| 22 provenance | Existing immutable package integrity retained; no new release attestation published |
| 23 maintenance | Documentation/entrypoint/path-test corrections in this batch; no remote issue closure |
| 24 framework trials | No framework replacement; experiments remain demand/benefit dependent |

## Verification record (2026-09-27)

**Implementation is locally exercised, not release-qualified or deployed.**

| Verification | Observed result |
| --- | --- |
| Full Runtime suite in a clean temporary candidate snapshot | 799 passed, 1 failed out of 800 |
| Debug suite with `TMPDIR=/private/tmp` | 196 passed, 2 Linux-only lifecycle tests skipped |
| Immutable package + Python entrypoint suites | 20 passed, including the Node hook and verified snapshot path |
| Windows/WSL launcher simulations | 12 passed, 1 Linux process-identity test skipped |
| Dependency recovery suite | 18 passed, 3 Linux first-start ownership tests skipped |
| Native Codex 0.153.4, local scripted Responses, fake target | Passed: 2 MCP calls (start/cancel), 1 text-only continuation, exact final confirmed, restart restores cancelled state with no additional device call |
| Previous immutable Git ref using the updated builder | Passed; older ref still builds without adding a nonexistent host-hook launcher |
| Python compilation, Node syntax, `git diff --check` | Passed |

The full Runtime candidate was the temporary snapshot
`5131ebc0136e0cd57ef626e2ac6c435ca6102bb0`; no commit was made on the working
branch. The failing case was
`test_public_semantic_seams_survive_storm_capacity_and_restart_soak`:

- Capacity: 128/128 Runs completed, zero failed calls/incomplete operations;
  **44.854 s > 40 s** budget.
- Restart soak: 64/64 Runs completed, zero replay mismatches and zero replay
  backend reads; **37.530 s > 30 s** budget.
- The qualifier correctly returned `promotable=false`. These performance limits
  were **not** relaxed. Host load versus a candidate regression has not been
  isolated with a controlled baseline comparison; do not attribute the failure
  to either without evidence.

Test-only portability corrections allowed mocked `pidfd` tests to execute on
macOS, separated actual Linux-parent checks, and allowed synthetic process startup
before asserting retained timeout output. A 50 ms reconcile fixture window was
raised to 200 ms while preserving its 500 ms bounded-response assertion; no
production deadline or release-performance threshold changed.

Reproducible focused commands (use the repository's test Python environment):

```sh
TMPDIR=/private/tmp python -m unittest discover -s openubmc-debug/tests
python -m unittest scripts.tests.test_plugin_package scripts.tests.test_plugin_python_entrypoints
TMPDIR=/private/tmp python -m unittest scripts.tests.test_plugin_windows_launcher scripts.tests.test_plugin_dependencies
python scripts/qualify_host_continuity.py probe --codex /path/to/codex-0.153.4
```

For the full Runtime suite, construct a temporary source snapshot using the same
`scripts.tests.plugin_fixture.package_fixture` helper and execute unittest in its
clean `source/` directory with `PYTHONDONTWRITEBYTECODE=1`. Its temporary fixture
commit represents the candidate, not the base branch HEAD; never bypass the
existing clean-source evidence requirement to label a dirty worktree qualified.

The isolated `colima-openubmc-workflow` Docker daemon was checked and is not
running. No VM/container was started, no WSL or live BMC validation was performed,
and no user hook trust, plugin installation, push, release or hosted CI was changed.
