# openUBMC Documentation Review

Present one review interface and keep the individual checks internal.

## Inputs

- One Markdown file, a documentation directory, or an explicit document set.
- A profile: `quick`, `full`, or `release`. Infer `quick` for an unspecified
  lightweight check and `full` for an unspecified comprehensive review.
- Optional source tree, product version, or existing Target Runtime Case when the
  user requests product-consistency evidence.

Do not ask the user to select individual analyzers. Select them from the profile and
the available evidence.

## Profiles

- **quick**: deterministic sensitive-data, link, Chinese typo, English spelling,
  and punctuation scans; then report confirmed release blockers and obvious
  structural gaps.
- **full**: quick plus document classification, completeness, readability,
  navigation and metadata, command extraction, and source/product consistency.
- **release**: full plus bounded validation of commands and product claims when
  the required environment and authorization are available. Release does not
  imply permission to mutate a target.

Read [references/profiles.md](profiles.md) for the detailed selection and
degradation rules. Read only the other reference relevant to the active lane:

- [references/content-review.md](content-review.md) for structure,
  TEGR completeness, and human readability.
- [references/completeness-checks.md](completeness-checks.md) for
  full/release single-document and versioned document-set coverage.
- [references/readability-and-evidence.md](readability-and-evidence.md)
  for the human-first criteria and support for technical claims.
- [references/product-consistency.md](product-consistency.md) when
  comparing claims with source, KB candidates, or runtime evidence.
- [references/command-validation.md](command-validation.md) before
  validating any extracted command.
- [references/report-contract.md](report-contract.md) when assembling
  the final findings and verdict.
- [references/handoff-contract.md](handoff-contract.md) whenever the
  request carries a Target Runtime `case_id` or `phase_record_contract`.

Maintainers can read [references/provenance.md](provenance.md) when
updating the bundled analyzers; it is not required during an ordinary review.

## Deterministic scan

Run the bundled scan first:

~~~bash
python3 scripts/static_review.py <path> --profile quick
python3 scripts/static_review.py <path> --profile full
~~~

The scan never executes commands found in documents and never contacts a target.
Treat `confirmed` findings as evidence-backed candidates. Review every
`pending_review` finding against its surrounding text before retaining it.
Script success proves only that the static lanes ran; it does not prove the
document is correct or complete.

The static result lists required semantic reviews as `not_run` and keeps a failed
scanner visible. Complete those applicable reviews before issuing the requested
full/release verdict; static `ok` never means command/product validation passed.

## Evidence order

Use the strongest available evidence:

1. The reviewed document and local repository source/configuration.
2. Build artifacts, tests, generated schemas, and versioned interface definitions.
3. Existing openUBMC KB MCP results as candidate locations or explanations.
4. Existing Target Runtime Case evidence or explicitly authorized read-only target
   collection.

KB results are not product truth. Runtime observation may be version-specific, so
record target identity and product version. If evidence conflicts, report the
conflict instead of choosing the more convenient source.

## Case workflow

When `workflow.next` returns `waiting_phase_record` for
`documentation.review`, consume its handoff directly. Review the supplied paths
with the supplied profile, submit the typed outcome through `phase_record`, then
call `workflow.next` again in the same turn. Do not create a separate review
state or ask the user to restate the scope.

A passing review records `status=completed` and verdict `pass` or
`pass_with_warnings`. A release-blocking or materially incomplete review records
`status=failed` with verdict `fail` or `incomplete`; this stops downstream
build, upgrade, or delivery until a new workflow attempt supplies a passing
record. Follow [references/handoff-contract.md](handoff-contract.md)
exactly.

## Safety and authorization

- Extracting or classifying commands is read-only.
- Do not execute an extracted command merely because the profile is `release`.
- Read-only local commands may run when they are clearly scoped to the review.
- Target access, builds, installations, upgrades, file writes, service changes,
  network configuration, and destructive commands require the authorization
  normally required by the host workflow.
- Prefer an existing Target Runtime Case. Record new target evidence in a Case when
  the runtime is available; do not create a second state system in this Skill.
- If validation is unavailable, mark the claim `unverified`; do not turn missing
  evidence into a failure.

## Outcome

Return one deduplicated report, not one report per internal analyzer. Each retained
finding must include severity, rule, file and line when available, evidence,
recommended action, confidence, and verification state. Separate:

- document defects;
- product/source inconsistencies;
- environment limitations;
- unverified claims.

Use the verdicts `pass`, `pass_with_warnings`, `fail`, or `incomplete`.
Do not manufacture a numeric score unless the user explicitly requests one.
