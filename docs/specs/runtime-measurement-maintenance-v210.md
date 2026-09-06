# Runtime measurement and plugin recovery

Tracking: #179; implementation and evidence: #180–#184.

## Problem Statement

The v2.0.9 end-to-end comparison mixes model behavior, provider waiting, an additional diagnosis
Gate, Runtime processing, and plugin startup. Its timing and token ratios cannot identify a Runtime
regression. Dependency preparation can also wait indefinitely, leave child processes after an
interruption, and delete a usable cache before a repair succeeds.

## Solution

Measure the same legal workflow with and without a model, preserve every attempted invocation,
and expose separately timed plugin startup stages. Prepare dependencies in an isolated directory
and publish only verified content, with bounded waits and recoverable interruption.

## User Stories

1. As an operator, I want the exact source identity beside each result so that I can reproduce it.
2. As a maintainer, I want Runtime time without a model so that provider delays do not become CPU claims.
3. As a maintainer, I want each Gate measured so that added acceptance work remains visible.
4. As an operator, I want full diagnosis evidence and Gate bindings so that smaller output remains usable.
5. As a maintainer, I want the same release compared with itself so that I can see measurement noise.
6. As a maintainer, I want model and cache conditions recorded so that tokens remain interpretable.
7. As an operator, I want dependency stage progress and deadlines so that an installation cannot silently hang.
8. As an operator, I want cancellation to stop owned workers so that retries cannot race old writers.
9. As an operator, I want failed repairs to retain the last verified cache so that recovery is repeatable.
10. As an operator, I want offline packages checked against the same hashes so that caching preserves integrity.
11. As a maintainer, I want cold and warm plugin startup measurements so that verification cost is visible.
12. As a release owner, I want final-source qualification and an immutable lock so that publication is auditable.

## Implementation Decisions

- v2.0.9 source `117ffcc580a98e2754c2cc998fa3cd4ce427d7c6` is the reference for new measurements.
  v2.0.2 remains historical context. No published tag or signed report is rewritten.
- The deterministic client uses the public JSON-RPC Endpoint with the real RunEngine and SQLite
  repository. A local Domain Adapter supplies fixed diagnosis facts and has no target transport.
- Each flow discovers tools, starts a source-only Run, submits diagnosis.acceptance bound to its
  Evidence IDs, then submits developer.change and checks the terminal Outcome.
- Endpoint wall/CPU time includes dispatch, storage and MCP text projection. Final JSON encoding,
  serialized request/result bytes, field sizes, storage deltas and event counts are separate.
  This is a local fixture measurement, not a BMC performance claim.
- Fresh-service samples recreate Runtime state; reused-service samples reuse the last completed
  service. Python module and OS caches are not flushed and are explicitly recorded as uncontrolled.
- Real-model A/A uses identical source, prompt, Codex CLI 0.153.4, low reasoning effort, disabled
  shell/plugins, fresh client homes, and interleaved A/B labels. Primary gpt-5.6-sol and secondary
  gpt-5.6-luna are reported separately. Provider cache counts and returned model names are retained;
  a returned name does not prove an immutable backend model build.
- A loopback relay forwards provider responses unchanged and records first-byte and request time.
  Provider time includes network, queue and model generation. It cannot isolate server compute.
- A correct rejected model request is a model request deviation, not a Runtime failure. Completed
  flows with recovery or extra calls remain in success/efficiency denominators and are separately
  identified from clean three-call flows. Timeouts, provider errors, Runtime failures and harness
  faults remain distinguishable. Failed requests and incomplete batches remain visible.
- Dependency preparation uses an exclusive cache lock, phase deadlines, process-group cleanup,
  bounded retries and a staging directory. Install workers retain the lock if their parent is
  forcibly killed. A previous directory permits recovery across interrupted publication.
- Python hash-locked wheels and npm lockfile integrity remain mandatory. Offline mode uses local
  wheel locations and npm caches. No receipt is published for a partial installation.
- Optional startup timing records travel outside MCP stdout and outside the immutable plugin.
  Measurements distinguish verification, dependency identity/lock, snapshot verification/copy,
  process initialization, and first tool round-trip.
- Output reductions are conditional on measured duplication and preserved semantics. There is no
  byte target that removes a Gate, evidence, or accepted diagnosis; ADR-0007 remains authoritative.

## Testing Decisions

Tests exercise public MCP and relocated plugin CLI behavior, using real filesystem state and
package-manager boundaries. Existing package/activation and Runtime qualification tests provide
prior art. Tests must detect unsafe outcomes rather than assert private call sequences.

- Local pilot: two 30-fresh/10-reused batches, retained separately from subsequent measurements.
- Final deterministic comparison: at least 30 fresh and 10 reused flows per source; retain failures.
- Model A/A: 10 interleaved pairs per model, fixed before execution. Pilots are labelled separately;
  no unsuccessful invocation is overwritten or silently replaced. Extend to 30 only if the result
  is needed for a release decision and remains inconclusive; explanatory A/A is not itself a gate.
- Dependency tests cover ordinary failure, timeout, cancellation, force-killed parent, held lock,
  interrupted directory publication, failed repair, retry, offline wheels and hash tampering.
- Plugin observations cover three separated process windows, both servers, cold and warm snapshot
  caches, followed by full native Codex install/update/rollback/reinstall and lifecycle qualification.
- Timing regression review uses medians and p95 against both A/A noise and semantic equivalence.
  No strict timing confidence claim is made from unpaired local batches or insufficient samples.
- Run complete repository validation, review, fresh final-source Runtime/plugin qualification, CI,
  and the existing lock-only release gates before publishing a maintenance version.

## Out of Scope

BMC writes, new target upgrades, additional plugin hosts, and changes to Run/Mutation ownership.

## Further Notes

Raw records contain source and harness identity; reports include their digests. Final audit links
attempt denominators, review findings, test results, package digest, CI and release decisions.
