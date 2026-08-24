# openUBMC Agent Workflow roadmap completion audit

Date: 2026-08-25  
Closeout: [Issue #70](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/70)

Canonical machine-readable evidence: [`roadmap-completion.json`](roadmap-completion.json)

## Final state

The implemented roadmap is present on canonical `main` at merge commit
`a28350d19fde5808626f3c73b4e070db440cfa83`. The final compatibility-retirement qualification used
source `e7dc74c052f3874d3d9214ce0cfae8949a397765` and lock-only commit
`7dc350cd3ecf2ffab2d1d4db89d4bac81f1ccec4`.

- Execute A/B: 10 valid pairs, 0 invalid pairs, decision `passed`.
- Release Gate: 13/13 gates passed, `promotable=true`.
- Release Gate evidence digest:
  `sha256:e18fdbfcbc04e84a5ba79f2160728cedc11d4a873a40e7091f2beaa35c9b2a67`.
- Compatibility decision: five writers and the profile were `ready=true`.
- Historical telemetry readers and old-event upcasters remain; retired inputs cannot be enabled.
- Canonical main validation: [run 32764011480](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32764011480), successful.

## Completion matrix

| Roadmap batch | Delivery evidence | Public verification seams | GitHub evidence |
| --- | --- | --- | --- |
| Runtime Core continuous validation and v2 qualification | Issues [#38](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/38), [#36](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/36); PR [#37](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/37) (`a96e84e`), PR [#39](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/39) (`f9c312d`) | `scripts/tests/test_release_gate.py`, `scripts/tests/test_runtime_qualification.py`, `scripts/tests/test_agent_gateway_ab.py`, `python scripts/validate_workflow.py` | PR checks passed; [main run 32544813303](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32544813303) passed |
| Compatibility retirement preparation and retirement | Issues [#48](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/48), [#66](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/66); PR [#49](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/49) (`3a3ea67`), PR [#67](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/67) (`a28350d`) | `scripts/tests/test_compatibility_retirement.py`, old-schema gates in `scripts/tests/test_release_gate.py`, old-event tests in `openubmc-target-runtime/tests` | [PR #67 checks](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32761811656) and [main run 32764011480](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32764011480) passed |
| Incident recovery and stress qualification | Issues [#50](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/50), [#52](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/52); PR [#51](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/51) (`fbee4fe`), PR [#53](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/53) (`dd34d37`) | `openubmc-target-runtime/tests/test_incident_lifecycle.py`, `scripts/tests/test_runtime_qualification.py`, mutation crash-cut suites | [main run 32621449677](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32621449677) and [run 32629450828](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32629450828) passed |
| ArtifactRef lifecycle and Log Bundle stages | Issue [#54](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/54); PR [#55](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/55) (`d7ea891`) | `openubmc-target-runtime/tests/test_artifact_lifecycle.py`, `openubmc-log-analyzer/tests/test_log_bundle_stages.py` | [main run 32634018685](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32634018685) passed |
| Domain Pack contract and bounded read-only execution | Issues [#56](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/56), [#58](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/58); PR [#57](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/57) (`88c7e57`), PR [#59](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/59) (`521a3dd`) | `openubmc-target-runtime/tests/test_domain_pack_conformance.py`, selector and ObservationRef behavior in `test_agent_gateway.py`, Debug lease/scope tests | [main run 32642484271](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32642484271) and [run 32649732340](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32649732340) passed |
| Evidence retrieval and Skill progressive disclosure | Issues [#60](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/60), [#62](https://github.com/lihuabai629-star/openubmc-agent-workflow/issues/62); PR [#61](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/61) (`4bda60f`), PR [#64](https://github.com/lihuabai629-star/openubmc-agent-workflow/pull/64) (`657d6ee`) | `openubmc-target-runtime/tests/test_evidence_index.py`, `openubmc-debug/tests/test_skill_progressive_disclosure.py`, Skill-disclosure A/B in `scripts/tests/test_agent_gateway_ab.py` | [main run 32653027351](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32653027351) and [run 32697848377](https://github.com/lihuabai629-star/openubmc-agent-workflow/actions/runs/32697848377) passed |

Every delivery PR used a dedicated head branch and passed both GitHub jobs: `CI contract preflight`
and `Complete repository validation`. The PR records also preserve the independent Standards and
Spec review result for each batch. Tests added with each batch exercise the public Agent,
Operator/CI, Runtime repository, ArtifactStore, Domain Pack, or qualification seams rather than a
second Agent-facing control surface.

## Release identity

A managed release uses a source commit followed by a lock-only commit. The tag identifies the
lock-only commit, whose `release-lock.json` identifies and digests its sole source parent. That
identity is immutable and can be verified at the lock-only commit.

The development branch is intentionally different: mutable `main` continues after a qualified
source and can merge documentation or later implementation. Its checked-in `release-lock.json`
therefore remains a historical release snapshot and is not expected to verify the current main
tree. This does not indicate source corruption. A future release must select a new final source,
rerun qualification, and create a new lock-only commit; it must not rewrite the previous identity.

No `v2.0.0` tag or GitHub Release is created by this closeout.
