# Verified credential autosave: endpoint identity and legacy overlay

## Specification

- Goal: after an authenticated SSH or Redfish session, remember its account for
  the literal IP, BMC/OS purpose, transport and actual port. A later task at a
  different port or IP must not select that endpoint's record.
- Repository and files: `openubmc-agent-workflow`; Runtime local credential
  memory, source, resolver, configuration validation, focused tests and local
  target credential documentation. The dedicated `codex/issue-277` checkout
  starts at `73eeba56dda909c41dfb66345aa15f0dcd067142`.
- Contract: retain schema version 1 and the existing default-port `targets`
  references. Add optional `target_ports` for nondefault port references. An
  activated legacy overlay keeps the original private `.env` file unchanged
  and uses its existing per-field environment precedence whenever an exact
  verified target reference is absent. Explicit local selectors stay bound.
- Standard gate: this changes private Runtime configuration format and
  compatibility, so mark local interface review risk. It does not change BMC
  MDB/MDS, Redfish URIs, request/response fields, permissions or generated
  component files. The applicable references are the existing local target
  credentials and configuration activation contracts and the developer
  standard matrix's ProfileSchema/config row.
- Acceptance: fresh-task resolution at SSH 2222 and Redfish 8443; default
  port/other IP/transport isolation; old SSH, Redfish, OS, Telnet, OS-port and
  environment values before and after overlay; exact original file bytes;
  pending edits and concurrent source changes fail soft; packaging includes
  the changed Runtime; public receipts contain no secrets.
- Risk and rollback: an incomplete or ambiguous legacy source cannot be
  overlaid; return a bounded local reason without activating a snapshot.
  Revert this issue commit to restore prior code. The original private file
  stays byte-for-byte intact, and existing saved/active revisions retain
  optimistic conflict checks. No production target access is needed.

## Execution

1. Extend the local schema validator and resolver for port-qualified
   references while preserving existing default-port lookup behavior.
2. Add a legacy overlay to verified autosave. Reuse the original private file
   as the fallback source and guard its bytes under the configuration lock.
3. Test synthetic authenticated lanes, fresh tasks, legacy equivalence,
   conflicts, fail-soft errors and secret-free receipts.
4. Run the focused Runtime and local configuration tests, then build the
   Runtime package and inspect its contents. Review the diff and commit only
   this branch before opening a PR.

## Local verification

- Python 3.14 `unittest discover` passed: credential memory 20, local
  credentials 13, configuration activation 7, MCP contracts 36, SSH lane 14,
  and Redfish lane 3 (93 tests total).
- `uv build --wheel` produced the Runtime wheel. An isolated environment
  installed that wheel and resolved a remembered SSH port 2222 through a fresh
  resolver while port 22 and another IP still used the unchanged legacy file.
- `git diff --check` passed. No live BMC or native Windows writer was used in
  this local qualification; installed-plugin and Linux/WSL acceptance belong
  to the integrated roadmap gate.
