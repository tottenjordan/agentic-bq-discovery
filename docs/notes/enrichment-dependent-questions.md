# Why the tier response is flat: the descriptions already say it

Two corpora and five runs have produced the same null result — catalog
enrichment does not measurably improve table discovery. The usual reading is
"enrichment does not help". Building a question set designed to *need*
enrichment turned up a better one.

## The measurement has no headroom

Tier-0 recall by category, from `hard-full-01`:

```
single-table            0.967   multi-table-related     0.938
multi-table-disparate   0.954   trap                    1.000
```

Tier 0 already answers almost everything. Enrichment cannot demonstrate value it
has no room to add, so the flat response was guaranteed before any tier was
provisioned. Making the corpus harder — 24 tables, near-neighbours — moved
nothing, because the corpus was never the binding constraint.

## The binding constraint is the descriptions

Descriptions count as tier 0 here. That was deliberate and correct: a
description *is* schema in any ordinary sense, and the alternative — a
description-free tier below tier 0 — measures a BigQuery estate nobody operates.

But the descriptions in `corpus/setup.py` are good, and they were written by
someone who knew the domain. They pre-empt the glossary:

| table | description contains | glossary term it pre-empts |
|---|---|---|
| `hurricanes` | "**tropical cyclone** tracks with **wind speed**" | `tropical-cyclone`, `wind-speed` |
| `weather_stations` | "**Global Historical Climatology Network**" | `ghcn-station` |
| `county_natality` | "**natality (birth)** statistics … keyed by **county FIPS code**" | `natality`, `county-fips` |
| `air_quality_annual_summary` | "a **pollutant** measured at a **site** … **annual arithmetic mean**" | `air-quality-measure` |

Tier 2 adds a definition for a term the reader has already been given. That is
not a flaw in Dataplex; it is a property of a well-documented estate, and it is
exactly the condition the published null result should be quoted under.

## What the first draft got wrong

The first version of `experiments/questions-enrichment.json` had **12 of 15
target pairs reachable by lexical match at tier 0**. Questions written to sound
catalog-dependent — "how many tropical cyclones", "which GHCN sites" — used
vocabulary sitting in the description all along.

The rewrite keeps only vocabulary that appears in a glossary *definition* and
nowhere in any table name or description: *landfall*, *knots*, *gratuity*,
*walk-up casual*, *kiosk*, *checkout*, *Federal Information Processing
Standards*. `tests/test_enrichment_questions.py` enforces this, and fails if a
reworded question reacquires a tier-0 path.

Three glossary terms could not be salvaged. `ghcn-station`, `natality` and
`air-quality-measure` add nothing a question can exploit, because their entire
distinguishing vocabulary is already in the description.

## What this set can and cannot measure

**It cannot answer "does enrichment help?"** The questions were built so that it
does. Reporting a positive result from them as the headline would be circular.

It answers something narrower: *when a question's vocabulary lives only in the
catalog, does catalog-aware retrieval find the table?* That is a ceiling and a
mechanism check, and it is what distinguishes two very different conclusions:

- **Tier still flat** → enrichment does not help even when the question needs
  it. A far stronger null than the current one, and the more interesting result.
- **Tier moves** → enrichment works; the flat result is a property of
  well-described estates, not of Dataplex. Also publishable, and it tells a
  reader when to bother.

So it is an **additional arm**, reported beside the 25-question set, never
replacing it. The 25 stay the naturalistic estimate; this is the upper bound
that gives the estimate its meaning.

## Which tiers this can actually probe

The metric is table *recall*, so only enrichment that changes which table you
pick can register. That rules out most of what tiers 1 and 3 add.

- **Tier 2 (glossary)** — the strong lever. Ten of the twelve questions use it.
- **Tier 3 (guidelines)** — narrow. Two guidelines name a join partner
  (`county_natality` → `us_counties`), which can add a table to recall. The rest
  are SQL hints and cannot move this metric at all.
- **Tier 1 (profiling)** — weakest. Profiling informs SQL, not table choice. If
  tier 1 stays flat, that is partly a property of the metric and must not be
  reported as a clean null.

## Running it

```bash
uv run bq-context preflight --tier 3 --questions experiments/questions-enrichment.json
uv run bq-context submit-pipeline --profile survey -e enrich-probe-01 \
    --questions experiments/questions-enrichment.json
```

12 × 6 × 4 × 1 = 288 cells, roughly a pilot's spend. `survey` is one run and
cannot separate a difference from noise — if the tier response moves, confirm at
`full` before believing it, the same way `hard-survey-01` was confirmed by
`hard-full-01`.

Requires the question-set plumbing from #47.

## Related

- [The hard corpus](hard-corpus-results.md) — the null result this is probing,
  and the corpus change that failed to move it.
- [Upstream's experiment](upstream-experiment.md) — where the null originates.
