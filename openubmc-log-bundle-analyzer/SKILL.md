---
name: openubmc-log-bundle-analyzer
description: Validate and analyze local openUBMC one-click log bundles with bounded problem-driven routing and file-and-line evidence. Use for a supplied .tar/.tar.gz archive or extracted dump_info tree; not for collecting a bundle from a BMC, live-runtime debugging, or observability-system diagnosis.
---

# openUBMC local log bundle analysis

Analyze a supplied local `.tar`/`.tar.gz` or extracted `dump_info` tree. Require a problem statement except for explicit `--validate-only` checks. This Skill opens no network connection. Remote collection and live investigation require their own available workflow owners.

Run `python3 scripts/analyze_bundle.py --bundle <archive> --problem '<question>' --json`, or use `--bundle-dir <directory>`. Resolve scripts from this Skill's resource directory. Read [the analysis workflow](references/workflow-guide.md) for input limits and the evidence/report rules.

Keep file-and-line evidence, scan failures, truncation, definition reliability and missing coverage explicit. Timelines and event correlations provide context and do not establish causality. Structured summaries omit sensitive values; unknown status codes remain opaque. Use `scripts/audit_catalog_coverage.py` for explicit catalog coverage audits.

## References to load only when needed

The analyzer owns the maintained catalog; normal analysis does not require loading these catalog references into context. Load [analysis details](references/analysis.md), [catalog definitions](references/catalog-schema.md) or [the output schema](references/analysis-output.schema.json) only for the current question or an analyzer change.

At a stage change, select and read the current owner through the Host catalog; carry the existing Run and authorization. Report the smallest missing read-only probe when bundle evidence is insufficient.
