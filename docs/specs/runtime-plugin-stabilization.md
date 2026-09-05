# Runtime Stability and Codex Plugin Delivery

## Problem Statement

The Runtime implementation has passed broad behavioral qualification, but the active Codex installation is linked to a different mutable source and its installed Runtime cannot prove the candidate identity. Loose skill directories, Runtime code, MCP launchers, and knowledge-base dependencies therefore drift independently, making failures hard to reproduce, audit, migrate, or roll back.

## Solution

Deliver one relocatable, content-addressed Codex plugin containing the supported Runtime skills and MCP launchers. Installation and startup verify the plugin manifest, source commit, composition digest, dependency state, and launcher inputs before serving requests. Migration preserves external credentials and state, while install, update, rollback, uninstall, and doctor operations produce auditable results.

## User Stories

1. As a Codex operator, I want one plugin identity for Runtime skills and MCP servers, so that the active version is unambiguous.
2. As an operator, I want a deterministic archive and file digest map, so that the package can be audited and reproduced.
3. As a plugin maintainer, I want launchers to derive their root from their installed location, so that relocation does not change behavior.
4. As an operator, I want startup to fail closed when any managed file or dependency changes, so that drift cannot silently execute.
5. As an operator, I want installation to preserve credentials and durable Runtime state, so that updates do not lose operational history.
6. As an operator, I want a doctor command to explain identity, readiness, dependency, and MCP health separately, so that repair is actionable.
7. As an operator, I want update and rollback to be idempotent, so that interrupted installs can be safely retried.
8. As a Runtime user, I want observe and execute to remain available through the packaged MCP transport, so that packaging does not change domain semantics.
9. As a maintainer, I want Python and Node dependency requirements to be explicit and verified, so that clean hosts fail with useful diagnostics.
10. As a release owner, I want qualification evidence bound to the exact source and package digest, so that a published version is defensible.

## Implementation Decisions

- The plugin uses the Codex plugin manifest and standard `skills/` contract.
- The complete existing eleven-skill profile is included with the Runtime package; all components share one plugin identity.
- The MCP configuration invokes relocation-safe launcher scripts. Launchers resolve their own plugin root and never embed a development checkout path.
- Runtime startup verifies API version, composition file digests, source identity, and absence of unmanaged executable artifacts before importing a verified snapshot.
- Knowledge-base dependencies are prepared from the production lockfile into an external dependency cache with content verification before startup. Installation never writes the plugin payload.
- Credentials, Runtime state, and configuration remain outside the plugin payload and are not copied into archives.
- Package metadata contains a source commit, package version, manifest digest, file digest map, and qualification references; the release audit records the archive digest alongside the package lock.
- Existing Runtime state ownership remains unchanged: RunEngine and MutationJournal remain the sole durable authorities.

## Testing Decisions

- Test at public seams: package build/verify, relocated launcher startup, MCP observe/execute, install doctor and migration, and Codex plugin add/list/remove in an isolated home.
- Use deterministic archive tests with fixed metadata and stable ordering.
- Use tamper tests for manifest, source files, dependency lock, launcher, and external state.
- Run the existing Runtime qualification and repository validation suites without private-method assertions.

## Out of Scope

- New BMC business behavior or another hardware upgrade.
- Additional plugin hosts beyond Codex.
- Reintroducing retired compatibility writers or changing Runtime state ownership.

## Further Notes

The plugin is released only after clean-source qualification, package verification, isolated Codex lifecycle testing, repository CI, review, and the existing immutable release gate.
