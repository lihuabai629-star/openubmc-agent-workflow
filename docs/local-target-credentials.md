# Local target credentials

The Runtime reads the current user's `openubmc/credentials.json` under
`XDG_CONFIG_HOME`, or `~/.config` when that variable is unset. POSIX files must be
owned by the current user with permissions `0600` or stricter. Sources are bounded
regular files; symbolic links are rejected. Values stay in local Runtime memory
and Domain Adapter input. Public receipts contain readiness and failure reasons.

A record contains a complete username/password pair, or an SSH username and
existing key-file reference. Records can be explicitly shared between purposes:

```json
{
  "schema_version": 1,
  "credentials": {
    "lab-bmc": {"user": "BMC_USERNAME", "password": "LOCAL_BMC_PASSWORD"},
    "special-bmc": {"user": "OTHER_USERNAME", "password": "LOCAL_OTHER_PASSWORD"},
    "host-os": {"user": "OS_USERNAME", "identity_file": "/private/path/id_ed25519"}
  },
  "defaults": {
    "bmc": {"ssh": "lab-bmc", "redfish": "lab-bmc"},
    "os": {"ssh": "host-os"}
  },
  "targets": {
    "192.0.2.10": {"bmc": {"ssh": "special-bmc", "redfish": "special-bmc"}}
  }
}
```

Target keys must be literal IPv4 or IPv6 addresses. Equivalent IPv6 spellings
select the same override; contradictory entries are rejected. Hostnames can use
default records, but DNS resolution never extends an IP override to a hostname.
An override selects the entire record. An incomplete record fails locally; it
cannot borrow a username or password from the default. An authentication failure
does not cause the Runtime to try the default password for that target.

| Source choice | Behavior |
| --- | --- |
| Source already bound to a task | Reused for its subsequent targets and requests |
| Explicit local `config_path` | Selects that source |
| `OPENUBMC_CREDENTIALS_CONFIG`, `OPENUBMC_CREDENTIALS_FILE`, `OPENUBMC_DEBUG_CREDENTIALS_FILE` | All supplied selectors must name the same normalized path |
| No explicit source | Prefer standard `credentials.json`, then existing `credentials.env`, then legacy environment values |
| Explicit per-operation username / named environment / key selectors | Retain the legacy source family for that transport; do not combine it with a JSON record |

Within a JSON source, an exact IP override wins over the corresponding default.
BMC and OS purposes, and SSH and Redfish transports, are independent. OS records
are used only when an OS target is explicit. A Runtime-selected JSON record wins
over ambient legacy defaults in Debug, Log, Live Patch and Upgrade adapters.

Legacy `KEY=VALUE` files remain readable and are preserved. Their existing
per-field environment precedence remains: named environment or explicit argument,
then canonical environment values, then file values. `REDFISH_USERNAME` and
`REDFISH_PASSWORD` remain supported legacy names. Different explicitly selected
source files and conflicting duplicate fields are reported as conflicts. The
Runtime does not search other directories for credentials.

New installer-managed shell hooks leave target credential selectors unset unless
the caller explicitly selected one. Older `env.sh` files may already have exported
`OPENUBMC_CREDENTIALS_FILE` for the standard legacy file. That inherited value remains
an explicit source to the Runtime; it cannot safely infer whether the user chose it.
Regenerating the managed hook takes effect in a new shell. For a single invocation
that should use standard discovery, clear only the selector known to come from the
old hook, for example `env -u OPENUBMC_CREDENTIALS_FILE <command>`. Keep a deliberately
selected source. Plugin-only migration preserves old shell files, so it does not
by itself refresh that hook or the environment of an already running task.

`CredentialResolver.resolve_local` returns a typed SSH or Redfish credential and
whether that task/target/purpose/transport lookup reused its cache. Missing local
records return `credentials_missing`, conflicting sources return
`credentials_conflict`, and invalid data or unsafe file access returns
`credentials_invalid`. These failures stop before target access. Credentials do
not authorize new operations; the existing observation and mutation boundaries
continue to decide what may execute.
