# Terminal answer gate

A terminal Run is not delivered until a final answer record is durably bound to the same task, Run, Outcome fingerprint, status, and delivery stage. The record contains only user-visible text and terminal facts. It is written atomically, and replaying delivery returns the existing record instead of sending a second mutation or changing the text.

Qualification fails closed for a missing, empty, interrupted, or mismatched answer. Recovery reads the durable record and renders it once. Completed, partial, failed, cancelled, and blocked statuses include the same delivery stage used by Runtime and a next action when work remains.

## Host integration

Production MCP captures a task/Run bookmark after `execute` returns. The optional
`_meta["openubmc/host-continuity"]` is transport metadata, not another domain tool
or an execution result. A failed checkpoint leaves the original result intact and
never requests a repeated mutation. Native Codex `threadId` metadata and legacy
task-id aliases map to the same task identity used by host hooks.

`SessionStart` reconstructs a compact handoff from the current Runtime ledger.
`Stop` requests at most one **text-only** continuation when a terminal answer is
missing. A second failure leaves the prepared answer pending. An exact observed
canonical final is acknowledged with `codex-stop-v1`; a richer model answer is
left intact but unconfirmed until independently checked. Tool success, printed
CLI output, and a prepared record are not delivery acknowledgements.

The bundled `hooks/hooks.json` uses the existing Linux/WSL adapter and verified
execution snapshot. It does not install dependencies or change hook trust. Codex
requires the user to trust the current hook definition; unsupported/untrusted
hosts retain the local handoff/answer CLI. See the official
[hooks guide](https://learn.chatgpt.com/docs/hooks) and
[plugin hook rules](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks).
The CLI probe may bypass trust only for its own vetted hook in a disposable
`CODEX_HOME`; this is not an installation recommendation.

## Local recovery

See `openubmc-debug/references/host-continuity.md`. Reads never open a target or
execute/resume a Run. Missing ledger data, changed Outcome fingerprints, and
unavailable storage remain explicit gaps; old answers cannot manufacture success.

Native qualification (loopback Responses fixture, fake target, actual Codex CLI):

```sh
python scripts/qualify_host_continuity.py probe --codex /path/to/pinned/codex
```

This verifies protocol/event binding, one text-only recovery, and resumed-session
readback. It is not a live model, BMC, Desktop rendering, or clean dependency-install
qualification. Package entrypoint tests separately exercise the verified snapshot.
