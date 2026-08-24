---
name: openubmc-upgrade
description: Use when an already-built openUBMC HPM must be uploaded, activated, monitored, and version-verified on a specific BMC through Redfish, including a typed build-upgrade handoff from an existing diagnose-and-fix task. Also assess an explicit firmware rollback request against a separately available recovery path; the current production backend does not expose a standalone firmware rollback action. Requires current-task mutation authorization, a target, and verified artifact identity. Do not use to build HPMs, publish Conan packages, or diagnose source code.
---

# openUBMC Upgrade

Upgrade owns remote BMC firmware mutation. It accepts either a verified HPM
summary returned by Build or an already-built HPM whose path, SHA-256, and
product version are supplied by the current task. It starts when Target Runtime
selects the typed Upgrade operation for a Case or a direct non-Case request enters
that operation. Use the typed decision without reinterpreting or reconfirming it.

It does not build components, edit a manifest, publish Conan packages, or claim
runtime acceptance by itself.

An `upgrade-and-verify` intent or `diagnose-and-fix` plus
`delivery_strategy=build-upgrade` is parsed once and carried through upload,
reconnect, installed-version verification, and optional Debug acceptance.
Internal phases do not ask the user to repeat the target, credentials,
artifact identity, final purpose, or authorization. Build supplies the HPM
path, SHA-256, product version, and build evidence; it never opens the target.

When a Runtime `run_id` is present, consume `build.artifact`, target, credential selector,
final purpose, and authorization from that Run. Bare “继续” or “continue” means call
`execute(kind=resume)`; do not re-upload an HPM or reconstruct the operation from conversation history.
Upload, activation, reconnect, and fresh Debug verification stay in the same workflow. A mutation
outcome unknown blocks automatic continuation until the same durable MutationJournal is reconciled
with the same Run and Effect identity. Upgrade results are Runtime-owned domain Effects, not
separate workflow claims.
Build evidence IDs remain attached to the Run's `build.artifact` provenance; consume the artifact
identity from that record without asking the user to restate or confirm it.

Before upload, synchronize the Upgrade domain's local TargetRun to the Case-provided target epoch
floor. A successful upgrade advances the shared epoch once, so later Debug and other mutation
domains invalidate older capability state even when they own separate local connections. Never
restart epoch numbering from the Upgrade backend's local zero or ask the user to manage epochs.

Invoke bundled helpers from `$HOME/.agents/skills/openubmc-upgrade`. If that
canonical Skill link is unavailable, route the local setup gap to
`openubmc-environment-setup` before running Upgrade. Never set a generic
`SKILL_ROOT`.

## Required input

Before an upgrade write to a BMC, require all of the following:

- a typed Upgrade authorization accepted by Target Runtime;
- one HTTPS BMC target;
- HPM absolute path, expected SHA-256, and expected product version;
- a Redfish credential selector already carried by Target Runtime, explicit
  Redfish environment variables, a direct internal-development Redfish password,
  or a user-selected credentials file.

Require a confirmed recovery path only when rollback or recovery is actually in
scope. Authorization does not create a capability that the backend lacks.

When a shared credentials file is selected, it must contain the Redfish entries
below. It may also contain the documented openubmc-debug SSH, Telnet, and
OS-host credential keys; Upgrade ignores those values and reads only the
Redfish pair.

~~~text
REDFISH_USERNAME=...
REDFISH_PASSWORD=...
~~~

Direct Redfish password arguments are accepted in internal development mode and
may continue through the current Case workflow. Do not read SSH credentials as
a Redfish fallback.

## Preflight

Prefer the unified read-only preflight when all artifact fields are available. It performs stable
artifact hashing, credential validation, target-local UpdateService discovery, and the current
Manager version read without uploading:

~~~bash
python3 "$HOME/.agents/skills/openubmc-upgrade/scripts/preflight_upgrade.py" \
  --target https://<bmc> \
  --artifact-path <hpm> \
  --artifact-sha256 <sha256> \
  --product-version <version>
~~~

Its result reports whether a version change is required, artifact size versus any advertised
`MaxImageSizeBytes`, which upload method would be selected, and legacy endpoint compatibility
warnings. A green result authorizes no mutation by itself; pass the same typed artifact identity
into the task-owned Upgrade operation so target, credentials, and artifact are not asked for again.

Re-hash the HPM before touching the target:

~~~bash
python3 "$HOME/.agents/skills/openubmc-upgrade/scripts/artifact_identity.py" \
  --path <hpm> --expected-sha256 <sha256>
~~~

Validate the target and credential source without printing secrets:

~~~bash
python3 "$HOME/.agents/skills/openubmc-upgrade/scripts/redfish_credentials.py" \
  --target https://<bmc>
~~~

Target Runtime defaults to `allow_insecure_tls=true` for internal BMC environments; set it to
`false` when the target has a trusted certificate. The standalone preflight retains system
verification unless `--allow-insecure-tls` is supplied. Do not follow cross-host redirects or reuse
an upload URI discovered from another BMC.

Read the current installed version through the selected target's Redfish
service before upload. A separate openubmc-debug baseline is optional and is
requested only when the caller explicitly needs pre-upgrade runtime evidence.

## Upgrade workflow

1. Query the target's own Redfish UpdateService.
2. Select only an upload method advertised by that response, preferring
   MultipartHttpPushUri, then HttpPushUri, then SimpleUpdate.
3. For SimpleUpdate, use only an image URI that the target can reach; do not
   pretend a local HPM path is a reachable URI.
4. Upload once through an in-process HTTP client that keeps credentials out of
   command arguments and output. Large byte uploads use a separate 600-second
   request timeout, bounded by the task deadline; `upload_timeout` may override
   it. Record target, time, request size/timeout on transport failure, HTTP
   status, selected method, and Task or Monitor URI.
5. Monitor the created task. A disconnect during upload or activation is
   ambiguous: inspect the durable journal and target version/inventory before
   reopening the local artifact or uploading again. Recovery can finish even
   when the temporary local HPM has already been removed. When read-only
   evidence proves there was no installed, available, or pending artifact
   effect, return `replan_required` without uploading in that recovery call.
6. Confirm task completion and the intended installed version. A recovered TCP
   port or HTTPS listener is not success. When the target returns on the old
   ActiveBMC version, the requested version exists only as AvailableBMC, and
   UpdateService reports no pending activation work, classify the result as an
   activation fallback instead of polling forever or uploading again. Record
   it as a terminal failed verification so it does not block later work on the
   target; replaying the same operation reports the same failure without
   uploading again.
7. If the caller requested runtime acceptance, hand the target, installed
   version, and acceptance checks to openubmc-debug.

Use `scripts/target_runtime_adapter.py` for the typed transaction. Upgrade owns
the `upgrade` Redfish lease; Log Analyzer and formal Redfish Testing retain
separate sessions. A completed upgrade advances the target epoch, invalidates
all old lanes, reconnects Redfish for the installed-version read, and admits
optional Debug verification only as fresh evidence from the new epoch.

The MCP mutation operation identity excludes later Debug verification
selectors but includes the target and verified artifact identity. Repeating
the same upgrade with changed acceptance checks reuses the durable mutation
journal and does not upload the HPM again; fresh Debug verification still
runs. Change the artifact or mutation parameters, or start a new task, when a
deliberate new upgrade operation is required.
If the local MCP process reconnects with the same task ID, Target Runtime restores the target,
artifact-routing intent, and mutation journal identity but opens a new Redfish resource. Never use
the restored task context as proof that an upload or verification completed; inspect the durable
journal and collect fresh installed-version evidence.

Upgrade keeps a 32-entry target-binding LRU per task by default. This limits retained Redfish
resources, not the number of environments in the Case; selecting an evicted target reconnects it.
Failed workflow steps receive a new attempt through the same RunEngine route, while a durable
terminal journal receipt prevents an already completed or terminally failed upload from being
executed again.

Read [Redfish upgrade flow](references/redfish-upgrade.md) before performing
the write.

## Rollback

The current production Upgrade backend does not expose an independent firmware rollback action. A
Case may carry a distinct rollback authorization, but that decision does not create the missing
backend capability. Do not invent one, relabel a normal upgrade as rollback, or claim that a rollback
HPM was applied. Use only a separately available, explicitly selected recovery mechanism; if none
exists, report the capability gap and stop. Never roll back automatically because an upload,
timeout, restart, or mutation outcome is ambiguous, and never treat Upgrade authorization as
rollback authorization.

## Report

Return the target, artifact path and SHA-256, method, task URI or status, and
version result. Include the separate openubmc-debug verification status only
when runtime acceptance was requested. Report a partial external mutation
honestly when the task state is unknown.

When the Run becomes terminal, return its terminal Outcome. Closeout documents,
build evidence, HPM identity, mutation journal, installed-version proof, and
fresh Debug acceptance evidence remain available through the Operator / CI Plane.

## Resources

- [Redfish upgrade flow](references/redfish-upgrade.md)
- scripts/artifact_identity.py
- scripts/redfish_credentials.py
- scripts/target_runtime_adapter.py
