# Stable Skill promotion

`scripts/stability_promotion.py` prepares a clean candidate from the Windows
Skill center and checks its identity before an installer change. It does not
activate Skills, change installer state, publish a release, or contact a target.

The center is the source of Git objects and the merged ref. A dirty development
checkout can coexist with a clean candidate. When that same dirty checkout is
used as the candidate, the check fails. A candidate must be a complete Git
worktree root whose HEAD exactly matches the selected commit. Its commit and
tree must exist in the center, and the commit must be an ancestor of the center's
resolved merged ref. The report records that ref's full commit so later ref
movement cannot be mistaken for the original check.

Create a separate checkout outside the center:

```bash
python3 scripts/stability_promotion.py prepare \
  --center /mnt/c/Users/liqhs/Documents/Codex/skills-control-plane \
  --candidate /home/workspace/skill-candidates/runtime-stable \
  --commit '<full-merged-commit>' \
  --main-ref main
```

`prepare` clones only Git objects and checks out the selected commit in detached
HEAD state. It requires a new destination and does not register a worktree in
the center, move center refs, or copy uncommitted files. It rejects unmerged
commits before creating the destination. Preparation alone does not qualify a
candidate for installation.

Check it against the existing installation:

```bash
python3 scripts/stability_promotion.py check \
  --center /mnt/c/Users/liqhs/Documents/Codex/skills-control-plane \
  --candidate /home/workspace/skill-candidates/runtime-stable \
  --commit '<full-merged-commit>' \
  --main-ref main \
  --installer-home /root > /tmp/skill-promotion-check.json
```

The installer remains the authority for the active source, profile, Runtime,
and Skill links. The helper reads its protected `environment-state.json` through
the installer's loader and calls its Runtime inspector. It checks the recorded
source commit against the active clean checkout, verifies Runtime content,
manifest, launcher and composition, and verifies recorded Skill links. Missing
state or identity drift blocks a ready decision. An active dirty checkout is an
identity problem even when a separate candidate is clean.

Each candidate Skill gets the existing release-lock package digest, computed
from its declared `skill.json` files. The candidate Runtime and composition use
the installer's digest rules. A standalone Skill center need not contain
`workflow.json`; if a release lock exists, its verifier must pass. These facts
are a transient report, not a second activation database or release registry.

The JSON includes a concrete `installer_proposal.argv` and shell rendering for
the existing linked-source installer. It targets Codex and preserves the
recorded Skill profile and preserved-Skill list. It is a proposal only: a
`ready` result means these source and installation identity checks passed, not
that Runtime release qualification or device acceptance passed. Keep the clean
candidate available while installer links point to it, and rerun `check`
immediately before any separately approved installation. After installation,
use the normal installer `check --deep --json` and the applicable Runtime
qualification. Rollback continues to use installer state and its existing
rollback workflow.
