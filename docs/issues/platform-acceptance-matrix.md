Part of #278, roadmap item 20. Coordinates with the separate Desktop item 18.

## Problem

macOS local tests and simulated Windows/WSL adapters cannot certify actual
Linux x86_64, Windows plugin bootstrap, WSL routing, or the Desktop installer.
GitHub Actions is currently unable to start jobs because of account billing,
which is external to a source-code fix.

## Scope

- Run the existing locked complete-repository validation and immutable plugin
  qualification on a Linux x86_64 host. Preserve source/toolchain identity,
  exit status, test counts and artifact hashes. Do not equate an arm64 Docker
  result with the requested x86_64 WSL target.
- Exercise installed plugin activation and the supported Windows -> selected
  WSL Runtime MCP path on an actual Windows/WSL environment, including healthy
  MCP, unavailable MCP, shell fallback receipt, credential reuse and no
  duplicate Effect after interruption. Keep native Windows, WSL and target BMC
  execution hosts distinct in reports.
- Confirm the separate Desktop installer presents the same Run/Outcome as the
  plugin using a synthetic target. Do not modify that project's active
  checkout here or infer real-BMC acceptance from a fixture.
- Use exact-source hosted GitHub CI when it can start. When account billing
  blocks the jobs, #288 provides an explicit installed-candidate path based on
  one archive, its qualification report and the native platform rows. Keep
  skipped hosted jobs untested; an executed CI failure remains blocking.

## Acceptance

- An evidence matrix gives OS/architecture, client/runtime version, source
  commit, package digest, commands, exit codes, and pass/fail/untested reasons.
- All supported platform rows pass their existing gates without weakening
  timeout or safety thresholds. Unsupported or unavailable rows remain open.
- The source release claim is blocked until the native Linux/Windows/WSL and
  Desktop rows pass, plus either exact-source hosted CI or #288's installed
  candidate path. Stateful Agent trial acceptance remains separate.

## Ownership

Own platform qualification scripts, CI-compatible tests and evidence docs.
Do not edit real user credentials, global Codex trust, payment settings or a
real BMC.

The executable evidence format, native collection procedure and current open
rows are in [platform acceptance evidence](../platform-acceptance-evidence.md).
