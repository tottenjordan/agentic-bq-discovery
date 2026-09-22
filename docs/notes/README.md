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
- [Provisioning the four-tier corpus](gcp/corpus-provisioning.md) — what `ensure-infra` built and
  how long it took, and the finding that **tier 2 is not a distinct factor level**: glossary entry
  links are created correctly but never reach the capsule the agents read.

### Tooling

- [KFP prior art at `/home/user/novastorm`](prior-art-novastorm-kfp.md) — never commit pipeline
  YAML (with the drift incident that proves it), and measured uv-in-Docker cold-start fixes.

## Plans

- [Milestone 1: reference architecture and first iteration](../plans/2026-09-22-bq-context-milestone-1.md)
