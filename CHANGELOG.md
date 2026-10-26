# Changelog

## 2.2.14 (development)

- Direct Live Patch CLI journals have an explicit reconcile-only command with the original task and operation IDs. Recovery pins the original artifact identity and refuses missing or mismatched journals without a new mutation; CLI task IDs remain separate from public Runs.
- Documentation review analyzer references resolve to bundled resources instead of removed peer Skill entries.

## 2.2.13 (development)

- Runtime and KB keep stdio connections while the current parent process identity is verified. Idle expiration remains for unverifiable owners; parent loss, stdin closure and explicit shutdown still drain active requests. Already closed Host connections require an MCP reload.

## 2.2.12 (development)

- Post-change verification exposes unsupported capabilities or an exhausted three-attempt budget as an Incident with an acceptance gap. Completed mutations and journals remain intact, including after restart and for previously deferred Runs.
- Prepared execution copies retain a complete release lock separately from dependencies. Backend children use a stable working directory; updates retain older caches used by existing hosts, and rollback refuses to move an active newer cache.
- SessionStart distinguishes relevant active Runs from saved terminal Outcomes. Runtime startup is optional for unrelated and offline conversations.
- Documentation review uses the current Run evidence interface and relocates optional references to the installed Skill paths. Numeric PII checks exclude digits inside hashes and identifiers while retaining separate sensitive values.
- Debug examples resolve the active plugin root. Source routing distinguishes coexisting implementation generations; build and QEMU guidance keeps large generated files out of host Git snapshots and checks actual output-volume space.

## 2.2.11 (development)

- Knowledge requests recover an expired refresh token when OneID repeats the expiration message in its `error` field, then continue once with the saved account. Explicit client and scope errors retain their original failure.

## 2.2.10 (development)

- Supplied task credentials use private local input without repeated entry. BMC account checks and typed property assignments reuse the selected transport policy; strict certificate/host-key qualification remains optional.
- Routine validation can run directly; targeted QEMU checks use scoped commands without requiring a wrapper, TLS qualification or a full healthy-device baseline. Missing optional checks remain visible as gaps.
- Existing HPM identity checks report release-provenance labels as warnings while preserving digest, size and product-version conflicts.
- Authorized OS identity and network observations can use BMC SSH and native SOL when no OS IP is available. BMC and OS accounts remain separate; busy consoles are left untouched and owned logins are closed after collection.
- The configuration page accepts an OS console account without an OS IP. Linux/WSL packages include the pinned SOL transport dependencies.
- WebUI upgrade rejection diagnostics retain the endpoint and bounded, redacted server error codes and messages.
- Standalone diagnostic bundle collection resolves accounts through its validated Runtime loader without requiring a global Python package installation.

## 2.2.9 (development)

- Offline analysis and bounded evidence reads use shared structured tools through Codex MCP and native DSH adapters on Windows, Linux and WSL. Full redacted JSON remains local while model responses retain coverage limits and failures.
- Analyzer output uses portable evidence paths and UTF-8 JSON without a BOM; new result files preserve existing files.

## 2.2.8 (development)

- Upgrade preflight and local Redfish account diagnostics reuse activated target accounts through the Runtime resolver, retaining explicit credential sources and TLS policy.

## 2.2.7 (development)

- Active-alarm capability reflects the latest collected endpoint instead of retaining the preflight placeholder; uncollected endpoints remain not checked.
- Standalone SSH object and alarm readers reuse activated target credentials through the Runtime resolver while preserving explicit account selection.

## 2.2.6 (development)

- Local and remote diagnostic bundles share one workflow entry, catalog and bounded analysis core.
- The default native catalog has 11 Skills; documentation review is optional and Runtime recovery belongs to plugin management.
- Knowledge-base startup is optional. Runtime remains available independently.
- KB 1.3.3 accepts null optional references in context-only retrieval responses.
- Conan failures return a classified cause and a private diagnostic file location.
- Source provenance uses the request's remaining time budget. Rotated-log evidence retention stays bounded across files.
- The main validator includes panel build, native contract and tests. Native installation is the recommended path; loose profiles remain migration tools.
