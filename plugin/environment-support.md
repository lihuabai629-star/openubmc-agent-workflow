---
name: openubmc-environment-setup
description: Inspect and repair an installed openUBMC Codex plugin, configure private BMC and knowledge-base credentials, and explain required local tools on Linux or WSL.
---

# openUBMC plugin environment

Resolve `<plugin-root>` as the parent of the `skills` directory containing this Skill. Codex manages the plugin's Skills and MCP registration. Keep credentials, dependencies and Runtime history outside the plugin directory.

## Inspect and repair

```bash
python3 -I <plugin-root>/scripts/pluginctl.py doctor
python3 -I <plugin-root>/scripts/pluginctl.py prepare --repair
```

The MCP launchers prepare locked Python and npm dependencies on first startup. Subsequent starts verify and reuse the cache. Installation progress goes to stderr. A modified package or dependency cache fails verification; use the repair command for dependency drift and reinstall the selected marketplace version for package drift. Do not modify the installed package, bypass hashes or create duplicate loose Skill/MCP registrations.

Linux, Python 3.12 with pip, Node.js 20+ with npm, Git and Codex are the required host tools. On Debian/Ubuntu, install missing command-line tools when environment setup is requested. SDKs, compilers, Docker installation and Conan remotes belong to their respective workflows.

## Private credentials

When the user needs to enter or change BMC/OS, KB or Conan credentials, open the local browser page:

```bash
python3 -I <plugin-root>/scripts/pluginctl.py configure
```

Keep the page process alive while the user edits. The page displays the Linux/WSL environment and exact local source, separates global BMC/OS defaults from IP overrides, and supports explicit import while retaining the original file. Secret fields support keep, replace and remove; values stay in the local page and Runtime. Ask for missing target or account context only, never ask the user to paste a password or application secret into chat.

Saving creates a private revision; **Save and activate** selects it for subsequent Runtime/KB requests. Existing requests keep their original account. With an already authorized target, append `--target <ip> --purpose bmc|os --transport ssh|redfish`; activation then runs that bounded connection check. Without a target, saving performs no device probe. The page also offers explicit checks for selected targets and configured KB/Conan services. Report their actual status: saved and active do not mean verified.

SSH checks preserve strict host identity verification, Redfish checks verify TLS, and no check retries a rejected IP override with global credentials. Conan authenticates only an existing named remote and uses the native per-user token cache. KB requires the user's authorized OAuth application settings; interactive authentication requirements remain visible as such. The plugin supplies no shared OAuth client secret.

For a machine without an accessible browser, the existing `install_environment.py credentials` hidden-input helper remains available. A headless page can be started with `configure --no-browser`; open its session URL in the same machine's browser. Use the WSL environment containing the installed plugin and credentials.

## Lifecycle

For a legacy loose installation or `openubmc@personal`, use `pluginctl.py migrate --disable-only --preview` to inspect ownership and pending changes, then `migrate --disable-only` to save them. Keep the returned transaction ID for `restore-legacy --transaction <id>`. This route retains old files and links, disables the canonical Skill entries and owned MCP entries, and preserves the target `openubmc@openubmc-public` registration. Use `--target-plugin` when preserving a different target registration. Start a new Codex task to load the saved state. A later config or ownership edit blocks restoration until reconciled. Explicit `migrate --remove` retains the old removal behavior.

Use `codex plugin list` to identify the installed marketplace and `codex plugin remove openubmc@<marketplace>` to uninstall. For a Git marketplace, refresh with `codex plugin marketplace upgrade <marketplace>` and reinstall with `codex plugin add openubmc@<marketplace>`. Start a new Codex task after a version change.

For an archive installation managed by `install_plugin.py`, use its `plugin_admin.py audit` and recorded rollback entries. Do not apply archive-administration commands to an installation managed only by the native marketplace. Credentials and durable Runtime records survive plugin removal.
