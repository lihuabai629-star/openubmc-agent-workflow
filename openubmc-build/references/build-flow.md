# openUBMC Post-Change Build Flow

## Goal

After code is modified, produce a product package that contains those changes:

```text
changed components -> service.json version +1 -> bmcgo gen when contracts changed -> component Conan package -> manifest Conan ref -> product version parity -> product package -> verified artifact handoff
```

The entry may come from any skill or user request. This skill should not assume a fixed upstream caller; it must determine or be told which components changed. Prefer `references/handoff-contract.md` when another workflow routes into this one.

Use `bmcgo` for generation, component build, manifest build, and publication. Do not switch to `bingo` commands; any `bingo` version mention in `.bmcgo/config` is a tool-package constraint surfaced through `bmcgo`.

## 1. Identify Changed Components

Use this precedence order:

1. Structured handoff from the current session: changed files, changed components, generation need, build type, stage, manifest root, board, delivery strategy, and optional deployment-routing metadata.
2. Current conversation / task context: files just edited, implementation summary, or user-named components.
3. Explicit file path mapping with `--path`.
4. Git status scan as a fallback candidate list only.

Git status is not authoritative in this workspace because historical dirty files may be present. Always filter scan results against the current task.

If file paths are known:

```bash
/root/.agents/skills/openubmc-build/scripts/detect_changed_components.py --root /home/workspace/source --path general_hardware/src/lualib/foo.lua
/root/.agents/skills/openubmc-build/scripts/detect_changed_components.py --root /home/workspace/source --path mdb_interface/json/intf/mdb/bmc/kepler/Foo.json --json
```

If the conversation does not identify files/components, use fallback scan:

```bash
/root/.agents/skills/openubmc-build/scripts/detect_changed_components.py --root /home/workspace/source
/root/.agents/skills/openubmc-build/scripts/detect_changed_components.py --root /home/workspace/source --json
```

A component root normally has `mds/service.json` and/or `conanfile.py`. Treat changes under `mdb_interface`, `profile_schema`, and shared interface/model repos as dependency-impacting changes; they may require rebuilding dependent components or updating manifest package refs. Do not infer that every dirty component should be built.

## 2. Component Version and Generation

For every component build, increment `mds/service.json` `version` by one patch step before building.

Dry-run first, then write:

```bash
/root/.agents/skills/openubmc-build/scripts/bump_openubmc_versions.py --component-root <component-root>
/root/.agents/skills/openubmc-build/scripts/bump_openubmc_versions.py --component-root <component-root> --write
```

Run generation with `bmcgo gen` when changes affect generated contracts or generated code inputs, including:

- `mds/model.json`
- `mds/service.json`
- `mds/types.json`
- `mds/ipmi.json`
- MDB interface/path JSON
- added/changed properties, methods, events, or model fields

Default command:

```bash
cd <component-root>
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- bmcgo gen
```

Do not pass the component semantic version, such as `1.80.3`, to `bmcgo gen -v`; current `bmcgo gen -v/--version` expects an integer generator/template version. Use `-v <integer>` only when the component convention or local help requires it. Configuration-only components such as `profile_schema` may not need generation even when `mds/service.json` is bumped; record the reason when skipping gen.

After any `bmcgo gen` error, inspect the component worktree before continuing. The command can delete or partially rewrite generated files before failing, especially when remote auth or dependency resolution fails. Restore unintended partial `gen/` deletions/rewrites, then rerun only after the blocking cause is fixed.

## 3. Component Build

Debug package:

```bash
cd <component-root>
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- bmcgo build -bt debug --stage dev
```

Release package:

```bash
cd <component-root>
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- bmcgo build -bt release --stage stable
```

Upload only when needed:

```bash
conan remote list
bmcgo build -u -r <expected_remote> -bt <debug|release> --stage <dev|stable>
```

Rules:

- `debug` normally pairs with `--stage dev`.
- `release` normally pairs with `--stage stable`.
- Use `--conan2`, profile, options, or remote flags only when repository help/config requires them.
- Record the final Conan reference from actual build output or `conan list`; do not synthesize it. Debug builds may use a user/channel like `profile_schema/1.80.3@openubmc.dev/dev`, not `@openubmc/dev`.

If Conan remotes are slow or unavailable, `bmcgo build` may fail during Conan `-u` remote update checks even for a component that can build from local cache. For local validation only, wrap the build with temporary remote disable/enable and restore state immediately:

```bash
conan remote list > /tmp/openubmc_conan_remote_before.txt
conan remote disable openubmc_sdk
conan remote disable openubmc_opensource
conan remote disable local
bmcgo build -bt debug --stage dev
rc=$?
conan remote enable openubmc_sdk
conan remote enable openubmc_opensource
conan remote enable local
exit $rc
```

Use the same local-cache remote guard for manifest product builds when remote username prompts or EOF errors appear. This is a validation workaround, not a publish path.

## 4. Manifest Wiring

Find the package owner under manifest:

```bash
rg -n '"<component_name>/' <manifest-root>/build/subsys || \
  grep -RIn '"<component_name>/' <manifest-root>/build/subsys
```

Prefer the helper because it prints all matched refs before writing:

```bash
/root/.agents/skills/openubmc-build/scripts/update_manifest_conan_ref.py \
  --manifest-root <manifest-root> \
  --component <component_name> \
  --new-ref <component_name>/<version>@openubmc/<stage> \
  --stage <dev|stable>
```

Replace the matching Conan ref in the correct `build/subsys/<stage>/*.yml`; when a manifest has no `dev/` directory, top-level `build/subsys/*.yml` may be the dev/current lane. If the component is new to the product, ensure `build/product/<board>/manifest.yml` dependencies select the component or subsystem. Do not require the product manifest to contain the component name directly when the product selects a subsystem and the component is inside `build/subsys`.

Do not assume source directory presence means the component enters the image. Product inclusion comes from manifest/subsys wiring.

## 5. Product Version Bump

Update:

```text
build/product/<board>/manifest.yml
base.version
```

Dry-run first, then write:

```bash
/root/.agents/skills/openubmc-build/scripts/bump_openubmc_versions.py --product-manifest <manifest.yml> --build-type <debug|release>
/root/.agents/skills/openubmc-build/scripts/bump_openubmc_versions.py --product-manifest <manifest.yml> --build-type <debug|release> --write
```

Policy:

- Every product package build bumps the version by `+1` or `+2`.
- Last segment parity matters: even = debug package, odd = release package.
- Choose the next version whose parity matches the intended output.
- Routine package bumps do not require syncing `build/rootfs/etc/version.json`, `build/version.yml`, or `build/sdk/manifest.yml`.

Example:

```text
26.03.00.01 -> 26.03.00.02  # debug
26.03.00.02 -> 26.03.00.03  # release
26.03.00.03 -> 26.03.00.04  # next debug
```

## 6. Product Build

From the manifest root, use `bmcgo`. Before build, check `.bmcgo/config` for required tool versions. In this repo, manifest help/build can trigger an upgrade check if the installed tool version does not satisfy config, so do not run broad manifest commands blindly. If `[bingo].version` is not satisfied and package indexes are unreachable, stop and fix the build environment before product build; do not edit `.bmcgo/config` just to bypass the gate.

Run the preflight from the manifest root before long product builds:

```bash
/root/.agents/skills/openubmc-build/scripts/preflight_build_env.sh --board <board>
```

Fix reported empty required files before starting rootfs/HPM generation. Known blockers include `build/manufacture/misc/pme_profile_en.dat` and `build/manufacture/misc/datatocheck_upgrade.dat` being present but zero bytes, which can fail late in `work.task_build_rootfs_img`.

Check HPM signing before spending time on rootfs/HPM tasks:

- Self/server signing can come from product manifest keys such as `base/signature/simple_signer_server` or `base/signature/certificates`, or from `.bmcgo/config` sections `hpm_self_sign` / `hpm_server_sign`.
- When `simple_signer_server` points to `http://127.0.0.1:5000/sign`, verify the local signing agent is running before build; common service commands are `/usr/local/bin/signing-agent-service start|status|stop`.
- If localhost signing returns a proxy `502` or times out, check proxy environment. Use `--noproxy '*'` for curl checks or ensure `no_proxy` includes `127.0.0.1,localhost`.
- In `bmcgo_pro`, if self/server signing is not configured, `work.task_hpm_envir_prepare` may copy `temp/board_<board>/sign_img.xml` and call `/usr/local/signature-jenkins-slave/signature.jar`.
- Missing `temp/board_<board>/sign_img.xml` causes a late HPM failure like `cannot stat ... sign_img.xml`; treat that as signing setup, not as a component Conan or manifest-ref problem.
- `/usr/share/bmcgo/signature/sign_img.xml` and `/usr/share/bmcgo/signature_sm2/sign_img.xml` are templates to inspect or use according to local product policy; do not silently copy them into a product build without recording why.

Typical pattern after confirming the current repo supports it:

```bash
cd <manifest-root>
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- \
  bmcgo build -t personal -b <board> -bt <debug|release> --stage <dev|stable>
```

For long product builds, prefer a background wrapper that leaves durable state:

```bash
cd <manifest-root>
RUN_DIR=/tmp/openubmc-build PREFIX=product \
  /root/.agents/skills/openubmc-build/scripts/run_bmcgo_background.sh -- \
  /root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- \
  bmcgo build -t personal -b <board> -bt <debug|release> --stage <dev|stable>
```

Poll with targeted checks only:

```bash
pid=$(cat /tmp/openubmc-build/product-*.pid | tail -1)
log=$(ls -1t /tmp/openubmc-build/product-*.log | head -1)
rc=$(ls -1t /tmp/openubmc-build/product-*.rc 2>/dev/null | head -1)
ps -p "$pid" >/dev/null && echo running || echo stopped
[ -n "$rc" ] && cat "$rc" || echo rc_missing
grep -nE 'product_rc=|构建成功|执行失败|构建失败|ERROR|Traceback' "$log" | tail -n 80 || true
tail -n 80 "$log"
```

Interpretation:

- `pid` running: keep polling; do not start a second product build against the same output tree.
- `pid` stopped and `rc` missing: wrapper was interrupted or killed; treat the build as incomplete even if files exist under `output/`.
- `rc` non-zero or strong failure markers: fix the failing stage, restore temporary build guards such as Conan remotes, then rerun.
- `rc=0` is necessary but not sufficient; still verify log completion and package contents.

For release/publish flows, use the repository-supported `bmcgo publish` or build target after checking local help and product policy.

Verify:

- package file exists under `output/` or product-defined packet path and its timestamp is newer than the recorded build start
- package name/version matches `base.version`
- manifest/subsys output references the new component Conan versions
- image/package metadata such as `package_info` includes the new component Conan refs
- logs do not show fallback to old package refs
- target verification can read `/etc/version.json`; some images do not have `/etc/version` or `/etc/bmc_version`

If the wrapper is not used, capture the full log and scan it manually. Do not rely only on `$?`; `bmcgo` may print failed tasks while exiting 0.

Avoid short external timeouts around product builds. Rootfs/HPM tasks can run longer than 30 minutes on this host; if an outer timeout kills `bmcgo`, the build may leave HPM/img files under `output/` even though the final task did not complete. Treat those files as incomplete until the log shows clean task completion. After any killed build, explicitly restore Conan remotes before retrying.

## 7. Return the Build Result

After all build checks pass, re-hash the final HPM and write its digest-bound metadata before
handoff:

```bash
python3 scripts/write_artifact_metadata.py \
  --path /absolute/path/to/openubmc.hpm \
  --product-version <built-version> \
  --provenance openubmc-build
```

Keep the generated `<hpm>.metadata.json` adjacent to the HPM for Runtime validation; do not add its
path to the Agent-facing Build result. Return the HPM absolute path, SHA-256, product version,
`openubmc-build` provenance, and build evidence IDs. An HPM left by a failed or interrupted build
is not a result.

For `delivery_strategy=build-upgrade`, pass the typed Build result to `openubmc-upgrade` together
with the task-owned target and rollback context. Build performs no Redfish discovery, upload,
activation polling, rollback, or runtime collection. `openubmc-upgrade` owns the target mutation;
`openubmc-debug` owns fresh acceptance evidence afterward. See `references/redfish-upgrade.md` for
the handoff boundary.

## Failure Triage

| Symptom | First check |
| --- | --- |
| package still has old behavior | changed component detected, component version bumped, manifest Conan ref updated |
| too many components detected | git status picked up unrelated dirty files; use session context or explicit `--path` mapping |
| `bmcgo` command tries to upgrade tools | `.bmcgo/config` version constraint vs installed `bmcgo --version` |
| product build cannot resolve package | Conan remote, upload/local cache, stage/channel mismatch |
| generated code missing new property | did `bmcgo gen` run after MDS/interface change; was `-v` incorrectly given a semantic version |
| `bmcgo gen` failed and many `gen/` files disappeared | partial generation side effect; restore unintended generated deletions before retrying |
| component build times out on remote ping | Conan `-u` checked unavailable remotes; use temporary remote disable only for local-cache validation |
| Conan asks for username then EOF | remote auth is unavailable; for local validation disable affected remotes and restore them after |
| package version parity wrong | `base.version` last segment does not match debug/release policy |
| product build starts auto-upgrade then hangs | `.bmcgo/config [bingo].version` is not satisfied and pip/package source is unreachable |
| product build fails late in rootfs image task | run preflight; check required manufacture files such as `pme_profile_en.dat` and `datatocheck_upgrade.dat` are non-empty |
| product build fails late copying `sign_img.xml` | HPM signing setup missing; check manifest signing config, `.bmcgo/config` hpm signing sections, generated `temp/board_<board>/sign_img.xml`, and signature templates |
| localhost signer returns proxy `502` | proxy env captured `127.0.0.1`; use `no_proxy`/`--noproxy '*'` and recheck signing-agent status |
| product build is killed by an outer timeout | do not trust residual HPM/img files; restore Conan remotes, inspect the latest log tail, then rerun with a longer/no timeout |
| background product build pid stopped but no rc file exists | wrapper or shell was interrupted; treat output as incomplete, inspect latest log tail and timestamps before rerun |
| `output/rootfs_<board>.hpm` exists but timestamp predates the run | stale artifact; do not upgrade it |
| `bmcgo` exits 0 but output shows failed task | treat log failure as build failure; rerun via `scripts/run_bmcgo_checked.py` |
| runtime upgrade succeeds but behavior old | verify package installed, service restarted, DBus/API path is the one changed |
| Redfish TCP 443 opens but TLS hangs | treat Redfish as unavailable for this run; do not upload |
| validation needs live upgrade but no rollback package exists | stop and ask before changing the target |
