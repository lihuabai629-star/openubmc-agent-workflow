# openUBMC Agent Workflow

Coordinated openUBMC development, diagnosis, build, delivery, and target-runtime workflow for
Codex. The integrated Skills baseline is
`ceb46e8ca5542a4273128b1a4aa8606b2f5eaee0`.

## Install

The bootstrap downloads the installer and leaves only the managed checkout:

```bash
WORKFLOW_REF="vX.Y.Z" # published release tag or full commit
export GH_TOKEN="$(gh auth token)"
gh api \
  "repos/lihuabai629-star/openubmc-agent-workflow/contents/bootstrap.py?ref=${WORKFLOW_REF}" \
  --header "Accept: application/vnd.github.raw" \
  | python3 - --ref "${WORKFLOW_REF}"
unset GH_TOKEN
```

Use the same immutable ref in the bootstrap URL and `--ref`. Managed installation rejects a
missing ref, a branch such as `main`, or a name that does not resolve as an exact remote tag.
Private GitHub access uses `GH_TOKEN` or `GITHUB_TOKEN` only for authenticated downloads and Git
fetches; the installer does not write the token to the checkout remote, installer state, or logs.

The default `full` profile installs 11 Skills, the Target Runtime, and the standalone
`openubmc-kb` stdio MCP. The smaller runtime profile keeps the seven runtime-path Skills:

```bash
WORKFLOW_REF="vX.Y.Z" # published release tag or full commit
export GH_TOKEN="$(gh auth token)"
gh api \
  "repos/lihuabai629-star/openubmc-agent-workflow/contents/bootstrap.py?ref=${WORKFLOW_REF}" \
  --header "Accept: application/vnd.github.raw" \
  | python3 - --ref "${WORKFLOW_REF}" --skill-profile target-runtime
unset GH_TOKEN
```

Codex receives the managed Skill links and stdio MCP entries. Repair, update, rollback, and
uninstall can retire workflow-owned entries from older multi-client installations without touching
unrelated client configuration.

The Target Runtime MCP defaults to the two-operation Agent Interface: `observe` for bounded live
queries and `execute` for stateful workflows. Raw Evidence, Replay, Session Outcome governance,
Case lifecycle, and Runtime status are available only through the explicit `operator` profile.
The evidence-gated compatibility retirement is complete on canonical `main`: legacy Agent inputs
and the compatibility profile are rejected, while explicit old-event upcasters and historical
telemetry readers remain. See [Agent Semantic Gateway](docs/agent-semantic-gateway.md) and the
[machine-readable roadmap closeout](docs/roadmap-completion.json).

## Architecture and evolution

The stable product boundary, vocabulary, accepted decisions, market comparison, and phased roadmap
are maintained in the following records:

- [Domain context](CONTEXT.md)
- [Architecture Decision Records](docs/adr/README.md)
- [Architecture arbitration](docs/workflow-architecture-arbitration.md)
- [Market workflow design research](docs/workflow-design-market-research.md)
- [External research reconciliation](docs/external-workflow-research-reconciliation.md)
- [Evolution roadmap](docs/workflow-evolution-roadmap.md)
- [Roadmap completion evidence](docs/roadmap-completion.json)
- [Domain Pack authoring contract](docs/domain-pack-authoring.md)
- [Runtime-internal model planning prototype](docs/model-planning-prototype.md)
- [Product closeout qualification](docs/product-closeout-qualification.md)
- [Codex Adoption Qualification](docs/codex-adoption-qualification.md)
- [Continuous closeout evidence collector](docs/continuous-closeout-qualification.md)

## Credentials

BMC and OS credentials:

```bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" credentials
```

openUBMC KB OneID credentials:

```bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" credentials --kb
```

An existing private KB configuration can be imported with `credentials --kb --kb-config <file>`.
The MCP starts and reports configuration status even before OneID credentials are added.

## Lifecycle

```bash
INSTALLER="$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py"

python3 "$INSTALLER" check
python3 "$INSTALLER" repair
python3 "$INSTALLER" update
python3 "$INSTALLER" rollback
python3 "$INSTALLER" uninstall
```

For tag/commit installations, `update` revalidates the recorded immutable ref. To move to a newer
release, rerun the bootstrap with the new tag or full commit; the previous commit becomes the
rollback target. Legacy branch-based installations fail check, install, repair, and update until
they are migrated by rerunning bootstrap with an immutable ref. `rollback` restores the previous
known-good revision as an exact commit and keeps the displaced revision available for another
rollback. JSON install and check output reports `requested_ref`, `ref_kind`, and `resolved_commit`.

## Validation

```bash
python3 scripts/validate_workflow.py
```

Optional bounded dual-target smoke:

```bash
python3 scripts/live_smoke.py \
  --target 10.121.136.200 \
  --target 10.121.177.159
```

The default smoke runs the MDB-only capability preflight for every target concurrently and prints
a compact capability comparison; it does not open a full diagnostic Case. A slow target is cut off
at the configured timeout without delaying the other result. Live Patch remains plan-only in this
tool. Upgrade verification uses the read-only preflight path when an artifact and product version
are supplied. Internal BMC TLS mode is the default; pass `--strict-tls` for system certificate
verification.

## Skill entrypoint and package manifest

`SKILL.md` remains the Agent Skill entrypoint. Agents use its frontmatter and instructions to
discover, select, and execute a Skill.

Each installable Skill also contains a repository-specific `skill.json`. It is an internal package
manifest, not a replacement for `SKILL.md` and not a new cross-agent Skill standard. The workflow
validator and packager use it to declare the exact distributable file set, reject omitted files,
and calculate the per-Skill digest recorded in `release-lock.json`.

In short:

- `SKILL.md`: agent-facing behavior and instructions.
- `agents/openai.yaml`: client-facing presentation metadata.
- `skill.json`: this repository's packaging and integrity metadata.

## Immutable releases

Managed releases use a two-commit topology. The source commit contains the final code and the
following lock-only commit adds `release-lock.json`; the release tag points to the lock-only commit.
The lock records the source commit, workflow and schema identities, every Skill package digest, the
Target Runtime digest, and the supported client/profile compatibility matrix.

Verify that identity from the lock-only commit (or its release tag). Mutable `main` may advance
afterward while retaining the historical lock snapshot, so running lock verification against a
later main tree is expected to fail. A new release selects and qualifies a new source commit, then
creates a new lock-only child; it never rewrites the earlier snapshot.

Generate and verify the lock after the source tree is committed and clean:

```bash
python3 scripts/generate_release_lock.py generate \
  --root . \
  --source-commit "$(git rev-parse HEAD)"
python3 scripts/generate_release_lock.py verify --root .
```

For managed installations of workflow version 1.2 or newer, installation fails before managed
links are changed when the lock is missing, invalid, or incompatible. `check --json` reports the
resolved immutable release identity. Linked development checkouts remain mutable by design and are
reported as `linked-development-source` rather than being treated as a release.

For managed tag or full-commit installations, an unverified Release identity makes top-level
installation health fail even when the Runtime remains operational. The JSON report keeps
`operational_ready`, `release_identity_verified`, and `evaluation_ready` separate so development
usability cannot be mistaken for verified Release health.

Before promotion, run the execute A/B qualification described in
`docs/agent-semantic-gateway.md`, then pass its digest-bound `summary.json` (with the adjacent
`all_metrics.json`, `schedule.json`, and `run_evidence.json`) to the ordered release gate:

```bash
python3 scripts/release_gate.py \
  --current-ref v2.0.2 \
  --previous-ref v2.0.1 \
  --model-identity '{"model":"gpt-5.6-sol"}' \
  --codex-identity '{"version":"codex-cli 0.151.0"}' \
  --ab-evidence /path/to/qualification-results/summary.json \
  --ab-attestation-public-key /path/to/trusted/ab-evidence-signing-key.pub \
  --output release-gate.json
```

`--current-ref` may be the checked-out symbolic `HEAD`, the full lock-only commit, or a tag that
resolves to that commit. The gate records the requested ref, resolves it once, verifies the
lock-only topology, checks that GitHub can retrieve the candidate, and then passes the immutable
release commit to managed clean-install and upgrade checks. Unpublished local candidates stop
before the ordered gate sequence with publication guidance. The JSON report retains
`requested_ref`, `release_commit`, and the lock-recorded `source_commit` for audit replay.

The gate requires clean installation, previous-to-current upgrade, rollback, Agent Interface
contracts, and deterministic Case Replay smoke in that order. A failed gate skips all later gates
and prevents promotion. The GitHub Release workflow applies the same ordering and publishes the
prepared draft release only after the gate job succeeds.

Formal model and Codex identities are required at the Release Gate boundary, propagated into
Codex Adoption Qualification, and checked against the returned provenance before promotion.

## Session Outcome governance

The Target Runtime can record redacted Session Outcomes and aggregate recurring failures by
workflow, domain, outcome, and gap type. Outcomes follow an explicit review lifecycle: recorded,
reviewed, independently approved or rejected, and optionally promoted.

Approved outcomes may become an inert Golden Scenario, knowledge item, or ADR. Golden Scenarios
must carry a matching deterministic Case Replay Bundle. Hard-to-reverse architecture conclusions
must become ADRs linked to their Case and Replay evidence. Promotion artifacts are always marked
non-executable and never modify workflow execution rules automatically.
