# Changelog

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
