# Terminal answer gate

A terminal Run is not delivered until a final answer record is durably bound to the same task, Run, Outcome fingerprint, status, and delivery stage. The record contains only user-visible text and terminal facts. It is written atomically, and replaying delivery returns the existing record instead of sending a second mutation or changing the text.

Qualification fails closed for a missing, empty, interrupted, or mismatched answer. A persisted `final_answer` message alone is a candidate: delivery requires the matching `task_complete` event for that turn, after preparation and with no turn error. Recovery reads the durable record without rerunning the Run. Completed, partial, failed, cancelled, and blocked statuses include the same delivery stage used by Runtime; nonterminal stages show the next unverified stage and its required evidence when Runtime provides it.

## Host integration

Production MCP captures a task/Run bookmark after `execute` returns. The optional
`_meta["openubmc/host-continuity"]` is transport metadata, not another domain tool
or an execution result. A failed checkpoint leaves the original result intact and
never requests a repeated mutation. Native Codex `threadId` metadata and legacy
task-id aliases map to the same task identity used by host hooks.

`SessionStart` reconstructs a compact handoff from the current Runtime ledger.
`Stop` requests at most one **text-only** continuation per turn when a terminal
answer is missing. A second failure leaves the prepared answer pending. An exact
canonical Stop candidate suppresses another continuation in that turn, but it
does not confirm delivery: the turn can still be interrupted before the host
commits the final. A richer model answer is left intact but unconfirmed until
independently checked. The read-only rollout audit binds the first matching,
completed final to its task, Run, Outcome fingerprint and stage. Tool success,
printed CLI output, and a prepared record are not delivery acknowledgements.

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

This verifies protocol/event binding, one text-only recovery, a completed host
final audit, and resumed-session readback. It is not a live model, BMC, or Desktop
rendering qualification. A separate disposable Codex plugin installation test
exercises the installed Node hook, verified execution snapshot, terminal readback,
and rollout audit with an offline empty-lock fixture. It does not grant hook trust
in the user's global Codex configuration.

## Issue #250 verification (2026-09-27)

| Check | Result |
| --- | --- |
| Terminal delivery, host continuity, delivery stage unit tests | 41 passed |
| Evaluation harness and sanitized replay tests | 16 passed; an interrupted rollout is rejected even after a record was previously acknowledged |
| Plugin package and Python entrypoint tests | 21 passed, including temporary Codex installation, installed hook readback, audit, and immutable package verification |
| Native Codex 0.153.4 loopback probe | 2 MCP calls (start/cancel), 1 text-only continuation, completed final audited, restart restores the cancelled state without another device call |

The installed test uses a fake target and an empty dependency lock only in its
temporary package. It verifies the installed package path and hook launcher, but
does not establish real provider, BMC, Desktop rendering, or user hook-trust
acceptance.
