---
name: openubmc-build
description: Build openUBMC components and products, including bmcgo gen/build, Conan package refs, changed-component detection, manifest inclusion, HPM/rootfs output, product version bumping, verified artifact handoff, and build/package failure diagnosis. Use when source changes need compilation or the user requests a package or firmware artifact. Do not use for live target diagnosis, target connections, firmware upload, or runtime file replacement.
---

# openUBMC Build

## Overview

This skill owns the post-code-change build loop: normalize the caller's changed-component context, regenerate MDS output when needed, build component Conan packages, wire those packages into manifest, bump product version, build product output, and return a verified artifact identity for any later deployment.

Use `bmcgo` as the command-line tool for generation, component package builds, manifest product builds, and package publication.

Requirements: openUBMC component or manifest workspace, `bmcgo`, Conan, and Python. Build never reads target credentials or opens SSH, Telnet, or Redfish sessions.

Important validation rule: `bmcgo` can return exit code 0 even when the log contains failed tasks. For non-trivial `gen` or `build` runs, prefer `scripts/run_bmcgo_checked.py -- bmcgo ...` or explicitly scan the captured log for strong failure signals such as `ERROR`, `Traceback`, `执行失败`, `构建失败`, and unresolved package errors before declaring success. Do not treat generic words like `Failed validating` as build failure without context.

## Main Flow

1. **Identify changed components**
   - Primary source is the current conversation: components/files just changed by the agent, user-named components, or a structured handoff from any upstream workflow.
   - When `execute` returns a `waiting_response` Turn whose Gate is `build.artifact` and owned by `openubmc-build`, load this Skill immediately. Preserve the Turn's Run and Gate binding and use the Gate input schema as the exact result contract; do not ask the user to restate the build request.
   - Prefer the structured handoff in `references/handoff-contract.md`; do not assume the caller is only `openubmc-developer` or `openubmc-debug`.
   - Use git status only as a fallback candidate scan because dirty worktrees may contain unrelated old files; filter it against the active task before building.
   - When file paths are known, map them directly:

```bash
/root/.agents/skills/openubmc-build/scripts/detect_changed_components.py --root /home/workspace/source --path general_hardware/src/lualib/foo.lua
```

   - When the conversation lacks enough context, run the fallback scan and filter it against the current task:

```bash
/root/.agents/skills/openubmc-build/scripts/detect_changed_components.py --root /home/workspace/source
```

2. **For each changed component**
   - When component upload or dependency resolution needs remotes, verify Conan auth first. See `references/conan-auth.md`.
   - Increment `mds/service.json` `version` every time before building.
   - Prefer the version helper for dry-run and write:

```bash
/root/.agents/skills/openubmc-build/scripts/bump_openubmc_versions.py --component-root <component-root> [--write]
```

   - If MDS/interface/model/property files changed, run generation with `bmcgo`:

```bash
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- bmcgo gen
```

   - After a generation error, inspect the worktree before continuing; `bmcgo gen` may delete or partially rewrite `gen/` outputs before failing. Restore partial generated deletions unless they are part of the intended successful generation.
   - Build the component package:

```bash
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- bmcgo build -bt debug --stage dev
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- bmcgo build -bt release --stage stable
```

3. **Put the component package into manifest**
   - Replace the matching Conan reference in the owning `build/subsys/<stage>/*.yml`.
   - Prefer the manifest ref helper for dry-run and write:

```bash
/root/.agents/skills/openubmc-build/scripts/update_manifest_conan_ref.py --manifest-root <manifest-root> --component <name> --new-ref <conan-ref> --stage <dev|stable> [--write]
```

   - Add product `manifest.yml` dependency only when a new component must enter the product.
   - Keep channel/stage consistent with the intended package.

4. **Bump product version**
   - Update product `base.version` in `build/product/<board>/manifest.yml`.
   - Every package build increments the version by `+1` or `+2`.
   - Last segment parity is policy: even = debug package, odd = release package.
   - Prefer the version helper so parity is checked before writing:

```bash
/root/.agents/skills/openubmc-build/scripts/bump_openubmc_versions.py --product-manifest <manifest.yml> --build-type <debug|release> [--write]
```

   - Do not sync `build/rootfs/etc/version.json`, `build/version.yml`, or SDK manifest for this routine package bump unless the user asks or repo policy says so.

5. **Build product output**
   - From the manifest root, use `bmcgo` and current repo help/config.
   - Watch for `.bmcgo/config` tool constraints before running manifest build commands; help/build can trigger upgrade checks.
   - Run `scripts/preflight_build_env.sh --board <board>` from the manifest root before long builds; fix empty required manufacture files such as `pme_profile_en.dat` and `datatocheck_upgrade.dat` first.
   - For remote dependency, online signing, `umask`, and rootfs permission pitfalls, see `references/product-build-pitfalls.md`.
   - Do not use a short outer timeout for product rootfs/HPM builds; if a timeout wrapper kills `bmcgo`, treat the output as incomplete and restore Conan remotes before retrying.
   - For long product builds, start a background run that records `pid`, `log`, `rc`, and `meta`; a missing `rc` file means the build wrapper was interrupted and the output is not trustworthy.
   - Typical command shape after confirming local help:

```bash
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- \
  bmcgo build -t personal -b <board> -bt <debug|release> --stage <dev|stable>
```

   - Background command shape:

```bash
RUN_DIR=/tmp/openubmc-build PREFIX=product \
  /root/.agents/skills/openubmc-build/scripts/run_bmcgo_background.sh -- \
  /root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- \
  bmcgo build -t personal -b <board> -bt <debug|release> --stage <dev|stable>
```

   - Verify output package path and package metadata include the new component versions.

6. **Return artifact identity and route delivery**
   - After the final HPM hash and product version are known, run `scripts/write_artifact_metadata.py --path <hpm> --product-version <version> --provenance openubmc-build` so Runtime can bind the declared version and producer provenance to those exact bytes before Upgrade.
   - Keep the generated metadata adjacent to the HPM as `<hpm>.metadata.json`; it is Runtime-owned validation material and does not enter the Agent-facing typed Build payload.
   - Return the absolute HPM path, SHA-256, product version, and build evidence IDs as the typed Build result.
   - When the task carries a Runtime Turn, convert the verified HPM identity into the Gate's
     `ArtifactRef` and submit it through `execute(kind=respond)` using the exact `run_id`, `gate_id`,
     `gate_version`, and `schema_digest`. Use the Run-bound target identity; do not copy credentials
     into the reference. Put only fields declared by the returned Gate schema in `response.payload`:

```yaml
kind: respond
run_id: <current Run ID>
gate_id: <current Gate ID>
gate_version: <current Gate version>
schema_digest: <current Gate schema digest>
response:
  status: <completed|failed|cancelled>
  summary: <concise build result>
  payload:
    source_revision: <source revision built>
    artifact_ref:
      handle: <absolute HPM path; required when completed>
      digest: sha256:<64 lowercase hex characters>
      kind: openubmc-hpm
      size: <HPM byte size>
      provenance: openubmc-build
      retention_hint: run-lifetime
      version: <built product version>
      target: <target bound to the current Run>
      run_id: <current Run ID>
    component_versions: [<component and Conan package identities>]
    build_commands: [<exact commands executed>]
    build_logs: [<absolute log paths or build evidence IDs>]
    known_gaps: [<remaining validation gaps>]
```

   - Return only terminal Gate responses. Preserve logs and known gaps for failed or cancelled attempts instead of publishing a stale artifact. A running build remains local work until it can return a terminal response or the caller deadline yields control.
   - `openubmc-upgrade` owns any selected `build-upgrade` next step; do not upload from Build or acquire a target lease.
   - After Upgrade, route acceptance checks to `openubmc-debug` for fresh evidence from the new target epoch.
   - With a bound Run, bare “继续” or “continue” means call `execute(kind=resume)` before rebuilding anything. After returning a terminal `build.artifact` Gate response, use the returned Turn directly; do not issue an extra polling call. Target Runtime owns downstream routing and authorization, so Build must not re-evaluate or reconfirm them.
   - When the Run reaches a terminal state, report the terminal Outcome. Closeout documents, phase evidence, build logs, and HPM identity remain available through the Operator / CI Plane.

## Quick Commands

Component help confirmed locally:

```bash
bmcgo gen -h
bmcgo build -h
```

Component build options include `-bt debug|release`, `--stage dev|pre|rc|stable`, `-u`, `-r <remote>`, `--conan2`, `-o`, and `--user`.

## Guardrails

- Always know which components changed before building; prefer session context over git status, and do not build random modules from unrelated dirty files.
- Do not skip component version increment; the user expects every component build to bump `mds/service.json`.
- Do not upload with `-u` unless publish intent, remote, and stage are explicit.
- Do not use `bingo` as the build/generation command path in this skill; `bingo` may appear only as an internal config/version section reported by `bmcgo`.
- Do not hard-code board names or manifest paths beyond examples; discover them in the current manifest.
- Do not trust command exit code alone for `bmcgo`; check logs or use the bundled checker.
- Do not treat an HPM file left behind by a failed, interrupted, or timed-out build as valid; success requires `rc=0`, clean log completion, a package timestamp newer than build start, and metadata/package refs that include the new component versions.
- Do not replace missing product dependencies with local source packages unless the user explicitly approves a local workaround.
- Do not hand-edit `temp/build.../tmp_root` as a durable fix.
- Do not perform an upgrade from Build; `openubmc-upgrade` owns Redfish mutation, pre-version checks, version verification, and recovery assessment.
- Do not treat a missing structured handoff as permission to trust noisy git state; reconstruct changed components from the active session first and pause only when multiple materially different candidates remain.

## References

- `references/build-flow.md`: detailed closed-loop workflow.
- `references/conan-auth.md`: Conan remote authentication, missing binary, and proxy checks.
- `references/handoff-contract.md`: structured inputs expected from debug/developer skills.
- `references/product-build-pitfalls.md`: product package signing, `umask`, and rootfs permission pitfalls.
- `references/redfish-upgrade.md`: typed artifact handoff to the separately owned Upgrade lane.
- `references/2630-wsl-profile.md`: optional local ubmc/2630 profile only.
- `scripts/preflight_build_env.sh`: read-only environment check.
- `scripts/detect_changed_components.py`: changed component inventory.
- `scripts/bump_openubmc_versions.py`: service/product version bump with dry-run.
- `scripts/update_manifest_conan_ref.py`: manifest Conan ref locator/updater with dry-run.
- `scripts/run_bmcgo_checked.py`: run `bmcgo` and fail on failure-looking log lines even when exit code is 0.
- `scripts/run_bmcgo_background.sh`: launch long builds with durable `pid/log/rc/meta` artifacts.
