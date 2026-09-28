Blocked by: #250 (shared MCP boundary; implement after terminal-delivery branch is integrated).

Part of #278, roadmap item 02.

## Problem

Common model argument spelling and formatting mistakes still end a call even
when the intended request is unambiguous. The Runtime already has strict
preflight with canonical examples and a Domain argument normalizer; extend
those existing seams instead of introducing another command surface.

## Behavior

- Normalize a documented, finite set of legacy argument aliases and safe
  textual representations at the MCP/Agent adapter boundary. Exact canonical
  input stays unchanged. Conflicting aliases or values remain errors.
- Return a complete canonical retry input for recoverable preflight errors.
  A host may apply it once only when target, intent, action scope, artifact,
  Gate/Run identity and authorization meaning are unchanged; otherwise retain
  the explicit error and missing information.
- Carry known Run/Gate identities from the current Runtime Turn where the host
  already has them, so the model need not manually copy them. Do not infer a
  new target, approve a Gate, authorize rollback or reissue an unknown Effect.

## Acceptance

- Controlled legacy-call fixtures normalize to the same typed request and
  Runtime result as canonical calls, including direct MCP and packaged paths.
- Ambiguous target, conflicting aliases, malformed or stale Gate identity,
  unknown Effect and authorization-sensitive fields are never guessed.
- Formatting errors produce a usable canonical example without user
  interruption; any automatic retry has one-call budget and no duplicate
  device operation.
- Existing callers, strict validation, and public error/structured result
  contracts remain compatible. Document the MCP interface review risk.

## Implementation contract and plan

**Specification card.** The Agent MCP Adapter accepts only the finite aliases
and lossless textual forms below, then passes the canonical object through the
existing Runtime decoder. The affected repository is this workflow checkout;
the affected interface is `observe(Query)` / `execute(Action)`. RunEngine remains
the authority for target, Run, Gate, authorization, Effect, and outcome state.
Acceptance uses direct MCP fixtures, an isolated packaged plugin fixture, and
the existing Agent preflight and compatibility suites. The change is reversible
by reverting the Adapter and its focused tests; it changes no persisted format,
credential selection, or generated artifact.

| Operation | Accepted legacy input | Canonical input |
| --- | --- | --- |
| `observe` | `target_ip`, `bmc_ip` | `target` |
| `observe` | `selector` (one selector object) | `selectors` (one-element array) |
| `execute` | `action_type`, `actionKind` | `kind` |
| `execute` | `runId`, `gateId`, `gateVersion`, `schemaDigest`, `submissionId` | Corresponding snake_case identity field |
| `execute` start | `target_ip`, `bmc_ip`, `deliveryStrategy`, `entryOperation`, `entryArguments` | `target`, `delivery_strategy`, `entry_operation`, `entry_arguments` |

The canonical `selectors` and `targets` arrays and `freshness`, `response`,
and `entry_arguments` objects may be supplied as exact JSON text. A numeric
`deadline`, `gate_version`, or `freshness.max_age_seconds` may be supplied as
plain decimal text. No natural-language parsing, inferred target, inferred
intent, Gate approval, authorization, credential alias, or arbitrary case/field
guessing is allowed. Canonical typed input remains unchanged. Duplicate aliases
are accepted only if their normalized values agree; conflicts fail before a
Runtime call. Normalization is followed by the same request budget and strict
decoder as canonical input.

The Adapter remembers only the last active Runtime Turn binding per MCP task.
It may fill an omitted `run_id` and, for an explicit `respond` to that same
Run's current phase Gate, the wholly omitted `gate_id`, `gate_version`, and
`schema_digest` binding. A partial binding remains a strict error. Explicit
malformed or stale identities are never replaced.
The response and its authorization meaning must be supplied by the caller.
When there is no current Turn, the existing preflight error remains explicit.

The existing `error.example` and `next_action` fields carry a complete
canonical candidate when preflight can recover without invented data. Hosts
may issue at most one retry from a preflight failure after comparing target,
intent, action scope, ArtifactRef, Run/Gate identity, and authorization meaning.
`next_action` alone does not authorize an automatic retry. The MCP server makes
one Runtime dispatch per tool call and never retries an Effect after a Runtime
or transport failure.

**Standard gate / interface review risk.** This is a visible MCP input
compatibility change, so the Agent Interface maintainers should review the
finite alias list and error examples. It does not modify MDB, Redfish, CLI,
SNMP, IPMI, permission, or credential contracts. ADR-0001/0003/0004 and
`CONTEXT.md` preserve the Runtime ownership and Gate binding rules.

**Execution plan.** Add a pure input Adapter and task-scoped Turn binding;
connect it with a narrow `mcp.py` change; add direct and packaged contract
tests; run those tests plus the existing Agent preflight and MCP compatibility
suites. Rollback is the Adapter, `mcp.py` wiring, tests, and this note. No BMC
or QEMU access is needed for the offline acceptance fixtures.
