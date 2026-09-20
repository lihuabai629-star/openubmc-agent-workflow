# Sanitized replay evaluation

`evaluation/sanitized-replays` contains deterministic, secret-free positive and negative session fixtures. `scripts/sanitized_replay.py` reports routing, host, evidence lineage, delivery-stage calibration, release gates, credential containment, convergence cost, recovery/rollback, version consistency, and terminal-answer dimensions.

Negative fixtures declare the expected failed verdict, so a correctly detected failure passes the suite. Repeated equivalent actions exceed the fixture budget unless the evidence digest changed and the retry is explicitly justified. The known-good offline package-analysis fixture preserves the distinction between package evidence and runtime success.
