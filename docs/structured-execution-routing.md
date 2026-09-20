# Structured execution routing

Windows and WSL requests use a protocol health check before selecting an execution path. A healthy Runtime/MCP endpoint receives the operation. When the endpoint is unavailable or unhealthy, the shell path is a bounded observation fallback with a stable reason code, execution host, requested scope, and evidence boundary.

Every shell action consumes a per-task budget and is keyed by its command digest. Repeating an equivalent action stops with a convergence blocker. Shell output is never accepted as evidence for mutation, deployment, runtime verification, or rollback gates; those gates require the corresponding typed Runtime evidence.

The router report contains structured and fallback call counts, host mismatches, repetition count, unresolved fallback work, and a digest. Credential fields are redacted before the report is persisted.
