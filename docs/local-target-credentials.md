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
  },
  "target_ports": {
    "192.0.2.10": {"bmc": {"ssh": {"2222": "special-bmc"}}}
  }
}
```

Target keys must be literal IPv4 or IPv6 addresses. Equivalent IPv6 spellings
select the same override; contradictory entries are rejected. Hostnames can use
default records, but DNS resolution never extends an IP override to a hostname.
An override selects the entire record. An incomplete record fails locally; it
cannot borrow a username or password from the default. An authentication failure
does not cause the Runtime to try the default password for that target.
`targets` applies at the transport's default port (SSH 22, Redfish 443).
Optional `target_ports` selects an exact nondefault port for the same IP,
purpose and transport. Port keys are canonical decimal strings from 1 to
65535. An unmatched nondefault port may use a configured global default or
legacy fallback, but cannot inherit that IP's default-port override. The
resolved credential retains the requested port. This is an additive schema
version 1 field; old configurations keep their previous default-port meaning.

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

## Automatic remembering in the production Runtime

The packaged MCP/Runtime CLI composition enables best-effort remembering, with
no separate consent prompt and no additional authentication probe. A successful
BMC SSH authentication, or a Redfish transport that actually authenticates while
opening its session (currently log-bundle collection), saves an exact IP override.
Subsequent tasks reuse it through the existing resolver. The private setup page
already applies the same connect-then-save behavior for BMC/OS SSH and Redfish.

Saving is not a prerequisite for the current operation: a storage error, pending
draft, or configuration changed during authentication leaves the connection usable.
Lane status reports `credential_persistence.remembered` and a bounded reason code.
The existing account/defaults and other IPs/purposes/transports are preserved. An
unchanged account is not saved again; the original legacy file is never rewritten.
Successful authentication on a nondefault port saves only its exact
`target_ports` reference. A fresh task must select that same port to reuse it;
the default port and another IP do not receive the custom-port account.

An automatically created store sets `legacy_environment_fallback: true`: when
there is no configured reference for an IP/purpose/transport, existing canonical
environment credentials remain usable. Explicit references still win as complete
records, and ordinary existing JSON files retain their previous no-fallback rule.
This avoids breaking a second protocol or device when only the first was saved.

For a selected legacy `credentials.env`, the first verified save activates a
private JSON overlay at the same source path. The original file remains
byte-for-byte unchanged for rollback and compatibility CLIs. The overlay has
`legacy_source_overlay: true` and exact target references; when none matches,
the Runtime rereads the original legacy file with its existing per-field
environment precedence. Its Telnet, OS IP and OS SSH port settings remain
available. The overlay is guarded by revision checks and a comparison with
the original source under the storage lock. If aliases or source syntax make
equivalence uncertain, autosave leaves the active configuration unchanged and
reports `legacy_equivalence_unproven`; concurrent changes report
`configuration_changed`. These are bounded local reasons and do not fail the
successful connection. Restoring the original behavior requires deactivating
the private overlay marker; the original `.env` bytes require no restoration.

Hostnames remain in-memory/legacy because an IP-specific account must not
silently follow DNS changes. A Redfish Basic-auth client constructor is not evidence
of authentication and does not trigger saving. Standalone library users can inject
`VerifiedCredentialMemory` into `RuntimeMcpService`; it is not a global side effect.

If a credential may have entered a task transcript, process, Runtime record, or
log, follow [Credential exposure response](credential-exposure-response.md). The
response rotates the identity at its owning authority and activates a new private
revision without sending the value through the Agent Interface.
