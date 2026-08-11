# Build Handoff Contract

When another workflow routes into `openubmc-build`, prefer a structured handoff over `git diff`. The handoff records what was changed in the active session; it is not a dirty-worktree inventory.

## Context Runtime Handoff

When `workflow.next` returns `status: waiting_phase_record`, consume these fields directly:

```json
{
  "required_skill": "openubmc-build",
  "handoff_arguments": {
    "case_id": "<case ID>",
    "expected_revision": 12,
    "phase_type": "build.artifact",
    "source_revision": "<source revision>",
    "changed_files": ["<task-owned changed file>"],
    "changed_components": ["<component name>"],
    "delivery_strategy": "build-upgrade"
  },
  "phase_record_contract": {
    "phase_type": "build.artifact",
    "producer_identity": "openubmc-build"
  }
}
```

Use `handoff_arguments` instead of reconstructing inputs from the dirty worktree. Preserve the
returned `case_id`, revision, source identity, delivery route, and task-owned changed set. After the
checked build, merge the artifact identity, status, evidence, commands, logs, and known gaps into
the supplied `phase_record_contract`, submit it with `phase_record`, then call `workflow.next`
again. If the Runtime handoff omits a field that the build genuinely requires, derive it from the
active repository before asking the user.

## Minimal Handoff

```json
{
  "changed_files": [
    "general_hardware/src/lualib/example.lua",
    "general_hardware/mds/model.json"
  ],
  "changed_components": [
    {
      "name": "general_hardware",
      "root": "/home/workspace/source/general_hardware",
      "reason": "code and MDS model changed",
      "need_bmcgo_gen": true
    }
  ],
  "build_type": "debug",
  "stage": "dev",
  "manifest_root": "/home/workspace/manifest",
  "board": "BMC/openUBMC",
  "delivery_strategy": "build-upgrade",
  "deployment": {
    "requested": true,
    "method": "redfish",
    "target_bmc": "candidate",
    "rollback_required": true
  }
}
```

## Field Rules

| Field | Rule |
| --- | --- |
| `changed_files` | Files actually touched in the current task/session, not every dirty file from git status. |
| `changed_components` | Component roots to build; include interface/model packages when they must be rebuilt. |
| `need_bmcgo_gen` | `true` when MDS, MDB interface/path JSON, properties, methods, events, or generated-contract inputs changed. |
| `build_type` | `debug` or `release`; debug normally maps to `stage=dev`, release to `stage=stable`. |
| `stage` | Conan/product stage; normally `dev` for debug and `stable` for release unless the user or repo policy says otherwise. |
| `manifest_root` | Product build root; do not assume it is the same as the source checkout. |
| `board` | Product manifest selector/path when known; otherwise discover from manifest. |
| `delivery_strategy` | `build-upgrade` when the result must be handed to Upgrade; omit it for a standalone build. Live Patch does not consume a Build result. |
| `deployment.requested` | Routing metadata only. `true` means hand the completed artifact to `openubmc-upgrade`; Build still performs no target access. |
| `deployment.method` | `redfish` for the Upgrade handoff. Build does not implement transport fallback. |
| `deployment.target_bmc` | Optional target identity retained by the task. Null means build only. Do not store credentials here. |
| `deployment.rollback_required` | Passed through to Upgrade; Build does not inspect or execute rollback. |

If a handoff is missing, reconstruct it from the current conversation and explicit paths first. Use git status only as a fallback candidate list.

## Build Result

Return a typed artifact identity after the checked product build completes:

```json
{
  "artifact_path": "/absolute/path/to/openubmc.hpm",
  "artifact_sha256": "<64 lowercase hex characters>",
  "product_version": "<expected installed version>",
  "evidence_ids": ["<build evidence ID>"]
}
```

For `build-upgrade`, pass these fields unchanged to `openubmc-upgrade`. Target coordinates and
credential selectors stay in the task-owned TargetRun and are not copied into the Build result.
