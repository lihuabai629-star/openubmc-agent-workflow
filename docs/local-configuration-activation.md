# Local configuration activation

Target credentials and KB accounts use separate current-user configuration sources.
Existing source files remain intact. A saved revision is an immutable private JSON snapshot;
activation atomically selects the latest saved revision. Revision identifiers are random and
contain no secret-derived digest.

The local Python interface is `LocalConfigurationStore(path, kind="targets" | "kb")`.
`save(config, expected_revision=...)` rejects stale edits. `activate(revision,
expected_active_revision=...)` rejects stale activation. `status()` exposes saved and active
revision identities, without credential values. `verified: false` means no connection evidence
has been recorded by this configuration store; saving or activation never establishes that
an account works.

Target configuration uses the schema in [local target credentials](local-target-credentials.md).
KB configuration accepts account, OAuth and LightRAG endpoints, client configuration, scopes,
request timeout and token-cache path. Incomplete account records may be saved; connection
attempts report missing credentials. HTTPS is required for configured remote KB endpoints;
HTTP is permitted for loopback services. Passwords and client secrets remain local.

A live Runtime pins the selected revision for an entire public request, including internal
collection partitions and retries. Explicit activation takes effect on the next request,
replacing credential leases at a safe operation boundary. Run, MutationJournal and Outcome
identities survive the change. A live KB process similarly captures one immutable client per
request and reloads on activation. Tokens are scoped to account, endpoints and revision.
Legacy KB credential environment overrides keep their existing precedence.

Snapshots live in `.<source-name>.revisions/`; the `.<source-name>.saved.json` and
`.<source-name>.active.json` sidecars contain revision pointers only. Source and snapshot files
must stay local. Snapshot files are mode 0600 and their directory is mode 0700 on Linux/WSL.
The writer requires POSIX locking; native Windows activation is not qualified.
