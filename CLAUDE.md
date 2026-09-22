# CLAUDE.md

## Code standards

**Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code and
making environment changes.** It is the authority for this project's tooling,
dependency management, testing, and commit conventions. Read it before adding
dependencies, changing CI, or committing.

Key points it enforces (see the doc for the full set):

- Never add `Co-Authored-By` trailers to commits or PRs.
- Python package management is `uv` only — never bare `pip` or `python`.
- `ruff` for lint and format; `pytest` for tests; `ty` for type checking.

## Session notes

Durable findings from working sessions live in [docs/notes/](./docs/notes/),
indexed by [docs/notes/README.md](./docs/notes/README.md). One topic per file;
update existing notes rather than creating duplicates.
