# CONTRIBUTING — persona instance

This instance treats the operative constitution like code: diffable, versioned,
reviewed. Constitutional changes follow a deliberate flow.

## Branches

- **`main`** — stable, contains the current operative constitution.
- **`feat/§N-<topic>`** — feature branches for non-trivial constitutional
  additions (a new section, a policy change, a refactor). Merged via PR
  review + explicit principal approval.

## Commit Message Format

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types:

- `chore:` — setup, tooling, maintenance.
- `policy:` — operative policy change or constitution section update (§1+).
- `memory:` — L1/L2 memory structure or entry updates.
- `docs:` — documentation.
- `fix:` — bug fixes or clarifications to existing rules.

## Tags

Version tags `vX.Y` mark constitution releases:

- **Major (X):** breaking changes to IMMUTABILE sections (trust model, modes,
  boundaries).
- **Minor (Y):** new sections or significant policy additions.

## Policy changes

Constitution updates require:

1. Proposed in conversation or email (principal → agent).
2. Draft on a feature branch (if complex).
3. Explicit principal approval (per `RULES.md` enactment marker).
4. Committed with `policy:` type and a reference to the approval.
5. Tagged if a version bump is warranted.

## What NOT to commit

- `.env` (credentials, API keys).
- `state/` (transcripts, drafts, gateway logs).
- `memory/index.sqlite` (FTS index, auto-generated).
- `heartbeat/state/`, `voice/tmp/` (runtime output).
