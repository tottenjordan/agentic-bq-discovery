# What `lookupContext` actually returns

Investigated 2026-09-22 to settle whether glossary enrichment reaches the
capsule the agents read. **It does.** An earlier note in this repo said it did
not; that was wrong, and this note supersedes it.

## Glossary definitions arrive per-column, under `terms`

Not as a top-level `related_terms` object. On the column itself:

```json
{
  "name": "subscriber_type",
  "type": "STRING",
  "description": "Type of the Subscriber",
  "nullRatio": 0.18,
  "sampleValues": ["Annual", "Local365", "Walk Up", "..."],
  "terms": "Subscriber Type; Rider membership category. Distinguishes
            annual/monthly members from single-ride and walk-up casual users."
}
```

The value is the term's **display name plus its description** — not the term id.
Searching a capsule for `related_terms`, `glossar`, or the id `subscriber-type`
finds nothing, which is exactly how this was missed the first time.

Observed capsule keys, for reference:

| level | keys |
|---|---|
| top | `resources`, `relatedResources` |
| resource | `resource`, `catalogEntry`, `simpleName`, `type`, `description`, `schema`, `ancestors`, `bigqueryView`, `createTime`, `updateTime`, and `overview` when a tier-3 aspect is attached |
| column | `name`, `type`, `description`, `mode`, `nullRatio`, `distinctValues`, `sampleValues`, **`terms`** |

## The default capsule truncates the schema to 25 columns

This is the part that matters. `hurricanes` has 153 columns and 5 glossary
links. By default the capsule returns 25 columns and **zero** terms — the
annotated columns are simply past the cut.

`all_schema_fields=true` fixes it:

| option | columns | terms surfaced |
|---|---|---|
| *(default)* | 25 of 153 | 0 |
| `all_schema_fields=true` | 153 | all 5 |
| `context_budget=100000` | 25 | 0 |
| `budget=100000` | 25 | 0 |

So of the two options upstream flagged as documented-but-unverified,
**`all_schema_fields` works and the budget keys do nothing** — neither
`context_budget` nor `budget` changed the response at all. That resolves the
ambiguity upstream recorded in `util_lookup_context.py`.

## Cost of enabling it

Measured across the whole corpus, per tier:

| tier | default | `all_schema_fields=true` | ratio | terms default | terms all |
|---|---|---|---|---|---|
| 0 | 50,677 | 89,538 | 1.8× | 0 | 0 |
| 1 | 112,678 | 196,823 | 1.7× | 0 | 0 |
| 2 | 168,021 | 293,007 | 1.7× | 18 | **24** |
| 3 | 166,333 | 291,305 | 1.8× | 18 | **24** |

Roughly **1.8× the capsule**, and therefore ~1.8× the reranker prompt tokens for
the three context-consuming approaches. `kc_context` ships the whole corpus
capsule per cell, so it feels this most.

## Which tables actually lose links

Re-verified live on 2026-09-22 at tier 3. The loss is not spread thin — it is
two tables, and one of them loses everything:

| table | capsule cols | links wanted | reach capsule | with `all_schema_fields` |
|---|---|---|---|---|
| `hurricanes` | 25 *(of 153)* | 5 | **0** | 5 |
| `air_quality_annual_summary` | 25 | 2 | **1** | 2 |
| the other 7 linked tables | 10–20 | 17 | 17 | 17 |
| **total** | | **24** | **18** | **24** |

`hurricanes` contributes **zero** glossary signal to tiers 2 and 3 while
appearing fully enriched in every count taken from the catalog rather than the
capsule. Any table whose schema exceeds 25 columns is in the same position, so
this scales with corpus width, not corpus size.

## The open decision

Turning it on is **not** an obvious win, because it trades one confound for a
cost:

- **Leave it off** to match upstream's exact conditions. Their code omits the
  option, so this is what a faithful reproduction runs. Cost: 6 of 24 term
  links never reach the reranker, and the truncation lands unevenly — it only
  bites wide tables, so it silently weakens the enrichment variable on exactly
  the tables where glossary help would matter most.
- **Turn it on** for a sound experiment. Enrichment is the independent
  variable, and truncating it is a confound. Cost: ~1.8× tokens on the context
  approaches, which also makes our cost figures non-comparable to upstream's.

Reasonable split: run the reproduction with it **off** (comparable to upstream),
and any extension with it **on**. That way the reproduction stays honest and the
extension stays sound.

## Provisioning is complete — verified 2026-09-22

Checked against the live catalog, not the setup log:

| resource | expected | actual |
|---|---|---|
| tier datasets / views | 4 / 60 | 4 / 60 |
| DataScans | 45 | **45/45** |
| glossary | 1 | present |
| glossary terms | 11 | **11/11** |
| definition entry links | 48 (24 × tiers 2–3) | 48 requested |
| table-level aspects at tier 3 | 4 | **4/4** |

The tier-3 aspect covers only **4 of 15 tables** — `austin_bikeshare_trips`,
`nyc_taxi_trips_2022`, `county_natality`, `air_quality_annual_summary`. That is
upstream's design, not a gap: `GUIDELINES` is defined for those four. Spot-
checking any other tier-3 table finds no aspect and is not evidence of a
problem.

`guidelines` vs `overview` re-confirmed the same day: `dataplex.aspectTypes.get`
still 403s on Google's `dataplex-types` project while `overview` reads fine, so
the fallback remains correct and is not an IAM gap to chase.

## Why this was missed

The preflight gate compared **byte deltas** and looked only at top-level capsule
keys. Real glossary enrichment across 15 tables is ~1.6 KB — below the 4 KB
threshold it used to dismiss noise — and lives per-column, so nothing it
inspected could see it. It reported a fully-enriched tier 2 as a dead factor
level for several hours.

The gate now compares an enrichment **signature** — `(aspects, profiled columns,
term-annotated columns)` — and ignores byte counts entirely, because bytes
mislead in both directions: dataset timestamps differ when nothing else does,
and real enrichment can be small.

```
tier   tables     bytes  profiled  terms  aspects
0          15    49,089         0      0  —
1          15   118,275       209      0  —
2          15   119,882       209     18  —
3          15   122,346       209     18  overview
```

Every rung adds something. No warning.

Related: [[corpus-provisioning]], [[dataplex-catalog-gotchas]].
