# The upstream experiment: what it found and what that implies

Source: `statmike/vertex-ai-mlops`, `Applied ML/AI Agents/bigquery-context`, Apache-2.0.
The full 47KB methodology writeup is vendored verbatim at `docs/upstream/readme.md`; the
published results table is at `experiments/upstream_baseline/results.md`. This note is the
distilled, decision-relevant part — the things that would otherwise stay buried.

## The headline result is a null result

Across 3,000 cells (6 approaches × 4 tiers × 25 questions × 5 runs), **enrichment tier response
was completely flat: +0% for every approach.** Medians saturate at 100% recall on the 15-table
corpus.

Two things we have since established about that number, both of which weaken it further:

1. **It is computed with a median**, on a metric that saturates at 1.0 — so it is structurally
   unable to move. The same cells aggregated by mean *do* move (see `tier_response(aggregate=)`).
2. **At least one tier is not a real factor level.** On our own provisioning run, glossary
   enrichment never reached the context capsule, making tier 2 byte-equivalent to tier 1 — see
   [[corpus-provisioning]]. If the same happened upstream, their tier-2 arm contributed nothing
   by construction.

That is a ceiling effect, not a finding about catalog enrichment. The corpus is too easy to
discriminate between strategies. Two implications:

1. Any extension of this work needs a larger or harder corpus to get signal.
2. A flat tier response is *also* what a silently-broken catalog looks like — see the empty
   `lookupContext` failure in [[dataplex-catalog-gotchas]]. We cannot tell these apart without
   asserting enrichment is real, independently of the scores.

The other real finding: semantic search alone, properly scoped, already retrieves nearly all
correct tables before reranking. The reranker buys **precision, not recall** — `search_direct`
(no rerank) hit 0.967 final recall with zero rerank loss, but trailed badly on nDCG@5 for
disparate multi-table (0.70) and trap (0.78) questions versus 1.00 for reranked approaches.

## Measured cost, which drives our sharding

Per approach, 500 cells each. Total 11.35 h agent wall-clock (~12.5 h with delays), 38.7M
reranker tokens, 0 errors.

| approach | wall-clock | s/cell | reranker tokens |
|---|---|---|---|
| bq_tools | 5.99 h | 43.1 | 1.8M |
| context_prefilter | 2.34 h | 16.9 | 5.1M |
| kc_search | 1.24 h | 8.9 | 5.5M |
| semantic_context | 0.90 h | 6.5 | 7.4M |
| kc_context | 0.59 h | 4.2 | 19.0M |
| search_direct | 0.30 h | 2.2 | 0 |

`bq_tools` is **53% of runtime** (the only real LLM tool loop); `kc_context` is **49% of tokens**
(it ships the full capsule for all 15 tables to the reranker). Any shard plan has to account for
this 20:1 spread — balanced sharding is not the same as equal-sized shards.

**Only reranker tokens are instrumented.** Agent-side ADK LLM tokens — notably `bq_tools`' tool
loop and `context_prefilter`'s nomination LLM — are not measured anywhere. Upstream's footnote
that reranker tokens "are the dominant model cost" is true for four of six approaches, not all.

## Why their harness is hard-serial

Three module globals, all of which we remove:

| global | why it blocks parallelism |
|---|---|
| `config.SCOPE` / `ACTIVE_TIER` | flipped per tier; scoring matches on *short* table name and all four tier datasets hold identically-named tables, so a run must see exactly one tier |
| `context_cache._CACHE` | rebuilt per tier by `repopulate_for_tier` |
| `reranker.util_rerank._USAGE_LOG` | per-cell token accounting, exact only because runs are sequential |

A consequence that is easy to miss: **any prompt embedding scope or cached metadata must be
rebuilt per request, not frozen at import.** That is why upstream's approaches 1 and 4 use ADK's
*InstructionProvider callable* form for `instruction=`. `agent_search_direct/prompts.py` computes
its dataset list at import time — harmless upstream only because that prompt is dead code on the
happy path (its callback never returns `None`).

## Other upstream defects worth knowing

- `make report` references `examples/build_report.py`, which **does not exist** (only
  `build_results.py`). Broken target.
- `make test` exists but there is **no `tests/` directory**.
- `save_results` rewrites the whole 5.4MB JSON after every cell, non-atomically (no temp+rename).
  An interrupt mid-write truncates the file.
- **No retry or backoff anywhere.** The only 429 mitigation is a 1.0s inter-cell sleep plus
  re-running with `--resume`.
- `--resume` does not validate that the existing file's metadata matches current config, and the
  resuming process overwrites the stored header — so resuming across a config change silently
  mixes cells under one set of metadata.
- `final_recall` is not truncated to `RERANK_K`; it relies on the reranker honoring `top_k=5`.
- `precision`'s denominator is the deduped `set`, so duplicate `table_id`s would inflate it.

Related: [[gemini-endpoints-and-quota]] for the models these numbers were produced with.
