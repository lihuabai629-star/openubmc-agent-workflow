# Structured execution routing

The Agent still has two Runtime operations: `observe` and `execute`. Routing
chooses a transport for a requested diagnosis, build, upgrade, evidence, or
rollback operation. It does not create a Run, accept a Gate, authorize a
Mutation, or verify an Outcome. Those facts belong to the Runtime Core.

## Health and host selection

A healthy MCP probe completes `initialize` and `tools/list` round trips against
the same backend within a 1–5000 ms deadline, and the response advertises both
`observe` and `execute`. The adapter callbacks must enforce the deadline on I/O;
the router also rejects a late response. A process ID, port, configured server
name, or cached tool menu is insufficient. Errors become stable reason codes
without exception text.

On a Windows client, the packaged Runtime backend is in the selected WSL
distribution. A healthy native Windows endpoint cannot stand in for the WSL
backend. The Local Setup Plane chooses that distribution. Native Windows
Runtime execution is not claimed by this route.

| Client | Structured execution host | Permitted shell origin |
| --- | --- | --- |
| Windows | WSL | Explicit `windows-native` or `wsl` |
| WSL | WSL | WSL |
| Linux | Linux | Linux |

## Fallback receipt and command budget

`ExecutionRouter.choose` returns a bound receipt with route ID, operation,
client environment, exact shell origin or Runtime host, requested scope,
evidence boundary, protocol result, and stable fallback `reason_code`. Windows
fallback requires an explicit `shell_host`. The default task budget is eight
shell commands and cannot exceed 32 or be increased after routing. Every
command must pass `admit_shell` with the adapter-observed command origin before
execution. A mismatch with the receipt is rejected and counted. The router rejects chained
commands, exhausted budgets, and a repeated equivalent argument vector with a
`convergence_blocker`. It records known PowerShell → WSL → SSH hops in each
command receipt. Other opaque wrappers must be accounted for by their adapter;
this parser cannot prove arbitrary script internals.

The report contains structured calls recorded through `record_structured_call`,
accepted and blocked fallback calls, host accuracy, host transitions,
repetitions, and unresolved fallback work. It records keyed command digests,
not command text. Inline credential assignments are rejected from route scope
and boundary fields, and report fields are redacted. A fallback remains
unresolved until a Runtime-facing adapter verifies a Runtime evidence reference
and calls `note_runtime_resolution`; this bookkeeping does not accept a Gate.

Shell output may inform read-only diagnosis. `shell_can_satisfy_gate` denies all
Gate types, including future ones. Mutation, deployment, runtime verification,
and rollback still require the corresponding typed Runtime evidence and the
Runtime's own Gate/Outcome checks.

The source policy is `scripts/execution_router.py`; the identical packaged copy
is `openubmc-debug/scripts/execution_router.py`. Controlled adapter tests and
the sanitized replay entry point exercise the public behavior. Linux/WSL and
installed-plugin acceptance remain separate from a native Windows check.
