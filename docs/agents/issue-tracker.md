# Issue tracker: GitHub

Issues and specs for this repository live in GitHub Issues under
`lihuabai629-star/openubmc-agent-workflow`.

The checkout also has a self-hosted GitLab remote. GitLab remains a code remote,
but it is not the canonical issue tracker. Do not duplicate agent-ready issues
across GitHub and GitLab.

Use the `gh` CLI for issue-tracker operations. Because the GitHub remote is named
`github` rather than `origin`, pass `--repo lihuabai629-star/openubmc-agent-workflow`
explicitly to every `gh issue`, `gh pr`, and repository-scoped `gh api` command.

## Conventions

- **Create an issue**: `gh issue create --repo lihuabai629-star/openubmc-agent-workflow --title "..." --body-file <path>`.
- **Read an issue**: `gh issue view <number> --repo lihuabai629-star/openubmc-agent-workflow --comments`.
- **List issues**: use `gh issue list --repo lihuabai629-star/openubmc-agent-workflow` with the required state and label filters.
- **Comment on an issue**: `gh issue comment <number> --repo lihuabai629-star/openubmc-agent-workflow --body "..."`.
- **Apply or remove labels**: use `gh issue edit <number> --repo lihuabai629-star/openubmc-agent-workflow --add-label "..."` or `--remove-label "..."`.
- **Close an issue**: `gh issue close <number> --repo lihuabai629-star/openubmc-agent-workflow --comment "..."`.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and pull requests. Resolve an
ambiguous number by checking the pull request first and then the issue, always
with the explicit `--repo` selector.

## When a skill says "publish to the issue tracker"

Create a GitHub issue in `lihuabai629-star/openubmc-agent-workflow`.

## When a skill says "fetch the relevant ticket"

Read the corresponding GitHub issue and its comments.

## Wayfinding and ticket dependencies

- Use one GitHub issue as the map or parent specification.
- Use GitHub sub-issues when the repository supports them; otherwise maintain a
  task list in the parent and add `Part of #<number>` to each child.
- Use GitHub native issue dependencies when available. Otherwise place a
  `Blocked by: #<number>` line at the top of the child issue.
- A ticket is ready for implementation only when all blockers are closed and it
  carries the `ready-for-agent` label.
