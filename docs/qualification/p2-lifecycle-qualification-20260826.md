# P2 lifecycle continuous qualification

Date: 2026-08-26

Source: `7124b3b886e9e1232d846254cc304b2dfe72850b`

Decision: `promotable=true`

The persisted-Run compatibility group passed 9/9 checks. The current writer versions and known
legacy event kinds are published as a read-only support window. A frozen pre-RunDecision fixture
replays through current readers, retained compatibility telemetry has no writer API, retired Agent
inputs remain unavailable, and unknown persisted schemas or versions fail closed.

The semantic-projection group passed 15/15 checks. ObservationReceipt and Gate schema 4 KiB values
and the Turn 8 KiB value are soft display targets. Projection pressure preserves ObservationRef,
Gate, Blocker/Incident, DiagnosticReceipt and Outcome semantics; it neither requests manual
selector narrowing nor creates a projection budget blocker.

The Artifact lifecycle scenario created 64 records sharing one raw digest across four persistent
repository lifecycles, one redacted derivative, and one temporary record. Restart resolution
succeeded. The first GC removed 33 released/expired records while preserving shared retained
content; the second removed 31 more. Two audit records and two content files remained.

- Aggregate evidence digest:
  `sha256:9d0d8b873e2c72d68a015c456cc096c93ce9966b223b9153a7d7dcc9c95263b8`
- Runtime stability digest:
  `sha256:591e65ef1b41d066067b5f616d2aabfdf251e00183c0755794ae20fc162218b8`
- Machine-readable summary:
  [`p2-lifecycle-qualification-20260826.json`](p2-lifecycle-qualification-20260826.json)
