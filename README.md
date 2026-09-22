# agentic-bq-discovery

[![CI](https://github.com/tottenjordan/agentic-bq-discovery/actions/workflows/ci.yml/badge.svg)](https://github.com/tottenjordan/agentic-bq-discovery/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-3776AB.svg?logo=python&logoColor=white)](.python-version)
[![uv](https://img.shields.io/badge/packaging-uv-DE5FE9.svg)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/badge/lint-ruff-D7FF64.svg?logo=ruff&logoColor=black)](https://github.com/astral-sh/ruff)
[![Vertex AI](https://img.shields.io/badge/orchestration-Vertex%20AI%20Pipelines-4285F4.svg?logo=googlecloud&logoColor=white)](src/bq_context/pipeline/)

> **Which tables should an AI agent read before it writes SQL?**

## 📝 Description

`agentic-bq-discovery` is a benchmarking harness that measures **six competing
strategies for BigQuery table discovery** — the retrieval step that runs *before*
NL2SQL — across four levels of data-catalog enrichment. It is built for data and
ML engineers deciding how much to invest in catalog metadata, and for anyone who
needs a reproducible answer rather than a vendor claim. The problem it solves is
that an agent handed the wrong tables writes confidently wrong SQL, and there
was no rigorous, rerunnable way to compare the strategies that pick those
tables.

It reproduces and extends the experiment published in
[`statmike/vertex-ai-mlops`](https://github.com/statmike/vertex-ai-mlops/tree/main/Applied%20ML/AI%20Agents/bigquery-context)
(Apache-2.0), rebuilding its serial harness into a sharded, resumable,
pipeline-orchestrated one.

## ✨ Key Features

- **Six discovery approaches, one comparable contract.** BigQuery metadata
  tools, Knowledge Catalog search, cached context capsules, an LLM pre-filter, a
  hybrid, and a no-reranker control — all emitting the same `RerankerResponse`.
- **A verified metrics port.** A golden test scores upstream's real 3,000-cell
  results and reproduces **all 18 numbers of their published table**, so our
  figures are directly comparable rather than merely similar.
- **Resumable at two levels.** Per-cell JSONL checkpointing (verified under
  `SIGKILL`) and per-shard KFP caching, so a long sweep never loses more than
  60 seconds of work.
- **Honest gates.** `preflight` refuses to let you measure a catalog that only
  *looks* enriched — `lookupContext` returns an empty response rather than 403
  on missing permissions, which otherwise yields a plausible wrong null result.
- **Built for a shared quota pool.** Jittered exponential backoff, an adaptive
  rate limiter, and a circuit breaker, because these models run on Dynamic
  Shared Quota where a 429 means contention, not a raisable limit.
- **Retry-safe cost accounting.** Token usage is recorded once per kept
  response, never per attempt, so cost figures survive retries.
- **Runs locally or on Vertex AI.** Each pipeline component is a thin wrapper
  over the same CLI, so any pipeline failure reproduces on a laptop with one
  command.

## 🏗️ Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Language | Python | `3.13.14` |
| Packaging | uv (`uv_build` backend) | `0.11.x` |
| Agents | `google-adk` | `1.39.1` |
| Models | `google-genai` → Gemini 3.6 Flash / 3.5 Flash-Lite | `2.24.0` |
| Data | `google-cloud-bigquery` | `3.45.2` |
| Catalog | `google-cloud-dataplex` (Knowledge Catalog) | `2.20.0` |
| Orchestration | `kfp` + `google-cloud-aiplatform` | `2.17.0` / `1.165.1` |
| CLI | `typer` | `0.27.2` |
| Validation | `pydantic` | `2.13.5` |
| Analysis | `numpy`, `matplotlib` | `2.5.3`, `3.11.2` |
| Quality | `pytest`, `ruff`, `ty` | `9.1.1`, `0.16.8`, `0.0.83` |

Storage is Google Cloud Storage (per-shard JSONL). There is no database.

## 🚀 Getting Started

### System requirements

| Requirement | Notes |
|---|---|
| Python 3.13 | `uv` provisions it; no system install needed |
| [uv](https://github.com/astral-sh/uv) ≥ 0.11.28 | Required — the build backend is `uv_build` |
| Google Cloud SDK | `gcloud`, authenticated |
| A GCP project | BigQuery, Dataplex, Vertex AI, and Cloud Storage enabled |
| Docker *(optional)* | Only to build the pipeline image locally |

### 1. Install

```bash
git clone https://github.com/tottenjordan/agentic-bq-discovery.git
cd agentic-bq-discovery
make install          # uv sync --all-groups
```

### 2. Authenticate

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

### 3. Configure

```bash
cp .env.example .env
```

Only `GOOGLE_CLOUD_PROJECT` is required. See [`.env.example`](.env.example) for
every variable and its default.

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | *(none)* | **Required.** No default, deliberately |
| `BQ_LOCATION` | `US` | Dataset multi-region |
| `DATAPLEX_LOCATION` | `us-central1` | Data scans must be single-region |
| `AGENT_MODEL` | `gemini-3.6-flash` | Agent reasoning model |
| `TOOL_MODEL` | `gemini-3.5-flash-lite` | Reranker model |
| `RESOURCE_PREFIX` | `bigquery_context` | Prefix for the four tier datasets |
| `TOP_K` | `5` | Tables the reranker returns |
| `BQ_CONTEXT_IMAGE` | *(none)* | Pipeline only; from `make image-ref` |

> **Both Gemini models resolve only at the `global` Vertex endpoint** — they
> return 404 in `us-central1`. The client is configured automatically; you do
> not need to set a model location.

### 4. Verify and provision

```bash
bq-context validate-config     # identity, 16 permissions, models, storage (~30s)
bq-context ensure-infra        # 4 datasets, 60 views, 45 scans, glossary (~20 min)
bq-context preflight --tier 3  # prove enrichment actually reached the capsule
```

`ensure-infra` is idempotent and safe to re-run. `preflight` is **not optional** —
see [Key Features](#-key-features).

## 💻 Usage Examples

### Run one shard locally

```bash
# Smoke test: 3 questions, no LLM at all (~5s)
bq-context run-shard -e smoke --tier 3 --approach search_direct --limit 3 --runs 1
```

```json
{
  "shard_id": "tier3__search_direct",
  "planned": 3, "executed": 3, "succeeded": 3, "failed": 0,
  "elapsed_s": 5.134
}
```

```bash
# A full (tier, approach) shard — 25 questions × 5 runs. Resumable.
bq-context run-shard -e local-01 --tier 3 --approach semantic_context

# Re-running is safe: completed cells are skipped, failed cells retried
bq-context run-shard -e local-01 --tier 3 --approach semantic_context
```

Approaches: `bq_tools`, `kc_search`, `kc_context`, `context_prefilter`,
`semantic_context`, `search_direct`.

### Score and plot

```bash
bq-context merge -e local-01
bq-context score -e local-01 --report report.md
bq-context plot  -e local-01 --plots-dir plots/
```

```markdown
| Approach                  | Discovery recall | Final recall | Rerank loss |
|---------------------------|------------------|--------------|-------------|
| 1: BQ Tools *(control)*   | 100.0%           | 99.3%        | +0.007      |
| 6: Search Direct *(ctrl)* | 96.7%            | 96.7%        | +0.000      |
```

### Run the whole factorial on Vertex AI

```bash
make image                                       # build, verify, push

bq-context submit-pipeline -e full-01 \
  --profile full --image "$(make -s image-ref)" --skip-infra
```

| Profile | Factorial | Cells | Shards | Wall clock |
|---|---|---|---|---|
| `smoke` | tier 3 × 6 × 3q × 1 | 18 | 6 | ~5 min |
| `pilot` | 4 tiers × 6 × 5q × 1 | 120 | 24 | ~9 min |
| `full` | 4 tiers × 6 × 25q × 5 | 3,000 | 24 | ~2 h |

Resubmit with the **same `experiment_id`** to resume; the GCS prefix derives
from it.

### Inspect the enrichment ladder

```bash
bq-context preflight --tier 3
```

```text
tier   tables     bytes  profiled  terms  aspects
0          15    49,089         0      0  —
1          15   118,275       209      0  —
2          15   119,882       209     18  —
3          15   122,346       209     18  overview
```

Full command reference: [`experiments/README.md`](experiments/README.md).

## 🧪 Testing

```bash
make test        # pytest
make lint        # ruff format --check, ruff check, ty check src/
make check       # lint, then test
```

Or directly:

```bash
uv run pytest                          # all 153 tests, ~5s, no network needed
uv run pytest tests/test_metrics.py    # the golden test vs upstream's real data
uv run pytest -k resume                # resume semantics only
```

| Suite | Covers |
|---|---|
| `test_metrics.py` | Reproduces upstream's published tables from their 3,000 cells |
| `test_context.py` | Tier isolation and per-cell token accounting under concurrency |
| `test_resume.py`, `test_shard.py` | Checkpointing, dedupe, partial failure |
| `test_backoff.py` | Retry, rate limiting, circuit breaker |
| `test_pipeline.py` | Compiles the pipeline and asserts the compiled spec |
| `test_cli.py` | CLI surface, IAM preflight, enrichment-ladder gate |
| `test_merge.py` | Merge across shards, including dead ones |

The whole suite runs offline — no GCP credentials required. `tests/conftest.py`
pins a fake environment for every test, so a suite that passes locally cannot
quietly depend on your authenticated shell. CI runs with no credentials
configured, which keeps that honest.

## 🤝 Contributing & License

Contributions are welcome.

1. Fork and branch from `main`.
2. Read [`CODE_STANDARDS.md`](CODE_STANDARDS.md) — `uv` only, `ruff`, `pytest`,
   `ty`, and **no tool-attribution lines** in commits or PRs.
3. Make `make check` pass.
4. Open a PR describing what changed and why.

Durable findings belong in [`docs/notes/`](docs/notes/) — one topic per file,
indexed by [`docs/notes/README.md`](docs/notes/README.md).

Licensed under the **Apache License 2.0** — see [`LICENSE`](LICENSE).
Vendored upstream components are attributed in [`NOTICE`](NOTICE).
