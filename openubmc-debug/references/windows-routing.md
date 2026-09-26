# Windows/WSL execution routing

Check that the Runtime MCP responds to its protocol and exposes `observe` and
`execute`, then use the corresponding structured operation. A configured server
name or live process alone does not establish health.

`environment="windows"` identifies the client, not the Runtime host. The packaged
execution host is `wsl`; a healthy WSL probe selects structured execution. Receipts
keep `client_environment` and `execution_host` separate. A `windows-native` or
unrelated Linux probe cannot impersonate that selected backend. Distribution
selection remains owned by the existing Local Setup Plane.

When the protocol is unavailable or an operation is unsupported, record the
fallback with `scripts/execution_router.py` (`ExecutionRouter.choose`) before
using a shell. Retain `reason_code`, actual execution host, target scope,
evidence boundary, and a call budget of at most 32. Admit each shell command
through `ExecutionRouter.admit_shell`; stop on repetition or exhausted budget.
Count PowerShell→WSL→SSH transitions and name the host that observed each fact.

Shell output cannot close a mutation, deployment, runtime-verification, or
rollback Gate; those conclusions require Runtime evidence.
