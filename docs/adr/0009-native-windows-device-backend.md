# ADR-0009: Native Windows backend for device execution

- Status: Proposed
- Date: 2026-09-28
- Decision owners: openUBMC Agent Workflow maintainers
- Supersedes in part: [ADR-0008 Local setup plane](0008-local-setup-plane-and-cross-platform-bootstrap.md), for the Windows execution-host choice

## Context

The current Windows plugin installs under Codex but sends Runtime and KB requests to a selected WSL distribution. That design makes WSL mandatory even when the user only needs to connect to a BMC or its OS. Direct Windows execution currently fails before package verification because `pluginctl.py` imports `fcntl`; configuration activation and SSH password transport also depend on POSIX locks, permissions, ControlMaster sockets and `sshpass`.

## Decision

Keep the existing Runtime Core and its `observe`/`execute` Agent Interface. Add Windows Adapters at the host bootstrap, private local store, process lifecycle and device transport seams. For device operations, Windows Codex launches the packaged Runtime and KB directly on Windows. The bootstrap never substitutes WSL for an unavailable Windows backend. It exposes a bounded setup reason instead. Build tooling remains an optional, separately reported capability.

Windows stores credentials and durable Runtime state locally with current-user access control; it does not copy secrets from WSL by default. Both operating systems use the same package identity and Runtime safety contracts. The detailed behavior and qualification are in [the native Windows specification](../specs/native-windows-device-execution.md).

## Consequences

- A Windows installation without WSL can perform qualified device workflows after local setup.
- Windows portability requires concrete platform Adapters; POSIX-only operations cannot be hidden by changing the launcher command.
- Release qualification must exercise native Windows device connections, credential containment, restart recovery and lifecycle closeout. The previous Windows→WSL row is insufficient.
- Existing Linux behavior and release safety gates remain in force. The unpublished 2.1.3 candidate is superseded when this decision is implemented and must not be promoted using its old Windows acceptance claim.
