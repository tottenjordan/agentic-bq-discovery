# `hybrid-vertex` project state

*Verified 2026-09-22. Re-check before relying on any specific resource.*

Running from a Cloud Workstation, ADC as `admin@jordantotten.altostrat.com`, near-Owner.
Default region `us-central1`.

## Already enabled — nothing to turn on

`aiplatform`, `bigquery` (+connection/storage/reservation/etc.), `dataplex`, `datacatalog`,
`cloudresourcemanager`, `storage`, `compute`, `artifactregistry`, `cloudbuild`, `run`.

## It is a busy shared sandbox

**254 BigQuery datasets** already exist. This matters for the experiment: Knowledge Catalog
semantic search is scoped by a `parent:datasets/{ds}` predicate, so in principle the corpus is
isolated — but that predicate is fragile (see [[dataplex-catalog-gotchas]]) and the client-side
scope filter is the only backstop. `out_of_scope_dropped != 0` in search stats is the regression
signal to watch.

Clean as of this date: **0 Dataplex glossaries**, **0 DataScans**, only system entry groups
(`@analyticshub`, `@storage`). No `bigquery_context_tier*` dataset name collisions.

## Resources created for this project

- `gs://hybrid-vertex-bq-context` — regional `us-central1`, uniform bucket-level access.
  Created 2026-09-22. Holds pipeline root and per-shard JSONL.

## Gotchas

- **`gcloud ai pipeline-jobs` does not exist.** Vertex AI Pipelines are driven through the
  Python SDK (`google-cloud-aiplatform`), not gcloud. Don't waste time looking for the command.
- `bigquery-public-data` is readable with no extra grant (`allAuthenticatedUsers`); the
  `bigquery.jobUser` role in `hybrid-vertex` covers the billing side.
- Existing service accounts include `vertex-jt@` and the compute default
  `934903580331-compute@developer.gserviceaccount.com`. **Do not reuse the compute default for
  the pipeline** — in a long-lived sandbox it may carry Editor, so least-privilege bugs stay
  invisible here and surface in any properly locked-down project.
- Python 3.13.14 is already installed via uv; system `python3` is 3.12.3.

Related: [[gemini-endpoints-and-quota]], [[dataplex-catalog-gotchas]].
