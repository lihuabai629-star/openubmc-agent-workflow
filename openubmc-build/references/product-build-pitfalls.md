# Product Build Pitfalls

Use this when building full openUBMC product packages or validating an HPM/rootfs output.

## Remote Dependencies

If dependency resolution reports a missing binary, first verify Conan auth and remote availability in `conan-auth.md`. Do not replace a stable remote dependency with a local source package unless the user explicitly approves that workaround.

## Online Signing

When the environment uses online signing:

```bash
signing-agent-service status
signing-agent-service start
```

Some Codex/automation environments clean detached background processes. If the signer disappears during long builds, keep it alive in an active terminal/session and prove it is listening before the build. Missing `sign_img.xml` or failed signer contact is a late HPM/signing failure, not a source-package problem.

## Umask And Rootfs Permissions

Run product builds with a normal file creation mask:

```bash
umask 022
/root/.agents/skills/openubmc-build/scripts/run_bmcgo_checked.py -- \
  bmcgo build -t personal -b <board> -bt <debug|release> --stage <dev|stable>
```

A restrictive caller `umask` can be inherited by rootfs staging and produce directories such as `/opt` or `/opt/bmc` with mode `700`, which can make Web/Redfish content inaccessible after an otherwise successful upgrade.

After packaging, spot-check staging or extracted rootfs permissions when Web/Redfish content matters:

```bash
stat -c '%a %U %G %n' <tmp_root>/opt <tmp_root>/opt/bmc
```

Expected mode is normally `755 root root`.
