# Codex Adoption Qualification

Codex Adoption Qualification is the canonical Operator / CI Plane report for the supported
product client. It binds repository identity to a hermetic Codex MCP exchange and the Runtime
qualification matrix:

```bash
python3 scripts/codex_adoption_qualification.py \
  --model-identity '{"provider":"openai","model":"gpt-5.6-sol"}' \
  --codex-identity '{"version":"codex-cli 0.151.0"}' \
  --source-commit "$(git rev-parse HEAD)" \
  --output codex-adoption-qualification.json
```

Model and Codex provenance are required non-empty JSON objects:

```bash
python3 scripts/codex_adoption_qualification.py \
  --model-identity '{"provider":"openai","model":"gpt-5.6-sol"}' \
  --codex-identity '{"version":"codex-cli 0.151.0"}' \
  --output codex-adoption-qualification.json
```

The report builds a deterministic temporary lock-only child from the clean candidate source,
installs that child as a managed immutable source, and accepts installation identity only when the
installer reports operational readiness, verified Release identity, and evaluation readiness. The
installed Release lock, source tree, workflow, and Runtime identities must match the candidate.
Launcher verification uses a path-independent semantic identity bound to the Runtime API, Runtime
content digest, Final source commit, and installed entrypoint; temporary installation paths are not
part of that identity.

The installed-launcher exchange verifies `initialize`, `tools/list`, the exact `observe` and
`execute` Agent Interface, and a hermetic `tools/call` through `execute`. It deliberately uses an
actionable preflight request so it cannot touch a BMC or credentials. That protocol harness is
non-formal. Formal lifecycle evidence comes from two pinned Codex 0.151.0 processes using an
isolated local Responses endpoint; each process must start the installed Runtime launcher as its
direct child, expose the Runtime namespace containing exactly `observe` and `execute`, and leave a
stopped `client-terminated` lifecycle record. Restart evidence is derived from those two process
runs. Active-request drain and explicit task closeout remain independently covered by hermetic
Runtime lifecycle tests. Successful workflow completion is proven by the representative task
matrix. A requested source commit must match the clean workspace `HEAD` or the verified
release-lock parent. This allows a lock-only child to qualify its exact Final-source parent without
accidentally qualifying the child as source code.

It emits an ordered `failed_dimensions` list for Codex product qualification. Maintenance
checkpoint readiness requires those Codex-owned dimensions. External harness identity, presence,
or isolation status is recorded separately and cannot block either Codex product qualification or
the maintenance checkpoint.

External evaluation harness metadata is recorded as non-blocking provenance. It is not a product
client and cannot change the Codex qualification result. The report is deterministic for the same
source and inputs and includes a digest over its complete evidence payload.

`continuous_closeout_qualification.py` remains the internal evidence collector used by this
report. CI and Release Gate consumers should use Codex Adoption Qualification rather than treating
the internal collector as a second public qualification decision. Release Gate parses the report,
verifies its schema, internal digest, source binding, dimensions, and eligibility decision, and
retains the report as release evidence.
