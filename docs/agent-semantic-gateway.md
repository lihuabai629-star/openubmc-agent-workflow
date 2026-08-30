# Agent Semantic Gateway

## Architecture

The Target Runtime is organized around one Runtime Core, two Agent operations, and a separate
governance interface.

```text
Agent
  │
MCP Adapter / CLI Adapter
  │
AgentGateway
  ├─ observe(Query)  -> ObservationReceipt
  └─ execute(Action) -> Turn
  │
Runtime Core
  ├─ ObservationEngine
  ├─ RunEngine + WorkflowDefinitions
  ├─ DomainExecutor + target leases
  ├─ Evidence/Artifact storage and Acceptance
  └─ MutationJournal, automatic reconcile, and rollback

Operator / CI profile
  ├─ raw Evidence and Case inspection
  ├─ Replay
  ├─ Session Outcome review and promotion
  └─ lifecycle and Runtime status
```

The AgentGateway is a bounded decode and projection Adapter. It depends on a two-method typed
`SemanticRuntimePort`; orchestration, recovery, and terminal Outcome formation stay in the Runtime
Core. MCP and CLI are transport Adapters at the same seam. Runtime implementation concepts such as
Case revision, workflow attempts, phase records, evidence offsets, and continuation operation names
are not part of the Agent Interface.

## Interface profiles

The default profile is `agent` and exposes only:

- `observe`: exact, read-only, live observations;
- `execute`: start or continue a stateful workflow to the next real decision Gate.

One explicit non-Agent profile preserves governance access:

- `operator`: only operations marked `exposure=operator`, including Case inspection, digest-bound
  local Evidence attachment and readback, Replay, Session Outcome governance, lifecycle, and
  Runtime status.

The projections are disjoint. `operator` does not expose Agent or internal Domain execution
operations. The retired `compatibility` value is rejected during Runtime construction.

Set `OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE=operator` only for Operator / CI purposes. Domain
operations remain internal to Runtime Core regardless of the selected transport projection.

## Observation contract

`observe` accepts an immutable target and selector set. The initial selector adapters are
`capability` and `mdb`. An Adapter cannot add evidence surfaces that were not declared in the
query. The Runtime accepts the complete query under the 256 KiB Agent request boundary and keeps
explicit limits for target, selector IDs, capability names, selector count, and MDB query count and
size. It plans collection partitions around a 2 KiB internal target; that target is not a
control-flow limit.

All selectors needed for one answer belong in one `observe` call. Capability is not a separate
Agent preflight step: the internal Adapter performs capability discovery and the exact MDB reads in
the same observation. Wide scopes are partitioned internally and aggregated under one
`ObservationResult`, persisted source, and `ObservationRef`; the Runtime does not ask the Agent to
guess narrower selectors because a serialized query exceeds the internal target.

Selector execution is a Runtime-owned bounded plan. Selector IDs and declaration order remain
stable through the Domain Adapter, persisted source, and Receipt. Independent MDB reads may overlap
on one target-scoped lease, with a Runtime maximum of four active reads; the Agent cannot select an
unbounded policy. Lease reuse is bound to target and credential identity, while target epoch changes
invalidate transport sessions before another read.

Capability and MDB grammar validation runs before target collection. Errors name the invalid
capability and list the supported names, or quote the invalid MDB query and show the reviewed
read-only command forms needed to correct it.

Freshness is a time property. The Agent Interface currently accepts only live evidence with
`max_age_seconds=0`; the old `freshness` and `log-file` profiles are rejected because profiles
describe evidence scope, not evidence age.

The Runtime owns one automatic observation policy. It first performs the exact read and upgrades to
a scope-preserving assurance pass only when coverage contains `not_checked` values or freshness
cannot be established. The Adapter receives the prior observation and can reuse already collected
MDB values. Each attempt records its start and completion plus the same facts for every selector.
The Runtime classifies the selected facts as `coherent`, `partial`, or `inconsistent` using a fixed
five-second completion-skew window. Assurance replaces the fast result only when its classification
improves, or when selector coverage improves without degrading the classification; otherwise the
Runtime retains the fast result with an explicit gap. An Adapter that omits selector timing cannot
produce a coherent reusable snapshot. The retired `assurance` input is rejected. Assurance remains
automatic Runtime policy and is not returned in the Agent projection.

An `ObservationReceipt` contains semantic values, tri-state capability results
(`available | unavailable | not_checked`), coverage, observation time, target identity when
available, grounded claims, and receipt-local evidence references. It does not open a Case or
generate a Closeout. The model-visible document targets 4 KiB. Projection pressure may compact
inline values and set `projection_truncated`; it never changes source status or coverage, removes a
valid `ObservationRef`, or requires the Agent to guess narrower selectors. A projection that still
cannot preserve those semantics within the target may exceed it with
`projection_target_exceeded=true`.

The redacted raw observation and its selector timing are persisted in the Runtime Core as a
content-addressed source. Partial evidence remains available through receipt-local source links,
but only a complete, temporally coherent Receipt carries an `ObservationRef` and can seed
`execute(kind=start)`. The Runtime
reconstructs the observation, verifies its digest and target scope, and then uses it as the first
diagnostic evidence, so it does not recollect the same evidence and remains reusable after a
process restart. Validation also binds the persisted scope digest, observation time, target
metadata, and 15-minute reuse window before a Run is opened. Full ObservationReceipts are not
accepted as `execute` input; callers pass the Receipt's verified `ObservationRef`.

## Execution contract

`execute` accepts four action kinds:

- `start`: create and advance a Run;
- `respond`: satisfy the current phase Gate and continue;
- `resume`: continue an existing Run;
- `control`: reconcile an unresolved mutation or cancel a current Gate or Incident.

The Runtime Core advances deterministic steps internally and returns a `Turn` only at a real Gate,
an unresolved Incident, a running reattach point, or terminal Outcome. A Turn contains the Run ID,
semantic state, a small Gate input schema, verified facts, gaps, an optional `DiagnosticReceipt`,
and a bounded terminal Outcome. A `DiagnosticReceipt` projects each requested diagnostic surface
as an Agent-visible result or an explicit unavailable/not-checked item. It also carries coverage,
freshness, capability states, truncation, content completeness, gaps, and bounded Evidence
references. Raw Evidence remains in the Operator / CI Plane; the Agent does not need a third
Evidence-read operation to evaluate the current Turn.

The Runtime Core sanitizes, forms, and persists the typed `DiagnosticReceipt` because its status
participates in Closeout and Replay. `AgentGateway` targets an 8 KiB Turn projection but does not
compact facts, diagnostic previews, or other typed Turn fields merely to meet that display target.
If the typed semantics exceed the target, the Turn remains complete and reports that condition as
telemetry rather than a blocker. The separate 32 KiB durable-receipt boundary may compact persisted
diagnostic detail before Turn projection, while preserving the completeness rules described below.
The MCP Adapter also renders a bounded textual receipt summary in standard `content`, including
source status, `agent_acceptance=complete|partial|blocked`, coverage, freshness, source
completeness, capabilities, gaps, DiagnosticReceipt/result/Evidence identities, terminal Outcome
semantics, and next-action guidance. Diagnostic preview values remain in the complete typed Turn
and are not duplicated into standard text. A Gate Turn keeps a compact
`GateBinding` line with `run_id`, `gate_id`, `gate_version`, and `schema_digest` ahead of those
identities so the immediate response action remains evaluable even when diagnostic content is large
or compacted. The
typed Turn remains authoritative in `structuredContent`; the text prevents clients that underuse
structured MCP data from reducing `execute` to a generic completed/failed acknowledgement.
The projected `diagnostic_receipt` in `structuredContent` carries the same `agent_acceptance`
classification so clients do not need to parse text or reinterpret visible coverage.

Domain execution completion is not diagnosis completion. Zero source-evaluable requested items
produce a blocked receipt, incomplete source content produces a partial receipt, and complete is
permitted only when every requested source item is fresh and content-complete. Every accepted
request identity remains represented by a visible result or a bounded `compacted_results` gap;
`visible_*` coverage reports what the current Agent projection can inspect without rewriting the
source-owned status.
An observation timestamp alone is not freshness proof: complete also requires an explicit fresh or
complete freshness status, a non-empty observation time, and no unavailable, lost, or stale
freshness dimensions. A per-target `complete=false` is also a freshness gap. Multi-target
freshness gaps identify the affected Runtime-owned target ID.
Requested coverage is derived from Runtime-owned operation arguments and reconciled with the
Adapter result, so an omitted echoed request or skipped collector cannot shrink the acceptance
scope. Multi-target diagnosis projects each target result plus the bounded comparison result.
Closeout derives Agent acceptance from both the source receipt status and `visible_*` coverage.
Source-complete Evidence remains complete after compaction, but `stage.diagnosis` is partial or
blocked when the persisted Agent-visible receipt is only partially evaluable or not evaluable.
A generic operation summary therefore cannot satisfy `stage.diagnosis`.

Reusable observation evidence and accepted diagnosis are distinct Runtime facts. Passing an
`ObservationRef` to `execute(start)` avoids recollecting that evidence, but it does not assert a
root cause. In `diagnose-and-fix`, a complete evaluable Runtime DiagnosticReceipt may satisfy the
diagnosis step automatically. A partial or blocked receipt instead yields a durable
`diagnosis.acceptance` Gate before `developer.change`; development cannot open while that Gate is
unanswered.

A completed `diagnosis.acceptance` response is bound by `run_id`, `gate_id`, `gate_version`, and
`schema_digest` and supplies `root_cause`, non-empty `evidence_ids` drawn from the current Runtime
DiagnosticReceipt, and `known_gaps`. The Runtime rejects Evidence IDs from another receipt and
derives observation time and freshness from the persisted ObservationRef and DiagnosticReceipt.
`execute(kind=resume)` only reattaches the same unanswered Gate, so repeated resume calls cannot
repair missing diagnosis input or advance the workflow. A failed or cancelled diagnosis becomes a
terminal Run before any development phase.

Development and Build Gate responses classify validation at the boundary actually reached.
Official UT is `passed`, `failed_after_start`, or `dependency_blocked_before_start`; build is
`compiled`, `compile_failed`, or `dependency_graph_blocked`. Supplementary pure-logic or stub
checks remain separately labeled and never satisfy official UT acceptance. A shared dependency
readiness result is checked once and reused; a blocked external package cannot be fabricated or
vendored into success. Hardware Coverage records required protocols, observed devices, Evidence
identities, and gaps. Submitted identities must belong to the current Runtime DiagnosticReceipt,
and each declared device/protocol pair must be present in the referenced Evidence content. Unknown
or unrelated target evidence is rejected, so SATA/SAS-only observations cannot validate an NVMe
repair. If a Developer response omits validation fields, the Runtime records official UT and build
as `not_run` and hardware coverage as `not_reported`; those become visible gaps rather than an
implicit success.

A `source-only` Run can reach a completed in-scope Outcome while official validation or hardware
coverage remains blocked. Its Turn and Closeout retain those gaps and its claim level remains
`source_changed`; no package, firmware, upgrade, or hardware-repair claim is formed. A completed
Build Gate requires a verified ArtifactRef and a `compiled` classification before package or
firmware claims can be projected. `compile_failed` and `dependency_graph_blocked` cannot be sent as
a completed Build response and never advance to Upgrade. A failed Build response must retain one
of those classifications and its dependency readiness. Closeout merges validation dimensions and
keeps readiness keyed by official UT and build, so a later Build result cannot erase or relabel an
earlier official-UT or hardware-coverage result.

A Turn targets 8 KiB and a Gate schema targets 4 KiB; neither target can reject or replace a Gate.
`gate_projection_target_exceeded` reports a Gate schema above its display target. A Turn above its
8 KiB target remains an unchanged typed Turn and sets soft projection telemetry; the display target
does not compact structured result previews. The MCP fallback text keeps control bindings,
coverage, result and Evidence identities, terminal semantics, and next-action guidance without
duplicating preview values. Separately, durable-receipt compaction may retain a bounded substantive
summary; a result that cannot retain evaluable content becomes `not_checked` instead of remaining
`available`. Coverage also reports `visible_evaluable` and `compacted` counts. Projection-only
fields such as `projection_truncated`, `lines_truncated`, and `stdout_truncated` do not claim that
the source Evidence was truncated; explicit source truncation and `content_complete=false` still
fail completion closed. The internal
advancement limit is fixed at 64 steps and is not part of the Agent Interface. Exhaustion appears as an
`internal_step_limit` blocker rather than a caller-controlled continuation budget.

Projection telemetry distinguishes display pressure from workflow state:
for execute Turns, `projection_compacted` and root `content_compacted` are never set true because
the typed Turn is not display-compacted, while `projection_target_exceeded` reports a final Turn
above the 8 KiB target. `text_projection_compacted` and
`text_projection_target_exceeded` describe only the MCP fallback text. Durable receipt compaction
uses the receipt-level `content_compacted` and `compacted_results` fields described below.
`manual_narrowing_required` and `budget_blocker` remain false for execute Turns. None of these
fields participates in Closeout or Outcome formation. Gate-only pressure is reported independently
as `gate_projection_target_exceeded`; it does not set the Turn-wide target-exceeded flag unless the
final Turn itself exceeds 8 KiB.

The durable Receipt is separately limited to 32 KiB before it enters event history. If detailed
previews do not fit, the Runtime retains bounded result identities, source coverage counts, gaps,
freshness, capability states, and Evidence references. Persistence compaction records `visible_*`,
`compacted_results`, and `content_compacted` without rewriting source status or source completeness.
Closeout still requires every requested identity to remain visibly evaluable before it reports a
passed diagnosis.
The public `execute(start)` boundary rejects a Runtime-owned diagnostic scope that would require
more than 1,024 result identities after applying the target multiplier and multi-target comparison
item, so an accepted scope always fits the durable identity budget.

Each phase Gate exposes stable `gate_id`, `gate_version`, and schema digest. A response may carry a
submission identity; the Adapter otherwise derives one from the persisted Gate binding. Duplicate
identity plus identical normalized input is idempotent. Reuse with different input, a different
Gate, or a stale version is a conflict. The internal developer Runtime does not require a one-time
secret Gate token. A Runtime Adapter that bypasses Gate construction and returns an oversized phase
schema is rejected at the Agent boundary; projection never replaces it with a synthetic Gate.

An actionable preflight rejection returns one canonical retry Action. The retry preserves the
caller's valid Action semantics, Gate response, submission identity, ArtifactRef, and required
payload fields while replacing only the reported invalid binding or field. The Gateway does not
truncate those retry semantics to meet the 8 KiB display target. A larger retry example reports
soft projection telemetry and remains subject to the 256 KiB request boundary; it does not persist
caller payload bytes in Run state or authorize raw Evidence, logs, or artifact content there.

Patch and firmware inputs use `ArtifactRef`. The Runtime requires kind, content digest, byte size,
provenance, retention hint, target, and Run binding, then streams the local content to verify its
digest before any mutation begins. Versioned build artifacts also bind provenance and product
version in adjacent digest-bound build metadata. Missing content, cross-target or cross-Run
references, wrong kind, size or provenance mismatch, and tampering fail before Domain execution.

When a mutation result is unknown, `RunEngine` automatically performs one reconcile attempt using
the same durable operation ID and mutation journal. If the read-first recovery converges, execution
continues without another Agent Turn. If it cannot converge, the Runtime returns an Incident with
the affected Effect identity, recovery path, bounded allowed commands, and operator action. Repeated
reconcile calls reuse the same open Incident instead of appending duplicate lifecycle facts.
Explicit `control=reconcile` remains a bounded recovery fallback rather than the normal Agent path.

Recoverable Artifact and domain-preparation Incidents can be retried with `resume`. Any current
Incident can instead be cancelled through `execute kind=control, command=cancel` bound to its
`incident_id`; the Runtime derives a stable cancellation identity from the Run and Incident, so a
transport retry returns the same cancelled Turn without appending another Outcome.

The Operator `runtime_status` projection derives Incident counts, open age, resolution time,
recovery paths, duplicate raises, and unknown policy codes from the persisted Run ledger. It does
not maintain a second Incident state store, and the same metrics survive SQLite restart.

Operator Runtime status retains the anonymous compatibility counters captured before retirement.
The current Runtime exposes them as historical evidence but no longer increments them. No task,
target, payload, credential, or caller identity is recorded. The evidence model is documented in
[Compatibility retirement](compatibility-retirement.md).

Terminal Runs persist one authoritative Run Outcome. The Agent path does not write a Session
Outcome. An operator may explicitly project the redacted governance record from the persisted Run
Outcome; retries cannot create another Run Outcome or alter the Run ledger. Review, approval,
rejection, and promotion remain operator-only operations.

`evidence_attach` is an Operator / CI write to the Evidence seam, not a Run transition command. It
can attach verified local file bytes only while the selected Run is open. Runtime Core resolves the
Run-bound target, verifies the expected digest, stores the bytes content-addressably, and appends an
idempotent `EvidenceAttached` fact. The operation cannot answer a Gate or change phase, Incident,
Effect, or Outcome state, and it is absent from the Agent profile.

## Descriptor direction

Operation descriptors carry transport-independent metadata:

- `exposure`;
- `audience`;
- `cost_hint`;
- `scope_contract`;
- `result_projector`.

Transport tool definitions are projections of descriptors. New evidence kinds should normally be
implemented as internal selector Adapters behind `observe`, not as new top-level Agent tools.

## Release invariants

The release gate verifies the semantic interface in addition to install, upgrade, rollback, and
Replay:

- default tool count is two and `tools/list` stays within 8 KiB;
- ordinary ObservationReceipts and Gate schemas target 4 KiB, while Turns target 8 KiB;
- a projection may exceed its display target to preserve Runtime-owned Observation, Gate,
  Incident, Outcome, or complete DiagnosticReceipt semantics;
- observations do not create Cases;
- unsupported scope and stale freshness models are rejected before collection;
- capability and claim coverage remain explicit;
- diagnostic completion is fail-closed when requested results are not Agent-evaluable;
- Turn display pressure preserves the complete typed Turn; fallback-text compaction preserves
  control bindings, coverage, result/Evidence identities, terminal semantics, and next action;
- durable Receipt compaction preserves diagnostic coverage, truncation, completeness, gaps, and
  Evidence refs or fails diagnostic acceptance closed;
- Agent results do not expose Runtime sequencing mechanics;
- duplicate Gate delivery is idempotent and stale or conflicting delivery is rejected;
- unknown Mutation is automatically reconciled or returned as an Incident with a bounded recovery
  contract;
- Operator Incident metrics are reconstructed from persisted Run events;
- legacy operations are absent and governance operations require the operator profile.

The hermetic [diagnosis-chain qualification](diagnosis-chain-qualification.md) continuously checks
the blocked and recoverable `ObservationRef → diagnosis.acceptance → developer.change → Outcome`
path through the public Agent seam. It runs before the broader test roots in repository validation.
Runtime stability qualification also records representative Gate and terminal `execute` standard
text, structured-content, and combined MCP result bytes. The receipt includes long alarm, log, MDB,
service-tree, clock, and version results so the measurement proves that structured semantics remain
complete while preview values are not repeated in standard text. Correctness remains promotable;
byte-target regressions are secondary warnings only.

Live qualification remains a separate paired AB/BA experiment with a correctness-first release
contract. Historical execute qualification may check out a pinned pre-retirement source for the
baseline arm; the candidate and all current installations use the Agent profile.
The verifier retains the immutable v2.0.1 execute prompt contract by its recorded digest, so its
signed v3 evidence remains independently verifiable without rewriting the evidence schema or Run
records. New runs always use the current prompt contract; unknown digests and any signed-Run prompt
mismatch remain verification failures.

Use `scripts/agent_gateway_ab.py` to run or re-evaluate the qualification. The runner creates a
balanced AB/BA schedule, isolates every Codex home, applies semantic and scope acceptance, and
computes the paired geometric mean plus the one-sided 95% bootstrap upper bound for total tokens,
non-cached input plus output, tool-output bytes, model turns, wall time, and time to the next
actionable Turn. Correctness, semantic acceptance, exact scope, and scenario validity determine the
release `decision`. The six efficiency metrics produce a separate `efficiency_decision` and an
ordered list of `efficiency_warnings`; an authentic efficiency warning does not block promotion.
Missing or non-positive efficiency measurements produce `efficiency_decision=incomplete`: the
correctness decision remains unchanged and does not request more pairs, while verification rejects
the incomplete evidence. The 4 KiB Observation/Gate and 8 KiB Turn targets are display targets,
not qualification thresholds.
Ten valid pairs are the first decision point. A correctness or validity result that is not yet
sufficient expands to twenty and then thirty pairs. Each result records both source commits, the
model and environment fingerprint, thresholds, valid and invalid pairs, both decisions, warnings,
and digests for the schedule, raw metrics, and signed run events used to recompute every result.
Formal qualification pins an isolated `codex-cli 0.150.0` executable. The runner configures the
Target Runtime MCP with `required=true`, so failure to initialize the only Agent-facing Runtime
entry aborts session startup instead of producing a zero-call benchmark run. `--codex` must name
an absolute executable; the environment evidence binds its resolved absolute executable path and SHA-256.
Each run receives an isolated `CODEX_HOME` alongside its isolated `HOME`.
The fixed config disables Codex plugins because the qualification installs only its selected Skills;
this removes plugin-registry network synchronization from the MCP startup window.

```bash
python scripts/agent_gateway_ab.py run \
  --codex /path/to/codex-0.150.0/bin/codex \
  --work-root /path/to/benchmark-work \
  --credentials /path/to/private/credentials.env \
  --attestation-private-key /path/to/private/ab-evidence-signing-key \
  --attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub \
  --model gpt-5.6-sol \
  --scenario execute-source-only \
  --pairs 10 \
  --codex-config 'features.shell_tool=false' \
  --codex-config 'features.plugins=false' \
  --codex-config 'model_provider="cliproxy"' \
  --codex-config 'model_providers.cliproxy.name="CLIProxyAPI"' \
  --codex-config 'model_providers.cliproxy.base_url="http://82.156.104.157/v1"' \
  --codex-config 'model_providers.cliproxy.env_key="CLI_PROXY_API_KEY"' \
  --codex-config 'model_providers.cliproxy.wire_api="responses"' \
  --codex-config 'model_providers.cliproxy.supports_websockets=false'

python scripts/agent_gateway_ab.py verify \
  /path/to/benchmark-work/results-*/summary.json \
  --source-ref <candidate-commit> \
  --attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub
```

For a Skill-only progressive-disclosure comparison, run both variants through the same Agent profile
and the same semantic observation prompt:

The fixed live scope intentionally contains only MDBCTL readiness plus the Drive name, ResourceId,
and presence values needed for the conclusion. Keeping the benchmark question narrow prevents a
long multi-surface target snapshot from dominating a test whose independent variable is Skill
entrypoint disclosure.

The live prompts also distinguish dispatch from execution: a not-yet-dispatched MCP call is not a
failure or retry. In particular, 尚未发出的 MCP 调用不算失败或重试; the Agent waits for the
registered benchmark tool entry and still issues the required actual call. Skill disclosure issues
exactly one `observe`; execute source-only issues its fixed continuation calls. This avoids turning
transient deferred tool-entry resolution into an arm-specific validity failure without accepting a
run that never dispatches the required MCP call. The compatibility arm also treats its standard
“not completed” text as a waiting phase state and reads the same result's structured continuation
contract before deciding that `case_id`, revision, or phase arguments are unavailable.

```bash
python scripts/agent_gateway_ab.py run \
  --codex /path/to/codex-0.150.0/bin/codex \
  --work-root /path/to/benchmark-work \
  --credentials /path/to/private/credentials.env \
  --attestation-private-key /path/to/private/ab-evidence-signing-key \
  --attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub \
  --model gpt-5.6-sol \
  --baseline-ref github/main \
  --scenario skill-disclosure \
  --pairs 10 \
  --codex-config 'features.shell_tool=false' \
  --codex-config 'features.plugins=false' \
  --codex-config 'model_provider="cliproxy"' \
  --codex-config 'model_providers.cliproxy.name="CLIProxyAPI"' \
  --codex-config 'model_providers.cliproxy.base_url="http://82.156.104.157/v1"' \
  --codex-config 'model_providers.cliproxy.env_key="CLI_PROXY_API_KEY"' \
  --codex-config 'model_providers.cliproxy.wire_api="responses"' \
  --codex-config 'model_providers.cliproxy.supports_websockets=false'

python scripts/agent_gateway_ab.py verify \
  /path/to/benchmark-work/results-*/summary.json \
  --scenario skill-disclosure \
  --source-ref <candidate-commit> \
  --baseline-ref github/main \
  --attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub
```

The candidate is acceptable only when the signed evidence has at least ten valid pairs and passes
the scenario's semantic, exact-scope, source-binding, schedule, and validity gates. Efficiency
thresholds remain visible and tamper-checked optimization signals; missing or inconsistent metric
evidence fails verification, while an honestly reported threshold regression does not block
promotion. Because this Skill-only scenario has no side effects, zero-tool dispatch misses remain
signed instead of being selectively rerun: each arm must stay at or above 95% validity, invalid
pairs may not exceed 10%, and candidate validity may not regress by more than 5 percentage points
versus the baseline.
Both arms use identical per-run acceptance checks. A baseline non-noise invalid run counts against
the shared rate and invalid-pair thresholds, allowing measured baseline behavior noise within those
bounds. Only a candidate non-noise invalid run blocks the checkpoint immediately, so a candidate
cannot be promoted by averaging an active semantic, tool, or scope violation into the sample.
Runtime release and execute qualification continue to require zero invalid pairs.
The scenario records its own prompt digest, source commits, schedule, raw metrics, environment,
and signed run evidence. It evaluates Skill disclosure behavior; the default release qualification
remains `execute-source-only`.

Treat each checkpoint as a complete preregistered experiment. `collect_more` is reserved for
insufficient correctness or scenario-validity evidence; an efficiency warning never expands the
sample. If a 10-pair result says `collect_more`, start a new independent run at the full 20-pair
target. If that result is still insufficient, start another new independent run at the full
30-pair target. Never append, merge, or selectively reuse pairs from an earlier checkpoint; verify
and promote only the single complete result directory for the final checkpoint. Reaching the
30-pair checkpoint calculates p95 for every metric even when the validity policy excludes one or
more signed pairs. A missing terminal p95 value fails evidence verification; exceeding its target
adds an efficiency warning. Every signed run also binds the complete checkpoint pair count and
schedule digest, so a 30-pair run cannot be truncated or rebound as a smaller checkpoint.

Every run record carries its tested source commit and a unique execution identity. The runner
signs that record with the qualification key; verification uses a public key held outside the
candidate checkout. The GitHub Release workflow restores that trust root from the
`AB_ATTESTATION_PUBLIC_KEY_BASE64` repository variable managed outside source control, so editing
a run or rebinding an old result to another candidate invalidates the evidence.

For the GitHub Release workflow, package the four verified files as one xz-compressed,
digest-bound asset. The workflow rejects extra members and non-regular files before extraction.
Attach the archive to the draft release for the immutable release tag; workflow dispatch carries
only the asset name and digest, avoiding GitHub's total workflow-input size limit. The release stays
draft until the remote Release Gate passes:

```bash
tar -C /path/to/benchmark-work/results-YYYYMMDD-HHMMSS \
  -cJf agent-gateway-ab-evidence.tar.xz \
  summary.json all_metrics.json schedule.json run_evidence.json
sha256sum agent-gateway-ab-evidence.tar.xz
gh release create v2.0.2 --draft --verify-tag --generate-notes
gh release upload v2.0.2 agent-gateway-ab-evidence.tar.xz
gh workflow run release.yml --ref main \
  -f current_ref=v2.0.2 \
  -f previous_ref=v2.0.1 \
  -f ab_bundle_asset=agent-gateway-ab-evidence.tar.xz \
  -f ab_bundle_sha256="$(sha256sum agent-gateway-ab-evidence.tar.xz | cut -d' ' -f1)" \
  -f promote=true
```

## Recovery coverage

The three supported delivery paths are verified through the same `execute` Interface:

| Delivery path | Normal completion | Process restart | Injected failure and recovery |
| --- | --- | --- | --- |
| source-only | accepted diagnosis, source Gate, terminal source Outcome | resume restores the same unanswered diagnosis or source Gate | failed/cancelled diagnosis or source phase remains terminal and never creates a success Outcome |
| live-patch | accepted diagnosis, source Gate, mutation, fresh verification, terminal Outcome | resume restores the same Gate or running Effect; deferred verification retries without reapplying | unknown mutation reconciles through the same durable journal, including after restart |
| build-upgrade | accepted diagnosis, source Gate, build Gate, upgrade, fresh verification | resume restores either Gate or a running Effect with the same operation identity | interrupted upgrade reconciles through the same durable journal |

## Evolution

The Runtime Core remains the stable kernel. Future capability should deepen the two semantic
operations instead of adding Agent-facing tools:

1. retain explicit old-event upcasters until supported persisted Runs pass their retention or
   migration window;
2. Log Bundle collection now produces a content-bound ArtifactRef, while local index, bounded
   query, and redacted export are separate internal READ_ONLY Domain Packs. Collection remains a
   target-affecting Effect and is not reclassified as read-only;
3. add selector Adapters for D-Bus properties, verified active alarms, and bounded log search only
   from measured development gaps;
4. keep evidence inspection, Replay, governance, and lifecycle automation in the operator/CI plane;
5. keep historical qualification evidence and compatibility telemetry auditable without restoring
   retired writers.

Every new selector must reuse the same ScopeContract, claim grounding, content-addressed source,
freshness semantics, and result budgets. Every new workflow must cover normal completion, process
restart, and injected failure without widening the Agent Interface.
