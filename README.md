# openUBMC Agent Workflow

Coordinated openUBMC development, diagnosis, build, delivery, and target-runtime workflow for
Codex, Claude, and OpenClaw. The integrated Skills baseline is
`ceb46e8ca5542a4273128b1a4aa8606b2f5eaee0`.

## Install

The bootstrap downloads the installer and leaves only the managed checkout:

```bash
curl -fsSL \
  http://10.121.177.79/liqinghua/openubmc-agent-workflow/-/raw/main/bootstrap.py \
  | python3 -
```

The default `full` profile installs 11 Skills, the Target Runtime, and the standalone
`openubmc-kb` stdio MCP. The smaller runtime profile keeps the seven runtime-path Skills:

```bash
curl -fsSL \
  http://10.121.177.79/liqinghua/openubmc-agent-workflow/-/raw/main/bootstrap.py \
  | python3 - --skill-profile target-runtime
```

Codex and Claude receive managed stdio MCP entries. OpenClaw receives the same Skill links; its
current upstream configuration has no native MCP adapter, so no unsupported configuration key is
written.

## Credentials

BMC and OS credentials:

```bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" credentials
```

openUBMC KB OneID credentials:

```bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" credentials --kb
```

An existing private KB configuration can be imported with `credentials --kb --kb-config <file>`.
The MCP starts and reports configuration status even before OneID credentials are added.

## Lifecycle

```bash
INSTALLER="$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py"

python3 "$INSTALLER" check
python3 "$INSTALLER" repair
python3 "$INSTALLER" update
python3 "$INSTALLER" rollback
python3 "$INSTALLER" uninstall
```

`update` fast-forwards a clean managed checkout. `rollback` restores the previous known-good
revision and keeps the displaced revision available for another rollback.

## Validation

```bash
python3 scripts/validate_workflow.py
```

Optional bounded dual-target smoke:

```bash
python3 scripts/live_smoke.py \
  --target 10.121.136.200 \
  --target 10.121.177.159
```

The default smoke runs the MDB-only capability preflight for every target concurrently and prints
a compact capability comparison; it does not open a full diagnostic Case. A slow target is cut off
at the configured timeout without delaying the other result. Live Patch remains plan-only in this
tool. Upgrade verification uses the read-only preflight path when an artifact and product version
are supplied. Internal BMC TLS mode is the default; pass `--strict-tls` for system certificate
verification.
