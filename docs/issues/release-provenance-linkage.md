Part of #278, roadmap item 22.

## Problem

The repository already has a source-bound release lock, package integrity
checks and qualification evidence, but operators still need one verifiable
link from a distributable plugin artifact to its source, dependency inventory
and the exact qualification report. A passing local test is not a published
attestation.

## Scope

- Produce a bounded, deterministic provenance manifest for a locally built
  plugin package: source commit/tree, package digest, dependency inventory or
  SBOM digest, and qualification report digest. Use existing release-lock and
  package validation machinery rather than a second release authority.
- Verify the manifest against the actual package bytes and local evidence.
  Reject missing, stale, mismatched or unverifiable inputs; preserve the
  existing package and release paths for callers not requesting a manifest.
- Do not claim a signature, hosted CI run, or published release unless those
  exact artifacts were generated and independently verified. No release is
  published as part of this issue.

## Acceptance

- Reproducible offline fixture verifies the same package/source/dependency/
  qualification identity. Modifying any component fails verification.
- A source commit with a dirty worktree is either rejected or explicitly
  recorded as non-releasable; provenance never silently asserts clean source.
- Existing package, release-lock and CI scripts remain compatible. The
  generated manifest contains no credentials or machine-local secrets.

## Ownership

Own packaging, release provenance and tests/docs. Avoid concurrent Agent/MCP,
credential, source-index, and Debug evidence files.
