# Build Handoff Contract

When another workflow routes into `openubmc-build`, prefer a structured handoff over `git diff`. The handoff records what was changed in the active session; it is not a dirty-worktree inventory.

## Runtime Gate Handoff

When `execute` returns a `waiting_response` Turn for the `build.artifact` Gate, preserve this binding:

```json
{
  "run_id": "<run ID>",
  "state": "waiting_response",
  "gate": {
    "name": "build.artifact",
    "owner": "openubmc-build",
    "gate_id": "<gate ID>",
    "gate_version": 1,
    "schema_digest": "sha256:<digest>",
    "input_schema": {}
  }
}
```

Preserve the Run and Gate identity. Reconstruct changed files and components from the active task
and repository, not unrelated dirty-worktree state. After the checked build, submit the artifact
as an `openubmc-hpm` `ArtifactRef` containing its absolute handle, `sha256:` digest, size,
`openubmc-build` provenance, `run-lifetime` retention hint, product version, Run target, and Run ID.
Submit that reference, source revision, component versions, commands, logs/evidence IDs, and known
gaps with `execute(kind=respond)` using the exact Gate binding. The returned Turn is the next
workflow state; do not call a legacy continuation tool.

The Gate payload also carries `dependency_readiness` and `validation_results`.
Check dependency readiness once, set `resolution=available` or
`resolution=blocked_external`, and reuse its identity for official UT and build.
Classify build as `compiled`, `compile_failed`, or `dependency_graph_blocked`.
Supplementary checks remain separate with `counts_as_official_ut=false`. Do not
fabricate or vendor an unavailable external dependency. A completed Build Gate
requires the explicit `compiled` result; a verified ArtifactRef alone does not
infer compilation success.

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
