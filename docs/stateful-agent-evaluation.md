# Stateful Agent evaluation (#285)

`evaluation/stateful-agent/scenarios.v1.json` contains 20 versioned, sanitized
scenario prompts. `scripts/stateful_agent_evaluation.py` pins three independent
trial identities per scenario, dispatches them through an explicit Host adapter,
and scores persisted Runtime and Host evidence. The fixture target is the
documentation address `192.0.2.10`; an adapter must use the Runtime fake backend.
No live BMC or credential material is part of this corpus.

## Evidence boundary

The scorer reads `case_events`, `task_bindings`, and `evidence_index` from the
Runtime SQLite ledger in read-only mode. It derives Outcome and Gate state with
the Runtime projector, replays a redacted `CaseReplayBundle`, counts Effect
identities and Gate submissions, and checks the Runtime closeout's verified
delivery stage. A terminal result also needs a matching `TerminalAnswerStore`
record and a completed Codex Host final event. A prepared answer, mismatched
task, interrupted turn, or more advanced delivery claim is a failure. Reports
contain bounded issue codes and counts, without raw prompts, events, receipts,
transcripts, target addresses, or credentials.
The three positive diagnosis and replay scenarios also require a completed
Runtime Outcome; a failed final answer cannot satisfy their task expectation.

The checked-in offline suite uses the existing Runtime fake backend for a
diagnosis, a deadline-interrupted caller followed by resume of the same Run,
Gate replay, source Gate, and missing-credential path. The interrupted caller
probe checks that the fake backend is invoked once; only a real Host trial can
verify cancellation and completion events. Its
other negative cases deliberately mutate *copies* of Runtime facts to test the
scorer. The degraded-service, shell-fallback, build/upgrade, and unknown-Effect
prompts are represented in the manifest, but the offline proxy does not execute
those full task paths. An offline pass establishes scorer behavior only; it is
never counted as an Agent trial.

## Reproduce the offline result

From the repository root, with Python able to import the bundled Runtime:

```sh
python scripts/stateful_agent_evaluation.py plan \
  --source-ref HEAD --baseline-ref 772de46 \
  --model gpt-6-sol --reasoning-effort max \
  --client-version codex-cli/0.144.6 \
  --output evaluation/stateful-agent/trial-plan.json
python scripts/stateful_agent_evaluation.py offline \
  --plan evaluation/stateful-agent/trial-plan.json \
  --output evaluation/stateful-agent/offline-report.json
python -m unittest scripts.tests.test_stateful_agent_evaluation
```

The plan binds the manifest digest, candidate and prior source commits, model,
reasoning effort, client version, 20 prompt digests, seed, and shuffled 60-slot
schedule. Each slot has its own task ID. `validate_plan` recomputes the schedule
and plan digest before any scoring.

## Actual Agent trials

An executable Host adapter can be passed to `run-trials` from a checkout of the
plan's exact `source_commit`. The committed plan and reports were added in the
following evidence commit, so keep a copy of `trial-plan.json` outside that
source checkout and pass its absolute path. The runner invokes the adapter once per slot, with
the path to a `request.json` argument. It measures elapsed time and discards
adapter stdout and stderr. The request contains the prompt, pinned identities,
fixture target, and an output directory. The adapter must return success only
after an independent Agent turn and write these files in that directory:

- `runtime.sqlite`: the fake Runtime's persisted Run ledger, with the task ID
  bound to the produced Run ID;
- `terminal.json`: `TerminalAnswerStore` record for a terminal Outcome;
- `rollout.jsonl`: native Codex Host events including `session_meta`, a final
  assistant message, and `task_complete` for the same turn;
- `trial.json`: `scenario_id`, `scenario_version`, `trial`, `task_id`, `run_id`,
  `backend: "runtime-fake"`, `plan_digest`, and `identity` with the plan's
  `source_commit`, `model`, `client_version`, `reasoning_effort`,
  `prompt_digest`, and `schedule_digest`.

The adapter must configure its model and client from the request, isolate every
task, and never use a real target or log credentials. Model/client identity in
`trial.json` is an adapter attestation; the scorer can check its binding and
the Host/Runtime results but cannot independently prove which upstream model
served a request. A Host integration should retain its own authenticated model
metadata for an external audit. The local Codex CLI JSON stream alone is not
the native rollout format expected by the final-answer audit.

```sh
PLAN_PATH=/absolute/path/to/trial-plan.json
# Run the following from a checkout at the plan's source_commit.
python scripts/stateful_agent_evaluation.py run-trials \
  --plan "$PLAN_PATH" \
  --adapter /absolute/path/to/trusted-host-adapter \
  --trial-root /absolute/path/to/isolated-trials \
  --output /absolute/path/to/dispatch.json
python scripts/stateful_agent_evaluation.py summarize-live \
  --plan "$PLAN_PATH" \
  --trial-root /absolute/path/to/isolated-trials \
  --output /absolute/path/to/live-report.json
```

`summarize-live` recomputes all 60 rows from raw artifacts. Missing, invalid, or
interrupted Host evidence stays `unverified`. It reports zero-tolerance counts
for duplicate dangerous Effects and false success, per-case bounded failures,
unresolved work, elapsed p95, token totals when usage is complete, and tool
calls. Monetary cost is not calculated without a pinned price schedule. A
prior-source comparison remains `unavailable` until the prior commit is run
with the same manifest, model, client, prompt digests, and schedule; no
percentage is inferred from the offline fixtures.

## Current result

The committed offline report is a deterministic scorer result. It records
20/20 expected fixture verdicts and **0/60 actual Agent trials**. Live
acceptance and the prior-source comparison are explicitly unverified. The
repository's CLI is present, but no compatible fake-Runtime Host adapter was
configured, so authenticated model access through the full path was not
verified. The Runtime and release
gates are unchanged.
