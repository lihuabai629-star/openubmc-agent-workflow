# Issue #243 credential containment implementation plan

## Specification card

- **Goal:** A task selects an existing current-user local credential source by
  target, purpose, transport, and optional local selector. Runtime alone reads
  the secret; Agent and MCP arguments, durable records, and child process
  metadata contain no plaintext value or digest of it.
- **Change surface and repository:** Python Target Runtime and local MCP host in
  `openubmc-agent-workflow`; isolated branch `codex/issue-243` at `cf68d00`.
- **Contract:** Keep the current `observe`/`execute` and domain operation shapes.
  Secret-bearing argument keys are rejected at the model-visible boundary.
  Existing local source selectors remain references, not values. Runtime
  distinguishes `credentials_missing`, `credentials_conflict`, and
  `credentials_invalid`; target authentication and transport errors retain
  their lane-specific outcomes.
- **Standards gate:** Read `openubmc-developer` development workflow, standards,
  and verification references plus this repository's Runtime contracts. No
  MDB/DBus, Redfish API, or generated openUBMC interface is changed. This is a
  security and persistence behavior change; review compatibility of existing
  local selectors and stored Run facts. Interface SIG risk is not applicable
  unless a public device API is changed.
- **Acceptance:** Synthetic-secret tests of MCP rejection, local task binding,
  subprocess argv/environment/output/exception boundary, and each durable
  storage seam. Run focused Runtime tests, then the broader local suite. No
  live BMC or real credential is used.
- **Risk and rollback:** Rejection can expose previously accepted unsafe
  requests. Revert this commit to restore previous handling; saved local
  credentials and historical records are not edited. Historical exposure still
  requires operator-owned rotation using the runbook.

## Execution plan

1. Audit current local selection, task cache, model-visible arguments, and
   subprocess and persistence seams. Keep changes outside the parallel #282
   tracing files where feasible.
2. Harden the gaps at the lowest common boundaries while preserving Runtime
   effect and state authority and existing error classifications.
3. Add fail-closed synthetic-secret tests, run focused and broader suites, then
   document the operator rotation identification path and remaining limits.

Expected files: local selector and resolver contracts, OpenSSH transport,
Runtime/Host/Outcome persistence seams, dedicated tests, this plan, and the
response runbook. The small `mcp.py` task-close and exception-status changes,
plus the `run_engine.py` Effect fingerprint change, overlap #282's tracing
files; the integration coordinator must merge those hunks against its branch.
`agent_gateway.py` and `effect_runner.py` remain untouched.

## Local reference and containment boundaries

The existing activated `credentials.json` selects a record through the target,
purpose, transport, and optional exact port. The Agent sends the target and
domain intent; Runtime resolves that selection locally. Legacy named
environment selectors are accepted only as uppercase variable names with an
underscore. The selector fingerprint uses the variable name, never its
resolved password. A selected source is pinned on the first lookup, including
an incomplete lookup, and the task's cached values are discarded at task close.

For SSH password authentication, `sshpass -d 0` reads a short-lived pipe; no
password is placed in argv or child environment. The child is launched with an
allowlisted environment. Returned stdout, stderr, timeout output, and startup
errors are scrubbed before they can form a receipt or exception. Local source
files retain their pre-existing private owner/mode checks.

The MCP ingress rejects secret-shaped arguments. Run Effect intents and Turns
reject inline secrets before persistence, and Effect result identities redact
result and exception metadata before hashing. Runtime events and retry receipts
redact before public projection and storage; task context, Host notes, Session
Outcomes, and terminal answers reject secret-shaped input before storage. This does not
retroactively remove copies in host transcripts or external logs, and arbitrary
unlabelled strings that have never been registered as secrets cannot always be
recognized. Operators must use the rotation runbook for prior exposure.

## Integrated local verification (2026-09-27)

The containment commit `611122b` was integrated with the optional tracing and
Host continuity changes. At optimized code commit `37447c9`, 20 synthetic
containment tests, 36 MCP contract tests and the full 847-test Runtime suite
passed on macOS/Python 3.12. The 128-Run capacity scenario identified repeated
regex scanning of ordinary non-secret fields as a hotspot; `37447c9` adds a
delimiter-aware fast path that still performs full replacement whenever a
registered secret is present. It matched the previous redactor on 10,005
deterministic synthetic strings and passed a clean standalone stability report.
No real credential, target, rollout retention store or rotation was exercised.
Native Linux/Windows acceptance and operator-owned historical rotation remain
separate from this local code verification.
