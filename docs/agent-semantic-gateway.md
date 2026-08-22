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

Two explicit profiles preserve non-default access:

- `compatibility`: legacy domain and workflow operations marked `exposure=compatibility`, retained
  for migration and performance comparison;
- `operator`: only operations marked `exposure=operator`, including Case inspection, raw Evidence,
  Replay, Session Outcome governance, lifecycle, and Runtime status.

The projections are disjoint. `compatibility` cannot read Evidence, Replay, Session Outcomes,
Cases, or Runtime status, while `operator` does not expose legacy domain execution operations.

Set `OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE` to `compatibility` or `operator` only for those
purposes. Domain operations remain available inside the Runtime Core regardless of the selected
transport projection.

## Observation contract

`observe` accepts an immutable target and selector set. The initial selector adapters are
`capability` and `mdb`. An Adapter cannot add evidence surfaces that were not declared in the
query. The serialized scope is limited to 2 KiB, with explicit limits for target, selector IDs,
capability names, selector count, and MDB query count and size.

All selectors needed for one answer belong in one `observe` call. Capability is not a separate
Agent preflight step: the internal Adapter performs capability discovery and the exact MDB reads in
the same observation. Callers split a query only after an explicit incomplete Receipt requests a
narrower scope.

Freshness is a time property. The Agent Interface currently accepts only live evidence with
`max_age_seconds=0`; the old `freshness` and `log-file` profiles are rejected because profiles
describe evidence scope, not evidence age.

The Runtime owns one automatic observation policy. It first performs the exact read and upgrades to
a scope-preserving assurance pass only when coverage contains `not_checked` values or freshness
cannot be established. The Adapter receives the prior observation and can reuse already collected
MDB values. If no precise assurance Adapter exists, the Runtime retains the bounded result and its
explicit gaps. Legacy `assurance` input remains accepted during migration but is normalized to this
single policy and is not returned in the Agent projection.

An `ObservationReceipt` contains semantic values, tri-state capability results
(`available | unavailable | not_checked`), coverage, observation time, target identity when
available, grounded claims, and receipt-local evidence references. It does not open a Case or
generate a Closeout. The model-visible document is limited to 4 KiB; a result that cannot fit is
returned as an incomplete receipt requesting narrower selectors.

The redacted raw observation is persisted in the Runtime Core as a content-addressed source. A
complete Receipt carries an `ObservationRef` and can seed `execute(kind=start)`. The Runtime
reconstructs the observation, verifies its digest and target scope, and then uses it as the first
diagnostic evidence, so it does not recollect the same evidence and remains reusable after a
process restart. Validation also binds the persisted scope digest, observation time, target
metadata, and 15-minute reuse window before a Run is opened. Complete legacy Receipts remain a
compatibility input; modified, expired, or incomplete content cannot seed a Run.

## Execution contract

`execute` accepts four action kinds:

- `start`: create and advance a Run;
- `respond`: satisfy the current phase Gate and continue;
- `resume`: continue an existing Run;
- `control`: compatibility commands for continue, reconcile, or cancel at a phase Gate.

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
the affected Effect identity. Explicit `control=reconcile` remains as a compatibility and operator
fallback rather than the normal Agent path.

Recoverable Artifact and domain-preparation Incidents can be retried with `resume`. Any current
Incident can instead be cancelled through `execute kind=control, command=cancel` bound to its
`incident_id`; the Runtime derives a stable cancellation identity from the Run and Incident, so a
transport retry returns the same cancelled Turn without appending another Outcome.

Compatibility retirement is driven by persistent anonymous operation and feature counters exposed
through Runtime status. SQLite-backed Runtime instances share the counters across processes; no
task, target, payload, credential, or caller identity is recorded. The deletion order and zero-use
window are defined in [Compatibility retirement](compatibility-retirement.md).

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
- unknown Mutation is automatically reconciled or returned as an Incident;
- legacy and governance operations require explicit profiles.

Live performance qualification remains a separate paired AB/BA gate because token and wall-time
limits require a fixed model, target snapshot, and sufficient valid pairs. The compatibility
profile is retained as the baseline until the semantic interface meets that gate consistently.

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

1. use persistent compatibility telemetry to retire the remaining legacy writers while retaining
   explicit old-event upcasters;
2. add the first READ_ONLY Domain Pack only from measured development demand; the shared contract
   and conformance suite are already extracted from Live Patch and Upgrade;
3. add selector Adapters for D-Bus properties, verified active alarms, and bounded log search only
   from measured development gaps;
4. keep evidence inspection, Replay, governance, and lifecycle automation in the operator/CI plane;
5. retire the compatibility profile only after migration telemetry and paired AB/BA qualification
   show that the semantic Interface is both cheaper and at least as reliable.

Every new selector must reuse the same ScopeContract, claim grounding, content-addressed source,
freshness semantics, and result budgets. Every new workflow must cover normal completion, process
restart, and injected failure without widening the Agent Interface.
