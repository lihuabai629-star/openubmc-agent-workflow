# Installed candidate qualification

## Decision

Release qualification has two explicit validation paths. The default path uses
successful, executed GitHub Actions jobs for the exact Final source. The
installed-candidate path uses one immutable plugin archive installed and
exercised on the supported hosts. A skipped or zero-step hosted job is never
recorded as a passing job.

The installed-candidate path is available when hosted runners cannot start. It
does not remove the other Release Gate checks or turn a source-tree test into
installed-product evidence. The final Release Gate report identifies which
path was used.

## Evidence contract

- Select a clean, full source commit. Build and qualify one archive from that
  commit, record its SHA-256 and qualification report, and reuse those exact
  bytes across the Linux and native Windows observations. The release
  verifier reads the archive inventory and binds its source commit, content
  digest and version to the qualification report and release lock.
- Run complete repository validation and immutable plugin qualification on a
  native x86_64 Linux host. The captured commands, exit codes, test counts,
  toolchain identities and private-log hashes must be inspectable.
- Install the archive with native Windows Codex and exercise the marketplace
  bootstrap and unavailable-backend setup path. Exercise healthy Runtime MCP,
  native device routing, credential revision reuse and single-Effect recovery
  on Windows against a synthetic target. WSL evidence is optional and cannot
  replace the native Windows device row.
- Read the same Run ID and Outcome digest through the installed plugin and the
  separate Desktop client. Bind the Desktop installer and source identities.
- Keep the hosted-CI row explicitly untested with its reason. An executed CI
  failure is a blocker in either validation path.
- The platform matrix, Release Gate source commit and archive bytes must agree.
  The release verifier hashes the archive itself; a copied digest in a report
  is insufficient.

Each capture retains private stdout/stderr logs outside the repository or in an
ignored directory. Shared reports contain hashes and bounded status, never
credentials, tokens or live device identifiers. Real Agent trial acceptance,
Runtime safety, upgrade/rollback and formal A/B evidence remain separate gates.

## Interface and failure behavior

`platform_acceptance.py` initializes and verifies an installed-candidate matrix
with an explicit archive. `release_gate.py` accepts that matrix, archive and
qualification report together as an alternative to its default GitHub CI query. Missing inputs,
different source commits, different archive bytes, incomplete native rows,
failed commands, unsupported validation modes or tampered report digests fail
closed. Historical Release Gate and hosted-CI reports remain verifiable.

The upgrade baseline is the latest published public marketplace release, while
the candidate source and lock-only commit remain in the private workflow
repository. These repositories have different release histories; the source
repository's older GitHub Release list is not the plugin's installed baseline.

The immutable archive may be distributed as a draft candidate for installation
tests. A stable release is published only after every selected Release Gate and
the separate Agent-trial acceptance passes for the same source and package.
