# Semantic search answers different principals differently

Found 2026-09-25 while chasing why `enrich-probe-01/02` measured tier-0
`search_direct` recall at 0.292 in the pipeline and 0.625 on a laptop.

**Dataplex `search_entries` with `semantic_search=True` returns different tables
depending on who asks.** The same query, sent at the same moment and scoped to
the same dataset, gets one answer for the developer and another for
`bq-context-pipeline`. Every pipeline number is what the SA sees and every local
re-check is what the developer sees, so a local re-run can neither confirm nor
refute a pipeline result.

## The evidence

Searching *as the pipeline SA from the laptop* reproduces the pipeline exactly.
Region, image, and container have nothing to do with it:

```
enrichment set, tier 0, 12 questions
  pipeline cells vs laptop-as-SA:   12/12 identical
  pipeline cells vs laptop-as-ADC:   3/12 identical
```

Agreement between ADC and the SA, same query, same minute:

| question set | tier 0 | tier 1 | tier 2 | tier 3 |
|---|---|---|---|---|
| enrichment (12) | 3/12 — 0.625 vs 0.292 | 12/12 | 12/12 | 12/12 |
| shipped (25) | 1/25 — 0.967 vs 0.620 | 7/25 — 0.967 vs 0.893 | 25/25 | 15/25 — 0.967 vs 0.920 |

Recall is `search_direct`, ADC first, then the SA.

Which principals see which view:

| principal | created | matches |
|---|---|---|
| developer (`admin@…`, ADC) | long-standing | complete view |
| `934903580331-compute@developer` | long-standing | ADC, 12/12 |
| `notebooksa` | long-standing | ADC, 12/12 |
| `bq-context-pipeline` | 2026-09-22 | degraded view |
| a fresh probe SA, same roles as the pipeline SA | 2026-09-25 | the pipeline SA, 12/12 |
| the same probe SA, granted **Owner** for 45+ minutes | — | still the pipeline SA, 12/12 |

The split follows the **age of the principal**, not its grants. IAM on the
tier-0 and tier-1 tables and datasets is identical for all of them, and Owner
changed nothing.

## What re-indexing does and does not fix

Each of these was tried on the three tables the SA could not find even by
keyword (`hurricanes`, `citibike_stations`, `nyc_taxi_trips_2022`), all of which
had kept 09-22 entry-update times while their neighbours were patched on 09-23:

| action | keyword view (SA) | semantic view (SA) | semantic view (old principals) |
|---|---|---|---|
| add a label, forcing an entry update | healed in ~4 min | **not healed** after 35 min | re-indexed within ~20 min |
| edit the table description | healed | **not healed** after 16 min — not even when the query *is* the new description | — |

So the entry *is* re-indexed. Keyword search sees it, and old principals see the
new text through semantic search. New principals still get `[]` or wrong tables
through semantic search. Neither re-indexing nor role grants help, and nothing
on our side does.

All changes were reverted: the labels were cleared, the description was restored
byte-for-byte, and the probe SA and its bindings were deleted. The three tables
now carry 09-25 entry-update times with identical content.

This looks like Dataplex-side behaviour and is worth reporting to Google, with
the probe-SA experiment as the minimal reproduction.

## What it means for results already reported

- **`full-01`'s "index warming" diagnosis is wrong.** The "re-run later" that
  made every tier 0.967 was a local `run-shard` as ADC, not a pipeline re-run.
  It compared the SA's view with the developer's. The SA's tier-2 and tier-3
  numbers during the run (0.967, 0.920) match what the SA sees today, so
  nothing was converging. See [full-01](full-run-results.md).
- **The `enrich-probe` magnitude depends on the principal.** +0.583 is what
  the SA measures and +0.250 is what the developer measures. Both are real, and
  neither is "the" answer. See [enrich-probe](enrichment-probe-results.md).
- The earlier conclusion that caller identity was ruled out was a false
  negative. `preflight --impersonate` threaded the SA's credentials to the table
  cache but not to the search, so the check meant to compare identities
  searched as the developer both times.

## The guard

`search_entries_scoped` takes `credentials`, and `_live_search` forwards them.
With `--impersonate`, preflight now probes search twice, once as the SA and once
as you, and warns when they disagree:

```
WARN  semantic search returns different tables to bq-context-pipeline@… than to
      you for 9 of 48 (tier, question) pairs: tier0/cat-knots, … The pipeline
      measures what bq-context-pipeline@… sees, so a local re-run will not
      reproduce its numbers.
```

Tests: `tests/test_search_identity.py`, plus the preflight wiring tests in
`tests/test_cli.py`. All three links (client, bound search, preflight) are
mutation-tested. The middle one survived the first round of tests.

### Every result says who measured it

The check above catches the gap before a run. The results record it
afterwards, so a mixed experiment cannot pass unnoticed:

| where | what |
|---|---|
| each cell, and the BigQuery `principal` column | the account whose search it measured |
| each shard summary | the account that ran the shard |
| `experiment.json` | the first principal. A later shard run as someone else **warns**, just as a changed corpus does |
| `merged/missing.json`, run `manifest.json` → `measured_by` | ok cells per principal. `merge` **warns** when there is more than one |

User ADC has no email attribute, so the developer is identified through
Google's tokeninfo endpoint. Without that, a developer's cells recorded `""`.
An unknown principal never warns, so the one mix that matters would have been
invisible. `""` still means unknown (cells from before the field existed, or a
lookup that failed), and it is never counted as a second principal.

## Reproducing

```bash
# the gap, as the pipeline would see it
uv run bq-context preflight --tier 3 --impersonate bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com \
    --questions experiments/questions-enrichment.json

# a local run that measures what the pipeline measures
gcloud auth application-default login \
    --impersonate-service-account bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com
```

## Choosing whose view to measure

The two options are to run the pipeline as a principal with the complete view,
or to accept the SA's view and label results with it. Either way, compare only
numbers measured by the same principal, and run the guard above before quoting a
tier effect.
