# openUBMC Agent Workflow

Coordinated openUBMC development, diagnosis, build, delivery, and target-runtime workflow for
Codex. The integrated Skills baseline is
`ceb46e8ca5542a4273128b1a4aa8606b2f5eaee0`.

## Install

The supported backend runs inside Linux or WSL. Install through the native Codex marketplace:

```bash
codex plugin marketplace add lihuabai629-star/openubmc-codex-plugins
codex plugin add openubmc@openubmc-public
```

Start a new Codex task after installation or an update. The public marketplace version and
local development version are separate; inspect the selected version with `codex plugin list --json`.
See [plugin installation and lifecycle](docs/codex-plugin.md) for preparation, private account
configuration, migration and recovery.

The default catalog contains 11 workflow Skills: plugin management, diagnosis, log analysis,
development, bmcgo build, UT/IT, QEMU, source submission, upgrade, live patch and Conan upload.
Local log analysis and remote collection share one entry and analysis core. Documentation review
is an explicit optional resource outside default discovery. Runtime identity and recovery guidance
are part of plugin management.

`openubmc-code-submit` selects internal GitLab or community GitCode rules from repository facts.
It carries source, build and QEMU evidence into delivery. Conan binary upload belongs to
`openubmc-publish`. GitCode public messages require preview and explicit confirmation.

Runtime exposes `observe` for bounded live queries and `execute` for stateful workflows.
The knowledge base is optional: missing credentials or service failure does not make it a
prerequisite for source or Runtime work. The panel provides knowledge queries, progress and
evidence reads; the Host decides its display location.

The old bootstrap and loose `full` / `target-runtime` profiles are retained for migration and
rollback. New installations use the native plugin. Windows clients may use an explicitly
configured WSL stdio bridge for Runtime and KB; native Windows installation of those backends is retired. The independent offline-analysis MCP supports native Windows.

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
- [Product closeout qualification](docs/product-closeout-qualification.md)
- [Codex Adoption Qualification](docs/codex-adoption-qualification.md)
- [Continuous closeout evidence collector](docs/continuous-closeout-qualification.md)

## Credentials and lifecycle

Use the installed plugin's private configuration page for target accounts, OneID and Conan
account settings:

```bash
python3 -I <plugin-root>/scripts/pluginctl.py configure
python3 -I <plugin-root>/scripts/pluginctl.py doctor
```

Credentials and Runtime history remain outside the package. KB requires the user's authorized
OneID application configuration; the plugin supplies no shared OAuth secret.

```bash
codex plugin marketplace upgrade openubmc-public
codex plugin add openubmc@openubmc-public
codex plugin remove openubmc@openubmc-public
```

For a previous loose installation, preview `pluginctl.py migrate --disable-only --preview`
before applying `migrate --disable-only`. Retain its transaction ID for restoration.
See [migration and recovery](docs/codex-plugin.md) for ownership checks and separate archive administration.

## Validation

Pull requests and `main` pushes run Linux validation and package checks. Ordinary checks write status and report checksums to the Actions summary without uploading artifacts. A summary does not replace full release evidence.

The manual release workflow attaches selected evidence and its SHA-256 checksum to the existing draft Release, including failed gate results. Promotion requires every release gate to succeed; evidence upload failures also fail the job.

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

## Offline reviews

For an explicit documentation audit, read `<plugin-root>/optional-skills/openubmc-doc-review/SKILL.md`.
Its scanners do not execute document commands. `openubmc-log-analyzer` owns both local diagnostic
bundle analysis and remote collection, with bounded scanning and file-and-line evidence.

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

The current public Codex plugin release is
[2.2.16](https://github.com/lihuabai629-star/openubmc-codex-plugins/releases/tag/v2.2.16).
Use the archive and checksum attached to that release for installation. Development
versions become releases only after immutable source qualification and publication.

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

Codex plugin publication uses the marketplace's [Release Gate](https://github.com/lihuabai629-star/openubmc-codex-plugins/blob/main/docs/validation.md#codex-plugin-release-gate), which binds the archive, source lock, lifecycle evidence and both hosted CI runs. The source tag provides the same locked source; publication does not claim a new live-device, external-model or GUI qualification.

For a separately selected Runtime/model qualification, run the execute A/B experiment described in
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

Offline local analysis also has an independent `openubmc-offline` MCP server on Windows, Linux and WSL. Its `openubmc_analyze_bundle` and `openubmc_read_evidence` tools share the analyzer core with native DSH. Pass absolute paths; the plugin selects Python (`OPENUBMC_PLUGIN_PYTHON`, otherwise `python` on Windows or `python3` elsewhere) and launches JSON workers without a shell. Python 3.11 or later is required; no third-party dependencies, device accounts or Runtime initialization are needed. `OPENUBMC_OFFLINE_OUTPUT_DIR` selects the default artifact directory; otherwise results use the host's private state directory. `OPENUBMC_OFFLINE_TIMEOUT_SECONDS` controls the deadline (default 300, maximum 3600), and `OPENUBMC_OFFLINE_MAX_CONCURRENCY` limits simultaneous workers (default 2, maximum 16).

Analysis saves full redacted UTF-8 JSON outside the input and returns compact evidence pointers, explicit totals, scan failures and truncation. Compact analysis responses are capped at 256 KiB; evidence reads retain at most 32 warning and error samples with full counts. Read original text or gzip lines through the evidence tool; paths remain under `dump_info/`, byte/line limits apply, and credentials and common identifiers are redacted. A complete selected scan does not establish catalog coverage or device health. Runtime device workflows still use the supported backend below.

The backend is supported inside Linux or WSL. Windows clients can forward stdio
through a local copy of `scripts/openubmc-wsl-bridge.js`, pointing at the installed
Linux package; they need
no native Python, SSH adapter, credential store or Runtime installation.
The plugin includes the [Codex panel](codex-panel/README.md) for knowledge queries
and workflow receipts.
