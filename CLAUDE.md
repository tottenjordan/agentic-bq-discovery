# CLAUDE.md

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

## What this repo is

An experiment comparing **six strategies for finding the right BigQuery tables**
from a natural-language question — the retrieval step before NL2SQL. The
factorial is 6 approaches x 4 catalog-enrichment tiers x 25 questions x 5 runs =
3,000 cells, run locally a shard at a time or as a Vertex AI Pipeline.

`src/bq_context/cli.py` is the single entrypoint. **Every pipeline component is
a thin wrapper over a CLI subcommand**, so anything that fails in the pipeline
reproduces locally with one command. Keep it that way: logic belongs in the CLI
or below it, never in a component.

## Commands

```bash
make install        # uv sync --all-groups
make check          # lint + types + tests, what CI runs
make format         # ruff format + safe fixes
uv run bq-context --help          # 14 subcommands
```

Never run bare `python` or `pytest`; go through `uv run`.

## What will bite you

Each of these cost real time here. All verified against the current tree.

- **Gemini lives at `global`, not the compute region.** The models 404 in
  `us-central1` and 200 at `global`. `Locations` in `config.py` holds all seven
  locations precisely so this is not re-derived per call site.
- **No `from __future__ import annotations` in `pipeline/components.py` or
  `pipeline/dag.py`.** KFP introspects annotations at runtime, and PEP 563 turns
  them into strings — it fails with "Artifacts must have both a schema_title and
  a schema_version", which points nowhere near the cause. Both files carry a
  comment saying so; a test asserts the *statement* is absent, not the comment.
- **Never commit compiled pipeline YAML.** Compile at the point of use.
- **`lookupContext` returns an empty response rather than 403** when permissions
  are missing. An under-permissioned run therefore looks like a null result
  instead of an error. Run `bq-context preflight --tier 3` before trusting any
  tier number.
- **Semantic search answers each principal differently.** The pipeline SA sees a
  degraded index that the developer does not, so a local re-run will not reproduce
  a pipeline number. Compare only numbers measured by the same principal, and run
  `preflight --impersonate <sa>`, which warns on the gap. See
  [docs/notes/search-depends-on-identity.md](./docs/notes/search-depends-on-identity.md).
- **`corpus/setup.py` is vendored from upstream** (see `NOTICE`) and kept
  re-syncable — the diff is ~58 lines, nearly all formatting. Put new code
  beside it, not in it; `corpus/bucket.py` is the pattern.
- **Tests are hermetic.** `tests/conftest.py` pins a fake environment for every
  test, so nothing may depend on real credentials or an ambient
  `GOOGLE_CLOUD_PROJECT`.
- **Mutation-test any new guard.** Three tests here once passed while asserting
  nothing. Break the source on purpose and confirm the test fails; see
  [docs/notes/test-suite.md](./docs/notes/test-suite.md).

## Session notes

Durable findings from working sessions live in [docs/notes/](./docs/notes/),
indexed by [docs/notes/README.md](./docs/notes/README.md). One topic per file;
update existing notes rather than creating duplicates. Read the index before
debugging anything infrastructural — most sharp edges here are already written
down.

A note records what was true when written. **Re-verify any file, flag, or
command it names before acting on it.**

## Pull requests

Push and open the pull request in the same step. A pushed branch with no PR is
invisible to review. Branch from an up-to-date `main` rather than from whatever
branch happens to be checked out, or the new branch silently carries the
previous one's commits.

---

This file mirrors [GEMINI.md](./GEMINI.md). They are kept identical in substance
so no agent works to a different standard; `tests/test_agent_docs.py` fails if
they drift. Edit both, or neither.
