# Platform acceptance evidence for #284

This document applies to [the platform acceptance specification](issues/platform-acceptance-matrix.md).
It records the source acceptance decision separately from existing Runtime,
plugin and release gates. The default hosted-CI path requires all five rows to
pass for the *same full source commit*. The explicit installed-candidate path
requires the four native platform rows to pass for the same source and archive;
the hosted-CI row remains untested with a reason. See
[installed candidate qualification](specs/installed-candidate-qualification.md).
The macOS row is useful local evidence but cannot replace any required row.

## Current observation (2026-09-27, Asia/Shanghai)

| Row | Host and package identity | Result and reason |
| --- | --- | --- |
| Local regression, optional | macOS 26.6, Darwin 25.6.0 arm64; default Python 3.14.6, Node 26.5.0, Codex CLI 0.144.6; branch base `cf68d0044345e29e83eb639954d469e4048642fc` | 18 targeted Python tests and `validate_workflow.py --quick` passed in an isolated Python 3.12 environment. The locked CI toolchain is Python 3.12.13, Node 22.23.2 and plugin Codex 0.153.4. This row does not certify Linux or Windows. |
| Linux x86_64 complete validation and immutable plugin | No native Linux x86_64 host assigned to this task; source/package digest, client/runtime versions, command exit codes and test counts absent | **Untested**. Do not substitute the Mac or an emulated container. |
| Native Windows plugin bootstrap | No native Windows host assigned; installed package and client version absent | **Untested**. The existing CI script checks marketplace activation and unavailable-WSL setup mode, but has not run here. |
| Windows → selected WSL Runtime | No native Windows/WSL pair assigned; no synthetic Run/Outcome or interruption receipts | **Untested**. Simulator tests are insufficient. |
| Separate Desktop installer on synthetic target | Separate active Desktop project was not changed or run from this checkout | **Untested**. Same Run ID and Outcome digest still need direct readback from both clients. |
| Hosted GitHub CI | [Run 36266142666](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/36266142666) was for commit `e6d37324f39b6422ae6aaf51335ac90ba7260f7a`, not this candidate | **Untested for this source**. On that run, preflight failed before any step with the account billing/spending-limit annotation; Linux and Windows jobs were skipped. `collect-ci` returned exit 1; its local JSON digest is `c731fea7d60952751d55d060e1585ca3f1c86cc990c7e51dd03a864a3755cc7b`. The account owner must resolve billing before an exact-commit rerun. |

The matrix decision is currently **blocked**. The clean x86_64 WSL2 validation
and immutable-plugin qualification on PR #287 commit `421d554` passed, but that
older commit and WSL2 do not fill the new candidate's native Linux row. Native
Windows/WSL and Desktop rows also lack passing evidence. Hosted CI has not
started, and no fixture establishes real-BMC acceptance.

Local check details at the branch base: `.scratch/issue-284/venv/bin/python -m
unittest scripts.tests.test_continuous_validation_workflow
scripts.tests.test_platform_acceptance -v` exited 0 with 18 tests passing;
`.scratch/issue-284/venv/bin/python scripts/validate_workflow.py --quick`
exited 0. `--quick` compiles and checks repository metadata but skips the full
test and Node suites. Installing the committed `requirements-ci.lock` on
macOS arm64 exited 1 at `cffi==2.1.1` because the macOS wheel hash differs
from the lockfile's Linux wheel hash. Only PyYAML 6.0.3 was installed in this
isolated environment to run the contract tests; this is not a locked CI run.

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
test counts. The Windows/WSL row must identify native Windows, the selected
WSL distribution and synthetic target separately; credential revision and Run
ID must remain the same after interruption, with one Effect before and after.
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

### Native Windows and selected WSL

First run the existing `scripts/qualify_windows_plugin.ps1` against a package
built from the exact candidate. It proves native marketplace installation and
the unavailable-WSL setup path; capture its command exit, package hash,
installed Codex version and JSON output. Then use an actual Windows host with
an installed, selected x86_64 WSL2 distribution. Confirm the packaged plugin's
healthy Runtime MCP `initialize`, `tools/list`, `observe` and `execute` path on
a **synthetic target**, followed by unavailable MCP and bounded shell fallback
receipts. After interrupting a controlled synthetic operation, read the same
Run and Effect count back through the selected WSL Runtime. Record credential
source *revision only* before and after restart to prove reuse without exposing
the credential. The native Windows, WSL and synthetic target identities must
remain separate. Existing simulated routing unit tests cannot fill this row.

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

This work changes only platform evidence tooling and documentation. It does not
change Agent/MCP/Runtime behavior, credential sources, global hook trust,
billing settings, release thresholds, Desktop code or a real BMC. Remove the
matrix report and this collector to roll back; existing gates keep operating.
