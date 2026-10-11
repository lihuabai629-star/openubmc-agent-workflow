# Local configuration page selection

The local configuration launcher accepts a requested page kind: `targets`,
`conan`, or `kb`. Its loopback `/api/state` response must preserve that kind in
`page_session.kind`, including when the launcher supplies a discovered target
credential source. The selected kind determines the initial page and its
configuration-readiness result; source discovery only changes the backing store
for that source.

The 2.1.2 installed package can open the targets page after a KB request. The
server constructor reuses `kind` as the iteration variable for source overrides,
so the last override changes the requested page. The 2.1.3 draft inherited the
same behavior.

The public regression seam is the loopback `/api/state` response from a server
started with `kind="kb"` and an existing `targets` source override. It must
report `page_session.kind="kb"`. The existing `targets` and `conan` selections
must retain their behavior. This change has no effect on stored credentials or
configuration activation.
