# The factorial experiment

Compares six BigQuery table-discovery approaches for NL2SQL, with **catalog
enrichment** as the independent variable. See the repo
[`README.md`](../README.md) for the six approaches and
[`docs/notes/upstream-experiment.md`](../docs/notes/upstream-experiment.md) for
what the original run found.

## What it measures

Enrichment tier (0 → 3) over an identical 15-table corpus replicated per tier.
The sweep is approach (6) × tier (4) × question (25) × run (5) = **3,000
isolated approach-runs**, each a fresh ADK session on a per-approach runner.

Ground truth is graded — `must_have` / `nice_to_have` / `distractor`, see
[`GROUND_TRUTH.md`](GROUND_TRUTH.md) — so precision, rank quality, and the trap
questions all mean something. Metrics: discovery recall, final recall, rerank
loss, nDCG@5, precision, latency, reranker tokens.

> **Read before interpreting tier results.** All four tiers are distinct, but two
> caveats apply. The `lookupContext` capsule **truncates each schema to 25
> columns** by default, so 6 of the 24 glossary term links — mostly on the
> 153-column `hurricanes` view — never reach the reranker;
> `all_schema_fields=true` recovers them at ~1.8× the capsule size. And tier 3
> attaches the `overview` aspect rather than `guidelines`, which is not
> available in this project. Evidence in
> [`docs/notes/gcp/lookup-context-capsule.md`](../docs/notes/gcp/lookup-context-capsule.md).
> `bq-context preflight` prints the full ladder on every run.

## Files

| File | Role |
|---|---|
| `questions.json` | 25 questions across `single-table`, `multi-table-related`, `multi-table-disparate`, and `trap` categories, each with a graded `relevance` map. |
| `GROUND_TRUTH.md` | The corpus, the grading rubric, and how each distractor is baited. |
| `upstream_baseline/results.md` | The original published report, kept for comparison. Our scorer reproduces its headline table exactly (`tests/test_metrics.py`). |

Results are **not** written here. They go to
`gs://hybrid-vertex-bq-context/experiments/{experiment_id}/`:

```
shards/{tier}__{approach}/attempt-NNNN.jsonl   raw cells, one JSON per line
merged/results.jsonl                           deduped
merged/missing.json                            what is absent, if anything
```

## Prerequisites

```bash
make install                        # uv sync --all-groups
export GOOGLE_CLOUD_PROJECT=...     # required; there is no default

bq-context validate-config          # identity, 16 permissions, models, storage
bq-context ensure-infra             # 4 datasets, 60 views, 45 scans, glossary (~20 min)
bq-context preflight --tier 3       # assert enrichment actually reached the capsule
```

`preflight` is not optional. `lookupContext` returns an **empty response rather
than 403** when permissions are missing, so an under-permissioned run produces
tiers that score identically, a green pipeline, and a plausible wrong result. It
prints the enrichment actually present at each tier:

```
tier   tables     bytes  profiled  terms  aspects
0          15    49,089         0      0  —
1          15   118,275       209      0  —
2          15   119,882       209     18  —
3          15   122,346       209     18  overview
```

## Running

Two ways: locally, one shard at a time, or the whole factorial on Vertex AI
Pipelines. Both run the same code — each pipeline component is a thin wrapper
over the same CLI — so anything that fails in the pipeline reproduces locally
with one command.

### Locally, a shard at a time

```bash
# Quick smoke — 3 questions, no LLM at all (verifies scoping + plumbing, ~5s)
bq-context run-shard -e smoke --tier 3 --approach search_direct --limit 3 --runs 1

# One (tier, approach) shard, all 25 questions, n=5. Resumable.
bq-context run-shard -e local-01 --tier 3 --approach semantic_context

# Re-running is safe: completed cells are skipped, failed cells re-run
bq-context run-shard -e local-01 --tier 3 --approach semantic_context

# Subset by question id, or take the first N
bq-context run-shard -e local-01 --tier 0 --approach kc_search --questions-ids single-q1,trap-q4
bq-context run-shard -e local-01 --tier 0 --approach kc_search --limit 5

# Collect → score → plot (scoring never re-runs the sweep)
bq-context merge -e local-01
bq-context score -e local-01 --report report.md
bq-context plot  -e local-01 --plots-dir plots/
```

Approaches: `bq_tools`, `kc_search`, `kc_context`, `context_prefilter`,
`semantic_context`, `search_direct`.

`bq-context plan-shards -e local-01` prints the full 24-shard plan as JSON if
you want to drive it from a shell loop.

### On Vertex AI Pipelines

```bash
make image                                        # build, verify, push (refuses a dirty tree)

# smoke: tier 3 only, 3 questions, 1 run  -> 18 cells,  6 shards, ~5 min
bq-context submit-pipeline -e smoke-01 --profile smoke --image "$(make -s image-ref)"

# pilot: all tiers, 5 questions, 1 run    -> 120 cells, 24 shards, ~9 min
bq-context submit-pipeline -e pilot-01 --profile pilot --image "$(make -s image-ref)"

# full:  all tiers, 25 questions, 5 runs  -> 3,000 cells, 24 shards, ~2 h
bq-context submit-pipeline -e full-01 --profile full --image "$(make -s image-ref)"

# Infra already provisioned? Skip the 20-minute step.
bq-context submit-pipeline -e pilot-02 --profile pilot --image "$(make -s image-ref)" --skip-infra

# Compile and print without submitting
bq-context submit-pipeline -e pilot-03 --profile pilot --image "$(make -s image-ref)" --dry-run
```

Escalate **smoke → pilot → full**. Smoke deliberately runs tier 3 rather than
tier 0: tier 0 is unenriched, so a green tier-0 smoke would pass even if every
scan, term, link, and aspect were missing. Pilot runs the exact 24-shard
topology of the full run at 1/25th the cost, which is where `parallelism=8`
contention and cache-warm surprises show up.

### Resuming

Resubmit with the **same `experiment_id`**. The GCS prefix derives from it, so
reusing it resumes and changing it starts over. Never derive it from a
timestamp.

Two mechanisms stack. Per-cell, a shard lists its own attempt files and runs
only what is missing — verified under `SIGKILL` mid-run. Per-shard, KFP skips
finished shards on a rerun without starting a VM, which is safe only because
`code_version` is an explicit shard input, so a changed commit invalidates the
cache rather than silently returning results from old code.

Failed cells are recorded, not fatal, and re-run on the next pass. A shard that
fails systematically trips a circuit breaker rather than burning its full retry
budget.

## Reading the report

`bq-context score` prints four sections:

- **Discovery vs rerank** — did retrieval find the table, and did the reranker
  keep it? Reported as **means**: on this corpus most cells score 1.0, so a
  median saturates and hides the tail.
- **Enrichment response** — Δ recall from the lowest tier to the highest, shown
  as both median and mean. The median is the original measure and saturates;
  the mean is the one that can actually move. Read alongside the tier-2 caveat
  above.
- **Cost and latency** — p50/p95 and reranker tokens per approach.
- **nDCG@5 by category** — where `search_direct` (no reranker) falls behind, and
  therefore what reranking is buying.

Scoring reads `merged/results.jsonl` and never re-runs the sweep, so iterating
on metrics is free.

## Cost

The full factorial is ~12 h of agent wall-clock and ~39 M reranker tokens
serially; at `parallelism=8` it is ~2 h wall clock. The Vertex Pipelines
overhead is a few dollars — model tokens dominate. Smoke and pilot are cents.
