# Codex plugin

The `openubmc` plugin contains the eleven workflow Skills, Runtime, and knowledge MCP launchers.
The package version, source commit, and file inventory travel together in `plugin-lock.json`.
The supported qualification host is Codex CLI 0.153.4 on Linux, Python 3.12, and Node.js 20 or newer.

## Install a release

Download the archive and checksum from the same release. Run these commands in a new directory:

```bash
gh release download v2.0.8 --repo lihuabai629-star/openubmc-agent-workflow \
  --pattern openubmc-v2.0.8-codex.tar.gz --pattern openubmc-v2.0.8-codex.sha256
sha256sum --check openubmc-v2.0.8-codex.sha256
tar -xzf openubmc-v2.0.8-codex.tar.gz
python3 -I openubmc/scripts/install_plugin.py openubmc-v2.0.8-codex.tar.gz \
  --sha256 "$(cut -d' ' -f1 openubmc-v2.0.8-codex.sha256)"
```

Installation prepares hash-locked dependencies outside the package and checks both MCP servers.
It migrates legacy Codex entries only when the previous installer state proves their ownership.
A conflicting or subsequently edited configuration produces an error and retains its recovery journal.
Restart the Codex task after installation so it loads the selected plugin.

For a separate installation, supply `--home /absolute/home --codex-home /absolute/codex-home`.
The same paths must be used for audit, update, rollback, and removal.

## Check, update, and recover

```bash
python3 -I ~/plugins/openubmc/scripts/pluginctl.py doctor
python3 -I ~/plugins/openubmc/scripts/plugin_admin.py audit
```

Doctor reports package integrity, dependency integrity, MCP startup readiness, and whether a
credential file is configured. Startup readiness does not prove access to a target or knowledge service.
Audit compares the active native Codex cache with the distribution source and reports transaction state.

Install a newer verified archive with the same installation command. To select a previous release,
use its exact `VERSION-DIGEST16` directory name from `release_path` in the audit:

```bash
python3 -I ~/plugins/openubmc/scripts/plugin_admin.py rollback --release "$PREVIOUS_RELEASE"
python3 -I ~/plugins/openubmc/scripts/plugin_admin.py uninstall
```

Rollback selects the recorded archive and restores the version actually loaded by Codex. Uninstall
removes the native Codex registration and cache. Credentials, Runtime records, release archives,
ownership audits, and recovery journals remain available. Completed transactions retain previous
and failed package directories as recovery evidence; they are outside the active plugin cache.

An interrupted activation is reconciled before the next install. Recovery checks the previous and
planned digests before restoring anything. When a user edit conflicts, keep the journal and reconcile
that edit before retrying; the installer does not overwrite it.

## Build and qualify

```bash
npm ci --ignore-scripts --prefix plugin/host
python3 scripts/qualify_plugin.py --archive /tmp/openubmc-plugin.tar.gz \
  --output /tmp/plugin-qualification.json
```

The archive is built from an immutable Git commit. Qualification uses a clean installation, the
locked native Codex executable, and a local Responses fixture. It checks package reproducibility,
MCP discovery, `observe`/`execute` request validation, two independent Codex processes and their
Runtime shutdown records, uninstall, reinstall, and preservation of external configuration.
Published archives additionally pass the repository's immutable release gate.
