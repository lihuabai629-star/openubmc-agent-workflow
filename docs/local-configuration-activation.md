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
Its version 1 `target_ports` references and `legacy_source_overlay` flag are
additive. An overlay snapshot selects verified exact endpoints while the
unchanged original private legacy file supplies unmatched SSH, Redfish, OS,
Telnet and port values through the previous environment precedence. A verified
autosave compares both the active revision and original source before commit.
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

For the Linux/WSL backend, keep private configuration and test fixtures on a
filesystem that enforces these POSIX ownership and mode requirements. A Windows
drive mounted under `/mnt/c` may not preserve mode 0600 even after `chmod`;
the configuration writer then rejects its lock as non-private. Keep the
permission check intact. Use the WSL native filesystem for private state, and
use a native temporary directory such as `/tmp` for synthetic configuration
tests when the source checkout is on a mounted Windows drive. This does not
require moving the source checkout or changing mount options or Windows ACLs.

The verified save-and-activate path stages private marker backups under that
lock. On a recoverable write failure, it restores the prior saved/active
pointers and removes the new snapshot before reporting a storage failure.
If the filesystem also prevents rollback, inspect the private revision state
before another save.
