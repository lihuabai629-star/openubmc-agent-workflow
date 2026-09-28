# Platform acceptance evidence

The matrix records the source acceptance decision separately from Runtime,
plugin and release gates. The default hosted-CI path requires all five rows to
pass for the *same full source commit*. The explicit installed-candidate path
requires the four native platform rows to pass for the same source and archive;
the hosted-CI row remains untested with a reason. See
[installed candidate qualification](specs/installed-candidate-qualification.md).
The former Windows→WSL Runtime row and macOS local row are optional. Neither
can replace the required Windows-native device row. The original #284 scope is
archived in [the platform acceptance issue](issues/platform-acceptance-matrix.md);
[ADR-0009](adr/0009-native-windows-device-backend.md) governs device execution.

## Current observation (2026-09-29, Asia/Shanghai)

| Row | Host and package identity | Result and reason |
| --- | --- | --- |
| Linux x86_64 complete validation and immutable plugin | The final native-Windows candidate has not been frozen | **Untested** for the final source and archive. |
| Native Windows plugin bootstrap | An isolated 2.1.3 archive from `50627c1` passed Windows `verify`, `prepare --repair` and `doctor` for both MCPs | **Untested** for the final archive and Codex marketplace installation. The archive predates subsequent HPM fixes. |
| Native Windows device workflows | The same archive initialized both MCPs and listed `observe`/`execute`; source-level Windows SSH transport, Upgrade and Live Patch fixtures passed | **Untested** as a complete installed Agent Run. No synthetic `observe`, upgrade, Live Patch rollback or interruption receipt has been accepted for the final archive. |
| Windows → selected WSL Runtime, optional | Historical route only | **Untested**; it cannot certify Windows-native device work. |
| Separate Desktop installer on synthetic target | No same-Run readback for the final archive | **Untested**. |
| Hosted GitHub CI | No exact-commit run for the final candidate | **Untested**. The installed-candidate path is available if hosted jobs cannot start. |

The matrix decision remains **blocked** until one final source commit and
archive have all required native platform and Desktop evidence. The earlier
Windows→WSL and macOS observations do not fill those rows.

## Evidence format and collector

`scripts/platform_acceptance.py` creates and checks a JSON matrix. `verify`
returns 0 only when all five required rows pass, 1 when evidence is incomplete
or failed, and 2 for malformed input. It recomputes `release_ready`, `blockers`
and `evidence_digest`; those fields in an input file are never trusted. The
schema intentionally cannot mark a Linux arm64 container as native x86_64 or a
CI job with no executed steps as passing.

Create a matrix after committing the candidate. Keep captured logs outside Git
or under ignored `.scratch/`; inspect and redact any logs before sharing them.
The collector prints only paths and status, not command output or credentials.

```bash
SOURCE_COMMIT=$(git rev-parse HEAD)
python3 scripts/platform_acceptance.py init \
  --source-commit "$SOURCE_COMMIT" \
  --output .scratch/platform-acceptance/matrix.json
python3 scripts/platform_acceptance.py verify \
  --input .scratch/platform-acceptance/matrix.json \
  --output .scratch/platform-acceptance/assessed.json
```

For the installed-candidate path, first build and qualify one immutable archive
from the selected clean source. Then initialize and verify with the same file:

```bash
python3 scripts/qualify_plugin.py \
  --archive /tmp/openubmc-candidate.tar.gz \
  --output /tmp/openubmc-candidate-qualification.json
python3 scripts/platform_acceptance.py init \
  --source-commit "$(git rev-parse HEAD)" \
  --validation-mode installed-candidate \
  --candidate-archive /tmp/openubmc-candidate.tar.gz \
  --output .scratch/platform-acceptance/matrix.json
# Populate the four native rows from captured installed-package observations.
python3 scripts/platform_acceptance.py verify \
  --input .scratch/platform-acceptance/matrix.json \
  --candidate-archive /tmp/openubmc-candidate.tar.gz \
  --expected-source-commit "$(git rev-parse HEAD)" \
  --output .scratch/platform-acceptance/assessed.json
```

After the existing release-lock and formal evidence are ready, the local
`scripts/release_gate.py` invocation adds `--platform-matrix`,
`--candidate-archive` and `--candidate-qualification` to select this path. The
other Release Gate checks still run. Do not mark an unfilled row as passed or
reuse a matrix after changing the archive or source commit.

For each command, `capture` writes private stdout/stderr logs with 0600 file
modes on POSIX, captures their SHA-256 values, exit code, test counts, Git source/clean
state, host OS/architecture and tool versions, and hashes named artifacts.
For a direct Python command it records the invoked interpreter version separately
from the collector interpreter version.
Use a **new** log directory for every invocation. An absent artifact is an error.
The JSON is an index to inspectable evidence, not a trusted remote attestation.

```bash
python3 scripts/platform_acceptance.py capture \
  --cwd "$PWD" \
  --log-dir .scratch/platform-acceptance/linux-validation-logs \
  --output .scratch/platform-acceptance/linux-validation.json \
  -- python3 scripts/validate_workflow.py
```

Every passing row must give its full source commit, clean checkout state,
observation time, toolchain identity, separate client/Runtime versions, package SHA-256, host
identities, exact commands with exit 0 and log hashes, artifact hashes, and
per-check evidence digests. The Linux row additionally needs Python and Node
test counts. The native Windows device row must identify Windows client and
Runtime processes and a synthetic target separately. Its route must report
`execution_host=windows-native` with no selected WSL; credential revision and
Run ID must remain the same after interruption, with one Effect before and after.
The Desktop row requires the Desktop source commit and identical Run IDs and
Outcome hashes observed through plugin and Desktop clients. Do not record
credential values, cookies, tokens or a real BMC target in a matrix.

## Native execution checklist

### Linux x86_64

Use an actual x86_64 Linux host. Record kernel, CPU/architecture, whether it is
native/VM/WSL/container, Python 3.12.13, Node 22.23.2, npm and Codex CLI
0.153.4 versions, and the clean source commit. The requirement is native x86_64
execution; an arm64 host running x86_64 emulation does not qualify. Use an
isolated Python environment and the committed lockfile, then capture the exact
existing gates without changing their timeout or safety settings:

```bash
python3.12 -m venv /tmp/openubmc-platform-284-venv
. /tmp/openubmc-platform-284-venv/bin/activate
python -m pip install --only-binary=:all: --require-hashes -r requirements-ci.lock
npm ci --ignore-scripts --no-audit --no-fund --prefix plugin/host
export PATH="$PWD/plugin/host/node_modules/.bin:$PATH"
python scripts/validate_workflow.py
python scripts/qualify_plugin.py \
  --archive /tmp/openubmc-platform-284.tar.gz \
  --output /tmp/openubmc-platform-284-qualification.json
```

Capture each command independently and hash the archive and qualification JSON
as artifacts. `qualify_plugin.py` must return `ok: true`, exact `source_commit`,
`archive_sha256`, `content_digest`, native installation, MCP health and native
Codex execution evidence. Read test counts from the complete validation logs.
Do not infer this gate from a prior release lock or an arm64 Docker run.

### Native Windows device execution

First run the existing `scripts/qualify_windows_plugin.ps1` against a package
built from the exact candidate. It proves native marketplace installation and
the unavailable-backend setup path; capture its command exit, package hash,
installed Codex version and JSON output. Confirm the packaged Windows Runtime
and KB MCPs initialize and list their tools with WSL unavailable. Run `observe`
and typed diagnosis, existing-HPM upgrade, Live Patch and rollback against
isolated targets. Check credential reuse, host-key and TLS rejection, interrupted
Effect recovery and the build-only blocker when no compiler is installed.
Record credential source *revision only*, Runtime process identity, route receipt,
Run ID and Effect count before and after restart. Source-level transport tests
and a passing `doctor` result cannot fill the device row alone.

### Desktop synthetic target

Install the separate Desktop candidate without modifying its active checkout
from this task. Record its own source commit and installer digest, version,
synthetic target identity and commands. Read the Run ID and terminal Outcome
from plugin and Desktop; compare the exact Run ID and Outcome SHA-256. A UI
screen or matching prose alone is insufficient.

### Hosted CI

After the account owner restores Actions execution, rerun the canonical
`.github/workflows/validate.yml` on the final candidate source commit. Capture
the exact run, all three job conclusions and nonempty executed steps with:

```bash
python3 scripts/platform_acceptance.py collect-ci \
  --source-commit "$SOURCE_COMMIT" --run-id <run-id> \
  --output .scratch/platform-acceptance/hosted-ci.json
```

This collector checks the workflow path, exact `head_sha`, event, successful
overall run, and successful nonempty preflight, complete validation and Windows
bootstrap jobs. A skipped job, a billing preflight with zero steps, or a run for
another commit remains open. The existing `github_ci_evidence.py` and Release
Gate keep their own authority; this matrix adds a source acceptance decision.

## Ownership and rollback

This matrix indexes evidence and does not change Runtime decisions. An invalid
or incomplete row blocks the release claim; it never authorizes device writes.
