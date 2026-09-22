# full-01 — the first complete 3,000-cell run

Run 2026-09-22. **3,000/3,000 cells, 0 missing.** The approach comparison is
sound. **The tier comparison is not** — read the confound section before quoting
any enrichment number from this run.

## Headline: the approach comparison

| Approach | Discovery recall | Final recall | Rerank loss | p50 | Reranker tokens | Precision | nDCG@5 |
|---|---|---|---|---|---|---|---|
| 1 · BQ Tools *(control)* | 100.0% | 99.2% | +0.008 | 21.0 s | 3,396 | 100% | 1.000 |
| 2 · KC Search | 77.2%\* | 74.6%\* | +0.026 | 4.1 s | 6,396 | 100% | 1.000 |
| 3 · KC Context | 100.0% | 94.0% | +0.060 | 3.4 s | 43,095 | 100% | 1.000 |
| 4 · Pre-Filter | 100.0% | 96.3% | +0.037 | 9.4 s | 10,331 | 100% | 1.000 |
| 5 · Semantic | 77.2%\* | 74.5%\* | +0.027 | 3.4 s | 11,754 | 100% | 1.000 |
| 6 · Search Direct *(control)* | 77.2%\* | 77.2%\* | +0.000 | **0.4 s** | **0** | **40%** | **0.821** |

\* Depressed by the index confound below. Measured against a converged index,
all three search approaches reach **0.967** discovery recall — which matches
upstream's published figure exactly.

**The reranker buys precision, not recall.** `search_direct` is free and
instant, and matches the rerankers on recall, but scores 40% precision against
100% and nDCG@5 0.821 against 1.000. Its nDCG collapses exactly where it should:
0.67 on multi-table-disparate and 0.78 on trap questions, versus 1.00 for every
reranked approach. This reproduces upstream's central finding, more sharply.

**`bq_tools` is the latency outlier**, as upstream found: 21 s p50 against
0.4–9.4 s for everything else, because it is the only genuine LLM tool loop.
**`kc_context` is the token outlier** at 43k per cell — it ships the entire
corpus capsule to the reranker.

## The confound: Dataplex search index warm-up

The run reported a large enrichment effect on the three search-based approaches:

```
discovery recall by tier   tier0   tier1   tier2   tier3
kc_search / semantic /     0.520   0.680   0.967   0.920
search_direct
raw search hits/question    2.88    3.92    4.64    4.96
```

Read naively, that is the experiment's headline question answered: catalog
enrichment improves retrieval by +0.40. **It is an artifact.**

Re-running every tier hours later, with nothing changed:

| tier | recall during run | recall re-run later | hits during | hits later |
|---|---|---|---|---|
| 0 | 0.520 | **0.967** | 2.88 | 4.64 |
| 1 | 0.680 | **0.967** | 3.92 | 4.64 |
| 2 | 0.967 | **0.967** | 4.64 | 4.64 |
| 3 | 0.920 | **0.967** | 4.96 | 5.52 |

All four tiers are identical once the index settles. **Shards run in plan
order — tier 0 first, tier 3 last — so tier was confounded with elapsed time.**
The Dataplex semantic index was still converging when the early shards ran, and
"enrichment" was measuring nothing but the clock.

The three full-corpus approaches (`bq_tools`, `kc_context`,
`context_prefilter`) are unaffected: they perform no retrieval, and score 1.000
discovery at every tier.

### What is and is not usable from this run

| Result | Status |
|---|---|
| Approach comparison at tiers 2–3 | ✅ valid |
| Reranker value (precision, nDCG) | ✅ valid |
| Latency and token cost per approach | ✅ valid |
| `bq_tools` / `kc_context` / `context_prefilter` at all tiers | ✅ valid, no retrieval step |
| **Tier response for the three search approaches** | ❌ **invalid** |

### A second, independent fingerprint

Audited separately, the same confound shows up as **cells where semantic search
returned nothing at all**:

| approach | tier0 | tier1 | tier2 | tier3 |
|---|---|---|---|---|
| `kc_search` / `semantic_context` / `search_direct` | **10** | **5** | 0 | 0 |

Fifteen questions got zero hits, every one of them at the two tiers queried
earliest in the run, none at the two queried last. Those cells score recall 0
and, for `kc_search`, make no reranker call at all — `store_empty()` firing
correctly, not a bug, but it does mean `reranker_calls` has a legitimate `min=0`
for that approach.

Two independent measures (hit counts and zero-hit cells) pointing the same way
is what moves this from "suspicious" to established.

### The guard

`bq-context preflight` now probes one fixed question against every tier and
compares raw hit counts. The corpus is identical across tiers, so a converged
index returns the same count everywhere; a rising count is the warm-up
signature. It warns when the spread exceeds 15% *and* the absolute gap is at
least 2 hits — counts are small (3–6), so ±1 is normal noise and would
otherwise cry wolf.

Replaying this run's counts (3, 4, 5, 5) trips it. The current converged state
(3, 3, 3, 4) does not. See `assess_search_convergence` in `cli.py`.

### A better fix, not yet done

The guard detects the confound; it does not remove it. **Shuffling the shard
plan so tier does not correlate with execution order** would make the design
robust rather than merely monitored. Worth doing before the next scoring sweep —
a residual warm-up drift too small to trip the guard would still bias tier
systematically as long as tier 0 always runs first.

## Provenance and caveats from the resume

**The dataset is built from two code versions.** 2,993 cells were produced by
`5de0dde` and 7 by `28a7b2c`. The only difference between them is the ADK retry
configuration, which changes nothing about agent behaviour in the absence of a
429, so the results are comparable — but the mixture is real and should be
stated rather than discovered later.

**The retry fix was not exercised by the resume.** No genuine 429 occurred in
the resumed run; concurrency was far lower, with only 7 cells of real work
spread across 24 shards. The fix is unit-tested and in place, but it has not yet
been proven under the load that produced the original failures.

**Resuming after a code change costs a full shard-startup sweep.** The KFP cache
key includes the image, so changing it invalidated all 24 shards: every one
started a VM, and 19 of them found nothing to do and exited. That is correct —
a cache that ignored the image would silently return results from old code — but
it is roughly 54 machine-minutes of no-op startup to re-run 7 cells. Worth
knowing before treating resume-after-a-fix as free.

**The re-run cells are not anomalous.** All 7 scored recall 1.00, at or slightly
above their same-(tier, approach) peers, with latency in range. All 7 are
`bq_tools` or `context_prefilter` — full-corpus approaches that perform no
retrieval — so they are immune to the search-index confound above and did not
contaminate the tier axis further.

## Integrity

Audited after the resume:

```
expected keys 3000 · got 3000 · missing 0 · unexpected 0 · duplicates 0
all status ok: True · all 24 (tier, approach) groups: exactly 125 cells
search_direct reranker calls: 0 everywhere (by design)
```

## Reliability

Seven of 3,000 cells failed on the first attempt, all `429 RESOURCE_EXHAUSTED`,
all on `bq_tools` or `context_prefilter` — the only two approaches reaching
ADK's own Gemini client, which had no retry configured. Fixed, then resumed
under the same `experiment_id` to reach 3,000/3,000. See
[[local-smoke-results]] for the related ADK environment gap.

Related: [[upstream-experiment]], [[lookup-context-capsule]], [[pipeline-runs]].
