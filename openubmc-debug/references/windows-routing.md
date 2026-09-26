# Windows/WSL execution routing

For diagnosis, build, upgrade, evidence, and rollback requests, first probe the
selected WSL Runtime MCP with bounded `initialize` and `tools/list` requests.
Require the same backend to answer both and advertise `observe` and `execute`.
Then continue with the typed Runtime `observe` or `execute` operation. A live
process, relay, port, or remembered tool list does not prove protocol health.

`environment="windows"` identifies the client. The packaged structured
execution host is `wsl`; a native Windows probe cannot impersonate that
backend. WSL distribution selection belongs to the Local Setup Plane. This
route does not claim broad native Windows Runtime support.

The installed MCP bootstrap checks `pluginctl doctor` protocol health before
proxying the Runtime. It emits an `openubmc-routing` receipt on MCP stderr
before forwarding each `observe`/`execute` call. The receipt includes the
selected WSL distribution and a process-keyed HMAC of bounded request
arguments, not raw target arguments, credentials, or a guessable digest. An
unhealthy probe stays on the setup surface; an inconsistent tool list blocks
the Runtime call.

When MCP is unavailable, unhealthy, on the wrong host, or the requested
operation is unsupported, call `ExecutionRouter.choose` in
`scripts/execution_router.py` and retain its receipt. Set `shell_host` to the
actual command origin (`windows-native` for PowerShell, `wsl` for a shell
already running in WSL). The receipt records `reason_code`, requested scope,
evidence boundary, and a total call budget of at most 32. A Windows fallback
without `shell_host` is rejected.

Pass each argument vector through `ExecutionRouter.admit_shell` with the
adapter-observed `observed_host` before running it. A host mismatch is rejected.
Preserve the returned receipt, including command digest and host path.
PowerShell → WSL → SSH counts as two transitions; the final SSH target remains
bound to the requested scope. Stop when a repeated equivalent command or an
exhausted budget returns `convergence_blocker`. Split command chains into
separately budgeted actions. Do not place passwords or tokens on command lines.

Shell output is observational only. It cannot close any typed Gate, including
mutation, deployment, runtime verification, or rollback. A fallback stays
unresolved in metrics until a Runtime-facing adapter verifies an actual Runtime
evidence reference. The Runtime alone owns Run, Gate, Effect, Outcome,
verification, and rollback decisions.

The MCP bootstrap does not see Codex Host terminal calls. A trusted plugin
`PreToolUse` hook can deny supported `Bash` and `exec_command` calls, but the
hook input alone cannot reliably identify an openUBMC target operation, bind
it to a fallback receipt, or establish the actual command host. Some specialized
tool paths can opt out. `ExecutionRouter` enforces its budget only where the
Host or another caller passes every applicable shell action through
`admit_shell` with its observed host. Do not describe an unobserved terminal
command as a routed fallback.
