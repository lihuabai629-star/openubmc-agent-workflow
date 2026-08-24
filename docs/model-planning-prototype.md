# Runtime-internal model planning prototype

Date: 2026-08-24
Status: isolated experiment; not composed into the production Runtime path
Issue: [#65](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/65)

## Verdict

The state and persistence model is viable, but the experiment does not yet demonstrate product
leverage over pinned static `WorkflowDefinitions`. Keep the implementation isolated behind the
internal `PlanResolver.resolve()` Interface. Do not inject it into `RunEngine`, add Agent
operations, or claim production adoption.

The deterministic paired evaluation preserves all architecture invariants and runs both paths on
six equivalent tasks. The static resolver and isolated candidate each produce six valid pinned
plans, but only the candidate requires a model call for every task. A separate containment corpus
rejects four invalid proposals with zero false accepts. The experiment therefore shows containment
but no plan-validity advantage, so its verdict is `isolate` rather than `advance`.

Run the evaluation with:

```bash
python scripts/model_planning_evaluation.py
```

## Interface and authority boundary

`PlanResolver` is a deep Runtime-internal Module with one Interface method:

```python
decision = resolver.resolve(
    PlanningRequest(
        run_id=run_id,
        slot_id="primary",
        generation=1,
        planning_input=planning_input,
    )
)
```

The Module derives a stable invocation identity from `run_id`, `slot_id`, and `generation`. The
input digest binds the typed planning input, complete provider/model configuration, bounded IR
version, and validation policy. Reusing the identity with the same digest returns or reconciles the
existing record. Reusing it with another digest conflicts.

The Module owns only:

- `ModelInvocationRecord` persistence and recovery;
- `PlanProposal` decoding, binding, validation, and rejection;
- immutable, content-addressed `PlanRevision` creation.

It receives no `RunDriver`, `DomainExecutor`, Gate writer, Incident writer, Mutation authority, or
Outcome writer. A proposal therefore cannot change Run facts, call a Domain Adapter, authorize a
mutation, answer a Gate, or declare success. Existing tests hold a real Run at its current Gate,
then pass the Gate and execute a mutation while withholding the required fresh target epoch. The
accepted revision cannot cause either transition or form an Outcome.

The Agent Interface remains exactly `observe` and `execute`. The model Provider is a true-external
Seam represented by a narrow Adapter protocol and a deterministic fake used by the experiment.
The repository is a local-substitutable Seam with in-memory and SQLite Adapters. Execution remains
local and single-process.

## Contracts

### ModelInvocationRecord

The record pins:

- schema and record version;
- derived invocation identity, Run, planning slot, and generation;
- input, provider configuration, and policy digests;
- provider, Adapter version, model revision, prompt-template version, parameters, timeout, and
  output budget;
- `non_deterministic` Effect classification;
- `running`, `unknown`, `succeeded`, `rejected`, or `failed` status;
- result digest and PlanRevision identity on success;
- bounded error code/message on unknown, rejection, or failure.

Each status admits only its own evidence shape: `running` has neither result nor error, terminal
non-success states have an error and no result, and `succeeded` has a result/revision pair with no
error. The revision identity is derived from the proposal digest, and replay validates the complete
record/revision settlement tuple.

The record is persisted before provider dispatch. A timeout after dispatch becomes `unknown`.
Another call to `resolve()` reconciles the same identity; it never silently creates a replacement
invocation.

### PlanProposal

The typed proposal is bound to the invocation identity, Run, input digest, full provider
configuration, provider configuration digest, proposal status, result nodes, and error fields.
Provider output that claims another binding is rejected.

The bounded IR supports only:

- registered action references;
- sequence;
- choice;
- bounded parallel;
- bounded repeat;
- bounded timer;
- registered Gate-schema reference;
- version-pinned subflow reference;
- compensation-only action links.

The default policy limits serialized output to 32 KiB, nodes to 32, depth to 8, parallel width to
4, repeat count to 3, each timer to 300 seconds, and worst-case expanded steps to 64. Unknown
actions, unknown node kinds, invalid or cyclic references, unreachable nodes, unpinned subflows,
invalid compensation links, and budget overflow are rejected. There is no Outcome, Incident,
script, shell, credential, authorization, or mutation instruction.

### PlanRevision

An accepted proposal is frozen as a `bounded-plan-ir/v1` revision. The revision pins its schema
version, Run, slot, generation, source invocation, input/provider/policy/proposal digests, complete
proposal result, status, and empty error fields. SQLite restart and Replay resolve the same revision
without another model call.

## Deterministic evidence

The CI Adapter requires no network, credentials, or external model. It scripts provider success,
failure, timeout, unknown, and reconcile results. Behavior coverage includes:

- successful validation and revision freeze;
- schema, action, construct, reference, and budget rejection;
- same-identity replay and different-input conflict;
- timeout-to-unknown and same-identity reconcile;
- first-terminal-writer settlement so a late unknown cannot erase an accepted revision;
- SQLite restart for both accepted and unknown invocations;
- persisted revision rejection when nested proposal bindings contradict the revision;
- complete bounded IR acceptance;
- unchanged `observe`/`execute` exposure;
- proof that an accepted revision cannot bypass a Gate or fresh terminal verification.

The paired evaluation compares six equivalent planning tasks using exact expected semantic step
sequences. Its deterministic fake planner derives proposals from the actual objective and rejects
an unrelated objective in an input-sensitivity control. Static workflows and isolated model
planning each produce a valid pinned plan for all six tasks, while the candidate adds six model
calls and shows no validity improvement. Four deliberately invalid outputs are evaluated
separately and all are rejected. This is useful containment evidence, not adoption evidence.

## Adoption gate

Production composition requires a later experiment that demonstrates at least one of:

- fewer model-visible turns to the next actionable Gate on real openUBMC tasks;
- fewer plan defects than the current Skill plus static-workflow path;
- useful handling of a workflow gap that cannot be expressed economically as a static definition.

That experiment must also prove:

- no duplicate provider invocation for one identity;
- provider request identity and reconcile support under timeout and restart;
- zero Gate bypasses, mutation-authority bypasses, and false terminal successes;
- no regression in existing `observe`/`execute` behavior, budgets, A/B quality, or release
  qualification;
- bounded storage, latency, token, and provider cost;
- a pinned fallback static workflow for every experiment case.

Until those measurements exist, `RunEngine` remains unmodified and static `WorkflowDefinitions`
remain the production planning authority.

## Removal path

Because the Module is not composed into the production Runtime, rollback is deletion of
`model_planning.py`, its tests, the evaluation script, and this documentation. No Agent contract,
Run event, release lock, tag, or migration changes are required. If a future experiment persists
records in a supported environment, retain versioned SQLite readers until that explicit retention
window ends.
