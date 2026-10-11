# MCP compatibility decision (2026-09-27)

This records roadmap items 16 and 17 for the current packaged client. It is a
transport decision, not a change to the `observe`/`execute` Agent contract or
the Runtime's Run/Effect authority.

## Verified current path

- The Runtime stdio endpoint negotiates MCP `2025-06-18` through `initialize`,
  advertises only the two Agent tools, and returns bounded text alongside
  `structuredContent`. The local `test_mcp_contracts.py` suite passed 36/36 on
  the integrated source. The synthetic installed-client probe also exercised
  `initialize`, `tools/list` and `tools/call`; it did not prove a live model or
  Windows client.
- The pinned Codex CLI 0.153.4 reports `mcp_2026_07_28` as under development
  and disabled. Its observed native stdio path used legacy `initialize` and
  per-call `threadId` metadata. The current Runtime is **not** a dual-era MCP
  server: `server/discover` and modern per-request protocol metadata are not
  implemented. A 2026 client must not be told this server supports 2026.

## Upstream delta and trial decision

The [2026-07-28 versioning specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
replaces the handshake with per-request `_meta` and requires
`server/discover`; [tool results](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
have a `resultType` discriminator. Supporting that version therefore needs a
dual-era adapter and version-specific contract tests, not a constant change or
an added field on the legacy response.

The current [Tasks extension draft](https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks)
is separately negotiated as `io.modelcontextprotocol/tasks`; `tasks/get`,
`tasks/update` and `tasks/cancel` describe a server task and require durable
task identity. It must not become a second Run ledger or let the Agent bypass
Runtime Gate/Effect rules. Since the pinned client does not use the modern
protocol, no Tasks extension is advertised or enabled. This is an explicit
**non-adoption** decision for the current client, not a claim that a 2026
client or Tasks path passed.

Revisit when a supported client enables 2026 and a hermetic A/B can compare
legacy and dual-era paths. Required evidence: `server/discover` and unsupported
version errors; `tools/list` and text-plus-structured result equivalence;
per-request task identity; cancellation/restart with the same Run and no new
dangerous Effect; old-client fallback; input/output budgets; and installed
Linux/Windows/WSL client tests. A Tasks trial additionally needs extension
capability negotiation, `tasks/get/update/cancel` lifecycle tests and a proof
that its task ID only refers to a Runtime-owned Run. Keep the existing legacy
path in production until those results exist.
