# Issue 276: incremental source navigation

## Specification card

- **Goal:** Locate an exact error, MDB path or symbol in mixed Lua, C/C++ and model/configuration source, then optionally merge bounded LightRAG candidates without losing the identity of each local hit.
- **Change surface and repository:** Debug source retrieval in this `openubmc-agent-workflow` Git worktree only. The source trees being searched are read-only; the local index is a disposable cache outside them by default.
- **Contract:** The existing `source_trace.py --symbol` output and Runtime `observe`/`execute` contracts stay compatible. A new `--query` mode emits ranked local and optional KB candidates with repository, catalog product/component, branch, commit, matched-file dirty state, content digest and freshness. KB references are unverified candidates and never product implementations.
- **Standard gate:** No MDB/MDS definition, northbound interface, device API, permission, generated artifact or Runtime state change. This is a local Debug helper output; it needs no Interface SIG review. Cache format is versioned and can be rebuilt.
- **Acceptance:** Offline mixed-source fixtures test edits, commit changes, exact identifiers, wrong-product ranking, KB outage and index failure. Compare correct-source location against the current text-search baseline. Run focused tests, workflow validation and packaged entrypoint checks.
- **Risk and rollback:** Cache failure falls back to bounded existing source search. Revert this issue commit to restore the prior helper; deleting the disposable cache is optional. No device contact is performed.

## Implementation plan

1. Extend Debug's packaged source helper with a query mode and a local SQLite content index. Walk bounded source files, hash content, and retokenize only changed digests; source provenance is resolved at query time so a branch/commit change cannot leave a stale citation.
2. Rank exact code identifiers and paths ahead of loose words; annotate every returned local hit through the existing source catalog. Keep a small deduplicated result set and explicit partial/fallback status.
3. Accept an already obtained, bounded `openubmc_kb_query` MCP receipt as optional input. Merge only its referenced candidate paths; never infer local repository identity from a KB label or call the KB/device from the indexer.
4. Document CLI use and limitations. Verify changes with the offline fixtures and packaged helper, then commit and open the issue PR.

## Local verification record

- `python3 -m unittest discover -s openubmc-debug/tests -p 'test_source*.py' -q`: 30 tests passed, including mixed repository identity, edit/commit/deletion updates, KB outage, index fallback and the query CLI.
- Fixed offline ranking fixture: the current unranked text-search baseline located the selected product first for 0 of 3 queries; the indexed query located it first for 3 of 3. Queries were an error code, MDB path and function name. This is a controlled fixture comparison, not a production relevance claim.
- `package_skill.py` created a shareable Debug tree, and its packaged `source_trace.py --query` returned `openubmc.source-navigation.v1`, `status=complete` and the expected model path.
- No live LightRAG or BMC call was used. The helper accepts an existing MCP receipt; authentication and network behavior remain owned by `openubmc-kb-mcp`.
