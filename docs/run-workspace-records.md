# Host workspace binding and Run records (v1)

Specification: [#299](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/299).
Implementation: [#300](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/300).

The Host may supply a non-secret workspace selection when starting a Run. Runtime
validates and freezes that selection in the existing durable start input. The
binding is committed with the Start RunDecision before any Domain Effect. It
describes requested project/source context; it does not prove observed target
identity, authorize an operation, check out a repository, or detect live drift.

## Trusted composition seam

`RuntimeMcpService(..., host_context_provider=provider)` accepts an optional
callable `provider(task_id) -> Mapping[str, object] | None`. The trusted Host
adapter selects the Task's context. Runtime calls it once for each Start request,
validates the returned snapshot, and passes an immutable value through StartRun.
Provider errors fail the request with a generic `invalid_workspace_context` error.

The Agent Interface still has only `observe` and `execute`. An Agent action cannot
supply `workspace_context`; notes and MCP `_meta` do not establish this binding.
Resume and control never consult the provider. They use the existing Run's facts.
Credential/configuration activation continues at the existing public-call seam;
the workspace snapshot does not freeze credentials.

No provider, or a provider returning `None`, retains the legacy semantic input and
command digest. Readers report unavailable context and never backfill historical
Runs from the Host's current selection.

## Workspace snapshot

The v1 allowlist is:

| Field | Value |
| --- | --- |
| `schema_version` | Integer `1` |
| `context_digest` | SHA-256 of the canonical normalized body without this field |
| `selection_ref`, `project_ref` | Non-secret opaque references |
| `requested_machine_ref`, `requested_firmware_ref` | Opaque references or `null` |
| `repositories` | Ordered list of at most 32 repository identities with unique `repo_ref` |

Each repository contains `repo_ref`, `repo_class` (`product`, `internal`,
`community`, or `unknown`), `commit`, `branch`, `dirty`,
`identity_availability` (`available`, `partial`, or `unavailable`), and
`identity_source_ref`. Commit is a lowercase 40- or 64-digit hexadecimal identity
or `null`; branch and source reference may be `null`; dirty is bool or `null`.
An available identity requires commit and dirty. Unavailable identities have
null commit, branch, and dirty. Partial identities retain only known values.

References use `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Branch text is bounded to
256 UTF-8 bytes and rejects control/space characters, colon, and an absolute
path prefix. Raw roots, endpoint fields, credentials, configuration, commands,
prompts, diffs, file contents, and logs are not part of the schema. Existing
bounded-request and secret-free validation also applies.

Normalization fills nullable fields and repository defaults (`unknown` class,
`unavailable` identity), retains repository order, and rejects unknown fields.
Canonical JSON uses sorted keys, separators `(',', ':')`, UTF-8, and
`ensure_ascii=False`. The supplied digest must match; it is an integrity check,
not proof of Host authority or a digest of configuration/secrets.

The independent worked examples are
[`workspace-selection-a.json`](../openubmc-target-runtime/tests/fixtures/workspace-selection-a.json),
[`workspace-selection-b.json`](../openubmc-target-runtime/tests/fixtures/workspace-selection-b.json),
and [`workspace-selection-unknown.json`](../openubmc-target-runtime/tests/fixtures/workspace-selection-unknown.json).
For example A, `context_digest` is
`020327250db87ecf91bb4d317dd7357a3d5420bda5c0d626226531856bb7bf50`.

The canonical context participates in the Start input digest. Repeating the same
operation identity and snapshot returns the same Run without redispatch. A
different snapshot under that identity returns `CommandConflict`. Host selection
changes apply to new Starts, so a retry must preserve its original snapshot.

## Fresh records and Task aggregation

The existing `HostContinuity.handoff(task_id, read_run=...)` response adds
`runs[].run_record` and `task_aggregate`. Use a fresh Operator / CI Plane Runtime
projection, such as `read_runtime_projection(ledger_path, run_id)`, for `read_run`.
The projectors in `run_record.py` are pure and write no second record ledger.
The existing handoff may still update its private terminal-answer preparation;
this does not make the complete handoff filesystem-read-only.

Run record v1 fields:

| Field | Meaning |
| --- | --- |
| `schema_version`, `run_id`, `task_ref` | Version `1`, Run identity, and this Host bookmark association |
| `runtime_availability` | `available` or `unavailable` for the fresh read |
| `workspace_binding` | `{status: bound, snapshot: ...}` or `{status: unavailable, snapshot: null}` |
| `workflow_definition_ref` | Existing `schema`, `definition_id`, `version`, `fingerprint`, or `null` |
| `runtime_state` | State reconstructed from fresh Runtime facts, or `null` |
| `outcome_ref` | `sha256:` plus the existing Outcome fingerprint, or `null` |
| `usage` | The explicit unavailable object below |

```json
{"status":"unavailable","input_tokens":null,"output_tokens":null,"cached_tokens":null,"source_ref":null}
```

Task aggregate v1 contains `schema_version=1`, `task_ref`, sorted unique
`run_refs`, `unique_run_count`, and `usage_totals` with the same unavailable
object. A Task association does not create Run ownership or an aggregate success
state. Unknown usage never means zero; v1 does not collect or total provider
tokens, cost, or time. Consumers must tolerate null and explicit availability.

If fresh readback is missing or fails, retain Run and Task identity while all
authoritative record fields become unavailable/null. Notes and cached Turns do
not replace those facts. Host capture failures preserve the committed Runtime
result, and retrying does not repeat the Effect. The existing `terminal_answer`
delivery fields remain the only Host delivery report; a Run record never promotes
prepared output into delivered output.

## Offline acceptance and limits

`test_workspace_run_record.py` tests the public Start-to-Run and Host-handoff
seams with controlled Host/Domain adapters and non-secret fixtures. It covers
atomic binding before the first Effect, failed commit, two projects in one Task,
Task isolation, duplicate/conflicting Starts, immutable resume/fresh reads,
legacy/unavailable identities, malformed/secret input, attempted Agent authority,
Host capture/readback failure, and terminal preparation versus delivery.

W01 does not add an installed Desktop selector, a Desktop or evaluation
implementation, a usage collector, an export/retention service, a device/repo
drift gate, or any live model/device qualification. Those require their own
contracts and evidence. The existing installed-final gate remains independent.
