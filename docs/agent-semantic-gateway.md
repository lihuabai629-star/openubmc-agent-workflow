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

- `operator`: only operations marked `exposure=operator`, including Case inspection, raw Evidence,
  Replay, Session Outcome governance, lifecycle, and Runtime status.

The projections are disjoint. `operator` does not expose Agent or internal Domain execution
operations. The retired `compatibility` value is rejected during Runtime construction.

Set `OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE=operator` only for Operator / CI purposes. Domain
operations remain internal to Runtime Core regardless of the selected transport projection.

## Observation contract

`observe` accepts an immutable target and selector set. The initial selector adapters are
`capability` and `mdb`. An Adapter cannot add evidence surfaces that were not declared in the
query. The serialized scope is limited to 2 KiB, with explicit limits for target, selector IDs,
capability names, selector count, and MDB query count and size.

All selectors needed for one answer belong in one `observe` call. Capability is not a separate
Agent preflight step: the internal Adapter performs capability discovery and the exact MDB reads in
the same observation. Callers split a query only after an explicit incomplete Receipt requests a
narrower scope.

Selector execution is a Runtime-owned bounded plan. Selector IDs and declaration order remain
stable through the Domain Adapter, persisted source, and Receipt. Independent MDB reads may overlap
on one target-scoped lease, with a Runtime maximum of four active reads; the Agent cannot select an
unbounded policy. Lease reuse is bound to target and credential identity, while target epoch changes
invalidate transport sessions before another read.

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
generate a Closeout. The model-visible document is limited to 4 KiB; a result that cannot fit is
returned as an incomplete receipt requesting narrower selectors.

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
semantic state, a small Gate input schema, verified facts, gaps, and a bounded terminal Outcome. It
is limited to 8 KiB; each Gate schema is limited to 4 KiB. The internal advancement limit is fixed
at 64 steps and is not part of the Agent Interface. Exhaustion appears as an
`internal_step_limit` blocker rather than a caller-controlled continuation budget.

Each phase Gate exposes stable `gate_id`, `gate_version`, and schema digest. A response may carry a
submission identity; the Adapter otherwise derives one from the persisted Gate binding. Duplicate
identity plus identical normalized input is idempotent. Reuse with different input, a different
Gate, or a stale version is a conflict. The internal developer Runtime does not require a one-time
secret Gate token.

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
- ObservationReceipt stays within 4 KiB;
- Turn stays within 8 KiB and Gate schemas stay within 4 KiB;
- observations do not create Cases;
- unsupported scope and stale freshness models are rejected before collection;
- capability and claim coverage remain explicit;
- Agent results do not expose Runtime sequencing mechanics;
- duplicate Gate delivery is idempotent and stale or conflicting delivery is rejected;
- unknown Mutation is automatically reconciled or returned as an Incident with a bounded recovery
  contract;
- Operator Incident metrics are reconstructed from persisted Run events;
- legacy operations are absent and governance operations require the operator profile.

Live performance qualification remains a separate paired AB/BA gate because token and wall-time
limits require a fixed model, target snapshot, and sufficient valid pairs. Historical execute
qualification may check out a pinned pre-retirement source for the baseline arm; the candidate and
all current installations use the Agent profile.

Use `scripts/agent_gateway_ab.py` to run or re-evaluate the qualification. The runner creates a
balanced AB/BA schedule, isolates every Codex home, applies semantic and scope acceptance, and
computes the paired geometric mean plus the one-sided 95% bootstrap upper bound for total tokens,
non-cached input plus output, tool-output bytes, model turns, wall time, and time to the next
actionable Turn. Ten valid pairs are the first decision point; an uncertain result expands to
twenty and then thirty pairs. Each result records both source commits, the model and environment
fingerprint, thresholds, valid and invalid pairs, and digests for the schedule, raw metrics, and
the run events used to recompute every promoted metric.

```bash
python scripts/agent_gateway_ab.py run \
  --work-root /path/to/benchmark-work \
  --credentials /path/to/private/credentials.env \
  --attestation-private-key /path/to/private/ab-evidence-signing-key \
  --attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub \
  --model gpt-5.6-sol \
  --scenario execute-source-only \
  --pairs 10 \
  --codex-config 'features.shell_tool=false' \
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

The prompt also distinguishes dispatch from execution: a not-yet-dispatched MCP call is not a
failure or retry. In particular, 尚未发出的 MCP 调用不算失败或重试; the Agent waits for the
registered benchmark tool entry and still issues exactly one actual `observe` call. This avoids
turning transient tool-entry resolution into an arm-specific validity failure.

```bash
python scripts/agent_gateway_ab.py run \
  --work-root /path/to/benchmark-work \
  --credentials /path/to/private/credentials.env \
  --attestation-private-key /path/to/private/ab-evidence-signing-key \
  --attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub \
  --model gpt-5.6-sol \
  --baseline-ref github/main \
  --scenario skill-disclosure \
  --pairs 10 \
  --codex-config 'features.shell_tool=false' \
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

The candidate is acceptable only when semantic and exact-scope checks pass for the paired metric
sample, there are at least ten valid pairs, and every bounded regression metric passes. Because
this Skill-only scenario has no side effects, zero-tool dispatch misses remain signed instead of
being selectively rerun: each arm must stay at or above 95% validity, invalid pairs may not exceed
10%, and candidate validity may not regress by more than 5 percentage points versus the baseline.
Both arms use identical per-run acceptance checks. A baseline non-noise invalid run counts against
the shared rate and invalid-pair thresholds, allowing measured baseline behavior noise within those
bounds. Only a candidate non-noise invalid run blocks the checkpoint immediately, so a candidate
cannot be promoted by averaging an active semantic, tool, or scope violation into the sample.
Runtime release and execute qualification continue to require zero invalid pairs.
The scenario records its own prompt digest, source commits, schedule, raw metrics, environment,
and signed run evidence. It evaluates Skill disclosure behavior; the default release qualification
remains `execute-source-only`.

Treat each checkpoint as a complete preregistered experiment. If a 10-pair result says
`collect_more`, start a new independent run at the full 20-pair target. If that result is still
uncertain, start another new independent run at the full 30-pair target. Never append, merge, or
selectively reuse pairs from an earlier checkpoint; verify and promote only the single complete
result directory for the final checkpoint. Reaching the 30-pair checkpoint activates the p95 bound
for every metric even when the validity policy excludes one or more signed pairs: calculate p95 from
the retained valid paired sample, and fail verification when any terminal p95 value is missing.
Every signed run also binds the complete checkpoint pair count and schedule digest, so a 30-pair run
cannot be truncated or rebound as a smaller checkpoint.

Every run record carries its tested source commit and a unique execution identity. The runner
signs that record with the qualification key; verification uses a public key held outside the
candidate checkout. The GitHub Release workflow restores that trust root from the
`AB_ATTESTATION_PUBLIC_KEY_BASE64` repository variable managed outside source control, so editing
a run or rebinding an old result to another candidate invalidates the evidence.

For the GitHub Release workflow, package the four verified files as one xz-compressed,
digest-bound input. The workflow rejects extra members and non-regular files before extraction.
High-ratio xz compression keeps the evidence, including the run events, within the supported
workflow-dispatch input:

```bash
tar -C /path/to/benchmark-work/results-YYYYMMDD-HHMMSS \
  -cJf agent-gateway-ab-evidence.tar.xz \
  summary.json all_metrics.json schedule.json run_evidence.json
sha256sum agent-gateway-ab-evidence.tar.xz
base64 -w0 agent-gateway-ab-evidence.tar.xz
```

## Recovery coverage

The three supported delivery paths are verified through the same `execute` Interface:

| Delivery path | Normal completion | Process restart | Injected failure and recovery |
| --- | --- | --- | --- |
| source-only | terminal source Outcome | resume phase Gate from persisted Run | failed/cancelled phase remains terminal and never creates a success Outcome |
| live-patch | mutation, fresh verification, terminal Outcome | resume before or after the phase Gate; deferred verification retries without reapplying | unknown mutation reconciles through the same durable journal, including after restart |
| build-upgrade | source Gate, build Gate, upgrade, fresh verification | resume either Gate or a running Effect with the same operation identity | interrupted upgrade reconciles through the same durable journal |

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
