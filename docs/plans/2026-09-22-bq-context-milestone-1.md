# BigQuery Context Experiment — Reference Architecture & Milestone 1

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this task-by-task.
> First action in execution: copy this file to `docs/plans/2026-09-22-bq-context-milestone-1.md`.

**Goal:** Stand up a repo that reproduces statmike's six-approach BigQuery table-discovery
experiment on `hybrid-vertex`, orchestrated by a Vertex AI Pipeline, with milestone 1 ending at
a green *pilot* run (120 cells) and a proven resume.

**Architecture:** Vendor upstream's six ADK agents and refactor their three process-globals into
explicit context objects, so the factorial can shard across processes. A single CLI does all the
work (`ensure-infra`, `run-shard`, `merge`, `score`, `plot`); the KFP pipeline is a thin wrapper
over that CLI. Shards write append-only JSONL to GCS keyed by a globally-unique `cell_key`, which
makes resume work across pipeline reruns and even across changes to the shard plan.

**Tech Stack:** Python 3.13 · uv · google-adk 1.36.x · google-genai · google-cloud-dataplex ·
google-cloud-bigquery · kfp 2.x · Vertex AI Pipelines · ruff / pytest / ty

---

## Context

We are evaluating six strategies for the retrieval step that precedes NL2SQL: given a natural-language
question, find the right BigQuery tables. Upstream (`statmike/vertex-ai-mlops`, `Applied ML/AI Agents/
bigquery-context`, Apache-2.0) has a complete working implementation and published results from a
3,000-cell factorial: 6 approaches × 4 catalog-enrichment tiers × 25 questions × 5 runs.

Two upstream findings shape this work:

1. **Their tier response was completely flat** — +0% for every approach, with medians saturating at
   100% recall on a 15-table corpus. The enrichment axis produced no signal.
2. **Their harness is hard-serial** (~12.5 h) because tier scope, the context cache, and token
   accounting are all module globals.

So the plan is *reproduce, then extend*: get a trustworthy baseline on our own project first, because
without one we cannot distinguish a real finding from a setup bug. Milestone 1 stops short of the full
3,000-cell run — it delivers everything needed to launch it.

### Decisions already made

| Decision | Choice |
|---|---|
| Code source | Vendor upstream, refactor to our standards |
| Goal | Reproduce first, then extend |
| GCP project | `hybrid-vertex` (existing) |
| Orchestration | Build the Vertex AI Pipeline now |

### Verified environment facts

Checked directly against `hybrid-vertex` during planning:

- **`gemini-3.6-flash` and `gemini-3.5-flash-lite` are `global`-endpoint only.** `countTokens` returns
  **404 in `us-central1`, 200 at `global`**. Pipeline *compute* is `us-central1`; the genai client must
  use `GOOGLE_CLOUD_LOCATION=global`. Conflating these two is the most likely day-one failure.
- **Those models are on Dynamic Shared Quota** — no per-project quota bucket exists, so there is no
  headroom number to read. 429s mean shared-pool contention; the mitigation is backoff, not a quota bump.
- APIs enabled: `aiplatform`, `bigquery`, `dataplex`, `datacatalog`, `cloudresourcemanager`, `storage`,
  `artifactregistry`, `cloudbuild`, `run`.
- **254 existing BigQuery datasets**, 0 Dataplex glossaries, 0 DataScans. `bigquery-public-data` readable.
- Python 3.13.14 already available via uv; system python3 is 3.12.3.
- Compute is not a constraint: 2,200 training CPUs, 600 parallel pipeline tasks.
- The **24h pipeline timeout does not exist** — current limit is 7 days per task.

### Reusable prior art (verified present)

- `/home/user/novastorm/bq_insights_agent/Dockerfile` — uv-in-Docker with measured cold-start notes.
  Adopt its layer split and `ENV PATH="/app/.venv/bin:$PATH"` trick.
- `/home/user/novastorm/bq_insights_agent/src/pipelines/compilation.py` — "compile at the point of use,
  never commit pipeline YAML," written up with the drift incident that motivated it. **Adopt verbatim.**
- `/home/user/novastorm/bq_insights_agent/src/pipelines/{dag,components,deploy}.py` — working KFP v2
  patterns in this same GCP project.

### Standards deviation to record

`CODE_STANDARDS.md` says `requires-python = ">=3.11"`. The vendored code requires `>=3.13.3`. **Go with
3.13** and note the deviation in `CODE_STANDARDS.md`. Upstream's `[project.optional-dependencies]` must
become `[dependency-groups]`.

---

## Reference Architecture

### Repo structure

```
bq_context/
├── CLAUDE.md  CODE_STANDARDS.md  README.md  NOTICE     # NOTICE = Apache-2.0 attribution
├── Dockerfile  Makefile  pyproject.toml  uv.lock  .python-version
├── docs/notes/  docs/plans/
├── experiments/questions.json  experiments/GROUND_TRUTH.md
├── src/bq_context/
│   ├── config.py              # Locations, ExperimentConfig, TierContext — NO globals
│   ├── schemas.py             # RerankerResponse (vendored) + Cell, ShardSpec, ShardResult
│   ├── discovery_common.py    # shared search/rerank helpers, now context-taking
│   ├── context_cache/         # cache.py, util_lookup_context.py
│   ├── reranker/              # util_rerank.py, function_tool_rerank.py
│   ├── approaches/            # agent_bq_tools/ … agent_search_direct/ (six) + orchestrator
│   ├── corpus/                # setup.py, cleanup.py (the 4-tier infra)
│   ├── runner/                # shard.py, resume.py, backoff.py, planner.py
│   ├── scoring/               # metrics.py, plots.py
│   ├── pipeline/              # components.py, dag.py, compilation.py, submit.py
│   └── cli.py                 # the single entrypoint everything wraps
└── tests/
```

**Why `src/bq_context/approaches/` rather than upstream's top-level `agent_*/`:** upstream is a flat
script directory, not a package. We need one importable package so the container and the CLI share
exactly one import path.

### The three globals → three objects

This is the core refactor and everything else depends on it.

| Upstream global | Becomes | Why it matters |
|---|---|---|
| `config.SCOPE` / `ACTIVE_TIER` | `TierContext(tier, dataset, scope)` passed explicitly | Scoring matches on *short* table name and all four tier datasets hold identically-named tables. A run must see exactly one tier. |
| `context_cache._CACHE` | `TierContext.cache`, built per shard | Rebuilt by `repopulate_for_tier`; blocks parallelism |
| `reranker.util_rerank._USAGE_LOG` | `contextvars.ContextVar[UsageLog]` | Per-cell token accounting; exact only because runs were serial |

Making `_USAGE_LOG` a `ContextVar` (not just a parameter) is deliberate: it later enables in-shard
`asyncio` concurrency without touching the measurement apparatus.

### Shard strategy: `(tier × approach)` → 24 shards, `parallelism=8`

Per-approach cost is wildly unbalanced (upstream's measured numbers, 500 cells each):

| approach | s/cell | h per shard |
|---|---|---|
| bq_tools | 43.1 | **1.50** |
| context_prefilter | 16.9 | 0.59 |
| kc_search | 8.9 | 0.31 |
| semantic_context | 6.5 | 0.23 |
| kc_context | 4.2 | 0.15 |
| search_direct | 2.2 | 0.08 |

Makespan = `max(longest_shard, total_work / P)` = `max(1.50, 11.35/P)`:

- `P=4` → 2.84 h · `P=6` → 1.89 h · **`P=8` → 1.50 h** ← knee · `P=12` → 1.50 h · `P=24` → 1.50 h

**Above P=8 the makespan is pinned by the single `bq_tools` shard**, so extra concurrency buys zero
wall-clock and costs linear 429 exposure in a shared project. **P=8, ~7.6× speedup.**

Ship `runner/planner.py` as a pure function with `split_runs_for: list[str] = []` so splitting
`bq_tools` along `run_idx` in milestone 2 is a parameter change, not a refactor.

### Pipeline topology

```
[0] validate_config    ~30s    testIamPermissions + model resolution + bucket    retries=0
[1] ensure_infra       12-40m  idempotent; datasets→views→scans→glossary→links   retries=1
[2] preflight_probe    ~3m     assert lookup_context non-empty + Gemini ramp     retries=0
     │
     ├─ dsl.ExitHandler(exit_task=merge_score_publish):
     │    dsl.ParallelFor(shard_plan, parallelism=8):
[3]  │        run_shard   5m–1.5h   cache warm + N cells                          retries=2
     │
[4] merge_results      ~2m     GCS glob, dedupe on cell_key, load BigQuery       retries=1
[5] score_and_plot     ~2m     metrics + figures                                 retries=1
[6] verify_completeness ~10s   THE ONLY TASK ALLOWED TO FAIL THE RUN             retries=0
[7] publish            ~30s    copy to latest/, write run report                 retries=1
```

Key structural choices:

- **`ensure_infra` is one component, not five.** The phases are strictly ordered and cheap; splitting
  buys five more VM startups (~10 min pure overhead) and five more IAM surfaces to debug.
- **Cache warm lives inside `run_shard`, not as a component.** Making it a component means serializing
  the cache to GCS — building a distributed cache to avoid rebuilding a local one. *Instrument it*: we
  now warm 24× instead of 4×. If warm exceeds ~2 min, that is the one number that flips the
  recommendation back to tier-level sharding.
- **Do not `ParallelFor` the 45 DataScans.** Gated by Dataplex's 30 runs/min/user quota; parallelism
  converts a 5s sleep into a 429 storm and buys nothing since wall time is polling, not issuing.
- **`ExitHandler` around the sweep** so merge runs even if shards die. Inject
  `dsl.PipelineTaskFinalStatus` into the exit task for the run report.
- **Shards never fail.** `run_shard` catches everything, writes markers, exits 0. `verify_completeness`
  reading `merged/missing.json` is the single place that turns the run red — and its failure message is
  the list of missing cell keys.

### Artifacts: raw GCS paths, not KFP artifacts

```
gs://hybrid-vertex-bq-context/
├── pipeline_root/                         # KFP-managed
└── experiments/{experiment_id}/           # experiment_id is a PIPELINE PARAMETER
    ├── manifest.json                      # factorial + code_version + image SHA
    ├── shards/tier3__bq_tools/attempt-0001.jsonl …  + _SUCCESS
    ├── merged/results.jsonl  merged/missing.json
    ├── scoring/  plots/
```

**This is the decision that makes cross-rerun resume possible.** KFP `Output[Dataset]` URIs live under
`pipeline_root/{pipeline_job_id}/`, and that ID changes every run — a resume built on them cannot find
the previous run's data. `experiment_id` is a parameter, so the prefix is stable by construction.
Declare `Output[Dataset]` anyway, but only as a lineage pointer (set `.metadata` counts); never route
data through it. This also dodges the **100-artifact-per-task cap** and the **131,072-byte output-
parameter cap**.

**"Append-only" JSONL, honestly:** GCS objects are immutable. Write locally to `/tmp/shard.jsonl` with
real append + `fsync`, and overwrite `attempt-NNNN.jsonl` every 60s or 25 cells. Bounds worst-case loss
to 60 seconds. Also flush on `SIGTERM` — Vertex sends it before killing a task, which turns a cancelled
run into a resumable one.

**Resume** is per-shard and needs no coordination: list `attempt-*.jsonl`, last-write-wins per
`cell_key`, drop `status != "ok"`, run the difference. Because `cell_key =
"{question_id}|{approach}|tier{tier}|run{run_idx}"` carries no shard identity, **this is correct even
if the shard plan changes between runs.**

**Second, free resume mechanism:** KFP execution caching skips completed shards without starting a VM.
This is safe **only if `code_version` (git SHA) is an explicit input to every shard** — otherwise you
change a prompt, rerun, and silently get cached stale results. This is the single most dangerous KFP
behavior for an experiment.

**BigQuery as a sink, not the system of record.** Merge loads `merged/results.jsonl` once into
`hybrid-vertex.bigquery_context_results.cells` (partitioned by `DATE(written_at)`, clustered on
`(approach, tier)`, variable payload in a `JSON` column). Streaming from 24 concurrent shards would add
quota limits and partial-commit semantics to a problem JSONL already solves.

### Reliability

429s must never propagate out of a shard. Three layers:

1. **Backoff at the Gemini call site** — base 1s, ×2, full jitter, cap 64s, 8 attempts. **Implement it
   at our own call boundary, not via the SDK's built-in retry**, so retries sit *outside* token
   accounting and a retried call counts once. This is what makes the token figures trustworthy.
2. **Per-shard adaptive token bucket** — halve on 429, recover +10%/successful minute. Eight shards
   converge on the pool's real capacity with no central coordinator.
3. **Cell-level failure is recorded, not raised** — write `{status: "error", ...}` and continue.

**Circuit breaker:** abort the shard on 20 consecutive failures or >10% error rate after 30 attempts.
Without it, `set_retry(num_retries=2)` on a broken shard burns 4.5 machine-hours to learn nothing.

**Heartbeat every 30s** (`cells_done/total, eta, tokens`) — on Vertex there is no stdout to tail, and a
healthy 90-minute shard is otherwise indistinguishable from a hung one.

### Service account and IAM

Create `bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com`. Do **not** reuse the compute default
SA — omitting `service_account=` silently falls back to it, and in this sandbox it may have Editor,
which means it works here and breaks anywhere least-privilege is enforced.

Project-level unless noted: `roles/aiplatform.user`, `roles/bigquery.dataEditor`,
`roles/bigquery.jobUser`, `roles/dataplex.dataScanEditor`, `roles/dataplex.catalogEditor`,
`roles/dataplex.catalogViewer`, `roles/storage.objectAdmin` **(bucket-scoped)**, `roles/browser`
(for `resourcemanager.projects.get`), `roles/serviceusage.serviceUsageConsumer`, `roles/logging.logWriter`.
Plus `roles/iam.serviceAccountUser` on the SA for whoever submits, or `actAs` fails confusingly.

**The worst failure mode in the system:** `lookupContext` **filters by permission and returns an empty
response rather than 403**. An under-permissioned SA makes tiers 1–3 score identically to tier 0, the
pipeline goes green, and we publish a plausible, wrong result. `preflight_probe` must assert non-empty
context on a known tier-3 table before any shard runs.

Avoid `roles/dataplex.admin` — it is the tempting unblock at minute 35 of a failing setup and it makes
the IAM story untestable.

---

## Tasks

### Task 0: Preflight verification

No code. Fail fast on anything that invalidates the design.

1. Confirm models resolve at `global` (re-run the `countTokens` probe; expect 404/404 regional, 200/200 global).
2. `gcloud storage buckets describe gs://hybrid-vertex-bq-context` — create regional `us-central1` if absent.
3. Confirm `bigquery_context_tier*` datasets do **not** already exist (name collision in a 254-dataset project).
4. Confirm `bigquery-context-glossary` does not exist at `locations/us`.

**Commit:** nothing. Record findings in `docs/notes/`.

---

### Task 1: Project skeleton

**Files:** `pyproject.toml`, `.python-version`, `Makefile`, `NOTICE`, `.gitignore`, `src/bq_context/__init__.py`

```bash
cd /home/user/bq_context && git init
uv init --package --python 3.13
uv add google-adk'>=1.36,<2' google-genai google-cloud-bigquery google-cloud-dataplex \
       google-cloud-resource-manager google-cloud-storage pydantic kfp'==2.17.0' \
       google-cloud-aiplatform'>=1.158,<2' numpy matplotlib typer
uv add --group dev pytest pytest-asyncio pytest-cov ruff ty
```

Pin `google-cloud-aiplatform<2` deliberately: a 2.0.0 reference page exists with no migration guide.
`kfp` goes in **runtime** deps (not dev) so `--no-dev` keeps it in the image — that is the precondition
for `install_kfp_package=False`.

`NOTICE` must carry Apache-2.0 attribution to `statmike/vertex-ai-mlops`.

Update `CODE_STANDARDS.md` to record the 3.13 deviation.

**Verify:** `uv run python -c "import kfp, google.adk; print('ok')"`
**Commit:** `feat: uv project skeleton with vendored-code dependencies`

---

### Task 2: Vendor upstream

Copy from `https://raw.githubusercontent.com/statmike/vertex-ai-mlops/main/Applied%20ML/AI%20Agents/bigquery-context/`
into `src/bq_context/`, preserving license headers: `config.py`, `schemas.py`, `discovery_common.py`,
`context_cache/`, `reranker/`, the six `agent_*/` → `approaches/`, `scripts/{setup,cleanup}.py` →
`corpus/`, `examples/{questions.json,GROUND_TRUTH.md}` → `experiments/`.

Mechanical only at this stage: fix imports to the package path, run `ruff format`. **Do not refactor yet.**

**Verify:** `uv run ruff check . && uv run python -c "from bq_context.approaches.agent_search_direct import agent"`
**Commit:** `feat: vendor upstream six-approach implementation (Apache-2.0)`

---

### Task 3: Kill the three globals ← **the critical task**

**Files:** `src/bq_context/config.py`, `context_cache/cache.py`, `reranker/util_rerank.py`, `tests/test_context.py`

**Step 1 — failing test:**

```python
def test_two_tier_contexts_are_independent():
    a, b = TierContext.build(tier=0), TierContext.build(tier=3)
    assert a.scope == ["bigquery_context_tier0"]
    assert b.scope == ["bigquery_context_tier3"]
    assert a.scope != b.scope  # fails today: module global


def test_usage_log_is_context_isolated():
    async def one(n):
        with usage_scope() as log:
            await asyncio.sleep(0.01)
            record_usage_tokens(n)
            return log.total_tokens

    assert await asyncio.gather(one(100), one(200)) == [100, 200]
```

**Step 2:** `uv run pytest tests/test_context.py -v` → FAIL

**Step 3:** Introduce a frozen `Locations` dataclass holding **all seven** locations (pipeline
`us-central1`, Gemini `global`, Dataplex glossary/links `us`, DataScans `us-central1`, BigQuery `US`,
Artifact Registry `us-central1`, GCS `us-central1`). Thread `TierContext` explicitly through
`discovery_common` and every approach. Convert `_USAGE_LOG` to a `ContextVar`.

**Watch:** upstream's approaches 1 and 4 use ADK's *InstructionProvider callable* form
(`instruction=agent_instructions` where it is a function) precisely so scope and cached briefs are read
per-request rather than frozen at import. Preserve that. `agent_search_direct/prompts.py` computes its
dataset list at import time — harmless upstream because that prompt is dead code, but fix it.

**Step 4:** `uv run pytest tests/test_context.py -v` → PASS; `uv run ty check src/`
**Commit:** `refactor: replace module globals with explicit context objects`

---

### Task 4: Cell runner + JSONL resume

**Files:** `src/bq_context/runner/{shard.py,resume.py}`, `tests/test_resume.py`

TDD the resume logic against a fake GCS — it is the safety net everything else rests on.

```python
def test_resume_drops_errors_and_keeps_last_ok():
    records = [
        {"cell_key": "q1|bq_tools|tier0|run0", "status": "error"},
        {"cell_key": "q1|bq_tools|tier0|run0", "status": "ok"},
        {"cell_key": "q2|bq_tools|tier0|run0", "status": "error"},
    ]
    assert completed_keys(records) == {"q1|bq_tools|tier0|run0"}


def test_resume_survives_shard_plan_change():
    """cell_key carries no shard identity, so cells complete under any plan."""
```

Then `shard.py`: `TierContext` → cache warm (**timed, logged**) → cell loop → rolling upload every 60s
or 25 cells → `_SUCCESS`/`_FAILED` marker → SIGTERM flush → 30s heartbeat.

**Commit:** `feat: shard runner with cell-level JSONL resume`

---

### Task 5: Backoff, circuit breaker, honest token accounting

**Files:** `src/bq_context/runner/backoff.py`, `tests/test_backoff.py`

```python
def test_retried_call_counts_tokens_once():
    """Retry must sit OUTSIDE token accounting or the 38.7M figure is wrong."""
    client = FlakyClient(fail_times=2, tokens=100)
    with usage_scope() as log:
        call_with_backoff(client.generate)
    assert log.total_tokens == 100  # not 300
    assert client.attempts == 3


def test_circuit_breaker_trips_on_consecutive_failures(): ...
```

**Commit:** `feat: jittered backoff, circuit breaker, retry-safe token accounting`

---

### Task 6: The CLI

**Files:** `src/bq_context/cli.py`, `src/bq_context/{corpus,scoring}/…`

Subcommands: `validate-config`, `ensure-infra`, `preflight`, `run-shard`, `merge`, `score`, `plot`.
Port upstream's `build_results.py` metrics verbatim — `GAIN = {must_have: 2.0, nice_to_have: 1.0,
distractor: 0.0}`, `RERANK_K = 5`, nDCG@5, discovery vs final recall, rerank loss — so our numbers are
directly comparable to theirs. Golden-file test the metrics against upstream's published table.

**Commit:** `feat: bq-context CLI (infra, shard, merge, score, plot)`

---

### Task 7: Create the real infrastructure

```bash
uv run bq-context validate-config
uv run bq-context ensure-infra          # 12-40 min
uv run bq-context preflight --tier 3
```

Creates 4 datasets, 60 views, 45 DataScans, 1 glossary + 11 terms, 48 entry links, 4 guidelines aspects.

**Gate:** `preflight` must show **non-empty** `lookup_context` on a tier-3 table and prove tier 3 differs
from tier 0. If tiers look identical, stop — that is the silent-empty-context failure.

**Commit:** `docs: record infra setup results and timings` (+ a `docs/notes/` entry)

---

### Task 8: Local smoke — before any container

```bash
uv run bq-context run-shard --tier 3 --approach search_direct --questions single-q1,single-q2,single-q3 \
  --runs 1 --experiment-id smoke-local --out gs://hybrid-vertex-bq-context/experiments/smoke-local
uv run bq-context run-shard --tier 3 --approach bq_tools    --questions single-q1 --runs 1 ...
uv run bq-context merge --experiment-id smoke-local && uv run bq-context score --experiment-id smoke-local
```

Then **kill a shard mid-run and re-run it** — confirm it resumes rather than restarting.

**Commit:** `test: verified local shard execution and resume`

---

### Task 9: Container

**Files:** `Dockerfile`, `cloudbuild.yaml`

Follow `/home/user/novastorm/bq_insights_agent/Dockerfile`'s layer split. `FROM python:3.13-slim`;
`COPY --from=ghcr.io/astral-sh/uv:0.9`; deps layer then source layer; `UV_COMPILE_BYTECODE=1`,
`UV_PYTHON_DOWNLOADS=never`, `UV_NO_CACHE=1`.

**No `ENTRYPOINT`/`CMD`** — KFP overwrites the command. The load-bearing line is:

```dockerfile
ENV PATH="/app/.venv/bin:$PATH" \
    GOOGLE_GENAI_USE_VERTEXAI=true \
    GOOGLE_CLOUD_PROJECT=hybrid-vertex \
    GOOGLE_CLOUD_LOCATION=global
```

Omit the `PATH` line and KFP's injected `python3` resolves to system Python with nothing importable —
it presents as a bare `ModuleNotFoundError` with no hint about PATH.

```bash
gcloud artifacts repositories create bq-context --repository-format=docker --location=us-central1
gcloud builds submit --region=us-central1 \
  --tag us-central1-docker.pkg.dev/hybrid-vertex/bq-context/runner:$(git rev-parse --short HEAD) .
```

**Verify:** `docker run --rm $IMAGE python3 -c "import kfp, google.adk, bq_context; print('ok')"`
**Always reference the SHA tag, never `:latest`** — the KFP cache key includes the image reference, so an
immutable tag makes the cache correct and a floating tag makes it lie.

**Commit:** `feat: uv-native container + Cloud Build`

---

### Task 10: Service account and IAM

Create the SA and grant the roles from the architecture section. Extend `validate-config` to call
`testIamPermissions` for the ~15 specific permissions so a missing grant surfaces in 30 seconds rather
than 40 minutes into `ensure_infra`.

**Verify:** `uv run bq-context validate-config --impersonate bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com`
**Commit:** `feat: dedicated pipeline SA with least-privilege IAM`

---

### Task 11: Pipeline

**Files:** `src/bq_context/pipeline/{components.py,dag.py,compilation.py,submit.py}`

Each component is a ~15-line wrapper over a CLI subcommand, `base_image=RUNNER_IMAGE`,
`install_kfp_package=False`. `RUNNER_IMAGE = os.environ["BQ_CONTEXT_IMAGE"]` — a `KeyError` at compile
time is the point, since `base_image` is not parameterizable at runtime.

`dag.py`: `ExitHandler` + `ParallelFor(parallelism=8)`, `code_version` threaded into every shard,
`set_retry(num_retries=2, backoff_duration="120s", backoff_factor=2.0, backoff_max_duration="600s")`.
Note `set_retry` args must be **compile-time constants**.

`compilation.py`: copy the pattern and the rationale from novastorm — **never commit pipeline YAML**.

`submit.py`: use `job.submit()`, not `job.run()`, so a dropped workstation session is not a failed
pipeline. Pass `service_account=` explicitly and `failure_policy='slow'`.

**Commit:** `feat: Vertex AI Pipeline wrapping the shard CLI`

---

### Task 12: Smoke → pilot → resume (milestone 1 exit)

Three profiles, one code path, differing only in `parameter_values`:

| profile | factorial | cells | shards | P | wall clock | cost |
|---|---|---|---|---|---|---|
| **smoke** | tier 3 × 6 × 3q × 1 | 18 | 6 | 6 | ~15 min | ~$0.10 |
| **pilot** | 4 tiers × 6 × 5q × 1 | 120 | 24 | 8 | ~25 min | ~$0.40 |
| full *(M2)* | 4 × 6 × 25q × 5 | 3,000 | 24 | 8 | ~2.0 h | ~$2.85 |

**Smoke runs on tier 3, not tier 0.** Tier 0 is unenriched — a green tier-0 smoke proves nothing about
Dataplex and would pass even if every scan, term, link, and aspect were missing. Tier 3 has the most
ways to fail informatively.

**Pilot runs the exact 24-shard topology at 1/25th the cost.** It is where we find out that P=8 triggers
429s, or that cache warm takes four minutes instead of thirty seconds.

**Exit criteria — pilot green twice: once cold, once as a resume after deliberately cancelling it
mid-flight.** That second run is the only real test of the mechanism the whole reliability design rests on.

**Commit:** `test: pilot pipeline green with verified mid-flight resume`

---

## Verification

```bash
uv run ruff format --check . && uv run ruff check . && uv run ty check src/ && uv run pytest
uv run bq-context validate-config
uv run bq-context preflight --tier 3        # MUST show non-empty lookup_context
uv run bq-context score --experiment-id pilot-01
bq query --use_legacy_sql=false 'SELECT approach, tier, COUNT(*) FROM `hybrid-vertex.bigquery_context_results.cells` GROUP BY 1,2 ORDER BY 1,2'
```

**Correctness checks that matter more than the tests passing:**

1. **Tier isolation** — every cell's ranked `table_id`s resolve to exactly one tier dataset. A cell
   citing two tiers means `SCOPE` leaked.
2. **Enrichment is real** — tier 3 `lookup_context` payloads are strictly larger than tier 0 and contain
   glossary/guidelines keys. If flat, the experiment measures nothing.
3. **Token accounting** — sum of per-cell reranker tokens is within a few percent of upstream's
   ~12,900 tokens/cell average; a 2–3× overshoot means retries are being double-counted.
4. **Resume is lossless** — cancel mid-pilot, resume, and confirm `merged/results.jsonl` has exactly
   `expected_cells` unique keys with no duplicates.

## Out of scope for milestone 1

The full 3,000-cell run; adaptive cost-model shard splitting (planner ships with the parameter, defaulted
off); in-shard `asyncio` cell concurrency (~4× more, but changing the measurement apparatus and the
execution topology together makes anomalies unattributable); Vertex Experiments/TensorBoard;
`PipelineJobSchedule`; VPC-SC/CMEK; and the harder corpus that upstream's flat tier response argues for —
that is the "extend" half, and it needs the reproduction baseline first.
