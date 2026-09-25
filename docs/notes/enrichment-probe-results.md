# The tier response is not flat — it was hidden by the questions

`enrich-probe-01`, 288/288 cells, base corpus, survey profile (one run).

Every previous run reported a flat enrichment response. This one does not. The
difference is not the corpus, the code or the models — it is the questions.

## The result

Final recall by tier, meaned over 12 questions:

| approach | tier 0 | tier 1 | tier 2 | tier 3 | |
|---|---|---|---|---|---|
| BQ Tools *(control)* | 1.000 | 1.000 | 1.000 | 1.000 | flat |
| KC Context *(context)* | 1.000 | 1.000 | 1.000 | 1.000 | flat |
| Pre-Filter *(context)* | 1.000 | 1.000 | 1.000 | 1.000 | flat |
| **KC Search** | **0.292** | 0.625 | 0.625 | **0.875** | **+0.583** |
| **Semantic** | **0.292** | 0.625 | 0.625 | **0.875** | **+0.583** |
| **Search Direct** *(control)* | **0.292** | 0.625 | 0.625 | **0.875** | **+0.583** |

Δ median t0→t3 is **+1.000** for all three search approaches, against +0.000 for
everything measured before this.

## What it means

The population splits cleanly in two, and the split is mechanical.

**Approaches that read table context are unaffected** — they were at 1.000 from
tier 0. `bq_tools` lists schemas directly and never touches the catalog;
`kc_context` and `context_prefilter` pull the `lookupContext` capsule. All three
hand a language model the table descriptions and let it reason. It resolves
*landfall* → `hurricanes` and *gratuity* → `nyc_taxi_trips_2022` with no
enrichment at all.

**Approaches that rely on catalog search are transformed.** `kc_search`,
`semantic_context` and `search_direct` all depend on Dataplex `search_entries`
matching the question against indexed text. At tier 0 there is nothing to match,
and recall is 0.292. By tier 3 it is 0.875.

So the honest statement of the original null result is narrower than it looked:
**catalog enrichment does not improve approaches that already read the table
context, because those are at ceiling. It substantially improves search-based
retrieval — which the previous 25 questions never stressed, because their
vocabulary was already in the descriptions the search index carries.**

## Where the gain actually comes from — and it is not the glossary

| step | Δ | what that tier adds |
|---|---|---|
| t0 → t1 | **+0.333** | data profiling |
| t1 → t2 | **+0.000** | glossary terms + entry links |
| t2 → t3 | **+0.250** | guidelines aspect |

**Tier 2 contributes nothing.** That is the surprise, since the question set was
built specifically around glossary-definition vocabulary. The glossary is
attached by entry links to columns, and on this evidence Dataplex search does
not surface linked term definitions in a way that helps `search_entries` match.

The two tiers that do move it are the ones that add *free text to the entry*:
profiling attaches column statistics, and the guidelines aspect attaches prose.
Per question, the tier-3 jumps are exactly where a guideline says the missing
word — `cat-walkup` (0.50 → 1.00 at t3) is answered by
`austin_bikeshare_trips`'s guideline "Filter **casual** vs. member ridership on
'subscriber_type'", and `cat-fips-join` by `county_natality`'s "Join to
us_counties on County_of_Residence_FIPS".

This wants confirming directly before it is quoted — see open questions.

## Per question

| question | t0 | t1 | t2 | t3 | |
|---|---|---|---|---|---|
| `cat-depression` | 0.50 | 1.00 | 1.00 | 1.00 | profiling |
| `cat-gratuity` | 0.50 | 1.00 | 1.00 | 1.00 | profiling |
| `cat-knots` | 0.50 | 1.00 | 1.00 | 1.00 | profiling |
| `cat-landfall` | 0.50 | 1.00 | 1.00 | 1.00 | profiling |
| `cat-walkup` | 0.50 | 0.50 | 0.50 | 1.00 | guidelines |
| `cat-fips-join` | 0.50 | 0.50 | 0.50 | 1.00 | guidelines |
| `cat-kiosk-join` | 0.50 | 0.50 | 0.50 | 0.75 | guidelines, partly |
| `cat-concentration` | 0.75 | 0.75 | 0.75 | 1.00 | guidelines |
| `cat-kiosk` | 0.50 | 0.50 | 0.50 | 0.50 | **never resolves** |
| `ctrl-obvious` | 1.00 | 1.00 | 1.00 | 1.00 | control, as designed |
| `ctrl-trap` | 1.00 | 1.00 | 1.00 | 1.00 | control, as designed |

0.50 is the signature of the split: the three context approaches find the table,
the three search approaches do not. 1.00 means all six do.

**The controls stayed flat at 1.00**, which is what makes the rest readable. A
set that was simply harder everywhere would have dragged them down too.

## Read these before quoting any of it

**One run.** `survey` is `runs=1`. It cannot separate a difference from noise.
The effect is large — 0.292 to 0.875 — and perfectly consistent across three
independent approaches, which is reassuring but is not a replication. Confirm at
`full` before publishing, exactly as `hard-survey-01` was confirmed by
`hard-full-01`.

**The questions were built so that enrichment could matter.** This measures a
ceiling, not the value of enrichment on naturally-arising questions. The
25-question result remains the naturalistic estimate; this is the upper bound
that gives it meaning. Report both or neither.

**`bq_tools` scoring 1.000 at tier 0 defeats the design guard.**
`tests/test_enrichment_questions.py` checks that no question shares a content
word with its target's tier-0 text. It does — and a language model reading the
schema still resolves every one of them semantically. The guard is necessary and
not sufficient: it rules out *lexical* findability, and nothing rules out an LLM
being good at its job. That is a property of the questions worth stating rather
than fixing.

**Twelve questions.** Each is worth 0.083 of the mean, so a single question
flipping moves a tier by eight points.

## Open questions

1. **Why does tier 1 help?** Profiling should inform SQL, not table discovery.
   The likely mechanism is that a DataScan attaches text to the catalog entry
   that `search_entries` then indexes — which would make it an artefact of
   *having more indexed text*, not of the statistics being useful. Testable by
   diffing what `search_entries` returns at t0 and t1 for one query.
2. **Why does tier 2 not help?** If linked glossary terms are genuinely invisible
   to search, that is worth reporting on its own — it is the single most
   commonly assumed benefit of a business glossary.
3. **Why does `cat-kiosk` never resolve?** The only question no tier fixes.

## Reproducing

```bash
uv run bq-context preflight --tier 3 --questions experiments/questions-enrichment.json
uv run bq-context submit-pipeline --profile survey -e enrich-probe-01 --skip-infra \
    --questions experiments/questions-enrichment.json
```

Corpus `861648cc513c3862` (base, 15 tables), questions `23d7d17fad7ce484`,
code `6927987`.

## Related

- [Why the tier response is flat](enrichment-dependent-questions.md) — the design
  of this question set, and why the descriptions pre-empt the glossary.
- [The hard corpus](hard-corpus-results.md) — the null this overturns the
  interpretation of, though not the measurement.
