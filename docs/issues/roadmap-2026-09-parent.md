## Goal

Complete the user-approved September 2026 openUBMC Agent Workflow plan, then
qualify an integrated source. Keep the Runtime as the sole Run/Effect/Outcome
authority and the Agent interface as `observe`/`execute`.

## User decisions

- Save a target credential automatically after verified authentication; no
  per-connection save confirmation.
- Do not add a new redaction approval gate. Preserve existing secret handling
  and never expose real credentials in issue or test output.
- Identify community, internal and product repositories separately in search.
- Finish independent issues in separate Codex tasks/worktrees using GPT-6 Sol
  at max reasoning; avoid repeated permission prompts.

## Work map

- [ ] 01 interruption baseline and controlled performance comparison
- [ ] 02 input compatibility and safe normalization
- [ ] 03 credential autosave compatibility: #277
- [ ] 04 retain existing protection; no new approval gate per user direction
- [ ] 05 Windows/WSL route: #247
- [ ] 06 partial recovery across optional services
- [ ] 07 terminal answer presence and delivery: #250
- [ ] 08 task handoff and fresh Runtime readback
- [ ] 09 incremental source navigation: #276
- [ ] 10 source/LightRAG fusion with repository identity: #276
- [ ] 11 bounded local evidence batching
- [ ] 12 hypothesis, support and counterevidence strategy
- [ ] 13 one-domain graph experiment, adopt only if measured benefit
- [ ] 14 bounded read-only helper experiment
- [ ] 15 counterevidence reviewer experiment
- [ ] 16 MCP structured result and compatibility validation
- [ ] 17 MCP Tasks experiment with old-client fallback
- [ ] 18 Desktop contract and same-Run acceptance (separate active project)
- [ ] 19 20 scenarios × 3 stateful Agent trials and independent scoring
- [ ] 20 Linux/WSL, Windows and hosted CI platform matrix
- [ ] 21 bounded OpenTelemetry/tracing with behavior-neutral disable path
- [ ] 22 artifact identity, SBOM and qualification/provenance binding
- [ ] 23 documentation, issue reconciliation and targeted module cleanup
- [ ] 24 conditional Agent SDK/persistent execution framework comparison

The existing candidate snapshot contains first-pass 03, 05, 06, 07, 08 and
repository ownership for 10. The remaining status and verification detail
live in `docs/workflow-continuity-implementation.md` and
`docs/workflow-roadmap-execution-20260927.md` in the local coordinator branch.
GitHub #253 is the final offline regression synthesis after behavior-defining
issues land. A checked box requires implementation evidence and the relevant
acceptance test, not merely a draft PR or local plan.
