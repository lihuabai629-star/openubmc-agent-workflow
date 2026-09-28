# Sanitized replay evaluation

`evaluation/sanitized-replays` contains deterministic, secret-free fixtures: one known-good offline package analysis and one negative case for each evaluation dimension. `scripts/sanitized_replay.py` reports routing, host, evidence lineage, delivery-stage calibration, release gates, credential containment, convergence cost, recovery/rollback, version consistency, and terminal-answer dimensions.

Negative fixtures declare the expected failed verdict, so a correctly detected failure passes the suite. Each failed dimension includes expected behavior, observed behavior, and a bounded evidence boundary. Repeated equivalent actions exceed the fixture budget unless the evidence digest changed and the retry is explicitly justified. The known-good offline package-analysis fixture preserves the distinction between package evidence and runtime success.

The lineage scorer also checks observation time: an exact `observed_at` binding
or an inclusive `observation_interval` with timezone-aware `start` and `end`
prevents evidence from another collection window satisfying a fixture. A
repeated command needs both changed evidence and an explicit justified-retry
flag; merely setting the flag cannot excuse an identical action. Secret-shaped
fixture input is rejected before a report can echo its values. These are
offline evidence-semantics checks, not proof that a model followed the path.

Fixtures with a `probe` derive the selected observation from the current public
build router, execution router, or terminal-delivery gate. Their stored
`observed` value is ignored for that dimension. The remaining fixtures are
static evidence-semantics examples; a green static comparison by itself does
not qualify the current plugin execution path.
Each live probe also pins an independent `expected_observation`, so a
different wrong result cannot make a negative case pass merely by disagreeing
with the scenario's ideal behavior.
