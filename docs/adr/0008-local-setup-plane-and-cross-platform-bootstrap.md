# ADR-0008: Local setup plane and cross-platform bootstrap

- Status: Active for Linux/WSL setup; the proposed native Windows backend in ADR-0009 was retired in 2.2.1
- Date: 2026-09-24
- Decision owners: openUBMC Agent Workflow maintainers

## Context

A required stdio server that exits before MCP initialization prevents a Codex task from opening.
This made absent WSL, Python, Node.js, package access, or a damaged local registration look like a
Runtime failure even though no Runtime command had been accepted. Windows also needs to select a
Linux execution environment without moving target credentials or durable Runtime state into the
Windows process boundary.

The semantic Runtime surface remains intentionally limited to `observe` and `execute`. Host setup
and installation recovery happen before that surface exists and must not acquire Runtime authority.

## Decision

Add a Local Setup Plane in the platform-neutral MCP bootstrap. When the selected Runtime or KB
backend is healthy, the bootstrap proxies the existing server unchanged. When it is unavailable,
the bootstrap completes MCP initialization and exposes only bounded local setup operations:

- report package identity, execution host, dependency state, local configuration revisions,
  duplicate registrations, authentication state, and protocol health as separate facts;
- report the WSL distribution explicitly selected by the client bridge;
- prepare locked dependencies explicitly;
- open the private loopback configuration page; and
- preview and apply recognized reversible Codex configuration repair.

Windows passes only bounded non-secret task identity into WSL. Credentials, dependency caches,
Runtime history and target operations remain in the selected WSL environment. The Windows launcher
does not inherit credential, proxy, registry, or package-manager secret variables. The client
bridge requires an explicit distribution and Linux plugin path; it never guesses a backend.

The Local Setup Plane does not create a Run, answer a Gate, invoke a Domain Adapter, record Evidence,
commit an Effect, or form an Outcome. A new task loads the normal backend after setup succeeds.
The bootstrap remains the MCP process directly owned by Codex and supervises the selected backend.
Formal lifecycle qualification binds the backend record to the exact task, session and source created
by that invocation, then proves that both the backend and its bootstrap supervisor terminate.

## Consequences

- Missing prerequisites no longer make task creation fail with a closed MCP handshake.
- Setup state is diagnostic and recoverable without widening Runtime domain authority.
- Linux and WSL use the same marketplace package while reporting their actual execution host.
- The thin Windows client bridge is tested separately from the Linux backend. Release qualification
  must compare the public marketplace payload with the qualified archive identity.
- New host recovery actions belong in this plane only when they remain local, bounded, reversible,
  and independent of target authorization.

## Rejected alternatives

- Run the POSIX Runtime natively on Windows while retaining Unix filesystem and process assumptions.
- Download dependencies during the required MCP handshake.
- Add setup and lifecycle repair operations to the normal Runtime Agent Interface.
- Forward Windows credential and proxy environments into WSL.

## References

- [ADR-0001](0001-runtime-core-and-semantic-agent-interface.md)
- [Issue #268](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/268)
