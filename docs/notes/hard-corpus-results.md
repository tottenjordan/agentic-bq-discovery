# The hard corpus — what a harder haystack did and did not change

Run 2026-09-24 against `bigquery_context_hard_tier0..3`, the opt-in 24-table
corpus (`CORPUS_PROFILE=hard`). **600/600 cells, 0 errors.**

**Short version: the approach comparison came alive, the tier comparison did
not.** Enrichment still shows no measurable effect, now on a corpus where
retrieval is genuinely capable of failing.

## Why there is a second corpus at all

On the original 15-table corpus every approach scored 100% recall and every
enrichment delta was `+0.000`. Two measured causes, both checked against the live
project rather than reasoned about:

- **The haystack was too small.** 15 tables with `TOP_K=5` lets a retriever
  return a third of the corpus and still look precise.
- **Tier 0 was never bare.** Its views carry the same hand-written descriptions
  and the same described columns as tier 3 — only the Dataplex side differs. A
  question like *"busiest bike share stations in Austin"* matches the tier-0
  description almost verbatim.

The second of these was **not** changed, deliberately. Descriptions are part of
the schema — BigQuery models them that way, as a constructor argument of
`SchemaField` — so `tier 0 = schema only` is accurate as it stands, and stripping
them would measure a different experiment. See the tier-ladder section below.

## What the survey measured

`survey` profile: every question, every tier, once. 600 cells.

| Approach | Discovery | Final | Rerank loss | p50 | Reranker tokens |
|---|---|---|---|---|---|
| 1 · BQ Tools *(control)* | 100.0% | 99.5% | +0.005 | 22.9 s | 3,576 |
| 2 · KC Search | 96.7% | 94.8% | +0.018 | 5.9 s | 8,378 |
| 3 · KC Context | 100.0% | **93.7%** | **+0.063** | 4.5 s | **72,985** |
| 4 · Pre-Filter | 100.0% | 98.5% | +0.015 | 11.2 s | 11,778 |
| 5 · Semantic | 95.7% | 93.2% | +0.025 | 4.5 s | 16,212 |
| 6 · Search Direct *(control)* | 96.7% | 96.7% | +0.000 | **0.4 s** | **0** |

**`kc_context` is the interesting line.** It finds everything — 100% discovery —
and the reranker then throws 6.3 points away, the largest loss of any approach at
the highest cost per cell. More context in, worse answer out. The original corpus
could not show this, because nothing was ever wrong to begin with.

`search_direct` degrades where a no-reranker control should: nDCG@5 **0.70** on
multi-table-disparate and **0.78** on traps, against ~1.00 elsewhere.

The added tables are doing work — **207 retrievals across 600 cells**:

```
nyc_green_taxi_trips_2022  56    us_states            35    chicago_crime   15
austin_incidents_2016      48    austin_waste         27    chicago_taxi    15
```

## The enrichment response is still flat

| Approach | Δ median (t0→t3) | Δ mean |
|---|---|---|
| all six | +0.000 | −0.020 … +0.020 |

By category, and this is the clearer view:

```
category                 t0     t1     t2     t3
single-table            0.967  1.000  1.000  1.000
multi-table-related     0.938  0.979  0.917  0.938
multi-table-disparate   0.954  0.947  0.921  0.940
trap                    1.000  1.000  1.000  1.000
```

Non-monotonic in both directions, with tier 2 often the *worst* rung. That is
noise, not a trend. **On this evidence Dataplex catalog enrichment does not
measurably improve table discovery over a well-described BigQuery estate** —
upstream's published null result, reproduced on a harder corpus.

## Confirmed at five runs — `hard-full-01`, 3,000/3,000 cells

`survey` is one run and cannot separate a difference from noise. `full` can, and
it tightens rather than overturns:

| Approach | Discovery | Final | Rerank loss | Δ mean (t0→t3) |
|---|---|---|---|---|
| 1 · BQ Tools *(control)* | 100.0% | 99.7% | +0.003 | −0.004 |
| 2 · KC Search | 96.7% | 94.5% | +0.022 | −0.004 |
| 3 · KC Context | 100.0% | **93.6%** | **+0.064** | +0.005 |
| 4 · Pre-Filter | 100.0% | 97.8% | +0.022 | −0.035 |
| 5 · Semantic | 96.7% | 93.7% | +0.030 | −0.009 |
| 6 · Search Direct *(control)* | 96.7% | 96.7% | +0.000 | +0.000 |

**Both findings survive averaging.** `kc_context`'s rerank loss moves 0.063 →
**0.064** across five runs — it is a property of the approach, not a fluke. And
the tier deltas *shrink*: the survey's ±0.020 scatter collapses to −0.009…+0.005
for five of six approaches. Averaging noise moves it toward zero, which is what
noise does.

Pre-Filter's −0.035 is the one figure that did not shrink, and it is negative:
tier 3 is slightly *worse* than tier 0 for that approach. With every other Δ
inside ±0.01 the honest reading is variance, not a real regression.

**The conclusion is therefore not "we could not detect an effect with one run".
It is that there is no effect to detect at this corpus size, with five runs of
evidence.**

## Read these caveats before quoting any of it

- **Five runs, one corpus, one project.** `full` averages per-cell variance but
  changes nothing else: same 24 tables, same 25 questions, same estate.
- **Traps are saturated at 1.000 on every rung.** All four name Austin, and
  descriptions settle them. The nine tables kept cannot pressure them.
- **Still only 24 tables.** Real discovery is hundreds.
- **Seven near-neighbours were deliberately excluded**, and they were the ones
  that would have pressured the twelve questions naming no place. They are
  *legitimate* answers to those questions, not distractors — see
  `AMBIGUOUS_RIVALS` in `tests/test_corpus.py`. Putting them back requires
  labelling them `nice_to_have` first, which is a ground-truth decision, not a
  code change. That is the most promising remaining lever.

## Two bugs this found, both invisible without running it

**Entry-link ids collided across corpora.** Links live in the shared `@bigquery`
entry group and their id embedded the tier but no corpus marker, so
`def-t3-county-fips-us-counties-geo-id` already existed from the baseline. Setup
reported "Link exists", skipped, and left the hard corpus with **zero glossary
links** — `terms=0` at every rung. A quarter of the independent variable, missing,
on a green provisioning run. `RESOURCE_PREFIX` scopes datasets, scan ids and the
glossary; the entry-link namespace was the one place it did not reach. Fixed; the
default prefix stays unscoped so the deployed baseline links remain reachable by
`cleanup.py`.

**`pilot` cannot see the science.** `question_limit=5` takes the first five
questions and all five are `single-table`, so a hard-corpus pilot exercised no
traps, no multi-table questions, and none of the twelve naming no place. It
reported `+0.000` everywhere and that number meant almost nothing. The `survey`
profile exists because of this.

## Reproducing

```bash
export RESOURCE_PREFIX=bigquery_context_hard CORPUS_PROFILE=hard
bq-context ensure-infra --yes          # ~50 min: 96 views, 72 scans, glossary
bq-context preflight --tier 3 --baseline 0
bq-context submit-pipeline --profile survey -e hard-survey-01 --skip-infra
```

Expected ladder — four distinct rungs, `terms=18` at tiers 2-3, fingerprint
`13f9fcb47deb5c32`:

```
tier  tables   bytes  profiled  terms  aspects
0         24  74,406         0      0  --
1         24 196,239       355      0  --
2         24 199,367       355     18  --
3         24 201,339       355     18  overview
```

The baseline corpus is untouched by any of this — 15 tables, fingerprint
`861648cc513c3862`. `setup.py` refuses a profile or a prefix that would modify it.

Related: [[full-run-results]] for the original corpus, [[upstream-experiment]]
for the published null result this reproduces, [[corpus-provisioning]].
