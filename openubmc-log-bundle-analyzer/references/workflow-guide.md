# openUBMC log bundle analyzer

Treat the one-click log bundle as the evidence boundary. Validate the artifact before interpreting it, select logs from the reported problem, and keep every conclusion traceable to a bundle-relative file and line number.

## Inputs and scope

Require a problem statement and one local source. Use `--validate-only` only
when the explicit goal is structure validation without problem analysis:

- local `.tar` or `.tar.gz` bundle;
- extracted directory containing one `dump_info/` tree;

This Skill is not the primary workflow when the required evidence is outside a
one-click log bundle:

- a BMC host from which a bundle still needs to be generated or downloaded requires a dedicated bundle-collection workflow first;
- live `systemctl`, `journalctl`, Skynet, MDB/D-Bus, coroutine, or ASAN investigation requires explicitly authorized live, read-only runtime diagnostics;
- Prometheus metrics, Elasticsearch logs, Zipkin traces, OTLP, Collector, or telemetry-path diagnosis requires evidence from the relevant observability system.

Do not open a network connection or collect a bundle from this Skill. When a
bundle cannot answer a question about current object or service state, report
the gap and recommend the smallest read-only live probe without assuming that
any external Skill or integration is installed.

## Run the deterministic analyzer

From this Skill directory, analyze a local archive:

```bash
python3 scripts/analyze_bundle.py \
  --bundle /path/to/openubmc-log-bundle.tar.gz \
  --problem 'BMC login failed' \
  --json
```

```bash
python3 scripts/analyze_bundle.py \
  --bundle /path/to/openubmc-log-bundle.tar.gz \
  --validate-only \
  --json
```

Use `--bundle-dir` instead of `--bundle` for an extracted tree.

## Analysis workflow

1. Establish artifact identity from the local archive path and SHA-256 when an archive is available.
2. Extract archives with path, member-type, member-count, and expanded-size guards.
3. Validate that exactly one bundle root contains `dump_info/` and at least one recognized section such as `LogDump`, `AppDump`, or `RTOSDump`.
4. Let the analyzer select logs from its maintained catalog. When the problem names a concrete file or bundle-relative path that actually exists, bind that physical file first and do not add directory companion logs unless the user explicitly asks for correlation. Respect the per-log path, per-file byte, total-byte, and maximum-line limits; do not scan every matching file by default.
5. Apply the incident time window when supplied. A date-only end bound includes the whole day; use `--analysis-timezone` (`UTC`, an offset, or an IANA zone) for timestamps without an embedded offset. Lines without parseable timestamps cannot satisfy an explicit time window.
6. Review the returned bundle-relative evidence pointers, stable `evidence_findings`, definition status, source type, source reference and revision when present, reliability, scan errors, and scan truncation state. A read failure is an evidence gap, not an empty match.
7. Use `timeline`, broad `correlation_windows`, and operation-anchored `event_correlations` as context only. A timeline is chronological only when `sequence_scope` is `global`; `time_domain` scope is ordered only within each timestamp domain and carries a scan warning. Check `correlation_scan` and each `candidate_scan_complete` flag before treating an absent nearby event as meaningful. A shared component or nearby timestamp narrows inspection; neither establishes causality.
8. Review `structured_summaries` for supported MDB, delimited-record, and PCIe-topology files. These summaries expose bounded structure and counts rather than raw property values, record bodies, or device identifiers. Treat undocumented status codes as opaque.
9. Form a bounded hypothesis from the returned result. Separate direct evidence, supporting context, contradictory or missing evidence, and the next discriminator.

Every observed path should resolve to either a maintained file/component definition or an `observed_path` artifact definition. The latter is a coverage contract only: it records that the file family exists and must not be mistaken for field-level semantics. Infer unknown meaning only from bundle-local context or a public source the user supplied; do not invent a file contract.

When the question is whether the catalog explains every physical file, run the
coverage audit instead of inferring completeness from ordinary analysis:

```bash
python3 scripts/audit_catalog_coverage.py \
  --bundle /path/to/openubmc-log-bundle.tar.gz \
  --json
```

The audit separates `semantic`, `explicit_opaque`, and `generic_fallback`
coverage, reports uncovered paths, and flags both same-priority ambiguity and
conflicting semantic-versus-opaque handling contracts.
Generic fallback coverage means the file is handled safely, not that its purpose
is known.

Treat nested archives in `CoreDump`, `OptBMC`, or other sections as opaque artifacts by default. Record their path and size when relevant, but do not recursively unpack core files, factory backups, configuration archives, or other potentially sensitive payloads unless the user explicitly authorizes it and the problem requires it.

## Evidence contract

Report:

- bundle identity and structure-validation result;
- incident question and time window;
- selected logs and why they were selected;
- the definition status, source type, source reference, optional `source_revision`, and reliability of each selected log contract;
- each selected definition's semantic level, artifact format, route mode, sensitivity, and `scan_mode`;
- evidence blocks with bundle-relative path, line number, timestamp when present, and redaction marker when applicable;
- scan completion, truncation, decode/read failures, and applied resource limits;
- a cross-log timeline with explicit global or per-time-domain ordering, plus temporal-correlation windows when timestamps are comparable;
- stable evidence findings with sampled counts, lower-bound status, and source pointers;
- operation-event anchor correlations, including signed time deltas and exact shared-component tokens;
- structured summaries, their interpretation limits, and omitted sensitive fields;
- root-cause hypothesis and confidence;
- contradictory or missing evidence;
- one minimal next read-only probe when the bundle is insufficient.

Use these confidence levels:

- **High**: a direct failure line identifies the failing layer, or two independent bundle sources agree;
- **Medium**: one strong source supports the hypothesis without contradiction;
- **Low**: evidence is indirect, stale, incomplete, or absent.

Do not call the system healthy merely because no matching line was found. Use `insufficient_bundle_evidence` when a complete bounded scan has no supporting evidence, and `analysis_incomplete` when read failures or limits leave material content unexamined.

## Safety

- Never embed credentials in this Skill, example hosts, committed files, or reports.
- Output uses the `shareable` disclosure profile by default: credentials are always redacted, while common host, account, browser, network, asset, and local location identifiers are also hidden. `output_policy.common_identifier_redaction` describes this best-effort coverage; bundle-relative evidence pointers remain available.
- Use `--include-sensitive-context` only when exact target, path, or ordinary asset identifiers are required. It switches to the `sensitive_context` profile but never disables credential redaction; review that output before sharing it.
- Human-readable terminal output escapes control characters from untrusted log text; JSON output preserves valid JSON escaping.

## References to load only when needed

- To interpret JSON fields, disclosure profiles, assessment states, scan gaps, findings, correlations, or structured-summary limits, read [references/analysis.md](analysis.md). Machine consumers can validate version 1.0 output against [references/analysis-output.schema.json](analysis-output.schema.json).
- To investigate an unfamiliar file or interpret known file families, read [references/logs.md](logs.md).
- To add definitions, review handling invariants, or audit semantic coverage, read [references/catalog-schema.md](catalog-schema.md). The executable routing catalog is `references/logs.json`; normal analysis does not require loading these catalog references into context because the analyzer reads the JSON itself.
