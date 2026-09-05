# 2630 WSL Host Profile

Use this only when the user is working on the local dual-WSL setup where development happens in `ubmc` and product package builds happen in WSL distro `2630`. The build workflow still uses `bmcgo`; this profile only locates the right workspace.

## Path Map

| Purpose | Path or command |
| --- | --- |
| ubmc development/source tree | `/home/workspace/source` |
| 2630 product package root | `/home/workspace/manifest` inside WSL distro `2630` |
| 2630 source checkout with 2603 community manifest | `/home/workspace/source` inside WSL distro `2630` |
| ubmc-side mirror of 2630 workspace | `/home/workspace/2630-mirror` |
| sync script | `/home/workspace/scripts/openubmc-2630-sync.sh` |
| safe wrappers | `status-2630-workspace`, `pull-2630-workspace` |
| destructive wrapper | `push-2630-workspace --force` |
| Windows WSL tool path | `/mnt/c/Windows/System32/wsl.exe` |

Confirmed 2630 workspace directories from prior work: `/home/workspace/manifest`, `/home/workspace/source`, `/home/workspace/general_hardware`, `/home/workspace/bios`, and `/home/workspace/vpd`.

## Preflight

```bash
<skill-dir>/scripts/preflight_build_env.sh --profile 2630-wsl
```

This checks WSL distro state and the 2630 workspace without writing files.

## Rules

- Do not assume `/home/workspace/manifest` exists in the current `ubmc` shell; it may only exist inside distro `2630`.
- Run manifest-side `bmcgo` commands from the 2630 manifest root, not from the ubmc source root.
- Select one product root for a run and keep component refs consistent with it. `/home/workspace/manifest` and `/home/workspace/source/manifest` can carry different product versions and component baselines; do not wire a Conan ref from one root and build the other.
- When sending multi-line scripts through `/mnt/c/Windows/System32/wsl.exe`, prefer `bash -s <<'EOF'` over nested `bash -lc '...'`; on this host nested quoting can strip inner `$variables` and make wrapper scripts report empty paths.
- Skill helper scripts live in the current Codex distro. Resolve `<skill-dir>` from the selected `SKILL.md` and do not assume that path exists inside `2630`; use equivalent inline checks there or explicitly copy the required helper.
- Prefer targeted file checks and bounded `rg/find` commands inside `2630`; avoid broad filesystem scans during build validation.
- For long product builds, create the Plan with the actual 2630 cwd and execute it through `run_build_attempt.py` in the environment that can access that checkout.
- Do not trust `output/rootfs_<board>.hpm` by filename alone; require a successful Attempt and `accepted_local_only` finalization.
- Separate WSL distributions do not share this Skill's checkout/output locks or guardian process boundary. Treat cross-distro builds as uncoordinated unless an external lock owner is explicitly provided.
- Use `status-2630-workspace` and `pull-2630-workspace` as safe operations.
- Use `push-2630-workspace --force` only after explicit user confirmation because it overwrites selected 2630 workspace directories.
