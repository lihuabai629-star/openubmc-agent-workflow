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

### One native pilot

`scripts/stateful_agent_host_adapter.py` supports only the positive
`diagnosis-complete` scenario. It starts a target-free fake Runtime MCP server
with a fresh SQLite ledger, launches the pinned Codex CLI against a configured
Responses provider, and copies the native rollout without rewriting events.
The Runtime task ID comes from the pinned schedule. Native MCP `threadId`
metadata and the rollout session ID must agree before the scorer accepts a
Host final. The adapter prepares the terminal record after the Runtime Outcome,
then asks the same Codex session for a final answer in a second turn. It
acknowledges only the persisted final event after `task_complete`.

Use a clean checkout at the plan's source commit. `run-one` selects exactly one
slot; it does not invoke the 60-slot dispatcher. Keep trial artifacts outside
the checkout. Set `OPENUBMC_EVAL_BASE_URL` to the provider's `/v1` base URL.
Remote providers require HTTPS; plain HTTP is accepted only for
`localhost`, `127.0.0.1`, or `::1`.
On macOS the adapter reads service `codex.user-api.cliproxy` from Keychain into
adapter memory; elsewhere supply `OPENUBMC_EVAL_API_KEY` through the adapter's
process environment. A loopback Responses relay adds the credential upstream,
while native Codex sees only a fresh random local marker. The relay rejects
requests without that marker before forwarding. The adapter disables
shell snapshots and checks that the CLI accepts that setting before invocation.
It scans trial files for the actual credential after each turn and removes any
matching file before failing the attempt.

```sh
python scripts/stateful_agent_evaluation.py plan \
  --source-ref HEAD --baseline-ref 772de46 \
  --model gpt-6-sol --reasoning-effort max \
  --client-version codex-cli/0.144.6 \
  --output /absolute/private/trial-plan.json
python scripts/stateful_agent_evaluation.py run-one \
  --plan /absolute/private/trial-plan.json \
  --adapter "$PWD/scripts/stateful_agent_host_adapter.py" \
  --scenario-id diagnosis-complete --trial 1 \
  --trial-root /absolute/private/trials \
  --output /absolute/private/dispatch.json
```

The shown client version is the attempted identity and currently fails closed
before another model invocation. Rebuild the plan with a client version that
can execute this MCP call while retaining a read-only sandbox.

Score the selected slot using the standard `score-live` command and the measured
`elapsed_seconds` from its `timing.json`. A trial counts only when the Runtime
task binding, persisted terminal Outcome, native MCP session link, matching
prepared answer, native final event and completed Host turn all verify. This
pilot does not implement interruption, fault injection, the other 19 scenario
behaviors or a baseline rerun.

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
pilot adapter covers only `diagnosis-complete`; the Runtime and release gates
are unchanged.

The single-slot native pilot on 2026-09-27 used Codex CLI 0.144.6, the
configured Responses provider, `approval_policy=never`, and `--sandbox
read-only`. The model called `runtime_fake.execute` twice, but the native Host
reported `user cancelled MCP tool call` before either call reached the fake
Runtime server. There was no persisted task-to-Run binding or terminal Outcome.
The provider returned model text, but this is **0 verified Agent trials**.
Codex CLI help does not expose a scoped read-only approval option for this
write-type MCP call. The adapter and scorer are retained for further Host
integration; the 60-slot run remains unattempted. The loopback credential
relay and snapshot guard were added after this observation and have not been
validated by another model turn. The adapter refuses Codex CLI 0.144.6 before
invocation under this known limitation.
