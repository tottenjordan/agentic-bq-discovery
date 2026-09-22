# Session Notes — Index

Durable findings from working sessions, one topic per file. This index is
pointers only; keep it under 200 lines and put the substance in the linked
notes.

## Conventions

- One topic per file. Cross-link related notes rather than merging them.
- Check for an existing note before adding a new one — update in place.
- Delete notes that become wrong or stale.
- A note describes what was true when written. Re-verify any file, flag, or
  command it names before acting on it.
- Only record what outlives the conversation and isn't recoverable from the
  repo, git history, `CLAUDE.md`, or existing docs. Favor the non-obvious:
  broken tooling, environment quirks, workarounds.

## Notes

### The experiment

- [full-01 — the first complete 3,000-cell run](full-run-results.md) — the approach comparison is
  sound and reproduces upstream's reranker finding; the **tier comparison is invalid**, because
  Dataplex search-index warm-up was confounded with shard execution order.

- [Upstream experiment: findings and implications](upstream-experiment.md) — their headline
  result is a **null result** (flat tier response, ceiling effect); the per-approach cost table
  that drives our sharding; the three globals; upstream's known defects.

### GCP environment

- [`hybrid-vertex` project state](gcp/hybrid-vertex-environment.md) — what's enabled, the 254
  existing datasets, resources we created, and `gcloud ai pipeline-jobs` not being a thing.
- [Gemini endpoints and quota](gcp/gemini-endpoints-and-quota.md) — our two models are
  **`global`-endpoint only** (404 in `us-central1`), they're on Dynamic Shared Quota so there is
  no headroom to check, and how to probe availability correctly.
- [Dataplex / Knowledge Catalog gotchas](gcp/dataplex-catalog-gotchas.md) — `lookupContext`
  returns **empty rather than 403** on missing permissions; parentheses silently break search
  scoping; the three different locations catalog resources must live in; quotas.
- [The pipeline service account](gcp/pipeline-service-account.md) — the grant set, why
  `--impersonate` is the only meaningful way to check it, and confirmation that the SA reads
  catalog context identically to a near-Owner account.
- [What `lookupContext` actually returns](gcp/lookup-context-capsule.md) — glossary definitions
  arrive **per-column under `terms`**; the default capsule truncates schemas to 25 columns;
  `all_schema_fields=true` works and the budget keys do nothing.
- [Provisioning the four-tier corpus](gcp/corpus-provisioning.md) — what `ensure-infra` built, how
  long it took, and why tier 3 falls back to `overview`.

- [First live runs](local-smoke-results.md) — measured per-cell cost vs upstream, cache warm at
  ~4s (settling the sharding question), verified resume after a hard kill, and the ADK
  environment bug that only a live agent run could surface.

### Tooling

- [Pipeline runs — milestone 1 exit](pipeline-runs.md) — the two ways to get identity wrong in a
  pipeline (a task cannot impersonate itself; `"default"` is an alias, not an identity), and the
  green cold + resumed pilot runs.
- [The Vertex AI Pipeline](kfp-pipeline.md) — the topology, and three KFP constraints that cost
  real time: PEP 563 breaks compilation, `parallelism` must be a compile-time constant, and a
  `dsl.If` group cannot be depended on from outside.
- [The runner container](container.md) — why `ENV PATH=/app/.venv/bin` is the whole KFP
  integration (confirmed by breaking it), two corrections to the plan, and the fact that Cloud
  Build substitutions do not nest.
- [KFP prior art at `/home/user/novastorm`](prior-art-novastorm-kfp.md) — never commit pipeline
  YAML (with the drift incident that proves it), and measured uv-in-Docker cold-start fixes.

## How to run it

- [`notebooks/walkthrough.ipynb`](../../notebooks/walkthrough.ipynb) — the six approaches
  unrolled by hand, with outputs. Executes end to end; `tests/test_notebook.py` guards it
  against package drift.
- [`experiments/README.md`](../../experiments/README.md) — prerequisites, the local and pipeline
  workflows, resume semantics, and reading the report.

## Plans

- [Milestone 1: reference architecture and first iteration](../plans/2026-09-22-bq-context-milestone-1.md)
