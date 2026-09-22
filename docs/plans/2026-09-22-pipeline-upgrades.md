# Pipeline Upgrades: Artifacts, Executive Report, Logging, Caching

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this task-by-task.
> First action in execution: copy this file to `docs/plans/2026-09-22-pipeline-upgrades.md`.

**Goal:** Make the Vertex AI Pipeline observable and its results durable — stop throwing away the
report and figures it already generates, surface results as first-class KFP artifacts, emit
queryable structured logs, and close two silent caching bugs.

**Architecture:** Artifacts on `preflight` and `finalize` only, as *lineage pointers*: `.uri` points
at the stable `experiments/{id}/` GCS prefix, counts go in `.metadata`, and the human-facing copies
are uploaded to GCS independently so they do not live under a per-job artifact path. A corpus
fingerprint — hashing enrichment **shape**, never bytes — is threaded into every shard so a corpus
change invalidates the cache as a code change does.

**Tech Stack:** kfp==2.17.0 · google-cloud-aiplatform==1.165.1 · matplotlib (data charts) ·
PaperBanana on a separate report image (diagrams only) · uv / ruff / pytest / ty

---

## Context

The pipeline works — it produced 3,000 cells — but it is close to write-only, and two caching bugs
are live. Everything below was verified against the current tree or by compiling against the pinned
SDK.

1. **It throws away what it makes.** `finalize` (components.py:220-231) runs `merge`, `score` and
   `plot` with only `--experiment-id` and `--out`. `score` writes markdown only when `--report PATH`
   is passed (cli.py:893); `plot` defaults `--plots-dir` to the relative `Path("plots")`
   (cli.py:903). **Every run renders the report to stdout and writes three PNGs into `/app/plots/`
   inside a container that is then destroyed.**

2. **No artifacts at all.** Not one `Output[...]` in the repo. The Vertex UI shows a green DAG and
   nothing else. `preflight` computes the enrichment ladder that gates the whole experiment and
   prints it to stdout.

3. **Logs are unstructured.** Components use bare `print(..., flush=True)`; the library logs a human
   format to stderr (cli.py:83-87). Recovering `cache_warm_s` meant `gcloud logging read` and a regex.

4. **Two caching bugs, both confirmed by reading the SDK:**
   - **Per-task caching settings have never taken effect.** `submit.py:77` passes
     `enable_caching=True` unconditionally, and `pipeline_jobs.py:85-101`
     (`_set_enable_caching_value`) *blunt-overwrites every task in every DAG*:
     `task["cachingOptions"] = {"enableCache": enable_caching}`. So the deliberate
     `set_caching_options(enable_caching=False)` on `ensure_infra`, `preflight` and `finalize`
     (dag.py:101, 109, 126) are silently stomped to `True` on every submit. The enrichment gate the
     experiment depends on has been eligible for cache reuse all along.
     `tests/test_pipeline.py:144-151` inspects the *compiled spec*, which is correct — the stomp
     happens after compilation, inside the SDK — so the test gives false confidence.
   - **The corpus is invisible to the shard cache key.** `run_shard` is keyed on `code_version`, so
     a code change invalidates it. Re-run `ensure-infra` to change enrichment, resubmit with the
     same SHA, and KFP returns cells scored against the *old* corpus.

### Decisions already made

| Decision | Choice |
|---|---|
| Artifact scope | `preflight` and `finalize` only |
| Cache safety | Thread a corpus fingerprint into every shard |
| PaperBanana | Diagrams only, inside `finalize` on a dedicated report image, gated off by default |

### Verified constraints — several found by compiling, not by reading docs

- **`sys.exit()` destroys every artifact output.** `executor.py:451-454` calls
  `write_executor_output(result)` *after* the function returns; `SystemExit` propagates past
  `executor_main.py:99` and the process dies, so `executor_output.json` is never written and no
  `.uri` or `.metadata` mutation is persisted. **`finalize`'s `sys.exit(1)` on missing cells
  (components.py:231) would blank every artifact on exactly the runs where a human most wants
  them.** Populate artifacts *first*, raise last.
- **A component may have both `Output[...]` params and a return value** — verified by compiling.
  But `task.output` raises `AttributeError` when a task has multiple outputs
  (`pipeline_task.py:293-302`). Use `task.outputs["name"]`, and prefer a `NamedTuple` return over
  bare `-> str` so the key is `"fingerprint"` rather than the literal `"Output"`. Never name a
  parameter `Output` (`component_factory.py:271-273`).
- **`dsl.Metrics.log_metric` accepts floats only** (`artifact_types.py:223-230`). The ladder's
  `aspects` is a `list[str]` and cannot go in a Metrics artifact.
- **The ExitHandler exit task can *declare* artifacts** (verified by compiling) but **cannot depend
  on anything**: `ExitHandler.__has_dependent_tasks` (`tasks_group.py:147-158`) raises
  `ValueError: exit_task cannot depend on any other tasks`. So `finalize` cannot receive the
  fingerprint as an input — it must read it from GCS.
- **No `from __future__ import annotations`** in `components.py` or `dag.py` (KFP introspects
  `__annotations__`).
- **Component bodies cannot reference helpers defined in `components.py`** — but they *can* import
  the installed library, and `finalize` already does (components.py:244-245).
- `ParallelFor(parallelism=)` and `set_retry(...)` args must be compile-time constants.
- Caps: 131,072 bytes per output parameter; 100 artifacts per task.
- **Adding `Output[...]` changes the cache key** — the output definition is part of the hashed
  interface. This invalidates every existing cache entry once, by design.
- `Output[HTML]` must be **self-contained** — images inlined as base64 data URIs.
- **Per-task `base_image` and `set_env_variable` both work, including on the exit task**, which can
  also declare artifacts (verified by compiling). So one task may carry a heavier image without
  imposing it on the other 24.
- **Nothing can be ordered after the exit task, and nothing outside a `TasksGroup` can depend on a
  task inside it** (verified): `report().after(finalize)` raises
  `ValueError: finalize does not exist.`, and `report().after(shard)` raises
  `InvalidTopologyException: Illegal task dependency across DSL context managers`. Any work that
  needs the merged results must live *inside* `finalize`.

### Web research informing this

- [Create, use, pass, and track ML artifacts | Kubeflow](https://www.kubeflow.org/docs/components/pipelines/user-guides/data-handling/artifacts/)
- [Configure execution caching | Google Cloud](https://cloud.google.com/vertex-ai/docs/pipelines/configure-caching) —
  cache key = input values + output definitions + component spec; `None` defers to per-task.
- [Vertex AI Pipelines: Catching Cache Complexity | Xebia](https://xebia.com/blog/vertex-ai-pipelines-catching-cache-complexity/) —
  external data changes are invisible to the cache; thread an explicit version parameter.
- [Structured logging | Cloud Logging](https://docs.cloud.google.com/logging/docs/structured-logging) —
  `severity` and `message` are lifted out of a stdout JSON line into real `LogEntry` fields.
- [View pipeline job logs | Vertex AI](https://cloud.google.com/vertex-ai/docs/pipelines/logging)

---

## Tasks

Ordered so the two confirmed bugs land first and independently.

### Task 1: Fix the caching stomp ← **confirmed live bug, smallest change**

**Files:** `src/bq_context/pipeline/submit.py`, `src/bq_context/cli.py`, `tests/test_pipeline.py`

**Step 1 — failing test.** The existing test inspects the compiled spec and cannot see this. Assert
at the SDK boundary instead:

```python
def test_submit_defers_caching_to_the_per_task_settings(monkeypatch) -> None:
    """Regression: a job-level bool blunt-overwrites every task's cachingOptions.

    _set_enable_caching_value (pipeline_jobs.py:85-101) rewrites
    task["cachingOptions"] for every task in every DAG, so the deliberate
    set_caching_options(False) on preflight was silently reset to True.
    """
    captured = {}
    # ... stub aiplatform.PipelineJob, capture kwargs
    submit_pipeline(...)
    assert captured["enable_caching"] is None
```

**Step 2** — run → FAIL.
**Step 3** — `submit.py:59` becomes `enable_caching: bool | None = None`; update the docstring,
which currently explains why `True` is safe and is now wrong. Add `bq-context submit --no-cache`
passing `False` (a global off is a legitimate escape hatch; a global `True` is not).
**Step 4** — run → PASS.
**Step 5 — commit:** `fix: stop job-level caching from overriding every per-task setting`

---

### Task 2: Stop discarding the report and figures

**Files:** `src/bq_context/pipeline/components.py`, `src/bq_context/runner/store.py`,
`tests/test_pipeline_cli_seam.py`

No new CLI surface — `score --report PATH` and `plot --plots-dir PATH` already exist and are simply
not passed.

**Write to `/tmp`, then upload to the stable GCS prefix.** Do **not** point `--plots-dir` at an
artifact path: artifact `.path` resolves to a `/gcs/...` gcsfuse mount, matplotlib's PNG writer is
not reliably sequential-only, and it would scatter human-facing output across per-job artifact
directories — regressing the stable-prefix decision in
`docs/plans/2026-09-22-bq-context-milestone-1.md:186-196`.

**Watch:** `ArtifactStore.write_text` (store.py:118-125) hard-codes
`content_type="application/json"` and takes `str`. PNGs need a `write_bytes` with a content type;
add it to the Protocol and both implementations.

**Step 1 — failing test** in `tests/test_pipeline_cli_seam.py`:

```python
def test_finalize_persists_the_report_and_figures() -> None:
    """Regression: every run rendered these and destroyed the container holding them."""
    argv = {a[1]: a for a in _invocations("finalize")}
    assert "--report" in argv["score"]
    assert "--plots-dir" in argv["plot"]
```

**Steps 2-4** — FAIL; implement (score → `/tmp/report.md`, plot → `/tmp/plots`, then upload to
`experiments/{id}/scoring/` and `experiments/{id}/plots/`); PASS.

**Step 5 — commit:** `fix: persist the report and figures finalize already generates`

---

### Task 3: Corpus fingerprint — hash the *shape*, never the bytes

**Files:** `src/bq_context/cli.py`, `src/bq_context/runner/planner.py`,
`src/bq_context/runner/models.py`, `src/bq_context/pipeline/{components,dag}.py`, tests

**This is the task most likely to be got wrong.** The obvious implementation — hash the ladder
`preflight` already computes — **would destroy the shard cache**. `_tier_profile` (cli.py:210-235)
reports `bytes = len(cache.all_detailed())`, and its own docstring warns "Byte deltas are a trap in
both directions: dataset timestamps and entry ids differ between tiers even when nothing else
does". `all_detailed()` is the raw capsule JSON including entry timestamps and `dataProfile` float
statistics, which drift as DataScans re-run. A fingerprint including `bytes` changes on nearly every
run, every shard misses cache, and a 15-minute resume becomes a 12-hour resweep.

The search-convergence probe (`_search_hits_by_tier`, cli.py:148-165) must also be excluded — it
drifts by design while the index warms, which is the entire reason it exists.

**Step 1 — failing tests, including the one that stops this rotting:**

```python
def test_fingerprint_ignores_capsule_bytes() -> None:
    """THE test. Capsule bytes carry entry timestamps and dataProfile floats that
    drift between runs; including them makes every shard miss cache."""
    a = [{"tier": 2, "tables": 15, "bytes": 119882, "profiled": 209, "terms": 18, "aspects": []}]
    b = [{**a[0], "bytes": 119999}]
    assert corpus_fingerprint(a) == corpus_fingerprint(b)

def test_fingerprint_moves_when_enrichment_shape_changes() -> None:
    a = [{"tier": 2, "tables": 15, "bytes": 119882, "profiled": 209, "terms": 18, "aspects": []}]
    assert corpus_fingerprint(a) != corpus_fingerprint([{**a[0], "terms": 24}])

def test_fingerprint_is_order_independent() -> None:
def test_fingerprint_is_stable_across_processes() -> None:   # no hash() / PYTHONHASHSEED
```

**Step 2** — FAIL.
**Step 3** — `corpus_fingerprint(ladder) -> str` in `runner/planner.py`: `sha256` over
`[[tier, tables, profiled, terms, sorted(aspects)] for r in sorted(ladder, key=tier)]`, truncated to
16 hex chars. The docstring must say *why* `bytes` and the probe are excluded.
**Step 4** — thread it: new `preflight --json PATH` emits `{"ladder": [...], "fingerprint": "..."}`;
`preflight` returns `NamedTuple("Preflight", [("fingerprint", str)])`; `dag.py` passes
`check.outputs["fingerprint"]` into `run_shard`; `run-shard` gains `--corpus-fingerprint` and
carries it in `ShardSpec` beside `code_version`.

> Pass it to the CLI rather than leaving it an unused input. Ruff's `select = ["ALL"]` will reject
> an unused parameter, `test_every_flag_a_component_passes_exists_on_the_cli` requires the flag to
> exist, and it belongs in the provenance record next to `code_version` anyway.

**Step 5** — recompile; confirm the shard task's `inputs.parameters` include `corpus_fingerprint`.

**Commit:** `fix: invalidate shard cache when the corpus changes, not only the code`

---

### Task 4: Artifacts on `preflight` and `finalize`

**Files:** `src/bq_context/pipeline/{components,dag}.py`, `src/bq_context/scoring/report.py`,
`tests/test_pipeline.py`, `tests/test_pipeline_cli_seam.py`

`preflight` gains `ladder: Output[Markdown]` + `tier_metrics: Output[Metrics]`.
`finalize` gains `merged: Output[Dataset]` + `report: Output[Markdown]` + `summary: Output[HTML]` +
`run_metrics: Output[Metrics]`.

**Three rules, all learned the hard way:**

1. **Populate artifacts before raising.** `finalize` must set `merged.uri`, all `.metadata` and
   `run_metrics` *then* `raise SystemExit(1)` for missing cells — otherwise the red runs, which are
   the ones worth inspecting, publish nothing.
2. **Always write the artifact file, even on the failure path.** `makedirs_recursively`
   (executor.py:103) creates only the parent directory. If `score` exits 1 (no merged results,
   cli.py:886-889) then `report.path` never exists and the UI shows a `system.Markdown` artifact
   pointing at nothing. Write a placeholder.
3. **Metrics are floats only.** `aspects` stays in the markdown ladder, not `tier_metrics`.

`report` is exactly `render_markdown`'s output via `score --report` — no figures, no new code.
`summary` needs a **new** `render_html(scores, figures, *, experiment_id) -> str` in
`scoring/report.py`; `render_markdown` (report.py:154-186) emits no image references, so "embed
whatever figures exist" is new rendering code, not free.

**Size check for `summary`:** `write_plots` renders at `dpi=130`, `figsize` up to `(12, 4.5)` —
~1560×585 px, 80–250 KB per PNG, +33% as base64. Render an embed-only set at `dpi=72` into a
`BytesIO` rather than base64-ing the full-size files, and never put base64 in artifact `.metadata`.

**Watch — this will break the seam tests silently.** `_invocations` calls
`components.preflight.python_func(**COMPONENT_ARGS["preflight"])` inside
`contextlib.suppress(BaseException)` (test_pipeline_cli_seam.py:84). New required params make that a
`TypeError` that is swallowed, yielding zero argv; only
`test_every_component_shells_out_at_least_once` fails, and it points at the harness rather than the
cause. Extend `COMPONENT_ARGS` with real artifact instances, e.g.
`dsl.Markdown(name="l", uri=str(tmp / "ladder.md"))`.

**Commit:** `feat: declare output artifacts on preflight and finalize`

---

### Task 5: Executive report — inside `finalize`, not a new component

**Files:** `src/bq_context/scoring/executive.py` (new), `src/bq_context/cli.py`,
`src/bq_context/pipeline/components.py`, `tests/test_executive.py`

**There is no legal place for a separate report or figures component.** The exit task cannot depend
on anything (`tasks_group.py:147-158`); a task before the `ExitHandler` would read the *previous*
run's merged results; `dsl.ExitHandler` is a `TasksGroup`, not a task, so nothing can `.after()` it.
This is the same constraint that already collapsed merge/score/plot into one component
(components.py:200-208). The executive report is therefore a fourth step inside `finalize`.

Likewise, **`finalize` cannot receive the fingerprint as an input.** `preflight` writes
`experiments/{id}/preflight.json` via `store_for`; `finalize` reads it. It belongs in the report as
the provenance for the whole sweep.

**Content:** verdict · approach comparison · figures (base64) · interpretation · reliability.

**The interpretation must carry the caveats, or the report is worse than nothing:**
- Tier response for the three search approaches is **invalid** when `assess_search_convergence`
  flags a spread, or when the run predates the shard-ordering fix
  (`docs/notes/full-run-results.md:67-73`).
- **Ceiling effect** — if every approach exceeds ~0.9 mean final recall, say so: there is no
  headroom, so a flat tier response is uninformative rather than a finding (`metrics.py:281-291`).
- **Reranker tokens exclude agent-side LLM calls**, understating `bq_tools` and `context_prefilter`
  (`sink.py:130-135`).
- Mixed `code_version`, if present.

Reuse `render_markdown`, `write_plots`, `summarize_by_approach`, `tier_response`, `category_ndcg`,
`load_summaries`, `assess_search_convergence`.

> `ApproachSummary.ndcg_iqr` is computed and rendered nowhere. Surfacing it is a two-line addition
> to `_recall_section` in `report.py` and is **not** part of this task — it cannot go in a `Metrics`
> artifact (tuple, not float). Do it separately or not at all.

**Tests** (the interpretation logic is pure):

```python
def test_a_saturated_corpus_is_called_out() -> None:
def test_a_flagged_convergence_warning_invalidates_the_tier_section() -> None:
def test_the_token_caveat_appears_whenever_bq_tools_is_present() -> None:
def test_the_html_is_self_contained() -> None:
    assert 'src="data:image/png;base64,' in html
    assert "file://" not in html and 'src="/' not in html
```

Add `bq-context report -e ID --html PATH` so it is usable without the pipeline.

**Commit:** `feat: executive report with figures and an honest interpretation`

---

### Task 6: Structured logging

**Files:** `src/bq_context/runtime.py`, `src/bq_context/cli.py`, `Dockerfile`,
`src/bq_context/pipeline/components.py`, `tests/test_logging_setup.py`

Cloud Logging lifts `severity` and `message` from a stdout JSON line into real `LogEntry` fields and
leaves the rest queryable under `jsonPayload` — turning
`jsonPayload.shard_id="tier3__bq_tools"` into a filter instead of a regex.

**Put `json_log()` in `bq_context/runtime.py` and import it in component bodies.** The
"hermetic body" constraint means a body cannot reference helpers in `components.py`; it *can* import
the installed library, which `finalize` already does (components.py:244-245). Do not duplicate a
helper per body. Note this cuts against components.py:5-8 ("every component shells out rather than
importing the library") — `finalize` is already the exception; document the rule as "shell out for
work, import for plumbing" rather than adding a third convention.

**Two traps:**
- `cli.py:83-87` pins `stream=sys.stderr`, and Vertex tends to tag container stderr as `ERROR`,
  which fights the `severity` field. The JSON formatter must switch to `stdout`.
- **`logging.basicConfig` is a no-op once the root logger has handlers**, and
  `executor_main.py:25-28` calls it before the component body runs. Configuring logging *inside* a
  component body will silently do nothing. The CLI subprocess is a fresh process, so cli.py:83 is
  fine.
- Honest framing: `executor_main.py:96` dumps the whole `executor_input` at INFO as plain text, so
  "stdout is now structured" is not literally true — our lines are structured, interleaved with the
  executor's.

Fields worth emitting — the ones that were painful to recover: `shard_id`, `experiment_id`, `tier`,
`approach`, `code_version`, `corpus_fingerprint`, `cells_done`, `cells_total`, `cache_warm_s`,
`abort_reason`.

**Commit:** `feat: structured JSON logging for Cloud Logging`

---

### Task 7: Per-task caching, now that it can take effect

**Files:** `src/bq_context/pipeline/dag.py`, `tests/test_pipeline.py`

Only meaningful after Task 1. Set every task explicitly with the reason beside it:

| task | caching | why |
|---|---|---|
| `validate_config` | **off** | identity and IAM change outside the pipeline |
| `ensure_infra` | off | corpus state is external |
| `preflight` | off | a cached "enrichment is fine" is worse than useless |
| `plan_shards` | on | pure function of its inputs |
| `run_shard` | on | safe **only** because `code_version` *and* `corpus_fingerprint` are inputs |
| `finalize` | off | must run on every attempt |

Add a test asserting the intended setting for every task, so a future change is deliberate.

**Commit:** `feat: deliberate per-task caching`

---

### Task 8: PaperBanana diagrams — `finalize` on its own report image

**Files:** `Dockerfile.report` (new), `Makefile`, `src/bq_context/pipeline/{components,dag}.py`,
`src/bq_context/scoring/figures.py` (new), `src/bq_context/cli.py`, `tests/test_pipeline.py`

Last because nothing depends on it, but it is now a real design rather than a gated maybe.

**Why it is not a new component.** Verified by compiling against `kfp==2.17.0` — a separate report
component cannot be sequenced after the results exist:

```
report().after(finalize)   ValueError: finalize does not exist.
report().after(shard)      InvalidTopologyException: Illegal task dependency
                           across DSL context managers
```

Nothing may depend on the exit task, and nothing outside a `TasksGroup` may depend on a task inside
it. A new component compiles only as an unordered *sibling*, which would run concurrently with the
sweep and report on absent data. The exit-task slot is the only correctly-ordered place and there
can be exactly one — so the figure work goes **inside `finalize`**, which already merges, scores
and plots.

**Per-task images and env do work** — also verified by compiling:

```
exec-finalize   image=.../report:sha256-abc   env=[('GOOGLE_GENAI_USE_VERTEXAI','0')]
exec-shard      image=.../runner:test
exit-task artifacts: ['report', 'summary']
```

So `finalize` takes `base_image=REPORT_IMAGE` while the 24 shards keep the lean runner image. The
earlier objection that PaperBanana would bloat the image every component shares does not survive
this — it lands in one task's image only.

**Division of labour, and it is not negotiable:**

| figure | tool | why |
|---|---|---|
| `discovery_vs_final`, `recall_vs_tier`, `latency_cost` | **matplotlib** | they plot *measured data*; bars whose heights are not derived from the numbers are a correctness hazard in a benchmark report, and a generative model is not reproducible run to run |
| architecture / methodology diagram | **PaperBanana** | what it is actually for, and the house style already exists in `docs/images/` |

**Never** point PaperBanana at anything with an axis.

**Build.** `Dockerfile.report` is `FROM <runner>:<sha>` plus `uv pip install paperbanana` — it
inherits `bq_context` (needed for merge/score/plot) and adds figure tooling, so the two images
cannot drift on the library. `make image` gains a `report` target; `image-ref` gains a variant.
`components.py` resolves `REPORT_IMAGE = os.environ["BQ_CONTEXT_REPORT_IMAGE"]` beside
`RUNNER_IMAGE`, with the same deliberate `KeyError`.

**The secret must not be a task env var.** `set_env_variable("GOOGLE_API_KEY", ...)` bakes the value
into the compiled pipeline spec *and* the `PipelineJob` resource, readable by anyone with viewer
access. Read it from Secret Manager at runtime inside the figure step instead.

**Scope the env to the subprocess, not the task.** `Dockerfile:70` sets
`GOOGLE_GENAI_USE_VERTEXAI=true`, and `ExperimentConfig.configure_adk_env()` sets it again
in-process. Rather than fight that, `finalize` shells out to a new `bq-context figures` with
`env={**os.environ, "GOOGLE_GENAI_USE_VERTEXAI": "0"}` — consistent with how `finalize` already
invokes merge/score/plot, and it cannot leak into the scoring process.

**Gating.** A `refresh_figures: bool = False` input on `finalize` (not a `dsl.If` — a conditional
group cannot be depended on). Default off: LLM image generation is slow, paid, and
non-deterministic, and architecture diagrams do not change between runs. The report embeds whatever
is already in `experiments/{id}/figures/`, so a default run reuses the last set rather than showing
nothing. `finalize` already has caching off, so no extra cache handling is needed.

**Fail fast, and never fail the sweep.** Extend `validate-config` to check
`secretmanager.versions.access` so a missing grant surfaces in 30 seconds rather than 90 minutes
into the exit task. If the secret is absent at figure time, skip with a clear message — `finalize`
is the only task allowed to turn a run red, and it must do so only for missing cells.

**Watch — one existing test must change, carefully.**
`tests/test_pipeline.py:136-139` asserts `len(images) == 1`. That becomes two. Preserve the
*intent*, which is that no image floats:

```python
def test_every_image_is_sha_pinned(spec) -> None:
    """Two images now: the lean runner for shards, the report image for finalize.
    The assertion that matters is unchanged — a floating tag makes the cache lie."""
    images = {c["container"]["image"] for c in spec["deploymentSpec"]["executors"].values()}
    assert images == {components.RUNNER_IMAGE, components.REPORT_IMAGE}
    assert not any(i.endswith(":latest") for i in images)
```

**Commit:** `feat: PaperBanana diagrams on a dedicated report image`

**The zero-cost alternative, if this stalls:** keep generating diagrams offline with the MCP skill
as `docs/images/` was, and have the report embed whatever is in `experiments/{id}/figures/`. The
embedding code from Task 5 is the same either way, so Task 8 can be dropped at any point without
rework.

---

## Verification

```bash
uv run ruff format --check . && uv run ruff check . && uv run ty check src/ && uv run pytest
# Both image vars are required from Task 8 onward — components.py resolves each at
# import time with a deliberate KeyError, so a missing one fails the compile loudly.
BQ_CONTEXT_IMAGE=$(uv run bq-context image-ref) \
BQ_CONTEXT_REPORT_IMAGE=$(uv run bq-context image-ref --report) \
uv run python -c "
from pathlib import Path; from bq_context.pipeline.compilation import compile_pipeline
compile_pipeline(Path('/tmp/p.yaml'))"
uv run bq-context submit --profile smoke -e artifacts-smoke
```

**Checks that matter more than the tests passing:**

1. **The Vertex UI renders them.** `preflight` shows a Markdown ladder and Metrics; `finalize` shows
   the report, the HTML summary with inline figures, and metrics. Cannot be asserted offline.
2. **Nothing is discarded.** `gcloud storage ls -r gs://…/experiments/artifacts-smoke/` contains
   `scoring/report.md`, `scoring/executive.html`, `plots/*.png`, `preflight.json`.
3. **Artifacts survive a red run.** Submit with a deliberately incomplete sweep so `finalize` exits
   1, and confirm the artifacts are still populated. This is the `sys.exit` trap.
4. **The fingerprint gates the cache without destroying it.** Submit twice unchanged → shards
   cache-hit *(if they miss, the fingerprint is unstable — check `bytes` crept back in)*. Change one
   tier's enrichment → shards re-run.
5. **Per-task caching now holds.** Submit twice identically; `preflight` must **not** report
   "Cached" in the UI.
6. **Logs are queryable by field:** `gcloud logging read 'jsonPayload.shard_id="tier3__bq_tools"'`
7. **The report is honest.** On full-01's data it must state that the tier response for the search
   approaches is invalid and that the corpus is saturated. A report reading as though the enrichment
   question were answered is a failed Task 5.
8. **Only `finalize` carries the heavy image** (Task 8). In the compiled spec, every `exec-*` except
   `exec-finalize` must be the runner image — if a shard picks up the report image, the 24 shards
   are pulling PaperBanana for nothing.
9. **No secret in the spec.** `grep -i "api_key\|AIza" /tmp/p.yaml` must find nothing. The key is
   read from Secret Manager at runtime precisely so it never reaches the compiled spec or the
   `PipelineJob` resource.
10. **The data charts are still matplotlib.** Regenerate with `refresh_figures=true` and confirm
    `discovery_vs_final.png`, `recall_vs_tier.png` and `latency_cost.png` are byte-identical to a
    `refresh_figures=false` run. If a PaperBanana call has touched a chart with an axis, that is the
    failure this split exists to prevent.

## Out of scope

Artifacts on `validate_config`, `ensure_infra`, `run_shard`, `plan_shards`; `dsl.Collected` over
shards (100-artifact cap, and the exit task cannot read them); Vertex Experiments / TensorBoard;
replacing matplotlib with PaperBanana for quantitative charts.
