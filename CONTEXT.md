# openUBMC Agent Workflow Context

This repository defines a single domain context for agent-assisted openUBMC diagnosis,
development, build, delivery, and target execution. The stable product boundary is a durable
Runtime Core with a small Agent Interface and a separate governance plane.

## Product boundary

The workflow is an openUBMC execution substrate, not a general-purpose agent orchestrator. It owns
the facts that must remain correct when a model retries, a process restarts, or a target operation
has an unknown outcome:

- target observations and their provenance;
- Run, Gate, Incident, Effect, and Outcome state;
- mutation identity, recovery, verification, and rollback authorization;
- bounded Agent projections and operator governance records.

Skills and models may propose intent and supply Gate input. They do not own durable state,
operation identities, retry safety, target fencing, or terminal success.

## Ubiquitous language

| Term | Meaning |
| --- | --- |
| **Runtime Core** | The single durable authority for observation provenance, Run state, external Effect safety, and terminal Outcome formation. |
| **Agent Interface** | The model-facing `observe` and `execute` operations, including their budgets, invariants, and error semantics. |
| **Operator / CI Plane** | The non-Agent governance interface for raw Evidence, Replay, lifecycle, Runtime status, and Session Outcome review. |
| **Adapter** | A transport or domain-specific implementation at a seam. MCP and CLI are transport Adapters; SSH, Redfish, build, patch, and upgrade integrations are Domain Adapters. |
| **ObservationQuery** | A typed, bounded request for exact read-only facts about an immutable target scope. |
| **ObservationResult** | The Runtime-owned result of an ObservationQuery before Agent projection. |
| **ObservationReceipt** | A bounded Agent projection of an ObservationResult. It is not Run state or mutation evidence. |
| **ObservationRef** | A stable handle and digest that lets the Runtime reconstruct and validate persisted observation content. |
| **ArtifactRef** | A bounded handle, digest, type, size, provenance, retention, target, and Run binding for content stored outside Run state. |
| **Run** | One durable execution of a pinned workflow definition for a target and intent. |
| **RunCommand** | A typed request to start, respond to, resume, or control a Run. |
| **RunEngine** | The only Module allowed to commit Run, Gate, Incident, Effect-reference, and Outcome transitions. |
| **WorkflowDefinitions** | Versioned deterministic workflow structure and transition rules. It performs no external I/O and does not write Run state. |
| **DomainExecutor** | The Module that invokes registered Domain Adapters and returns typed Domain results using stable Effect identity. |
| **Turn** | A bounded Agent projection at the next actionable Gate, Incident, running reattach point, or terminal Outcome. It is not the source of truth. |
| **Gate** | A durable, versioned request for external input with a one-time submission protocol. |
| **GateSubmission** | An audited response bound to one Run, Gate identity, Gate version, schema digest, submission identity, and input digest. Actor and time are derived by the Adapter or Runtime. |
| **Blocker** | A bounded reason the current call cannot advance safely. A Blocker is not automatically a durable Incident. |
| **Incident** | A durable state requiring operator attention after automatic execution cannot continue safely. |
| **Effect** | A potentially failing or repeat-delivered interaction with a domain or target. Model calls become Effects if the Runtime manages them in the future. |
| **Mutation** | An Effect that can change target, build, repository, package, or deployment state. |
| **MutationJournal** | The durable authority for mutation identity, effect-start status, result, verification, unknown recovery, and rollback state. |
| **Outcome** | The sole terminal fact for a Run. Success requires fresh Runtime-grounded verification. |
| **Session Outcome** | A governance projection generated from a terminal Outcome for review and possible promotion. |
| **Target epoch** | A monotonic identity for the observed target state used to reject stale verification and unsafe replay. |
| **Reconcile** | Read-first recovery of an unknown Effect using the same durable identity; it never silently creates a replacement operation. |
| **PlanResolver** | An isolated Runtime-internal experimental Module that records one model planning Effect, validates a bounded proposal, and freezes an inert PlanRevision. It is not composed into production Run execution. |
| **PlanProposal** | Versioned model output bound to one invocation and Run. It has no authority until Runtime validation accepts its bounded IR. |
| **PlanRevision** | An immutable, version-pinned validated proposal. It remains inert data; only RunEngine could ever pin and interpret it. |

## Ownership rules

This table is the target authority model. Agent writes enter only through `observe` and `execute`;
historical event upcasters are read paths and cannot commit new Run transitions.

| Fact or transition | Sole owner |
| --- | --- |
| Observation collection, assurance, and source persistence | `ObservationEngine` |
| Run, Gate, Incident, and Outcome transitions | `RunEngine` |
| Workflow structure and version pinning rules | `WorkflowDefinitions` |
| Domain Adapter selection and invocation | `DomainExecutor` |
| Mutation execution and recovery truth | `MutationJournal` |
| Artifact bytes, digest verification, retention, and access policy | `ArtifactStore` |
| Agent-visible redaction and bounded projection | `AgentGateway` |
| Review, approval, promotion, and lifecycle governance | Operator / CI Plane |
| Experimental model invocation, proposal validation, and inert revision persistence | `PlanResolver` |

No second Module may independently write the same fact. Historical readers may translate old
records into current projections, but they cannot accept old commands or create new facts.

## Domain invariants

- The default Agent Interface remains `observe` and `execute`.
- The Runtime exposes one developer-friendly default behavior. Assurance selection is automatic
  and is not an Agent input.
- `observe` is read-only and never doubles as Run-status polling.
- `execute` advances to the next semantic yield; transport acknowledgements are not Agent domain
  concepts.
- External Effects are designed for at-least-once delivery. The Runtime does not claim
  exactly-once target mutation.
- An Effect that may have started and lacks a proven result becomes `unknown` and fails closed.
- Unknown recovery uses the same Effect identity, begins with read-only inspection, and cannot
  produce success without fresh verification.
- Large Evidence, logs, build products, patches, and observation sources travel by ArtifactRef,
  not by embedding bytes in Run state or Agent context.
- Runtime-managed ArtifactRefs use content-addressed handles and persistent lifecycle metadata;
  redaction always derives new bytes and a new digest, while GC removes content only after its
  final retained reference expires or is explicitly released.
- Workflow definitions are version-pinned for each Run.
- MCP, CLI, queues, and future durable engines remain replaceable Adapters; their vocabulary does
  not enter the domain model.
- Outbox, Inbox, remote Workers, and distributed fencing are introduced only when an actual
  cross-process or active-active seam exists.
- Model-planning output is always a proposal. The isolated prototype cannot answer a Gate, invoke a
  Domain Adapter, authorize mutation, write Run state, or form an Outcome.

## Related decisions

- [ADR-0001: Runtime Core and semantic Agent Interface](docs/adr/0001-runtime-core-and-semantic-agent-interface.md)
- [ADR-0002: Single Run authority and external Effect recovery](docs/adr/0002-single-run-authority-and-effect-recovery.md)
- [ADR-0003: Turn, Gate, Artifact, and distribution boundaries](docs/adr/0003-turn-gate-artifact-and-distribution-boundaries.md)
- [ADR-0004: Developer-friendly default execution and Gate submissions](docs/adr/0004-developer-default-and-gate-submissions.md)
- [Architecture arbitration](docs/workflow-architecture-arbitration.md)
- [Market workflow design research](docs/workflow-design-market-research.md)
- [Evolution roadmap](docs/workflow-evolution-roadmap.md)
- [Domain Pack authoring contract](docs/domain-pack-authoring.md)
