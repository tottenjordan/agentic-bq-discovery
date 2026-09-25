# enrich-probe-01: a large tier effect, and why it is not yet believable

`enrich-probe-01`, 288/288 cells, base corpus, survey profile (one run).

The run reported the first non-flat enrichment response this project has seen.
Forty minutes later the tier-0 baseline had moved enough to halve it. **Do not
quote the headline number.** The interesting content of this run is the confound
it exposed, which affects how every future question set has to be measured.

## What the run reported

Final recall by tier, meaned over 12 questions:

| approach | tier 0 | tier 1 | tier 2 | tier 3 | Δ mean |
|---|---|---|---|---|---|
| BQ Tools, KC Context, Pre-Filter | 1.000 | 1.000 | 1.000 | 1.000 | +0.000 |
| KC Search, Semantic, Search Direct | 0.292 | 0.625 | 0.625 | 0.875 | +0.583 |

## Why it is not believable

Re-running **the identical tier-0 shard 40 minutes later**, same code, same
corpus, same questions:

```
search_direct @ tier0, during the run:  0.292
search_direct @ tier0, 40 min later:    0.625
```

Nothing changed except elapsed time. The tier-0 baseline the whole effect is
measured against is not stable, so `+0.583` is an upper bound on an artefact,
not an estimate of anything. If 0.625 is the settled tier-0 value the effect is
about `+0.250`; it could also be smaller.

Individual queries show the same drift. For `cat-landfall`, search at tier 0
returned `zip_codes` and `air_quality_annual_summary` during the run, and
returns `hurricanes` now — stably, 8 identical queries in a row.

This is the confound that invalidated `full-01`'s original tier comparison, in a
new costume. There it was execution order; here it is the age of the question
set.

## The mechanism, and why the existing guard missed it

The likely cause is that **Dataplex semantic search needs warming per novel
query**, not per entry. These twelve questions had never been asked before
03:31. During the run each was issued a handful of times; by the re-check they
had been issued more, and tier 0 — the tier with the least indexed text to match
against, and therefore the most marginal — improved.

`assess_search_convergence` exists precisely to catch a moving index, and it
would not have caught this. It probes with its own fixed queries, which were
stable at `tier0=3` both during the run and after:

```
search hits/tier  tier0=3  tier1=3  tier2=3  tier3=4
search hits/tier  tier0=3  tier1=3  tier2=3  tier3=4   (second pass)
```

**A warm fixed probe says nothing about a cold novel query.** That is a real gap
in the guard, and it matters more than this one result: it means the guard
cannot certify any run that introduces new questions.

Separately, and my own error: this run's preflight was invoked with
`--settle 0`, which skips the second probe entirely. It would not have changed
the outcome, but disabling the convergence check on the one run that introduced
a new question set was the wrong call.

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
