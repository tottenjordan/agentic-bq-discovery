# First live runs

Task 8, **2026-09-22**, against `hybrid-vertex` with real Gemini, BigQuery, and
Knowledge Catalog calls. Everything below is measured, not estimated.

## Measured per-cell cost, vs upstream

| approach | our p50 | upstream p50 | our tokens | upstream median |
|---|---|---|---|---|
| `bq_tools` | 38.4 s | 39.5 s | 3,165 | 3,470 |
| `semantic_context` | 5.1 s | 6.1 s | 10,162 | 14,909 |
| `search_direct` | 1.6 s | 2.1 s | 0 | 0 |

Close enough on a handful of cells to suggest the reproduction is tracking. The
qualitative result reproduces too: `search_direct` scored 50% precision against
100% for the reranked approaches, which is upstream's central finding that the
reranker buys precision rather than recall.

## Cache warm is cheap — the sharding question is settled

**4.2–4.4 s** to build a tier's context cache (15 tables via batched
`lookupContext`). The plan flagged "we warm 12× instead of 4×" as the thing that
might force coarser sharding, with ~2 minutes as the threshold of concern. At
4 s, the total extra cost across 12 cache-using shards is under a minute.

**Shard by `(tier, approach)` as planned.** No reason to revisit.

## Scoping holds despite 254 datasets in the project

Every search cell reported `out_of_scope_dropped: 0`. The
`parent:datasets/{ds}` predicate is doing its job, and the client-side filter
never had to catch anything. See [[dataplex-catalog-gotchas]] for why that
predicate is fragile.

## The bug only a live run could find

The first `bq_tools` cell failed with:

```
ValueError: No API key was provided.
```

**Cause:** ADK builds its *own* genai client for an LLM-driven agent, from
`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, and
`GOOGLE_CLOUD_LOCATION`. Upstream set these as an import-time side effect in
`config.py`; the Task 3 refactor removed that side effect without replacing it,
so ADK fell back to the Gemini Developer API and asked for an API key.

**Why nothing caught it:** four of the six approaches never reach the agent LLM
— they short-circuit in a `before_agent_callback`. Only `bq_tools` and
`context_prefilter` do. Every unit test, every import check, and four of six
live approaches pass with the setting absent.

**Fix:** `ExperimentConfig.configure_adk_env()`, called by `execute_shard` —
the single path through which any agent runs. Two regression tests.

Worth noting the planned Dockerfile already sets these as `ENV`, so the
container would have worked while local runs did not. A discrepancy like that
is much harder to diagnose than a plain failure.

## Durability and resume, verified live

- **Rolling upload fires mid-run.** Observed 12 cells durable in GCS while the
  shard was still executing cell 17 of 20.
- **Hard `SIGKILL` mid-run loses only the un-uploaded tail.** Killed a 20-cell
  shard at 80 s: `attempt-0001.jsonl` held 12 cells and no `_SUCCESS` marker.
- **Resume picks up exactly the remainder.** Re-running reported
  `20 planned, 12 already done, 8 to run` and wrote `attempt-0002.jsonl`,
  leaving the first attempt untouched.
- **Merge dedupes across attempts.** 28 records across two attempts → 20 unique
  cells, 0 missing.
- **Error cells self-heal.** The failed `bq_tools` cell in `attempt-0001` was
  re-run on the next invocation and superseded by the `ok` record; merge kept
  only the success (7 records → 6 unique).
- **Heartbeat works.** `11/20 cells (0 failed) · 60s elapsed · eta 49s`.

## Gotcha for anyone repeating this

Killing a shard mid-run is harder than it looks: `uv run` spawns a child, so
`pgrep`/`kill` on the obvious pid misses the worker, and a bare `pgrep -f`
pattern matches the checking shell itself. Use `setsid` and kill the process
group. Three attempts failed to land before that.

Also: below ~60 s a shard uploads nothing until it finishes, so a short run
killed early loses everything. That is correct and bounded behaviour, not a bug
— but it means a resume test needs a run long enough for the rolling upload to
fire.

Related: [[corpus-provisioning]], [[gemini-endpoints-and-quota]].
