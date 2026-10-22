# Target Runtime handoff

When `workflow.next` or `workflow.advance` returns
`status: waiting_phase_record` with
`required_phase_type: documentation.review`, it also provides:

~~~json
{
  "required_skill": "openubmc-doc-review",
  "handoff_arguments": {
    "case_id": "case-id",
    "profile": "quick|full|release",
    "reviewed_paths": ["docs/example.md"],
    "source_revision": "revision",
    "phase_record_contract": {
      "case_id": "case-id",
      "expected_revision": 1,
      "idempotency_key": "cycle:step:result",
      "phase_type": "documentation.review",
      "producer_identity": "openubmc-doc-review"
    }
  }
}
~~~

Use those values directly. The phase result adds:

~~~yaml
status: completed | failed | cancelled
source_revision: <reviewed source revision>
summary: <bounded review outcome>
profile: quick | full | release
verdict: pass | pass_with_warnings | fail | incomplete
reviewed_paths: [<path>]
report_path: <optional local report path>
finding_counts:
  critical: 0
  major: 0
  minor: 0
blockers: []
unverified_claims: []
command_validation: not_requested | planned | completed | unavailable
known_gaps: []
evidence_ids: []
~~~

Use `status=completed` only with `pass` or `pass_with_warnings`. Use
`status=failed` for `fail` or `incomplete`, because those verdicts are
workflow gates rather than successful downstream handoffs.

After recording a terminal phase, call `workflow.next` again. A failed phase
requires a new `workflow.advance` attempt after the blocker is addressed; do not
resubmit a different verdict against the same terminal attempt.
