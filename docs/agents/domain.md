# Domain Docs

This repository uses a single-context domain documentation layout.

## Before exploring

Read the following sources when they exist:

- `CONTEXT.md` at the repository root for the ubiquitous language.
- `docs/adr/` for architecture decisions relevant to the area being changed.

If either source is absent, continue silently. Domain documentation is created
when terminology or a hard-to-reverse decision is actually resolved.

## Layout

```text
/
├── CONTEXT.md
├── docs/adr/
└── docs/agents/
```

## Vocabulary

Use terms exactly as defined in `CONTEXT.md` in issue titles, specifications,
tests, refactor proposals, and implementation notes. Do not introduce synonyms
for concepts that the glossary distinguishes.

If a required concept has no stable name, treat that as a domain-modeling gap
rather than silently inventing a competing term.

## Architecture decisions

Read applicable ADRs before changing a module, interface, implementation, seam,
adapter, or ownership boundary. If a proposal contradicts an ADR, identify the
conflict explicitly and either preserve the ADR or supersede it with a new
decision record.
