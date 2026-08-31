# Codex Adoption Qualification

Codex Adoption Qualification is the canonical Operator / CI Plane report for the supported
product client. It binds repository identity to a hermetic Codex MCP exchange and the Runtime
qualification matrix:

```bash
python3 scripts/codex_adoption_qualification.py \
  --source-commit "$(git rev-parse HEAD)" \
  --output codex-adoption-qualification.json
```

Optional model and Codex provenance can be supplied as JSON objects:

```bash
python3 scripts/codex_adoption_qualification.py \
  --model-identity '{"provider":"openai","model":"gpt-5.6-sol"}' \
  --codex-identity '{"version":"codex-cli 0.150.0"}' \
  --output codex-adoption-qualification.json
```

The report verifies immutable source identity, the configured Codex Runtime launcher's installed
state and content digest, `initialize`, `tools/list`, one hermetic `tools/call` through `execute`,
the exact `observe` and `execute` Agent Interface, the representative task matrix, projection
correctness, and task-level MCP lifecycle closeout. A requested source commit must match the clean
workspace `HEAD` or the verified release-lock parent.

It emits an ordered `failed_dimensions` list for Codex product qualification. Maintenance
checkpoint readiness is a separate aggregate: it requires those dimensions and evaluation
isolation, while external harness identity or presence cannot block Codex product qualification.

External evaluation harness metadata is recorded as non-blocking provenance. It is not a product
client and cannot change the Codex qualification result. The report is deterministic for the same
source and inputs and includes a digest over its complete evidence payload.

`continuous_closeout_qualification.py` remains the internal evidence collector used by this
report. CI and Release Gate consumers should use Codex Adoption Qualification rather than treating
the internal collector as a second public qualification decision.
