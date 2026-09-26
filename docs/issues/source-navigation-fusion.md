## Roadmap

Items 09 and 10 of the user-approved workflow plan.

## Problem

The existing source trace and per-hit repository provenance do not provide an
incremental index or a joined local-source/LightRAG result. A mixed community,
internal and product checkout can return the right symbol from the wrong
product version unless identity is retained through ranking and presentation.

## Scope and contract

- Index Lua, C/C++, model and configuration files by content identity and
  source identity. Bind every hit to repository root, repository class,
  product/component, commit, branch and dirty state when available.
- Update changed files without rebuilding an unchanged repository. Make a
  missing index, unavailable language service or KB a bounded fallback to
  existing text/source navigation.
- Prefer exact symbol, error and path matches; fuse optional LightRAG results
  for natural-language questions. Deduplicate and rank a small bounded set.
- Keep evidence references and freshness in the result. An internal result
  must not be promoted to a product implementation when its product identity
  differs. Do not contact a device or write Runtime state from retrieval.

## Public acceptance

- A source edit or commit change updates only affected entries; a dirty file
  is distinguishable from its committed version.
- An error code, MDB path and function name return the correct repository and
  version in controlled mixed-source fixtures.
- A KB outage preserves useful local-source results with a clear partial
  status. A source-index outage preserves the existing search fallback.
- Compared with the current baseline, correct-source location improves or
  wrong-version citation decreases on a fixed offline fixture set.
- Focused tests and packaged entrypoint checks pass. No public Runtime
  `observe`/`execute` contract changes.

## Coordination

Part of the 2026-09-27 workflow roadmap. Start from local candidate snapshot
`73eeba56dda909c41dfb66345aa15f0dcd067142` in an isolated Git worktree.
Own Debug source retrieval and its tests/docs; coordinate shared files before
editing Runtime or package entrypoints.
