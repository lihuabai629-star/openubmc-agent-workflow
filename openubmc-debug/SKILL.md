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

## Agent Gateway

Use the default `openubmc-target-runtime` MCP through its semantic Interface:

- Call `observe` for exact read-only questions. Declare only the required selectors. A narrow MDB
  or capability query should complete in one call and return an inline `ObservationReceipt`.
- Treat one answer as one observation: combine capability and exact MDB selectors needed for the
  current answer in the same `observe` call. Do not run a separate capability preflight; the
  internal observation Adapter performs it. Split selectors only when the returned Receipt is
  explicitly incomplete and asks for a narrower scope.
- Call `execute` for work that may cross diagnosis, Developer, Build, Live Patch, Upgrade,
  verification, recovery, or acceptance phases.
- When the user says “继续” or “continue”, call `execute` with `kind: resume` and the retained
  `run_id`. Do not reconstruct the original request.
- When a Turn has `state: waiting_response`, load the Skill named by the Gate owner, execute the
  requested phase, and call `execute` with `kind: respond`. Put the phase receipt fields in
  `response.payload`; the Gateway supplies Runtime sequencing identity internally.
- Use `kind: control` with `command: reconcile` for an unknown mutation outcome. Use `cancel` only
  at a returned phase Gate. Mutation authorization remains frozen in the Run and is not broadened
  by continuation.
- Return control only for a terminal Outcome or a concrete blocker requiring new input, new
  authorization, an unavailable external capability, or unresolved mutation reconciliation.

Treat freshness as an evidence time property. The Agent Interface accepts live evidence with
`max_age_seconds: 0`; do not use `freshness` as a profile. Capability conclusions are tri-state:
`available`, `unavailable`, or `not_checked`. Never claim availability for an unobserved capability,
and bind every conclusion to the current Receipt and its coverage.

Do not call `case_read`, `evidence_read`, `workflow.advance`, `workflow.next`, `phase_record`, Replay,
Session Outcome governance, or Runtime status from the default Agent profile. Those operations are
available only in explicit `compatibility` or `operator` profiles. Raw Evidence and governance are
for operators and CI, not ordinary diagnostic reasoning.

`workflow_remote.py` and `compare_remote.py` remain input-compatible CLI baselines. The generic CLI
uses the same `observe/execute` Gateway as MCP; select the compatibility profile only for migration
or controlled performance comparison.

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

The active task keeps direct credential values for later Runtime operations. A stateful `execute`
Run keeps its workflow inputs internally; the separate restartable TaskContext snapshot remains
secret-free. Ordinary `observe` calls do not create that persistent workflow state.

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

For a narrow interactive MDB or capability query through MCP, call `observe` with only the required
selectors. Use `assurance: auto` unless the task explicitly requires a start/end freshness boundary.
The returned Receipt carries the exact values and coverage inline. Object/alarm and bounded log
selectors remain CLI or compatibility paths until their internal `observe` Adapters are available.

Use `execute` when the result must support a causal claim, cross-surface correlation, post-change
verification, mutation recovery, or a terminal acceptance Outcome.

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

Inside one MCP task, the TargetRun may retain capability readiness per declared scope and target/
lane epoch. A follow-up observation still recollects the requested values. A target epoch change or
connection rebuild invalidates readiness automatically. Do not treat this as result caching.

If the local MCP stdio process disconnects, reuse the same task ID and `run_id`. Target Runtime
restores typed intent, target bindings, bounded workflow summaries, and mutation journal identities.
It never restores a live connection or treats a previous observation as fresh. The default MCP
`structuredContent` is an `ObservationReceipt` or `Turn`; raw Evidence remains in the operator
profile and must not be pulled into ordinary Agent context.

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
