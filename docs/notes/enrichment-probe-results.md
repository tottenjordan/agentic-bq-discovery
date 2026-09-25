# enrich-probe-01/02: a large tier effect, reproducible but not yet explained

`enrich-probe-01` (03:31) and `enrich-probe-02` (13:00), 288/288 cells each,
base corpus, survey profile.

The first run reported the first non-flat enrichment response this project has
seen. I withdrew it, believing the tier-0 baseline had drifted. **That
withdrawal was wrong.** Re-running the whole sweep against a deliberately warmed
index, nine and a half hours later, reproduces the first run *exactly* — 0 of 48
(tier, question) search results differ.

The effect is real and reproducible **inside the pipeline**. What is not yet
explained is why the same code run locally measures a different tier-0 baseline,
which is the subject of the last section. Until that is resolved the magnitude
should not be published, though the direction is no longer in doubt.

## What the run reported

Final recall by tier, meaned over 12 questions:

| approach | tier 0 | tier 1 | tier 2 | tier 3 | Δ mean |
|---|---|---|---|---|---|
| BQ Tools, KC Context, Pre-Filter | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| KC Search, Semantic, Search Direct | 0.292 | 0.625 | 0.625 | 0.875 | +0.583 |

## It reproduces exactly

The warm-then-remeasure cycle: probe every question in every tier until the
convergence guard is quiet, then re-run the whole sweep under a new experiment
id so no cell is resumed.

```
probe-01 (03:31) vs probe-02 (13:00), both pipeline:  0 of 48 results differ
search_direct by tier, both runs:  t0=0.292  t1=0.625  t2=0.625  t3=0.875
```

Not index warm-up. The pipeline sees the same thing nine and a half hours apart,
across two separate image builds and two different commits.

## The discrepancy that is left

The same `run-shard` command, run from a laptop instead of a pipeline task,
measures a different tier-0 baseline — and *that* is stable too:

```
tier0 / search_direct   pipeline:  0.292   (03:31 and 13:00)
tier0 / search_direct   local:     0.625   (03:50 and 14:10)
```

Nine of 48 (tier, question) search results differ between the two, **all of them
at tier 0**. Tiers 1-3 are byte-identical. Examples:

```
tier0/cat-knots     pipeline: [air_quality_annual_summary, austin_crime]  local: [hurricanes]
tier0/cat-gratuity  pipeline: []                                          local: [nyc_taxi_trips_2022]
```

Ruled out so far, each tested directly:

| hypothesis | test | result |
|---|---|---|
| index still warming | two sweeps 9.5h apart | identical — not it |
| caller identity | preflight as ADC vs `--impersonate` the pipeline SA | identical — not it |
| request concurrency | local probe serial vs 8-way | identical — not it |
| question text | cell's recorded `question` vs the file | identical — not it |
| page size / scope | `search_stats` in the cells | both `page_size=20`, same dataset |
| `GOOGLE_CLOUD_LOCATION` | local probe with the container's `global` | identical — not it |

Tier 0 is the tier with the least indexed text, so it is where marginal matches
live and where any difference in retrieval would surface first. That is
consistent with what is seen but does not explain it.

**This must be resolved before the magnitude is quoted.** If the local view is
correct the effect is roughly +0.250; if the pipeline view is correct it is
+0.583. Both are non-zero, so the qualitative finding — enrichment substantially
helps search-based retrieval on questions whose vocabulary is not already in the
descriptions — survives either way.

## Confirmed: only tier 0 moved

Replaying the run's own `search_direct` results against a fresh probe, all 48
(tier, question) pairs:

```
7 of 48 moved -- and every one of them is at tier 0.

tier0/cat-landfall   during: [air_quality_annual_summary, zip_codes]  now: [hurricanes]
tier0/cat-knots      during: [air_quality_annual_summary, austin_crime]  now: [hurricanes]
tier0/cat-gratuity   during: []                                       now: [nyc_taxi_trips_2022]
tier0/cat-checkout   during: [air_quality_annual_summary]             now: [+ hurricanes]
tier0/cat-kiosk      during: [air_quality_annual_summary]             now: [+ citibike_stations]
tier0/ctrl-obvious   during: [2 tables]                               now: [+ citibike_stations]
tier0/ctrl-trap      during: [3 tables]                               now: [+ citibike_stations]
```

Tiers 1, 2 and 3 are byte-identical between the run and now. This is the exact
shape that manufactures a tier effect — the baseline artificially depressed
while every other rung holds still — and it is not a subtle one.

Note that **both controls moved too**, and still scored 1.00 because their
`must_have` was found either way. A control that stays flat does not prove the
index was still; it only proves the set was not uniformly harder.

## The guard that should have adjudicated this, and could not

`assess_search_convergence` probed with one hard-coded question — which was
`single-q1`, asked thousands of times across every prior run and therefore
maximally warm. It read `tier0=3` before and after, and would have said the same
during any of these runs.

It has been fixed regardless, because the reasoning stands even though warming
turned out not to be the cause here: a warm fixed probe cannot certify a cold
novel question set. `_probe_search_labels` now asks every question in every tier
and compares the table *identities* returned. Preflight prints something a reader
can act on:

```
search found/tier  tier0=11/12  tier1=11/12  tier2=11/12  tier3=12/12
```

Note what that number is and is not: how many questions return *anything*, not
how many return the right thing. It was 11/12 at tier 0 in every probe, local and
impersonated alike, while recall at tier 0 differed by a factor of two — so this
line is a warm-up indicator, not a recall estimate.

`convergence_from_cells` was separately found to be incapable of firing at all:
it built one observation and the guard needs two. It now splits each tier's cells
by write time. It reported a clean bill on every report ever rendered, including
`full-01`'s.

## What survives

Two things look robust, because neither depends on the tier-0 baseline.

**The population splits in two, mechanically.** Approaches that read table
context — `bq_tools` lists schemas, `kc_context` and `context_prefilter` pull the
`lookupContext` capsule — sit at 1.000 at *every* tier and never move. A language
model handed the descriptions resolves *landfall* → `hurricanes` unaided. Only
search-dependent approaches can show a tier effect at all, and the previous 25
questions never stressed them.

**`bq_tools` at 1.000 on tier 0 defeats the design guard.**
`tests/test_enrichment_questions.py` verifies no question shares a content word
with its target's tier-0 text. None does — and a model reading the schema still
answers every one. The guard rules out *lexical* findability; nothing rules out
semantic reasoning. Worth stating as a property of the set rather than patching.

Everything else in the first version of this note — the tier-by-tier attribution,
"tier 2 contributes nothing", the claim that profiling and guidelines carry the
gain — rested on the unstable tier-0 number and has been withdrawn pending a
re-measurement.

## What to do before measuring again

1. **Warm the question set.** Issue every question against every tier once and
   throw the results away, then measure. Cheap: `search_direct` at
   `--limit 0 --runs 1` is 48 cells and no reranker tokens.
2. **Re-measure tier 0 twice, separated in time,** and only proceed if they
   agree. This is `assess_search_convergence`'s job done with the *real*
   queries rather than a fixed probe.
3. **Then** run `survey`, and only then `full`.

Running `full` on the current evidence would spend five runs' worth of cells
confirming an artefact with more decimal places.

## Reproducing the discrepancy

```bash
# what the run measured
uv run bq-context score -e enrich-probe-01

# the same shard, re-run
uv run bq-context run-shard -e enrich-recheck --tier 0 --approach search_direct \
    --runs 1 --questions experiments/questions-enrichment.json
```

Corpus `861648cc513c3862`, questions `23d7d17fad7ce484`, code `6927987`.

## Related

- [full-01](full-run-results.md) — the original index-warming confound, which
  this is a variant of.
- [Why the tier response is flat](enrichment-dependent-questions.md) — the design
  of this question set.
