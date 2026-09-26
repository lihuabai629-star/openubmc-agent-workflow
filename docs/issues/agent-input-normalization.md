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
