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

Verified end to end on 2026-09-25 with two smoke pipelines at `169eff1`, one as
the pipeline SA and one as the compute SA (`--service-account`):

- **Every level names the right account.** That covers the BigQuery
  `principal` column (18/18 in each run), `experiment.json`, `missing.json` and
  the manifest's `measured_by`. So the metadata-server fallback resolves the
  `"default"` alias inside a real Vertex task, not just in tests.
- **Each pipeline run reproduces a local run as the same principal**, 3/3 each.
  The comparison was run with a laptop impersonating the SA, and a laptop as
  ADC for the compute SA.
- **Adding a local ADC shard to the SA's experiment triggers both warnings**:
  one from `run-shard` and one from `merge`.
- **The compute SA can run the whole pipeline.** It passed validate-config and
  preflight and produced all 18 cells. Running as a complete-view principal is
  therefore a working option, at the cost of that account's broad roles.

Both experiments were deleted afterwards.

## Watching for a heal

The open question is whether a new principal's semantic view improves with age.
The pipeline SA was three days old when this was found. A Cloud Run job answers
it once a day, independent of any laptop or session:

```bash
make identity-watch        # deploy: Cloud Run job + daily Cloud Scheduler trigger
make identity-watch-logs   # every result so far; a shrinking "N of 48" is a heal
make identity-watch-down   # remove the job, the schedule, and the grant
```

**The job's own identity is the baseline.** Preflight compares the impersonated
pipeline SA with whoever runs the job, so the job runs as the default compute
SA. That account matched the developer 12/12. A freshly created SA would
likely share the pipeline SA's degraded view, and the two would agree. The
check would then report a heal that never happened.

First data points, enrichment set, same 48 pairs:

| when (UTC) | from | differing pairs |
|---|---|---|
| 2026-09-25 morning | laptop | 9 of 48 |
| 2026-09-25 18:00 | the job (compute SA baseline) | 6 of 48 |
| 2026-09-25 18:02 | laptop (ADC baseline) | 6 of 48, same pairs |

The two baselines agree, so the job measures what the laptop does. The drop
from 9 to 6 is real but unexplained. The SA is older, and the afternoon's label
and description re-indexing may have caught up late. Tier 0 is the only tier
that differs.

The shipped set (25 questions, `search_direct`, laptop as ADC vs laptop as the
SA) moved the same way on tier 0 only:

| when (UTC) | tier 0 | tier 1 | tier 2 | tier 3 | SA's tier 0 → 3 gain, mean recall |
|---|---|---|---|---|---|
| 2026-09-25 morning | 1/25 identical | 7/25 | 25/25 | 15/25 | +0.347 |
| 2026-09-25 20:45 | 9/25 identical | 7/25 | 25/25 | 15/25 | +0.173 |

ADC's own gain is +0.000 at both times. Tier 0 converging while tiers 1 and 3
stay put fits neither "the SA ages into the full view" nor "nothing changes"
cleanly, which is why the daily job keeps watching.

The only grant it adds is `serviceAccountTokenCreator` for the compute SA,
on the pipeline SA only, and `down` removes it. `tests/test_identity_watch.py`
keeps the script in step with the CLI: its flags, its log query, and the config
it forwards.

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
