# openUBMC Agent Workflow

This repository contains the coordinated openUBMC development and target-runtime workflow for
Codex, Claude, and OpenClaw. The Environment Setup installer manages Skill links, workflow tools,
the Target Runtime MCP, client registration, updates, and repair.

## Install

```bash
git clone http://10.121.177.79/liqinghua/openubmc-agent-workflow.git
cd openubmc-agent-workflow
python3 openubmc-environment-setup/scripts/install_environment.py install \
  --source-mode managed --non-interactive
```

Use `--skill-profile target-runtime` for the seven-Skill runtime-focused profile. The default
profile also installs `openubmc-dt-testing`.

## Update

```bash
python3 "$HOME/.agents/skills/openubmc-environment-setup/scripts/install_environment.py" update
```

For a linked checkout, update it with Git and run `refresh` instead.
