Part of #278, roadmap items 01 and 19. Final offline synthesis #253 depends on
the behavior issues this evaluates.

## Problem

Current component tests and short synthetic probes do not establish that an
Agent can finish a stateful openUBMC task after interruption without repeating
an unknown Effect or claiming an unverified delivery stage.

## Scope

- Define at least 20 versioned, sanitized scenarios spanning diagnosis,
  source change, build/upgrade, interrupted and resumed Effects, Gate replay,
  wrong target/evidence, degraded optional services, repeated shell fallback,
  missing credentials, partial results and terminal delivery. Reuse the
  existing Runtime fake backend, Agent evaluation harness and replay scoring.
- Run each scenario in three independent Agent trials with pinned model,
  client version, source commit, prompt digest and schedule. A deterministic
  offline fixture suite is useful but does not substitute for the 60 actual
  Agent trials. No live BMC, customer identifiers or real secrets are needed.
- Score correctness from Runtime Run/Gate/Effect/Outcome and host final events,
  not the model's self-report. Record interruption recovery, repeated actions,
  duplicate dangerous Effects, false success, elapsed time, token/call cost and
  unresolved work. Publish bounded per-case failures, not raw transcripts.
- Compare with a pinned prior source and same fixture/schedule. If the
  baseline cannot be rerun, mark the comparison unavailable rather than
  inventing a percentage.

## Acceptance

- The 20 x 3 manifest, reports, scorer tests and source/model identities are
  reproducible. Negative fixtures fail for the expected reason; no secret or
  live target data appears in generated reports.
- Zero duplicate dangerous Effects and zero false-success claims. Other
  thresholds and any regression are reported without weakening existing
  Runtime or release gates.
- A cancelled turn followed by resume reads the same Run and does not repeat
  the backend action; final-answer confirmation follows actual host completion.
- If model/API access or host infrastructure is unavailable, keep the harness
  and offline corpus but leave live-trial acceptance explicitly unverified.

## Ownership

Own evaluation fixtures, runner, scoring and reports/tests. Do not alter
Runtime authority, credential semantics, or real device state to improve a
score.
