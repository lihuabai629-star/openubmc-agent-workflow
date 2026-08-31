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
| **DiagnosticRequestPlan** | One Runtime-internal normalized representation of accepted diagnostic result identities, shared by scope validation and durable receipt materialization. |
| **ArtifactRef** | A bounded handle, digest, type, size, provenance, retention, target, and Run binding for content stored outside Run state. |
| **Recovery Artifact** | An independently identified firmware package available before a target Mutation starts. Its absolute path, digest, size, and version are bound to Run Evidence; it is not applied automatically. |
| **Run** | One durable execution of a pinned workflow definition for a target and intent. |
| **RunCommand** | A typed request to start, respond to, resume, or control a Run. |
| **RunEngine** | The only Module allowed to commit Run, Gate, Incident, Effect-reference, and Outcome transitions. |
| **WorkflowDefinitions** | Versioned deterministic workflow structure and transition rules. It performs no external I/O and does not write Run state. |
| **DomainExecutor** | The Module that invokes registered Domain Adapters and returns typed Domain results using stable Effect identity. |
| **Turn** | An Agent projection at the next actionable Gate, Incident, running reattach point, or terminal Outcome. It targets a bounded display size but preserves Runtime-owned control semantics when that target is exceeded. It is not the source of truth. |
| **Gate** | A durable, versioned request for external input with a one-time submission protocol. |
| **GateSubmission** | An audited response bound to one Run, Gate identity, Gate version, schema digest, submission identity, and input digest. Actor and time are derived by the Adapter or Runtime. |
| **Blocker** | A bounded reason the current call cannot advance safely. A Blocker is not automatically a durable Incident. |
| **Incident** | A durable state requiring operator attention after automatic execution cannot continue safely. |
| **Effect** | A potentially failing or repeat-delivered interaction with a domain or target. Model calls become Effects if the Runtime manages them in the future. |
| **Mutation** | An Effect that can change target, build, repository, package, or deployment state. |
| **MutationJournal** | The durable authority for mutation identity, effect-start status, result, verification, unknown recovery, and rollback state. |
| **Outcome** | The sole terminal fact for a Run. Success requires fresh Runtime-grounded verification. |
| **Validation Readiness** | A durable phase classification that separates dependency availability, official UT start/result, compiler reach/result, and supplementary checks. It never promotes source-only evidence into package or runtime success. |
| **Hardware Coverage** | The required and observed protocol/device set bound to target Evidence. Coverage is protocol-specific; SATA/SAS observations do not prove NVMe behavior. |
| **Session Outcome** | A governance projection generated from a terminal Outcome for review and possible promotion. |
| **Target epoch** | A monotonic identity for the observed target state used to reject stale verification and unsafe replay. |
| **Reconcile** | Read-first recovery of an unknown Effect using the same durable identity; it never silently creates a replacement operation. |
| **PlanResolver** | An isolated Runtime-internal experimental Module that records one model planning Effect, validates a bounded proposal, and freezes an inert PlanRevision. It is not composed into production Run execution. |
| **PlanProposal** | Versioned model output bound to one invocation and Run. It has no authority until Runtime validation accepts its bounded IR. |
| **PlanRevision** | An immutable, version-pinned validated proposal. It remains inert data; only RunEngine could ever pin and interpret it. |
| **Final source** | The exact immutable commit selected to undergo fresh release qualification before a lock-only commit is generated. Mutable `main` is not a Final source until explicitly selected. |
| **Planned release** | A version selected for future qualification before a Final source and lock-only child exist. It is not yet a Release candidate. |
| **Release candidate** | A qualified Final source and its lock-only child considered for an immutable version tag. It is not a Release until the tag and publication record exist. |
| **Superseded unpublished candidate** | A Release candidate that was never tagged and is no longer eligible for publication because later canonical changes require a newly qualified Final source. |
| **Historical release snapshot** | Release identity retained on mutable `main` or in audit evidence for verification. It describes an earlier source and never proves the identity of the current development tree. |
| **Operational readiness** | An installer projection that says the installed Runtime, Agent transport, execution engine, and credentials can serve the configured workflow. It does not claim Release identity. |
| **Release identity verification** | Verification that a managed immutable source matches its recorded commit, release lock, Runtime, Skills, and source-tree identity. It proves source identity, not by itself that a tag was published. |
| **Release trust mode** | The installer classification of a source as linked development, unverified managed source, or verified immutable source. |
| **Evaluation readiness** | An Operator / CI Plane projection requiring installation consistency, Operational readiness, and Release identity verification before formal qualification evidence is accepted. |
| **Product Closeout Qualification** | An Operator / CI Plane verification of Runtime continuity, diagnosis, source, official UT, build, ArtifactRef identity, Recovery Artifact identity, upgrade, freshness, and exact hardware coverage. It does not write Run state. |
| **Historical product validation** | A verified reconstruction of original product evidence that predates the current Runtime identity. It remains non-promotable as a fresh Runtime closeout. |
| **Fresh Runtime product closeout** | A Product Closeout Qualification bound to a fresh Run, terminal Outcome, exact source and ArtifactRef identity, an independently identified Recovery Artifact, successful upgrade, freshness, and required Hardware Coverage. |
| **Maintenance checkpoint** | A repository-level qualification decision covering Runtime correctness, supported clients, evaluation isolation, and MCP lifecycle closeout. It does not imply a Fresh Runtime product closeout. |
| **Formal Codex run** | A Codex-owned qualification or release run whose task, session, direct parent process, immutable source, model/Codex identity, Runtime state root, and lifecycle root are explicit and auditable. Its MCP lifecycle evidence is promotable only after task closeout proves no active request or owned process remains live. |
| **DiagnosticReceiptRef** | A digest-bound terminal projection for an unchanged complete DiagnosticReceipt already shown within the same task. The durable full receipt remains Runtime-owned and reconstructable. |

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
| Diagnostic Evidence sanitization, completion semantics, and durable `DiagnosticReceipt` formation | Runtime Core |
| Diagnostic result identity planning and accepted-scope counting | `DiagnosticRequestPlan` |
| Validation Readiness and Hardware Coverage normalization | `RunEngine` |
| Final Agent projection and soft display-budget compaction | `AgentGateway` |
| Product-closeout evidence verification and maintenance checkpoint aggregation | Operator / CI Plane |
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
- [ADR-0007: Soft Agent projection budget](docs/adr/0007-soft-agent-projection-budget.md)
- [Architecture arbitration](docs/workflow-architecture-arbitration.md)
- [Market workflow design research](docs/workflow-design-market-research.md)
- [Evolution roadmap](docs/workflow-evolution-roadmap.md)
- [Domain Pack authoring contract](docs/domain-pack-authoring.md)
