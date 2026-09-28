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

On a Windows client, the packaged Runtime and KB backends run on Windows.
The Local Setup Plane reports the native backend's protocol health. WSL may
be selected separately for builds, but is not the device execution host.

| Client | Structured execution host | Permitted shell origin |
| --- | --- | --- |
| Windows | Windows | Explicit `windows-native` or `wsl` for a separately selected build shell |
| WSL | WSL | WSL |
| Linux | Linux | Linux |

## Fallback receipt and command budget

`ExecutionRouter.choose` returns a bound receipt with route ID, operation,
client environment, exact shell origin or Runtime host, requested scope,
evidence boundary, protocol result, and stable fallback `reason_code`. Windows
fallback requires an explicit `shell_host`. The default task budget is eight
shell commands and cannot exceed 32 or be increased after routing. Every
command must pass `admit_shell` with the adapter-observed command origin before
execution. A mismatch with the receipt is rejected and counted. The router
rejects chained commands, exhausted budgets, and a repeated equivalent argument vector with a
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

The installed MCP boundary is `plugin/openubmc/scripts/openubmc-mcp-bootstrap.js`.
Before proxying the Runtime, it uses `pluginctl doctor`'s bounded MCP
`initialize`/`tools/list` probe and requires Runtime protocol health. Before
forwarding each `observe` or `execute` call, it applies
`openubmc-execution-routing.js` and writes an `openubmc-routing` receipt to MCP
stderr. The receipt binds the native Windows or POSIX host, tool,
semantic operation when present, a process-keyed HMAC of bounded canonical
request arguments, and the typed Runtime result boundary. The key never
enters a receipt or log. Malformed, oversized, or deeply nested requests are
rejected before hashing or forwarding. The receipt contains no argument text
or credentials. The Runtime MCP response is forwarded unchanged. A failed health check exposes
the existing setup surface; an inconsistent tool list blocks the call before
it reaches the backend.

## Host shell boundary

The shell policy is `scripts/execution_router.py`; the identical packaged copy
is `openubmc-debug/scripts/execution_router.py`. The MCP bootstrap does not
observe shell calls made by the Codex Host. A separate, trusted plugin
`PreToolUse` hook can match `Bash`, including unified `exec_command`, and deny
supported calls before execution. The native Codex 0.153.4 probe
`scripts/qualify_pretool_shell_hook.py` verified this in a disposable
`CODEX_HOME`: its synthetic blocked command was denied and an unrelated
synthetic command ran. Both hook events had one `session_id`. The separate
`scripts/qualify_host_continuity.py` probe observed that Codex's MCP
`_meta.threadId` equals the hook's session ID, so this is a usable task
correlation key. No hook definition or trust setting was changed in the user's
setup. See the [official hook coverage and deny schema](https://learn.chatgpt.com/docs/hooks).

The same native shell fixture recorded the exact event key set:

| Binding needed for an openUBMC fallback | Native `PreToolUse` evidence |
| --- | --- |
| Task | `session_id` present; matches MCP `threadId` in the separate native probe |
| Command | `tool_input.command` present |
| Target and operation | No typed fields; neither is inferable from every shell string |
| Bound fallback receipt and remaining budget | No receipt or trusted route state in the event |
| Actual command host and selected WSL distribution | No execution-host field; `cwd` describes the session, not a nested WSL/SSH host |

The fixture passed an explicit `workdir` to `exec_command`, yet the hook's
`tool_input` contained only `command`. This observed shape agrees with the
documented `PreToolUse` schema; it is evidence for Codex 0.153.4 on macOS,
not native Windows/WSL acceptance.

A production hook has not been added to `plugin/openubmc/hooks/hooks.json`.
The `PreToolUse` input contains a shell command, session ID, and working
directory, but it does not provide the requested openUBMC operation, target
scope, bound fallback receipt, or a trustworthy observed command host. Inferring
these from arbitrary command text would miss wrapped calls and could block
unrelated shell work. Denying every shell call with missing binding would block
unrelated work; allowing an unmarked command would make the budget optional.
Enforcing the budget requires a Host adapter that supplies a protected,
session-bound target/operation/receipt and observed host for each applicable
command, then passes it through `admit_shell` before execution. A field or
marker controlled by the model in the command string cannot serve as that
binding. Plugin hooks also require user trust and some specialized tool paths
can opt out, so a hook alone is not a complete enforcement boundary.
Until the Host binding and native Windows device coverage are verified, the Python
router and Skill instruction govern only callers that use them; no global
shell-budget enforcement is claimed. Installed MCP tests use controlled Linux
and Windows adapters. Native Windows device workflows need their own installed
candidate evidence before release.
