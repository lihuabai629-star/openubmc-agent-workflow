# Redfish Upgrade Handoff

Build does not connect to a BMC or execute an upgrade. Use this reference only to prepare the typed
artifact result consumed by `openubmc-upgrade` when the selected delivery strategy is
`build-upgrade`.

Return all of the following from the completed product build:

```yaml
build_result:
  artifact_path: /absolute/path/to/openubmc.hpm
  artifact_sha256: <64 lowercase hex characters>
  product_version: <expected installed version>
  evidence_ids:
    - <build evidence ID>
```

The artifact must come from a completed build with a successful checked log, a package timestamp
newer than the build start, and metadata or package references matching the intended components.
Re-hash the final file after it reaches its handoff path. Do not return an HPM left by a failed,
interrupted, or timed-out build.

Pass target identity, Redfish credential selectors, rollback requirements, and runtime acceptance
checks separately through the task context. Never place credentials in the Build result.

`openubmc-upgrade` owns UpdateService discovery, upload, activation monitoring, target epoch
advancement, installed-version verification, ambiguity recovery, and rollback. After Upgrade,
`openubmc-debug` owns fresh runtime acceptance evidence. Build must not implement a fallback upload
path or use SSH, Telnet, Web REST, Live Patch, or a vendor CLI to mutate the target.
