# Runtime measurement and maintenance specification

Status: implementation-ready on `codex/runtime-measurement-maintenance`.

## Scope

This maintenance checkpoint measures the Runtime Core and the Codex plugin without changing
the v2.0.9 release. It uses the public MCP `observe`/`execute` seams and deterministic local
fixtures. No BMC write, upgrade, or credential value is part of the measurement.

## Identity and conditions

- Reference source is the immutable v2.0.9 source commit `117ffcc580a98e2754c2cc998fa3cd4ce427d7c6`.
- Every record includes source commit, plugin content digest when applicable, Python/Node/Codex
  versions, model identity, host identity, cache mode, and a run UUID.
- A/A uses the same source, prompt, client, model, configuration, and alternating order. A/B is
  historical context only unless a new ticket explicitly selects another source.
- Cold means no process, dependency, or execution snapshot cache. Warm means the dependency and
  execution snapshot are reusable. Model cache state is recorded separately and never inferred.

## Measurements

The deterministic client records, for each stage, monotonic wall time, CPU time, serialized
request and response bytes, Runtime storage bytes before/after, event count, and failure class:

1. `start`: initialize the Runtime and open the first legal Run.
2. `diagnosis.acceptance`: submit a valid diagnosis Gate response bound to the returned schema,
   receipt, and evidence IDs.
3. `developer.change`: submit a valid developer Gate response using the same public contract.
4. `mcp_tools_list`: transport discovery without model execution.

Plugin records additionally split dependency preparation, MCP process startup, first tool
response, and warm first tool response. Model records contain provider wait, model tokens, and
tool output bytes only when a real model is used; these are never mixed into Runtime timings.

## Sample and retry rules

- Deterministic local benchmark: 30 repetitions per stage in fresh temporary state, followed by
  10 warm repetitions. The first repetition is retained and separately marked as cold.
- A/A model measurement: at least 10 interleaved pairs; use the same valid run schedule and retain
  every attempted invocation. A failed invocation is classified once as `runtime`, `plugin`,
  `model`, `transport`, or `harness`; it is not silently retried into the denominator.
- A rerun is allowed only after recording the original failure, changing no source, and using a
  new run UUID. Reports show attempted, valid, and rerun counts.
- A maintenance change requires no statistically significant regression in deterministic Runtime
  medians or p95, no lifecycle leak, and zero failed qualification gates. Model A/A results are
  explanatory evidence, not a Runtime release gate.

## Evidence contract

JSON reports are immutable once produced and contain a schema version, source identity, condition
record, stage records, aggregate statistics, failure taxonomy, and SHA-256 digest of the report.
The release decision must link the report, test output, package digest, and qualification output.

