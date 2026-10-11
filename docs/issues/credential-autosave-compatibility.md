## Roadmap

Finish item 03, the user's approved automatic credential reuse after verified
authentication. Do not ask the user whether to save at each successful
connection. Item 02 input normalization is a separate follow-up.

## Existing baseline

The candidate snapshot `73eeba56dda909c41dfb66345aa15f0dcd067142`
automatically remembers an IP-specific BMC SSH or authenticated Redfish account
at the default port. It intentionally returns `unsupported_scope` for a custom
port and `legacy_source_not_migrated` for a legacy `.env` source. Existing
revision checks, target isolation and fail-soft persistence must remain.

## Required behavior

- A verified SSH/Redfish connection on a valid nondefault port can be
  remembered for that exact IP, purpose, transport and port without making a
  later task silently use a different port. Keep configured defaults and other
  IPs intact; prevent credential reuse across unverified transports.
- A selected legacy credential file can be converted or overlaid so verified
  target-specific autosave works while old SSH, Redfish, OS, Telnet, port and
  environment-precedence behavior remains available. Preserve the original
  private file bytes for rollback. If safe equivalence cannot be established,
  leave the active configuration unchanged and return a specific local reason.
- An authentication failure, trust failure, revision race or persistence error
  never saves a credential and never converts successful target execution into
  failure. Use private local storage only; receipts and tests never print
  secret values.

## Acceptance

- A synthetic successful connection at port 2222/8443 is reused by a fresh
  task with the same endpoint identity. A task for the default port or another
  IP does not inherit that endpoint's credential.
- Legacy SSH/Redfish/Telnet and OS-port fixtures have the same effective
  resolution before and after any migration/overlay; the original file can be
  recovered unchanged. Concurrent changes produce a conflict without data
  loss.
- Packaged Runtime and local configuration path work, focused tests pass,
  and public API/format compatibility is documented.

## Ownership

Part of the 2026-09-27 workflow roadmap. Use a dedicated Git worktree from
`73eeba56dda909c41dfb66345aa15f0dcd067142`. Own credential source,
configuration, resolver, autosave and their tests/docs. Coordinate before
editing terminal-delivery, routing or source-retrieval files.
