# agentic-bq-discovery

Evaluation harness for **six approaches to BigQuery table discovery** — the
retrieval step that runs *before* NL2SQL. Given a natural-language question,
which tables should an agent hand to the SQL writer?

This reproduces and extends the experiment published in
[`statmike/vertex-ai-mlops`](https://github.com/statmike/vertex-ai-mlops/tree/main/Applied%20ML/AI%20Agents/bigquery-context)
(Apache-2.0), adding sharded parallel execution, resumability, and Vertex AI
Pipelines orchestration.

## The six approaches

| # | Approach | Discovery mechanism | Reranked |
|---|---|---|---|
| 1 | BQ Metadata Tools | LLM tool loop over the BigQuery API; schema only | yes |
| 2 | Knowledge Catalog Search | `search_entries` + per-hit `lookup_entry` | yes |
| 3 | Knowledge Catalog Context | full cached capsule for the whole corpus | yes |
| 4 | Context Pre-Filter | LLM nominates from briefs, then rerank on detail | yes |
| 5 | Semantic Context | approach 2's search + approach 3's cached capsules | yes |
| 6 | Search Direct | search relevance order as the final ranking | **no** |

Approach 6 is the control that isolates the reranker's marginal contribution;
approaches 2–5 share one reranker so their outputs are directly comparable.

## The factorial

**6 approaches × 4 enrichment tiers × 25 questions × 5 runs = 3,000 cells.**

Tiers replicate an identical 15-table corpus (12 answer tables + 3 deliberate
distractors, all views over `bigquery-public-data`) with increasing Knowledge
Catalog enrichment: schema only → + data profiling → + business glossary →
+ authored guidelines.

Metrics per cell: discovery recall, final recall, nDCG@5, precision with
distractors, latency, and reranker token cost.

## Why this repo exists

Upstream's headline result is a **null result**: enrichment tier response was
completely flat (+0% for every approach), with medians saturating at 100% recall.
That is a ceiling effect on a 15-table corpus, not a finding about catalog
enrichment — and it is also indistinguishable from a silently broken catalog.
So the plan is *reproduce first, then extend*: establish a trustworthy baseline
before drawing conclusions from a harder corpus.

See [`docs/notes/upstream-experiment.md`](docs/notes/upstream-experiment.md) for
the distilled findings and [`docs/plans/`](docs/plans/) for the architecture.

## Status

Milestone 1, in progress. Done: vendored approaches refactored for parallel
execution, shard runner with cell-level resume, retry/circuit-breaker. Next: the
CLI, corpus provisioning, container, and the Vertex AI Pipeline.

## Running the experiment

See [`experiments/README.md`](experiments/README.md) for prerequisites, the
local and Vertex AI Pipelines workflows, resume semantics, and how to read the
report.

## Quick start

```bash
make install                      # uv sync --all-groups
make check                        # ruff format + lint, ty, pytest
export GOOGLE_CLOUD_PROJECT=...   # must be set; there is no default
```

Requires Python 3.13 (uv provisions it) and a GCP project with BigQuery,
Dataplex, and Vertex AI enabled.

## Conventions

- [`CODE_STANDARDS.md`](CODE_STANDARDS.md) — `uv` only, `ruff`, `pytest`, `ty`
- [`docs/notes/`](docs/notes/) — durable findings, one topic per file

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE) for attribution of
the vendored upstream components.
