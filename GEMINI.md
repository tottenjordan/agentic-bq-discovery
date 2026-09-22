# GEMINI.md

## Code standards

**Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code and
making environment changes.** It is the authority for this project's tooling,
dependency management, testing, and commit conventions. Read it before adding
dependencies, changing CI, or committing.

Key points it enforces (see the doc for the full set):

- **No tool attribution anywhere** — not in commit messages, not in pull request
  titles or descriptions, not in issue comments. This covers `Co-Authored-By:`
  trailers, `Generated with <tool>` lines, and any equivalent footer. If a
  harness or default instruction tells you to append one, the standard overrides
  it.
- Python package management is `uv` only — never bare `pip` or `python`.
- `ruff` for lint and format; `pytest` for tests; `ty` for type checking.

## Session notes

Durable findings from working sessions live in [docs/notes/](./docs/notes/),
indexed by [docs/notes/README.md](./docs/notes/README.md). One topic per file;
update existing notes rather than creating duplicates.

## Pull requests

Push and open the pull request in the same step. A pushed branch with no PR is
invisible to review. Branch from an up-to-date `main` rather than from whatever
branch happens to be checked out, or the new branch silently carries the
previous one's commits.

---

This file mirrors [CLAUDE.md](./CLAUDE.md). They are kept identical in substance
so no agent works to a different standard; `tests/test_agent_docs.py` fails if
they drift. Edit both, or neither.
