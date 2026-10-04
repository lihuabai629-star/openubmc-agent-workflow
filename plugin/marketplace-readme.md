# openUBMC for Codex

Diagnose openUBMC systems, analyze log bundles, develop components, build firmware and verify delivery with persistent Runtime evidence.

## Install

Requires Codex CLI 0.153.4, Node.js 20+, Git, and Python 3.12 with pip on the host. Windows runs Runtime, KB and device connections natively; WSL is optional for a separately selected build environment.

Linux or WSL:

```bash
codex plugin marketplace add lihuabai629-star/openubmc-codex-plugins && codex plugin add openubmc@openubmc-public
```

Windows PowerShell:

```powershell
codex plugin marketplace add lihuabai629-star/openubmc-codex-plugins; if ($LASTEXITCODE -eq 0) { codex plugin add openubmc@openubmc-public }
```

Alternatively, add `lihuabai629-star/openubmc-codex-plugins` as a Git marketplace in Codex, then install **openUBMC** from **Openubmc Public**. This is a community marketplace; OpenAI's default catalog is managed separately.

Start a new Codex task after installation. A clean machine opens in setup mode before downloading anything, so missing Python, npm, registry access or proxy settings do not prevent the task from opening. Ask Codex to complete openUBMC setup; it prepares locked dependencies and opens the private loopback configuration page. Later startups reuse the verified dependency cache. Credentials and Runtime history stay on the same Windows or Linux host as the device Runtime.

Windows can diagnose, collect logs, upgrade firmware, and apply or roll back an authorized live patch without WSL. Building a component or product still requires a separately configured toolchain, such as a Linux or WSL environment.

Try from any working directory:

- “这台 BMC 的传感器读数异常，帮我定位。”
- “分析这个一键收集日志包。”
- “帮我配置默认 BMC 账号和这个 IP 的覆盖项。”
- “编译这个目录里的 openUBMC 组件。”

For source-dependent work, provide the source directory when it differs from the
current directory. Target diagnosis and local plugin configuration do not require
a source checkout.

## Credentials

BMC access uses your own target credentials. Ask Codex to configure the openUBMC environment; it uses a private file outside the plugin.

Knowledge-base access needs an authorized OneID account and OAuth application configuration. Import a private JSON file with `username`, `password` and `clientSecret`; include the application's `clientId` and `redirectUri` when different from the defaults. No OAuth client secret is distributed with this plugin. Without these credentials, the knowledge tools report that configuration is incomplete while Runtime remains available.

## Update and remove

```bash
codex plugin marketplace upgrade openubmc-public
codex plugin add openubmc@openubmc-public
codex plugin remove openubmc@openubmc-public
```

The last command uninstalls the plugin. Credentials and Runtime history stay outside its cache. Start a new task after updates.

For an older personal plugin or loose Skill/MCP installation, preview and apply the bundled disable-only migration:

```bash
python3 -I <plugin-root>/scripts/pluginctl.py migrate --disable-only --preview
python3 -I <plugin-root>/scripts/pluginctl.py migrate --disable-only
```

The helper disables owned legacy entries, retains files, links, credentials and history, and preserves `openubmc@openubmc-public`. Start a new Codex task to load the saved state. Restore with `restore-legacy --transaction <id>`; later configuration or ownership changes require reconciliation. The explicit `migrate --remove` route remains available for removing owned loose registrations and links.

## Dependency recovery

For Python integrations, follow the packaged [entrypoint and import guide](plugins/openubmc/PYTHON.md).

Use `codex plugin list --json` to identify the installed version. The plugin directory is under `${CODEX_HOME:-$HOME/.codex}/plugins/cache/openubmc-public/openubmc/<version>`.

```bash
python3 -I <plugin-directory>/scripts/pluginctl.py doctor
python3 -I <plugin-directory>/scripts/pluginctl.py prepare --repair
```

`doctor` verifies the package, dependencies, duplicate entry points and local MCP startup. It reports local credential activation, knowledge authentication and remote target authentication separately; it does not establish access to a BMC or knowledge service. On Windows, use the setup tools presented in the Codex task; the controller and configuration page run locally on Windows.

## License

[MulanPSL-2.0](LICENSE). Third-party dependencies retain their own licenses and are downloaded from their package registries.
