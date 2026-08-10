# Remote file patterns

## Target mapping

| Local pattern | Remote pattern | Required evidence |
| --- | --- | --- |
| `src/lualib/...` | `/opt/bmc/apps/<app>/lualib/...` | Explicit `--app <app>` |
| `lualib/...`, `service/...`, `json_types/...`, `class/...`, `ipmi/...` | `/opt/bmc/apps/<app>/...` | Explicit `--app <app>` |
| `*.sr` | `/opt/bmc/sr/<filename>` | File identity and owning component |
| `vendor/openUBMC/*.csr` | `/opt/bmc/sr/<filename>` | File identity and owning component |

The repository directory name is not a deployment contract. Do not use it as an implicit application name. For an unsupported or ambiguous mapping, provide an explicit reviewed `--remote` target or stop.

Default direct-deploy and rollback targets are limited to `/opt/bmc/apps/`, `/opt/bmc/sr/`, and `/tmp/`. `--force-path` is an explicit exception, not an inference mechanism; a generated rollback plan preserves that exception for review.

## File handling

- Runtime Lua may be bytecode or binary on the target; replace the whole file instead of editing it inline.
- Back up an existing target to `/tmp` before overwrite unless the user explicitly accepts `--no-backup`.
- Preserve an existing target's numeric uid/gid and review its current mode. Pass that mode explicitly when exact mode restoration matters; otherwise the default is `0644`.
- Stage through `/tmp`, copy through the privileged debug shell, and compare the remote SHA256 with the local SHA256.
- A generated rollback command is plan-only. Review it before adding the mutation gates. Rollback preserves the backup uid/gid and verifies the reviewed mode as well as the checksum.
- When the target did not exist before deployment, the generated rollback uses
  `--remove-created` with the deployed SHA256. It removes only the exact regular
  file while that checksum still matches, then freshly verifies path absence.

## Mount behavior

Before changing `/opt/bmc`, discover the current root mount options from `/proc/mounts`.

- Original `ro`: remount `rw`, perform the operation, then remount `ro` in a `finally` path and verify it.
- Original `rw`: leave it `rw`; do not force it to `ro` afterward.
- Unknown mode: stop instead of guessing.
- `--no-remount`: use only when the target is already writable and the user has reviewed that exception.

## Restart scope

- `none`: run `sync` only. This is the explicit no-restart choice.
- `skynet`: run `sync` and restart the framework process tree. Expect transient disconnects and object-state recovery time.

SR/CSR changes do not automatically imply a framework restart. Determine the actual consumer and rescan/reload behavior first. For an explicitly requested live-patch verification, infer the minimum scope: use `none` when reload is unnecessary and `skynet` when it is required. Report the choice without asking the user to repeat authorization.
