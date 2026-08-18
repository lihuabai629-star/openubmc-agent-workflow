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

- `compatibility`: the previous Runtime operation set for migration and performance comparison;
- `operator`: Case inspection, raw Evidence, Replay, Session Outcome governance, lifecycle, and
  Runtime status.

Set `OPENUBMC_TARGET_RUNTIME_INTERFACE_PROFILE` to `compatibility` or `operator` only for those
purposes. Domain operations remain available inside the Runtime Core regardless of the selected
transport projection.

## Observation contract

`observe` accepts an immutable target and selector set. The initial selector adapters are
`capability` and `mdb`. An Adapter cannot add evidence surfaces that were not declared in the
query.

Freshness is a time property. The Agent Interface currently accepts only live evidence with
`max_age_seconds=0`; the old `freshness` and `log-file` profiles are rejected because profiles
describe evidence scope, not evidence age.

An `ObservationReceipt` contains semantic values, tri-state capability results
(`available | unavailable | not_checked`), coverage, observation time, target identity when
available, grounded claims, and receipt-local evidence references. It does not open a Case or
generate a Closeout. The model-visible document is limited to 4 KiB; a result that cannot fit is
returned as an incomplete receipt requesting narrower selectors.

## Execution contract

`execute` accepts four action kinds:

- `start`: create and advance a Run;
- `respond`: satisfy the current phase Gate and continue;
- `resume`: continue an existing Run;
- `control`: continue, reconcile, or cancel at a phase Gate.

The Runtime Core advances deterministic steps internally and returns a `Turn` only at a real Gate,
blocker, or terminal Outcome. A Turn contains the Run ID, semantic state, a small Gate input schema,
verified facts, gaps, and a bounded terminal Outcome. It is limited to 8 KiB; each Gate schema is
limited to 2 KiB.

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

## Evolution

The next selector adapters should be added in this order: D-Bus properties, active alarms, and
bounded log search. Each must reuse the same ScopeContract, claim grounding, freshness, and result
budget rules.

The next execution proofs are the three vertical workflows:

1. diagnosis to source-only Outcome;
2. diagnosis to Live Patch, fresh verification, and recovery;
3. diagnosis to build, upgrade, fresh verification, and Outcome.

Each proof must cover normal completion, process restart, and injected failure without widening the
Agent Interface.
