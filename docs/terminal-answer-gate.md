# Terminal answer gate

A terminal Run is not delivered until a final answer record is durably bound to the same task, Run, Outcome fingerprint, status, and delivery stage. The record contains only user-visible text and terminal facts. It is written atomically, and replaying delivery returns the existing record instead of sending a second mutation or changing the text.

Qualification fails closed for a missing, empty, interrupted, or mismatched answer. Recovery reads the durable record and renders it once. Completed, partial, failed, cancelled, and blocked statuses include the same delivery stage used by Runtime and a next action when work remains.
