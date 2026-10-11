Part of #278, roadmap item 21.

## Problem

Run, Gate, Effect and host-adapter progress is inspectable through separate
receipts, but there is no bounded trace that links their timings without
mistaking telemetry for Runtime authority.

## Scope

- Add optional OpenTelemetry-compatible tracing at the existing Agent/MCP and
  Runtime boundaries. Trace data is observational only: it cannot accept a
  Gate, commit an Effect, alter a Run/Outcome, or become an execution preflight.
- Link spans with stable opaque Run/Effect references. Use an explicit
  attribute allowlist; exclude credentials, command text, prompts, raw device
  output, target addresses and artifact contents. Do not emit remote telemetry
  unless a destination is explicitly configured.
- Bound attribute sizes, queue size, export timeout and per-Run span count.
  Export failure or backpressure drops telemetry without retrying a device
  operation or changing the returned Runtime result.
- Keep tracing disabled by default. Enabling it must preserve the same typed
  Run/Gate/Effect/Outcome events and MCP result contract.

## Acceptance

- Offline collector tests show parent/child correlation across observe,
  execute, Gate and Effect with an explicit dropped-span count.
- Enabled-versus-disabled tests compare authoritative Runtime event streams
  and outcomes byte-for-byte under normal, exporter failure and queue-full
  paths. No new mandatory gate or interruption is introduced.
- A secret-containing synthetic input produces no secret in spans, exception
  text, or export payload. No live target or external collector is contacted.
- Installed-plugin packaging and an explicit disable path are verified.

## Ownership

Own a small trace adapter and focused Agent/MCP/Runtime instrumentation and
tests. Do not change credential persistence, source-index, Debug evidence
processing, or the openUBMC BMC interfaces. Preserve the existing Runtime
authority and inspect current upstream OpenTelemetry APIs before coding.
