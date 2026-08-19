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
  ├─ WorkflowKernel and Run ledger
  ├─ Target leases and domain adapters
  ├─ EvidenceStore and Acceptance
  └─ MutationJournal, reconcile, and rollback

Operator / CI profile
  ├─ raw Evidence and Case inspection
  ├─ Replay
  ├─ Session Outcome review and promotion
  └─ lifecycle and Runtime status
```

The AgentGateway is the deep Module. MCP and CLI are transport Adapters at the same seam. Runtime
implementation concepts such as Case revision, workflow attempts, phase records, evidence offsets,
and continuation operation names are not part of the Agent Interface.

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

`assurance=auto` first performs the fast exact observation. It upgrades to the assured path only
when coverage contains `not_checked` values or freshness cannot be established. The assured
Adapter receives the prior observation and reuses already collected MDB values, adding only the
required assurance pass. `assurance=assured` fails closed when no scope-preserving Adapter exists;
`auto` retains the fast incomplete Receipt in that situation.

An `ObservationReceipt` contains semantic values, tri-state capability results
(`available | unavailable | not_checked`), coverage, observation time, target identity when
available, grounded claims, and receipt-local evidence references. It does not open a Case or
generate a Closeout. The model-visible document is limited to 4 KiB; a result that cannot fit is
returned as an incomplete receipt requesting narrower selectors.

The redacted raw observation is persisted in the Runtime Core as a content-addressed source. A
complete Receipt carries that source reference and can seed `execute(kind=start)`. The Runtime
reconstructs and verifies the Receipt from the source before using the observation as the first
diagnostic evidence, so it does not recollect the same evidence and remains reusable after a
process restart. Incomplete or modified Receipts cannot seed a Run.

## Execution contract

`execute` accepts four action kinds:

- `start`: create and advance a Run;
- `respond`: satisfy the current phase Gate and continue;
- `resume`: continue an existing Run;
- `control`: continue, reconcile, or cancel at a phase Gate.

The Runtime Core advances deterministic steps internally and returns a `Turn` only at a real Gate,
blocker, or terminal Outcome. A Turn contains the Run ID, semantic state, a small Gate input schema,
verified facts, gaps, and a bounded terminal Outcome. It is limited to 8 KiB; each Gate schema is
limited to 2 KiB. The internal advancement limit is fixed at 64 steps and is not part of the Agent
Interface. Exhaustion appears as an `internal_step_limit` blocker rather than a caller-controlled
continuation budget.

`control=reconcile` is distinct from ordinary continuation. It finds the latest unknown mutation,
restores the persisted domain arguments, retries with the same durable operation ID and mutation
journal, and continues the Run only after the mutation result is known. This prevents an
interrupted Live Patch or upgrade from being repeated under a new identity.

Terminal Runs automatically create a redacted Session Outcome record. Review, approval, rejection,
and promotion remain operator-only operations.

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
- Turn stays within 8 KiB and Gate schemas stay within 2 KiB;
- observations do not create Cases;
- unsupported scope and stale freshness models fail closed;
- capability and claim coverage remain explicit;
- Agent results do not expose Runtime sequencing mechanics;
- legacy and governance operations require explicit profiles.

Live performance qualification remains a separate paired AB/BA gate because token and wall-time
limits require a fixed model, target snapshot, and sufficient valid pairs. The compatibility
profile is retained as the baseline until the semantic interface meets that gate consistently.

Use `scripts/agent_gateway_ab.py` to run or re-evaluate the qualification. The runner creates a
balanced AB/BA schedule, isolates every Codex home, applies semantic and scope acceptance, and
computes the paired geometric mean plus the one-sided 95% bootstrap upper bound for total tokens,
non-cached input plus output, and wall time. Ten valid pairs are the first decision point; an
uncertain result expands to twenty and then thirty pairs.

```bash
python scripts/agent_gateway_ab.py run \
  --work-root /path/to/benchmark-work \
  --credentials /path/to/private/credentials.env \
  --model <fixed-model> \
  --pairs 10

python scripts/agent_gateway_ab.py analyze \
  /path/to/benchmark-work/results-*/all_metrics.json
```

## Recovery coverage

The three supported delivery paths are verified through the same `execute` Interface:

| Delivery path | Normal completion | Process restart | Injected failure and recovery |
| --- | --- | --- | --- |
| source-only | terminal source Outcome | resume phase Gate from persisted Run | failed/cancelled phase remains terminal and never creates a success Outcome |
| live-patch | mutation, fresh verification, terminal Outcome | resume before or after the phase Gate | unknown mutation reconciles through the same durable journal, including after restart |
| build-upgrade | source Gate, build Gate, upgrade, fresh verification | resume either Gate from persisted Run | interrupted upgrade reconciles through the same durable journal |

## Evolution

The Runtime Core remains the stable kernel. Future capability should deepen the two semantic
operations instead of adding Agent-facing tools:

1. add selector Adapters for D-Bus properties, verified active alarms, and bounded log search;
2. add typed workflow intents and Gate schemas behind `execute` without exposing Runtime sequencing;
3. evolve assurance into policy-driven freshness and identity checks while preserving exact scope;
4. keep evidence inspection, Replay, governance, and lifecycle automation in the operator/CI plane;
5. retire the compatibility profile only after migration telemetry and paired AB/BA qualification
   show that the semantic Interface is both cheaper and at least as reliable.

Every new selector must reuse the same ScopeContract, claim grounding, content-addressed source,
freshness semantics, and result budgets. Every new workflow must cover normal completion, process
restart, and injected failure without widening the Agent Interface.
