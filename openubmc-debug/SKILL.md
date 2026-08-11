---
name: openubmc-debug
description: Diagnose, compare, and verify openUBMC runtime issues by correlating local source or configuration, northbound interfaces, live MDB/D-Bus objects and alarms, logs or files, and optional OS-host evidence. Use for a supplied BMC IP, a runtime symptom, comparison of two or more live targets, a Developer diagnostic handoff, or post-upgrade verification. Prefer two to four complementary evidence surfaces for composite problems, and use an available openUBMC KB only as a non-blocking candidate router. Keep remote work read-only and route implementation, build, upgrade, or live mutation to their owning skills.
---

# openUBMC Runtime Debug

## Scope

Own read-only runtime diagnosis and post-change verification. Infer whether the task is:

- `diagnose`: explain and localize a current or reproduced symptom;
- `verify_delivery`: check requested behavior on the currently deployed target.

Accept direct prose or a concise handoff. Do not require a JSON envelope. Keep diagnosis separate
from implementation:

- source or design changes -> `openubmc-developer`
- component or product build -> `openubmc-build`
- firmware upgrade or rollback -> `openubmc-upgrade`
- temporary runtime replacement or mutation -> `openubmc-live-patch` through a typed delivery route
- offline dump or log-bundle-only analysis -> `openubmc-log-analyzer`

For a problem already narrowed to Systemd, Skynet, MDB/D-Bus mechanics, coroutine behavior, ASAN,
or openUBMC naming, read `references/mechanism-debugging.md` without abandoning this multi-surface
orchestration entrypoint.

## Case Continuation

Prefer the `openubmc-target-runtime` MCP for work that may continue across calls or Skills. The
first domain operation opens or resumes a persistent Case automatically. Keep its `case_id` and let
the Runtime retain the original intent, final purpose, change boundary, target roles, operation
receipts, evidence references, and next action.

- When the user says “继续” or “continue”, call `workflow.next` with the bound `case_id`; do not
  reparse the request or rebuild inputs already retained by the Case.
- Treat `workflow.next` as the continuation loop, not as a one-step status read. When it returns
  `waiting_phase_record`, load `required_skill` immediately and pass its `handoff_arguments`; do not
  wait for the user to name Build or Developer again. Record the returned phase with the included
  `phase_record_contract`, then call `workflow.next` again in the same user turn. Call it again
  after `budget_exhausted`; for
  `operation_in_progress`, wait for and reuse the current operation rather than creating another.
  Return control only for a terminal Closeout or a concrete blocker that requires new input, new
  task-level authorization, an unavailable external capability, or unresolved mutation
  reconciliation.
- Treat target bindings, credential selectors, artifact identities, delivery strategy, mutation
  authorization, and authorized exceptions as Case facts. Reuse them while their identity still
  matches; do not ask the user to repeat or reconfirm them at each Skill boundary.
- Target Runtime's typed authorization decision is authoritative. A named Live Patch, Upgrade, or
  rollback request authorizes only that action; Apply or Upgrade never implies rollback. Internal
  BMC workflows authorize insecure TLS by default and may explicitly set it to `false` for a trusted
  certificate. The Live Patch exceptions `force_path`, `no_backup`, and `no_remount` remain explicit
  task facts, while the minimum required `skynet` restart is announced rather than reconfirmed.
  Reuse every matching decision already carried by the Case.
- A mutation outcome unknown is a reconciliation blocker, not a confirmation question: reconcile
  the same durable journal before continuing. If recovery requires rollback that the Case does not
  authorize, preserve the original operation as `recovery_blocked`.
- A failed read or delivery step remains incomplete. The next `workflow.advance` creates the next
  numbered attempt and executes it again; it does not replay the failed receipt forever. Keep an
  unknown mutation outcome blocked until its durable journal is reconciled.
- Use `case_read` to recover the bounded Case projection after a task or MCP restart.
  Its `structuredContent` includes the typed continuation (intent, delivery route, targets,
  workflow cycle, required phase/operation, blocker, target epoch floor, and next action) plus the
  bounded Capsule, so resume from that contract instead of reconstructing the task from chat. A
  direct domain retry with the same `case_id` automatically rebuilds the retained target binding;
  do not ask for or resend the IP unless the target is intentionally changing.
- Use `evidence_read` only for the evidence slice needed now; do not pull every raw result back into
  context.
- Task completion closes TargetRun connections and transient leases but keeps the Case. Explicit
  `case_close` seals completed work; `case_forget` removes an ordinary terminal Case.
- A target switch or comparison remains in the same Case. Keep target identity, epoch, role, scope,
  and freshness separate; switching back may rebuild or reuse only the matching target lease.
- When the Case reaches a terminal state, Target Runtime automatically derives and persists
  `closeout`, `closeout_markdown`, and, by default, `closeout_bundle` from the Case event stream.
  `workflow.next` returns the Closeout Markdown as the user-facing first screen and exposes the
  structured Closeout plus its document, evidence, and artifact index for later retrieval.
- Explicit target-set or port changes advance `target_version` and invalidate old target-bound
  workflow steps. Selecting an existing multi-target entry with `target_id` changes only the active
  selector and does not invalidate the comparison or advance the version.
- A second completed `developer.change` starts the next workflow cycle. Replacing a downstream
  Build record keeps the current cycle but invalidates Upgrade and verification steps after it.
- Context Runtime is the authoritative sequencer. A direct domain operation that matches the next
  workflow step satisfies that step, so later `workflow.advance` continues forward instead of
  repeating Debug. A legacy nested `workflow` object may remain as compatibility input, but the
  domain backend must not start a second automatic end-to-end workflow in authoritative mode.

`workflow_remote.py` and `compare_remote.py` remain input-compatible CLI entrypoints, but both call
the same OperationCatalog and Context Runtime as MCP. Their process-local TargetRun closes on exit;
pass the returned `case_id` back with `--case-id` to resume persistent receipts and evidence from a
later CLI or MCP task. Use `--idempotency-key` when retrying the same completed operation.

## Environment and Inputs

Invoke bundled helpers from `$HOME/.agents/skills/openubmc-debug` and keep the working directory at
the task's openUBMC repository. Route a missing canonical Skill link to
`openubmc-environment-setup`; route missing host programs such as `ssh` or `sshpass` there as well.

Resolve source from the supplied repository/worktree, `OPENUBMC_SOURCE_ROOT`, or the current Git
root only when its remote identifies an openUBMC repository. Never substitute a control-plane,
example, or author workspace.

Internal development mode is the default: direct password arguments are accepted, BMC SSH
host-key verification defaults to `insecure`, sensitive path/member reads are permitted, and
diagnostic evidence is returned without automatic redaction. This replaceable-target policy does
not apply to an OS host: `doctor.py --os-check` keeps OS SSH host-key verification `strict`.

The active task keeps direct credential values for later domain calls. Context Runtime also keeps
the Case workflow inputs used by `workflow.advance`, including direct values in internal
development mode; the separate restartable TaskContext snapshot remains secret-free. Forgetting or
expiring the Case removes that continuation state.

## Workflow

### 1. Frame the Diagnostic Question

Record the symptom or acceptance item, target or source scope, expected behavior, and the last
reboot, replacement, upgrade, reload, or other change boundary. A request to check “now” always
requires fresh evidence.

### 2. Select a Delivery Strategy When Fixing

Do not treat `diagnose-and-fix` as a synonym for Live Patch. Select the route from the diagnosed
edit boundary and the intended delivery result:

- `source-only`: complete the source change and local validation without changing a target;
- `live-patch`: temporarily deploy a runtime-compatible file through `openubmc-live-patch`, then
  recollect fresh Debug evidence;
- `build-upgrade`: hand the source result to `openubmc-build`, pass its verified artifact identity
  to `openubmc-upgrade`, then recollect fresh Debug evidence.

When the user explicitly requests live patching or live verification, infer and carry the minimum
restart scope: `none` when the runtime consumer does not need a reload, or `skynet` when framework
reload is required for the requested verification. Inform the user before the bounded restart, but
do not ask them to choose or reconfirm the scope.

Use `source-only` until a concrete mutation route exists. Infer a later route from the owning Skill
handoff or workflow sections; do not ask the user to repeat a target, credentials, final purpose, or
delivery intent already present in the task. Build never opens a target connection, and QEMU remains
owned by its dedicated Skill.

### 3. Use Knowledge as a Candidate Router

For an unfamiliar or cross-component symptom, query the openUBMC KB when it is
already configured and the tools are available. Run it without blocking source or live collection.
Use it to suggest candidate components, objects/properties, log keywords, source entry points, and
common misdiagnoses. It is not an evidence surface and cannot establish root cause.

Use `naive` retrieval first with context and references. Retry once with `local` only when the first
result is insufficient. Do not use `mix` as the default routing mode. Read
`references/knowledge-routing.md` for the query and route-card format. Knowledge-tool failure never
blocks diagnosis.

### 4. Compose an Evidence Plan

Most runtime failures are composite. Use two to four complementary surfaces when making a causal
claim:

| Surface | Typical questions |
| --- | --- |
| source/configuration | Which definition, trigger, caller, generated boundary, CSR/SR/MDS, or mapping owns the behavior? |
| northbound interface | Does Redfish, IPMI, Web, CLI, or another exposed interface reproduce the symptom? |
| object/alarm | What do current MDB/D-Bus objects, properties, services, and active alarms show? |
| log/file | What timeline, state transition, loaded configuration, or startup failure appears in bounded live evidence? |
| OS/hardware | Does the host or hardware-facing layer corroborate visibility, identity, driver, BDF, or device state? |

A single surface is sufficient only for a genuinely narrow question such as reading one current
property or locating one definition. Do not promote that answer into a root-cause claim.

Use `combined_snapshot` when object/alarm plus log/file evidence and source correlation are all
needed. Otherwise select only the relevant helpers. Read `references/evidence-workflow.md` for
surface selection and correlation rules.

When one issue needs several reviewed `mdbctl` reads, pass repeatable `--mdb-query` values to
`workflow_remote.py` so the targeted reads share the workflow's TargetRun. Do not split them into
separate helper processes merely to vary the class, object, or property. Add `--mdb-only` when
those reads are the complete evidence surface; it keeps lightweight freshness while skipping
unrelated alarm, bus-tree, log, and file collectors. When object names must first be discovered
from a class, use repeatable `--mdb-expand-class`; it performs `lsobj` and the dependent `lsprop`
reads in the same workflow. Query count is not fixed; `--mdb-concurrency auto` applies per-target
backpressure while preserving every requested read.

For a narrow interactive “show the current object/alarm state” request through MCP, use
`debug_collect` with `profile: object-alarm`. It performs one fresh SSH-backed snapshot and skips
the end freshness pass, Telnet lane, and source correlation. Use the full `debug_run` workflow when
the result must support a causal claim, cross-surface correlation, or post-change verification.

For a narrow interactive MDB query through MCP, use `debug_collect` with `profile: mdb`, or pass
`mdb_only: true` without selecting another profile. It reuses the task TargetRun and capability
gate, immediately recollects the requested MDB values, and skips Telnet, source correlation, and
the end freshness pass. `debug_run --mdb-only` deliberately retains full freshness semantics.

For two or more environments, use `compare_remote.py` to collect the same typed request on every
target and compare either one reference against all candidates or all targets symmetrically. Do
not impose a fixed target-count limit; use the requested concurrency and deadline to control load.
The scheduler submits only one concurrency-sized window at a time, so queued target count does not
become an equally large Future queue.
Pass the same bounded workflow selectors to every target and keep automation output compact:

```bash
python "$HOME/.agents/skills/openubmc-debug/scripts/compare_remote.py" \
  --target <ip-a> --target <ip-b> \
  --concurrency auto --json --compact-json
```

When a verified alarm endpoint or other exact selector is already known, pass it to the comparison
instead of repeating broad discovery independently on each target.

### 5. Establish Remote Capabilities

Before any live collection, establish target freshness and available capabilities. Run full
preflight when log/file evidence may be needed; use `--skip-telnet` for an object-only check, or
`--mdb-only` when MDB is the only required object lane:

```bash
python "$HOME/.agents/skills/openubmc-debug/scripts/preflight_remote.py" \
  --ip <ip> --json --compact-json
```

Inside one MCP task, the TargetRun retains the capability snapshot per evidence profile and target/
lane epoch. A follow-up workflow reuses that snapshot but still refreshes the current SSH/Telnet
time anchors and recollects the requested evidence. A target epoch change or connection rebuild
invalidates the snapshot automatically. Do not treat this as result caching; object values, alarms,
logs, and files remain fresh reads.

If the local MCP stdio process disconnects, reuse the same task ID. Target Runtime restores the
typed intent, target bindings and credential selectors, bounded workflow summaries, and mutation
journal identities. It never restores an SSH/Telnet/Redfish connection or a previous evidence
result; the next domain call lazily rebuilds its connection and performs fresh reads. The default
MCP text content is a concise Chinese summary during active work. At terminal Closeout, it becomes
the Closeout Markdown report. `structuredContent` contains the real Context result plus the bounded
Agent Envelope; read full raw JSON, logs, journals, or detailed diffs through `evidence_read` only
when needed.

The same task may replace one target, expand into a comparison set, select a different target by
`target_id`, or change connection and evidence parameters. Preserve the original purpose and typed
delivery context unless the caller changes them. Reuse target-specific leases and credential
selectors when their identity matches; never let a previous target's host or ports leak into the
new selection. See `references/remote-automation.md` for workflow and mutation deduplication.

Domain connection caches are bounded LRU caches with a default capacity of 32 bindings or leases
per task and domain. This is not a target-count limit: an evicted target reconnects if selected
again, while comparisons may contain any number of requested targets.

On a cold full preflight, each lane starts as soon as its own prerequisites are complete: MDB after
SSH/MDB checks, D-Bus/alarm after the D-Bus and busctl checks, and log/file after Telnet. The full
preflight result still completes and is retained as one audit surface; readiness scheduling does
not add another probe or merge SSH and Telnet responsibilities.

Select helpers from the reported capability, not from transport preference. The bundled object and
alarm helpers currently use SSH; log/file helpers currently use Telnet. This is an implementation
fact, not a universal routing law.

For current alarms, prefer `active_alarms.py`. Use `mdbctl_remote.py` for model-oriented object
queries and `busctl_remote.py` for exact D-Bus path, interface, signature, or property evidence.
Without an override, the alarm reader first introspects the standard
`bmc.kepler.event` `/bmc/kepler/Systems/1/Events` endpoint and falls back to bounded discovery only
when that exact endpoint is unavailable or does not expose `GetAlarmList`.
Use `doctor.py --os-check` only when host-side visibility is part of the question and the user
supplied `OPENUBMC_OS_*` access.

### 6. Collect Once and Correlate

Run independent source, interface, object/alarm, and log/file collection in parallel when
useful. Keep one owner for target access and credentials; reasoning workers must not open additional
remote sessions. `workflow_remote.py` is the combined collector, not a mandatory entrypoint for
every issue.

Correlate surfaces using stable identity and time: object path, component or slot, EventName or
EventCode, BDF, state transition, sample/threshold, target timestamp, and reboot/change boundary.
Do not stitch unrelated definitions, log lines, objects, tests, or similar incidents into one causal
chain. A knowledge-base match or source keyword hit remains a candidate until the actual owner and
caller/trigger are evidenced.

Treat absence as evidence only after a successful, bounded, complete query observed it. Timeouts,
unavailable capabilities, stale snapshots, truncated reads, and failed commands remain gaps.

### 7. Conclude or Route

For diagnosis, identify the strongest evidenced owner and editable boundary. Route only after the
caller/trigger or interface contract supports it:

- handwritten Lua, MDB/MDS/interface/model, or Redfish/Web/CLI/SNMP/IPMI mapping -> `openubmc-developer` with the evidenced domain edit intent
- specialist driver, WebUI, compute, or other source -> the matching specialist
- insufficient evidence -> remain in diagnosis and state the next missing observation

For delivery verification, account for every requested item and keep deployed target identity
separate from local source or package evidence.

## Result

Return a concise human-readable report unless machine-readable output was requested:

1. status and shortest evidence-backed conclusion;
2. target/source scope and freshness boundary;
3. evidence table with surface, command/query, observation time, finding, and limitation;
4. causal chain or acceptance results;
5. contradictions, unavailable surfaces, and unverified claims;
6. next read-only check or owning Skill.

Use `references/diagnostic-contract.md` only when a structured diagnostic or verification result is
actually needed. A completed procedure is not a successful verification when requested items failed
or did not run.

For a terminal Case, do not compose a second ad-hoc summary. Use `closeout_markdown` as the primary
answer and `closeout_bundle` as the immutable index for `closeout.json`, `closeout.md`, stage
evidence, build artifacts, and other recorded outputs. Missing or unreadable evidence remains an
explicit unverified gap; phase completion alone must not be presented as business acceptance.

## Safety

- Keep all remote actions read-only. Do not restart services, mutate properties, upload, upgrade,
  replace files, or invoke arbitrary methods from this Skill.
- Keep object trees, logs, files, and source searches bounded. Preserve timestamps and relevant
  excerpts. Internal development mode returns the collected evidence without automatic redaction.
- Treat `GetAlarmList` as current alarm evidence and historical event APIs as historical evidence.
- A failed `mdbctl` query does not prove an object is absent; cross-check the exact path/interface
  with another available object capability.
- BMC SSH host-key verification defaults to `insecure` for internal development and tolerates
  replaceable-target key changes. OS-host SSH remains `strict`.

## References

- `references/knowledge-routing.md`: openUBMC KB candidate routing, retrieval mode, and route card.
- `references/evidence-workflow.md`: evidence surfaces, capability selection, freshness, and correlation.
- `references/remote-automation.md`: credentials, helper commands, JSON output, and transport limits.
- `references/workflow.md`: combined snapshot and optional parallel analysis.
- `references/diagnostic-contract.md`: structured diagnose/verify result when requested.
- `references/alarm-access.md`: current-alarm discovery and `GetAlarmList`.
- `references/object-access.md`, `references/busctl-access.md`, `references/mdbctl-access.md`: object evidence.
- `references/file-access.md`, `references/logs.md`: bounded live file/log evidence over the available capability.
- `references/mechanism-debugging.md`: localized runtime mechanism checks.
- `references/components.md`: ownership hints to verify in the actual repository.
- `references/optional-integrations.md`: personal memory, notes, and other explicit opt-in integrations.
