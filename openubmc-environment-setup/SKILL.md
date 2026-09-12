---
name: openubmc-environment-setup
description: Automatically install, inspect, repair, update, or uninstall the shared openUBMC agent workflow for Codex on Debian/Ubuntu Linux, WSL, or inside an existing Docker container. Use when preparing a workstation or container, installing required command-line tools, importing the full or Target Runtime-focused openUBMC Skill bundle, linking Skills for Codex, preserving independently updated Skill links while switching the managed source, registering the Target Runtime or openubmc-kb MCP server, selecting a private BMC/OS credentials file, repairing that managed configuration, or retiring workflow-owned entries from an older multi-client installation. Do not use to install SDKs, compilers, Docker itself, or to build, debug, publish, or upgrade openUBMC.
---

# openUBMC Environment Setup

Use the bundled installer as the only writer for this workflow. It installs
Skill links, the standalone Target Runtime, the bundled `openubmc-kb` stdio
MCP, their launchers, a small shell hook, private credential-file selection,
client MCP configuration, and installer state.

The installed `openubmc-target-runtime` MCP exposes exactly `observe` and `execute` to agents.
Selecting the `operator` profile exposes the disjoint Evidence, Replay, lifecycle, Runtime status,
and Session Outcome operations for CI or operator use. Its persistent Run database, evidence CAS,
task contexts, and mutation journals share `~/.local/state/openubmc-target-runtime` by default.
Repair and update preserve that state. Uninstall removes the managed launcher and client
registration but leaves Run history and mutation recovery state available for a later reinstall.

The installed Debug CLI uses the same Runtime Core and persistent state through an internal Domain
adapter; it does not reopen the retired Agent-facing profile. Installer health reports `engines.cli`
for this adapter and retains `engines.one_shot` only as an installer-state alias; the CLI process is
short-lived, but it does not create an independent workflow state.

On Debian and Ubuntu, install missing workflow executables automatically with
APT, pip, and npm. Keep the installation non-interactive. Do not download an
SDK, create or enter a container, or configure Conan remotes.

## Install

On a new Debian/Ubuntu or WSL machine, bootstrap the managed installation
without cloning first:

~~~bash
WORKFLOW_REF="vX.Y.Z" # published release tag or full commit
export GH_TOKEN="$(gh auth token)"
gh api \
  "repos/lihuabai629-star/openubmc-agent-workflow/contents/bootstrap.py?ref=${WORKFLOW_REF}" \
  --header "Accept: application/vnd.github.raw" \
  | python3 - --ref "${WORKFLOW_REF}"
unset GH_TOKEN
~~~

Private GitHub access uses `GH_TOKEN` or `GITHUB_TOKEN` only for authenticated downloads and Git
fetches. The installer does not persist the token in the checkout remote, installer state, or logs.

For development against an existing checkout, link that checkout explicitly:

~~~bash
python3 <skills-repository>/openubmc-environment-setup/scripts/install_environment.py \
  install --source <skills-repository>
~~~

When invoking an already installed copy:

~~~bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" install
~~~

The default source repository is
`https://github.com/lihuabai629-star/openubmc-agent-workflow.git`. Managed installation requires
an explicit release tag or full commit and checks out the resolved commit in detached-HEAD mode.
A managed clone is created only when no valid local repository is available. The installed state
records the requested ref, whether it is a tag or commit, and the exact resolved commit.

Use `--source <skills-repository>` or `--source-mode linked` for a checkout
owned and updated by the user. Use `--source-mode managed` to require an
installer-owned clone, together with `--ref <release-tag-or-full-commit>`. The selected mode is
persisted in installer state.

For a Runtime-focused rollout, add:

~~~bash
--skill-profile target-runtime
~~~

This profile links only `openubmc-environment-setup`, `openubmc-debug`,
`openubmc-log-analyzer`, `openubmc-developer`, `openubmc-build`,
`openubmc-upgrade`, and `openubmc-live-patch`. It deploys and registers the
Target Runtime MCP, but leaves `openubmc-dt-testing` and openubmc-kb
configuration untouched. The default `full` profile installs those seven plus
`openubmc-dt-testing`, `openubmc-publish`, `openubmc-lua-component`, and
`openubmc-qemu-testing`, for 11 Skills in total. The selected profile is persisted; check, repair, update,
refresh, reinstall, and uninstall use the recorded profile automatically.

When switching the Environment Setup or Runtime source, preserve a Skill that
has independent local updates by naming its existing canonical link:

~~~bash
python3 <new-skills-repository>/openubmc-environment-setup/scripts/install_environment.py \
  install --source <new-skills-repository> --source-mode linked \
  --skill-profile target-runtime \
  --preserve-skills openubmc-debug,openubmc-developer \
  --skip-credentials --non-interactive
~~~

The named links must already resolve to Skill directories containing
`SKILL.md`. The installer records each client's actual target separately and
does not copy or replace those directories. Repair, refresh, update, and a
later install without this option reuse the recorded targets. Repoint a link
and run install again with the explicit list to record an intentional new
target. Use `--preserve-skills none` on install to return those links to the
selected source. Uninstall leaves only the explicitly preserved Skill links in
place.

Use `--dry-run` before changing an unfamiliar environment. Use
`--non-interactive` for automation. Tool installation never prompts; missing
credentials remain unconfigured unless explicitly imported or configured.
JSON dry-runs keep the current installation under `workflow` and report the
proposed result separately under `planned_workflow`.

## Choose the target environment

Run the installer inside the environment being configured.

- On Linux or WSL, run it in that shell.
- For Docker, enter the existing container first and add `--target docker`.

From Windows PowerShell, enter the intended distribution first with
`wsl -d <distribution>`. When invoking it from another WSL distribution, call
`/mnt/c/Windows/System32/wsl.exe -d <distribution> -- <command>` from a Windows-backed
working directory to avoid cross-distribution current-directory warnings.

Do not infer the target from `/.dockerenv`, and do not create or modify a
container from this Skill.

## Client adapters

`--clients auto`, `--clients all`, and `--clients codex` all install only Codex
links under `~/.agents/skills`. Explicit Claude or OpenClaw selection is rejected
with a Codex-only replacement command.

The `full` profile deploys the repository-bundled `openubmc-kb` package and
registers its managed stdio launcher for Codex. The client starts it
on demand; no Studio process or localhost service is required. A pre-existing
custom external stdio entry is preserved. A legacy `openubmc-studio` entry is
renamed to `openubmc-kb`; the historical default standalone Node entry and the
legacy default localhost HTTP entry are migrated to the managed stdio launcher.
An explicitly customized external endpoint remains external. The
`target-runtime` profile leaves KB configuration untouched. Historical
workflow-owned Claude or OpenClaw links and registrations are migration input:
repair and update remove them while preserving unrelated files and entries.
KB health failure is non-blocking.

## Credentials

For interactive account setup, launch the bundled local page and show its printed session URL:

~~~bash
python3 -B <skills-repository>/openubmc-environment-setup/scripts/config_page.py --kind targets --wait-for-save
~~~

Use `--kind kb` or `--kind conan` for those services and `--focus-target <BMC IP>` to edit a
device without probing it. Keep the process alive until its `configuration_saved` or
`configuration_cancelled` event. Saving activates the requested configuration; inspect the
event's readiness/check results and continue authorized work without requiring a chat reply.
The page shares BMC SSH/Redfish credentials and stores optional BMC-to-OS associations.
An associated OS is not authorization to connect. The hidden-input commands below remain a
fallback when a browser is unavailable.

### Legacy hidden-input configuration

The private file used by the hidden-input helper is:

~~~text
~/.config/openubmc/credentials.env
~~~

The installer enforces mode `0600`. BMC SSH and Redfish share one username and
password. OS SSH is optional and uses a separate username and password; leave
the OS username empty to skip it. Each selected capability needs both fields. Target IP addresses are
not stored. Existing supported Telnet fields are preserved.

Credential values are never sourced into the login shell. The shell hook
exports only `OPENUBMC_CREDENTIALS_FILE` after verifying the file owner, type,
and mode. The managed Target Runtime entrypoint also selects this private file
when started directly by an MCP client or Debug CLI, and passes only
the required values to Debug, Log Analyzer, Live Patch, or Upgrade domain
backends.

Use hidden TTY input:

~~~bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" \
  credentials
~~~

Or import a pre-created private file:

~~~bash
chmod 600 <credentials-file>
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" \
  credentials --import-credentials <credentials-file>
~~~

Do not place passwords in command arguments, profiles, logs, or ordinary
environment variables.

Configure the separate openUBMC KB OneID password and OAuth clientSecret with
hidden TTY input. Enter at the clientSecret prompt preserves an existing secret.
The command validates local configuration with the KB loader before saving:

~~~bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" \
  credentials --kb
~~~

Or import an existing private JSON file:

~~~bash
chmod 600 <kb-config.json>
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" \
  credentials --kb --kb-config <kb-config.json>
~~~

The KB MCP can start before credentials are configured and reports that state
through `openubmc_kb_status`.

## Tools and Conan

The workflow requires `bmcgo`, Conan, Git, Python, OpenSSH, and Node.js 20 or
newer for the bundled KB MCP. On Debian and
Ubuntu, install missing Git, OpenSSH, `sshpass`, ripgrep, pip, Node.js, and npm
packages through non-interactive APT. Install the bundled verified `bmcgo`
wheel, Conan, and Codex into `~/.local`; add `~/.local/bin` to the managed shell
hook automatically.

Use `--skip-tool-install` only when a centrally managed environment forbids the
installer from changing system or user packages. In that mode, retain health
warnings for missing tools instead of attempting installation.

Health output also distinguishes non-blocking capability gaps:

- `sshpass` enables password-based SSH, remote log pulling, and Live Patch;
  SSH keys remain usable without it.
- `rg` accelerates source evidence search; a slower fallback remains available.
- The Codex executable may be absent while its Skill links and MCP configuration
  are staged for later use.

JSON lifecycle and check output includes `tooling`. `next_actions` remains for
explicit skip mode, credentials, or external MCP configuration; a normal
successful install completes tool setup directly. Knowledge readiness is
reported canonically as `knowledge_mcp` and `readiness.knowledge`; the older
`studio` fields remain as compatibility aliases in check output.

Conan remote selection and authentication belong to `openubmc-build`, not this
Skill.

## Lifecycle

~~~bash
INSTALLER="$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py"

python3 "$INSTALLER" check
python3 "$INSTALLER" check --json
python3 "$INSTALLER" repair
python3 "$INSTALLER" refresh
python3 "$INSTALLER" update
python3 "$INSTALLER" rollback
python3 "$INSTALLER" credentials
python3 "$INSTALLER" uninstall
~~~

Add `--json` to any lifecycle command for one machine-readable document. Install, repair, update,
refresh, credentials, and uninstall include their action messages plus the resulting workflow,
source, Runtime, and credential-preservation state; check retains its detailed readiness report.

- `check` verifies the recorded Skill profile, source identity, expected links,
  shell hooks, file permissions, required tools, credentials, and supported MCP
  adapters. Its default Git inspection is limited to the installed workflow;
  add `--deep` for a full-worktree check. `--json` separates core, credential,
  Runtime, MCP, engine, and openUBMC KB readiness. The KB being offline remains
  non-blocking.
- `repair` restores managed links and configuration without pulling source or
  replacing credential values. It restores preserved Skill links to their
  recorded external targets.
- `refresh` records the current commit of a linked checkout and repairs its
  configuration without running Git operations.
- `update` re-fetches and verifies the recorded tag or full commit for a clean
  installer-managed checkout. Rerun bootstrap with a new immutable ref to move
  to another release. Legacy branch-based state fails check, install, repair,
  and update until it is migrated by rerunning bootstrap with an immutable ref.
  A linked checkout must be updated manually, followed by `refresh`.
- `rollback` restores the previous known-good revision of a clean managed
  checkout. The displaced revision becomes the next rollback target, so a
  second rollback toggles back when both revisions remain available.
- `credentials` changes only the private credential file.
- `uninstall` removes only installer-owned configuration and preserves the
  credentials file.
- Add `--purge-credentials` to an explicit uninstall only when the private file
  must also be removed.

The legacy `--install`, `--check`, `--repair`, `--update`, and `--uninstall`
forms remain accepted for compatibility.

After install or repair, start a new login shell and rerun `check`.

## Boundaries

- Route component and product compilation to `openubmc-build`.
- Route target evidence collection to `openubmc-debug`.
- Route an already-built HPM deployment to `openubmc-upgrade`.
- Route package publication to `openubmc-publish`.
