# The test suite: shape, deliberate gaps, and how to extend it

347 tests, ~5 s, no GCP. `tests/conftest.py` pins a fake environment for every
test, so nothing can pass by inheriting a real `GOOGLE_CLOUD_PROJECT`.

## Coverage is 59%, and the number is close to honest

`upstream_build_results.py` is excluded in `[tool.coverage.run]`. It is vendored
from upstream as the reference for the metrics port and **deliberately never
imported**, so its 427 statements could never be covered and were costing 8
points of signal. Excluding it is why the number moved 45% → 59% without any
test being written.

What remains uncovered is mostly code that cannot run without GCP or an LLM:

| Module | Cov | Why |
|---|---|---|
| `corpus/setup.py` | 20% | 837 lines of provisioning; only the pure id/ladder logic is testable |
| `runner/cells.py` | 41% | the ADK execution path needs a live corpus |
| `reranker/util_rerank.py` | 24% | one Gemini call |
| `context_cache/*` | 18–32% | Dataplex reads |
| `approaches/*/callback_*` | 24–38% | the six discovery implementations |
| `agent_orchestrator/*` | 0% | the interactive demo agent; not on the experiment path |

Chasing these with mocks would mostly assert that the mocks were configured
correctly. The gap that *would* pay is `approaches/*/callback_*`: six near-
parallel implementations where a copy-paste divergence is plausible and silent.

## Test every seam, not just both sides of it

The highest-value tests here cover joins between modules, because each module's
own tests stay green while the join breaks:

- **`tests/test_pipeline_cli_seam.py`** runs each pipeline component's
  undecorated function with `subprocess.run` stubbed, captures the argv it
  actually builds, and checks every flag against the real Click command objects.
  Before this, components and the CLI were each well tested and the only thing
  joining them was an argv string — rename a CLI option and both stayed green
  while the pipeline failed 40 minutes in. Two real runs failed on this seam.
- **`tests/test_corpus.py`** runs `cleanup.delete_*` against stubbed Dataplex
  clients and asserts the ids it targets equal exactly what `setup` would
  create. This replaced an assertion that the two modules *import* the same
  constant, which a mutation proved was too weak.
- **`tests/test_notebook.py`** asserts every `from bq_context... import X` in
  the notebook still resolves. Nothing imports a notebook, so it rots silently.

## Mutation-test anything that looks like a guard

Three separate tests in this repo's history passed while asserting nothing:
two matched explanatory *comments* rather than code, and one checked module
imports rather than function behaviour. The fix each time was found by breaking
the source on purpose and confirming the test failed.

Do that before trusting a new guard. It takes one minute:

```bash
cp src/path/mod.py /tmp/bak
# break the thing the test claims to protect
uv run pytest tests/test_that.py -q      # must FAIL
cp /tmp/bak src/path/mod.py
```

Note that a mutation which changes no observable behaviour *should* pass —
`for tier in list(PROFILED_TIERS)` is a copy, not a defect. A test that fails on
that is over-fitted to the implementation.

## Prefer introspection to scraping

`--help` output is formatted to terminal width, so a CI runner wraps it
differently from a dev shell; a test grepping rendered help failed only in CI.
Introspect Click command objects (`typer.main.get_command(app).commands`)
instead. Same rule for the pipeline: assert on the compiled KFP spec or the
built argv, not on module source text.

The one legitimate source-text test left is the PEP 563 guard in
`tests/test_pipeline.py` — `from __future__ import annotations` in a module KFP
introspects is invisible any other way, and it matches the statement rather than
the comment explaining its absence.

## Known sharp edges pinned by tests

- `_bounded_id` collapses punctuation with no disambiguation below 63
  characters, so `a_b`, `a-b` and `a.b` are one id. Safe today only because no
  corpus pair differs that way; the uniqueness tests are what would catch it.
- The longest real resource id is **62 of 63** characters. A tripwire test fails
  when ids reach the cap, because that is when `_bounded_id`'s hash-suffix
  branch runs against this project for the first time.
- `_parse_ranked` returns `[]` on unparseable reranker output, which in the
  results is indistinguishable from a genuine empty search. A test asserts the
  warning is emitted, since that log line is the only way to tell them apart
  after the fact — see [[full-run-results]].

Related: [[kfp-pipeline]], [[corpus-provisioning]], [[full-run-results]].
