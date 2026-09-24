# GCS Layout: Provisioned Assets, Versioned Runs, and Run Metadata

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this task-by-task.
> First action in execution: copy this file to `docs/plans/2026-09-24-gcs-layout.md`.

**Goal:** Make the bucket say what was provisioned, what each run produced, and
which corpus produced it — without breaking resume.

**Architecture:** Split the experiment prefix into **durable state** (shards and
merged, at stable paths resume depends on) and **per-execution artifacts** (report,
HTML, plots, manifest, under `runs/{run_id}/`, never overwritten). Add a
`corpus/{fingerprint}/` area recording what provisioning created. A `run_id`
generated once at submission ties every task's output together.

**Tech Stack:** GCS via the existing `ArtifactStore` protocol · KFP v2 · uv / ruff /
pytest / ty

---

## Context

The bucket today is four things at the top level, only two of them deliberate:

```
gs://hybrid-vertex-bq-context/
├── _validate_config      3 bytes, written to the ROOT by cli.py:798
├── build-source/         dead: left over from the in-pipeline build, reverted in #32
├── experiments/          38.9 MB
└── pipeline_root/        11.1 MB, KFP-owned
```

Three concrete problems, all observed on real runs today.

**1. Re-running an experiment id destroys the previous run's outputs.**
`publish.py:40,55,129` write to `{experiment_prefix}/scoring/report.md`,
`/plots/*.png` and `/scoring/executive.html`. `hard-full-01` ran three times — the
original, a cache-hit no-op, and the `--no-cache` recovery — and only the last
report survives. The failed run's report, which was the evidence for the
completeness bug, is gone.

**2. Nothing records what was provisioned.** The corpus exists only as a Python
list in `corpus/setup.py`. Preflight's ladder goes to `/tmp/preflight.json`
(`components.py:241`) and a per-job KFP artifact. So the corpus a run measured
against is not recoverable from the bucket.

**3. Nothing detects a reused experiment id across corpora.** `experiment_prefix`
is derived from `experiment_id` alone — deliberately, so resume works
(`resume.py:47-52`). But resuming `hard-full-01` after switching `CORPUS_PROFILE`
would silently mix two corpora into one `results.jsonl`, and nothing would say so.

**The constraint that shapes everything:** shard data must stay at a stable path.
`resume.py:57` builds it from `experiment_id`, and `merge.py:69` globs it. Version
those and resume breaks. Derived artifacts have no such constraint.

Measured sizes decide what gets versioned:

| artifact | size | versioned? |
|---|---|---|
| `merged/results.jsonl` | 4.8 MB | no — deterministic from shards |
| `scoring/executive.html` | 240 KB | yes |
| `plots/*.png` | 177 KB | yes |
| `scoring/report.md` | 2.3 KB | yes |

~420 KB per execution.

---

## Target layout

```
gs://{bucket}/
├── corpus/{fingerprint}/           what provisioning created
│   ├── manifest.json               tables, sources, descriptions, profile, prefix
│   ├── ladder.json                 preflight's per-tier profile
│   └── provisioned.json            when, by whom, resource prefix, CORPUS_PROFILE
│
├── experiments/{experiment_id}/
│   ├── experiment.json             first corpus + code seen; the collision guard
│   ├── shards/{tier}__{approach}/  STABLE — resume and merge read this
│   │   ├── attempt-NNNN.jsonl
│   │   ├── summary-NNNN.json
│   │   └── _SUCCESS | _FAILED
│   ├── merged/                     STABLE — regenerated, deterministic
│   │   ├── results.jsonl
│   │   └── missing.json
│   └── runs/{run_id}/              IMMUTABLE per execution
│       ├── manifest.json           run_id, job, code_version, fingerprint, counts
│       ├── scoring/report.md
│       ├── scoring/executive.html
│       └── plots/*.png
│
├── pipeline_root/                  KFP-owned; left alone
└── _scratch/validate_config        the writability probe, off the root
```

`run_id` is `{UTC timestamp}-{code_version}`, e.g. `20260924T164612Z-14b273a`.
Sortable, meaningful without a lookup, and works for local runs that have no
Vertex job id — the job id goes in the manifest instead.

---

## Tasks

### Task 1: `run_id` as a pipeline parameter

**Files:** `src/bq_context/cli.py`, `src/bq_context/pipeline/{dag,components}.py`,
`tests/test_submit.py`

Every task must agree on the run id or artifacts scatter across two folders, so it
is generated **once** at submission and threaded through, exactly like
`experiment_id`.

```python
def new_run_id(code_version: str) -> str:
    """Sortable, unique per execution, meaningful without a lookup."""
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{code_version}"
```

`submit-pipeline` generates it and sends it; `finalize` takes it as a parameter.
Local `score`/`plot`/`report` accept `--run-id`, defaulting to a fresh one.

**Watch:** `test_submit.py::test_every_pipeline_parameter_is_sent_or_deliberately_defaulted`
will fail until the CLI sends it. That is the guard working — do not add it to
`DELIBERATE_DEFAULTS`.

**Tests:** ids from two calls differ and sort chronologically; the id contains the
code version; `submit-pipeline` sends it.

**Commit:** `feat: a run id that identifies one execution`

---

### Task 2: Publish derived artifacts under `runs/{run_id}/`

**Files:** `src/bq_context/pipeline/publish.py`, `src/bq_context/pipeline/components.py`,
`tests/test_publish.py`

The three `publish_*` functions gain a `run_id` and write under
`{experiment_prefix}/runs/{run_id}/…`. Add `run_prefix(experiment_id, run_id)`
beside `experiment_prefix` in `resume.py` rather than formatting paths inline —
`publish.py` already has three call sites that would drift.

`merged/` and `shards/` are **not** touched. `components.py:523` sets
`merged.uri` to the stable merged path and stays as it is.

**Tests:** two run ids produce two folders and neither overwrites the other; the
merged path is unchanged by a new run id; a `run_id` containing a slash is
rejected rather than silently nesting.

**Commit:** `feat: per-execution artifact folders`

---

### Task 3: The run manifest

**Files:** `src/bq_context/pipeline/publish.py`, `src/bq_context/runner/models.py`,
`tests/test_publish.py`

`runs/{run_id}/manifest.json` is what makes a run self-describing:

```json
{
  "run_id": "20260924T164612Z-14b273a",
  "experiment_id": "hard-full-01",
  "code_version": "14b273a",
  "corpus_fingerprint": "13f9fcb47deb5c32",
  "resource_prefix": "bigquery_context_hard",
  "corpus_profile": "hard",
  "profile": "full",
  "tiers": [0, 1, 2, 3], "runs": 5, "question_limit": 0,
  "expected": 3000, "present": 3000, "missing_count": 0,
  "pipeline_job": "projects/…/pipelineJobs/bq-context-factorial-20260924164612",
  "written_at": "2026-09-24T17:41:03Z"
}
```

Reuse `MergeResult` (`merge.py:44`) for the counts rather than recomputing them.

**Tests:** the manifest round-trips; a run with missing cells still writes one
(a failed run is exactly when you want it); the fingerprint matches the shard
summaries.

**Commit:** `feat: a manifest describing each run`

---

### Task 4: Record what was provisioned

**Files:** `src/bq_context/cli.py` (`ensure-infra`, `preflight`),
`src/bq_context/corpus/setup.py`, `tests/test_corpus.py`

`ensure-infra` writes `corpus/{fingerprint}/manifest.json` and `provisioned.json`;
`preflight` writes `ladder.json`. Both already compute everything needed —
`corpus_fingerprint()` is in `planner.py:42` and the ladder in `cli.py:1007`.

Keyed by fingerprint, not by resource prefix: a prefix is reused as enrichment
changes and would overwrite, while fingerprints accumulate. This is the same
identifier now stored on every cell and in the BigQuery sink, so
`corpus/{fingerprint}/` is where a reader lands after a `GROUP BY`.

**Chicken-and-egg to handle deliberately:** `ensure-infra` cannot know the
fingerprint before provisioning, since it hashes the provisioned state. Write the
manifest from `preflight`, which runs after and already computes it, and have
`ensure-infra` write only `provisioned.json` under the *resource prefix* it used.
Do not invent a second fingerprint function.

**Tests:** the manifest lists every table in the selected profile; provisioning
twice is idempotent; the fingerprint in the path matches `ladder.json`'s.

**Commit:** `feat: record the provisioned corpus in GCS`

---

### Task 5: Warn when an experiment id changes corpus

**Files:** `src/bq_context/runner/resume.py`, `src/bq_context/cli.py`,
`tests/test_resume.py`

`experiments/{id}/experiment.json` records the first `corpus_fingerprint` and
`code_version` seen. `run-shard` compares before writing:

```
WARN  hard-full-01 was last run against corpus 861648cc513c3862 and is now
      13f9fcb47deb5c32. Resuming will mix two corpora in one results file.
      Use a new --experiment-id, or delete the existing shards.
```

A warning, not an error: re-running after a corpus repair is legitimate, and
`finalize` is the only task allowed to turn a run red.

**Two things to get right.** A changed `code_version` is *expected* — it is what
invalidates the shard cache — so it is informational, not a warning; only a corpus
change is loud. And with 24 shards the warning fires 24 times; that is acceptable
and better than checking somewhere that standalone `run-shard` does not reach.

**Tests:** same fingerprint is silent; a changed one warns and names both; a first
run writes the file and says nothing; a changed `code_version` alone does not warn.

**Commit:** `feat: warn when an experiment id switches corpus`

---

### Task 6: Clear the top level

**Files:** `src/bq_context/cli.py:798`, docs

`_validate_config` moves from the bucket root to `_scratch/validate_config`.
`build-source/` is dead — it served the in-pipeline build reverted in #32, and
`grep` finds no reference in `src/`, the `Makefile` or `cloudbuild.yaml`. Delete
the prefix by hand; do not add cleanup code for a one-off.

`pipeline_root/` stays where it is. It is KFP's, `cli.py:1588` passes it, and
moving it buys tidiness at the cost of orphaning every existing job's artifacts.

**Commit:** `chore: keep the bucket root clean`

---

### Task 7: Documentation

**Files:** `experiments/README.md`, `docs/notes/gcs-layout.md` (new),
`docs/notes/README.md`

The note records the layout *and the reasoning*: why shards are stable, why
`merged` is not versioned, why the corpus is keyed by fingerprint. Someone
reproducing this on their own data needs the rule, not the tree.

**Commit:** `docs: the GCS layout and why it is shaped this way`

---

## Verification

```bash
make check
```

Then two executions of one experiment, which is the behaviour this exists for:

```bash
export RESOURCE_PREFIX=bigquery_context_hard CORPUS_PROFILE=hard
bq-context submit-pipeline --profile smoke -e layout-check --skip-infra
bq-context submit-pipeline --profile smoke -e layout-check --skip-infra
gcloud storage ls -r gs://$BUCKET/experiments/layout-check/runs/
```

**Checks that matter more than the tests passing:**

1. **Two runs, two folders, both intact.** The first execution's `report.md` must
   still be readable after the second. This is the failure that motivated the
   plan — `hard-full-01` lost two of its three reports.
2. **Resume still works.** The second execution must report shards already
   complete and re-run nothing. If it re-runs, a stable path was versioned by
   mistake and the plan has broken the thing it promised not to.
3. **`merged/results.jsonl` is not duplicated** into either run folder.
4. **The corpus area matches reality.** `corpus/13f9fcb47deb5c32/manifest.json`
   lists 24 tables and `ladder.json` shows `terms=18` at tiers 2-3, matching what
   `preflight` prints.
5. **The collision warning fires.** Re-run `layout-check` with the baseline
   prefix and confirm the warning names both fingerprints — then confirm it does
   *not* fire on an unchanged rerun.
6. **Old experiments still read.** `full-01` predates this layout; `merge` and
   `score` against it must still work, because nothing migrates them.

## Out of scope

Migrating existing experiment prefixes — the old runs keep their shape and the
note says so. Moving `pipeline_root`. A retention or lifecycle policy on
`runs/` — worth having eventually, but 420 KB per execution is not yet a problem.
Deleting the stale `t/` and `verify-tier*` prefixes, which is tidying, not layout.
