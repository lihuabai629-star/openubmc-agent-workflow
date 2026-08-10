# Downstream Source Handoff

Read this reference only when the user's current goal includes a stage after
source implementation. Developer reports verified source state; each downstream
Skill owns its operational inputs, mutation gates, and evidence.

If the user requests only a handoff, do not load a downstream Skill merely to
repeat its workflow. If the user requests execution, continue with the named
owner after the source stage completes.

## Pass only source facts

Provide:

- accepted behavior and the absolute source or component root;
- files and components changed in the current task, not every dirty file;
- whether authored inputs require generation and the observed generation state;
- component-local compilation, tests, validators, and their results;
- unresolved source gaps or unavailable generators;
- current workspace path, relevant workspace state, and retention intent;
- the user's requested next stage or sequence when one was stated.

Include Git identity when it already exists and matters, but do not create a
worktree solely for handoff. Preserve an isolated workspace when downstream work
still needs it. Do not invent product, target, artifact, or authorization facts.

## Preserve stage ownership

- `openubmc-build` consumes changed components and generation state, then owns
  build type, stage, manifest or board discovery, package creation, and verified
  artifact identity.
- `openubmc-live-patch` owns local-to-remote mapping, target, checksum, backup,
  restart scope, host-key policy, runtime checks, and rollback.
- `openubmc-upgrade` consumes an already-built HPM from Build or the user, then
  owns target, artifact verification, authorization, activation, and rollback.
- `openubmc-publish` consumes an already-built Conan package, then owns the exact
  recipe revision, remote, authorization, upload, and remote verification.

For a requested source-to-upgrade sequence, hand source facts to Build; Build
produces the HPM identity consumed by Upgrade. Developer never claims that source
completion produced a package or firmware artifact.
