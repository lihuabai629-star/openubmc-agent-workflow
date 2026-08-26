# P2 lifecycle continuous qualification

Date: 2026-08-26

Source: `5d9b7e32de563ab85c3c31e7b75d122cd3db4545`

Decision: `promotable=true`

The persisted-Run compatibility group passed 10/10 checks. The current writer versions and known
legacy event kinds are published as a read-only support window. A frozen pre-RunDecision fixture
replays through SQLite EventRunStore and CaseReplay readers without changing the ledger, retained
compatibility telemetry has no writer API, retired Agent inputs remain unavailable, and unknown
persisted schemas, versions, or unversioned event kinds fail closed.

The semantic-projection group passed 16/16 checks. ObservationReceipt and Gate schema 4 KiB values
and the Turn 8 KiB value are soft display targets. Projection pressure preserves ObservationRef,
Gate, Blocker/Incident, DiagnosticReceipt and Outcome semantics; it neither requests manual
selector narrowing nor creates a projection budget blocker.

The Artifact lifecycle scenario created 64 records sharing one raw digest across four persistent
repository lifecycles, one redacted derivative, and one temporary record. Restart resolution
succeeded. The first GC removed 33 released/expired records while preserving shared retained
content; the second removed the remaining 32 raw references and then deleted their shared content.
One redacted audit record and one content file remained.

- Aggregate evidence digest:
  `sha256:4fc3bc1b7dc0ad3f6267adcc1d1b1bec1cce425a21c3e429bf61f96725a9c513`
- Runtime stability digest:
  `sha256:ee83dba7073088e707dc5456788881e0b5e92864618c894671ad2d6ea3296c18`
- Machine-readable summary:
  [`p2-lifecycle-qualification-20260826.json`](p2-lifecycle-qualification-20260826.json)
- Runtime stability evidence:
  [`p2-runtime-stability-20260826.json`](p2-runtime-stability-20260826.json)
