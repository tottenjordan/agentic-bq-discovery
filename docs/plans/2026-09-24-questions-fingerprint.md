# Bring-Your-Own Questions, Without Breaking the Shard Cache

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this task-by-task.
> First action in execution: copy this file to `docs/plans/2026-09-24-questions-fingerprint.md`.

**Goal:** Let someone run the factorial against their own question set without
forking and rebuilding the image — and make the shard cache notice when they do.

**Architecture:** `submit-pipeline --questions` resolves the set, fingerprints
it, and snapshots it to `experiments/{id}/questions.json`. The fingerprint
becomes a `run_shard` cache-key input beside `code_version` and
`corpus_fingerprint`; shards read the snapshot and abort if it no longer matches.
`preflight` gains a gate that rejects questions naming tables the corpus does not
have.

**Tech Stack:** KFP v2 · GCS via `ArtifactStore` · Typer · uv / ruff / pytest / ty

---

## Context

The repo's purpose is an experimentation framework others can point at their own
data. Everything a user would want to vary is already reachable — corpus profile,
resource prefix, tiers, models, question count — except the questions.

Verified on `main` (`57feee6`): `run-shard` the CLI accepts `--questions`, but
the `run_shard` **component never passes it**, and `dag.py` has no questions
parameter. Every pipeline shard falls back to `/app/experiments/questions.json`,
baked in by `COPY experiments/ ./experiments/`. To change the questions you fork,
edit, rebuild, submit.

That is deliberate, and the Dockerfile says why: baking it in means the question
set is versioned with the code, so the git SHA in `code_version` describes the
questions too — and that SHA is part of the KFP cache key.

**Which is exactly what makes the naive fix dangerous.** Add
`--questions gs://…` and thread it through, and swapping the file then
resubmitting at the same commit returns cached cells scored against the *old*
questions. Green run, wrong numbers, nothing to notice. Same failure family that
`code_version` and `corpus_fingerprint` were each added to close.

Three consumers must agree on the set, or the run fails for the wrong reason:

| | what it uses questions for |
|---|---|
| `plan-shards` | the fan-out |
| `run-shard` | which cells to plan |
| `merge` | which cells are *missing* → `require_complete` → turns the run red |

If a custom set reaches the shards but not `finalize`, merge expects the
built-in 25, reports phantom missing cells, and fails a healthy run. `merge_args`
already carries a warning about precisely this for `question_limit`.

**One non-obvious property.** `--limit` takes a deterministic prefix —
`list(questions)[:limit]` in `cli.py` — so which questions a smoke run measures
depends on **file order**. The questions fingerprint must therefore be
order-*sensitive*, the opposite of `corpus_fingerprint`, which sorts its ladder.
Reordering the file with no edits changes what `--limit 3` measures, and that
must invalidate the cache.

---

## Tasks

### Task 1: `questions_fingerprint`

**Files:** `src/bq_context/runner/planner.py`, `tests/test_fingerprint.py`

Beside `corpus_fingerprint` (`planner.py:42`), and shaped like it — sha256 over a
canonical form, truncated to 16 hex chars. `hashlib`, never the builtin `hash()`:
that one is salted per process, so the submitting CLI and the shard would
disagree.

```python
def questions_fingerprint(questions: Mapping[str, Mapping]) -> str:
    """Hash of the question set's meaning, order included.

    Order is content here, unlike in `corpus_fingerprint`. `--limit` takes a
    deterministic prefix, so reordering the file changes which questions a smoke
    or pilot run measures while editing nothing.

    Within a relevance list order carries nothing, so those are sorted: a
    reordered `must_have` is the same experiment and must stay a cache hit.
    """
    canonical = [
        [
            qid,
            str(q.get("category", "")),
            str(q.get("question", "")),
            *(
                sorted(map(str, (q.get("relevance") or {}).get(k, [])))
                for k in ("must_have", "nice_to_have", "distractor")
            ),
        ]
        for qid, q in questions.items()
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]
```

**Tests:** identical sets match; an edited question text differs; **reordering
the file differs**; reordering a `must_have` list does *not*; a changed
`distractor` differs (traps are the point of that field); stable across
processes (compute in a subprocess and compare).

**Commit:** `feat: a fingerprint for the question set`

---

### Task 2: `_load_questions` reads `gs://`

**Files:** `src/bq_context/cli.py:218`, `tests/test_cli.py`

Shards will read the snapshot from the bucket, so the loader needs a URI as well
as a path. Reuse `store_for` (`runner/store.py:169`); do not add a second GCS
client.

```python
def _load_questions(source: str | Path) -> dict[str, dict[str, Any]]:
    text = (
        store_for(str(source).rsplit("/", 1)[0]).read_text(str(source).rsplit("/", 1)[1])
        if str(source).startswith("gs://")
        else Path(source).read_text()  # keep the existing "not found" Exit(2)
    )
```

Keep the `Exit(2)` with the path in the message — a missing questions file is a
typo, and the message is the whole diagnosis.

**Tests:** a local path still loads; a `gs://` URI loads through a `LocalStore`
seam; a missing file exits 2 and names the source; a list-shaped file and a
`{"questions": [...]}`-shaped file both parse (the existing behaviour).

**Commit:** `feat: load a question set from GCS`

---

### Task 3: preflight rejects questions the corpus cannot answer

**Files:** `src/bq_context/cli.py` (`preflight`), `tests/test_cli.py`

The gate this project keeps needing. A question naming a table that does not
exist scores 0 recall forever and reads as a genuine null result — the same
shape as the `lookupContext`-returns-empty trap that motivated preflight in the
first place.

Validate every name in all three relevance lists against the provisioned corpus.
`distractor` included: a distractor that does not exist is not a distractor, it
is a typo, and it silently disarms a trap question.

Reuse `corpus_manifest(setup)` from `corpus/manifest.py` — it already returns
`{"tables": [{"name": …}]}`. Use `difflib.get_close_matches` for the hint.

```
questions: 25 loaded, fingerprint a1b2c3d4

FAIL  trap-q2 references austin_bikeshare_station -- not in the corpus.
      Did you mean austin_bikeshare_stations?
FAIL  2 questions reference 3 unknown tables.
```

**Watch:** append to the existing `problems` list so it exits 1 through the same
path; do not add a second failure mechanism.

**Tests:** a clean set passes; an unknown `must_have` fails and names the
question; an unknown `distractor` fails too; the suggestion appears for a near
miss and is absent for a wild one; the default set validates against the default
corpus (a guard on our own data).

**Commit:** `feat: preflight rejects questions naming tables the corpus lacks`

---

### Task 4: snapshot and fingerprint at submit time

**Files:** `src/bq_context/cli.py` (`submit_pipeline_cmd`), `tests/test_submit.py`

`submit-pipeline` gains `--questions`, defaulting to `DEFAULT_QUESTIONS`. It
loads them, computes the fingerprint, and writes a snapshot to
`experiments/{experiment_id}/questions.json` before compiling.

The snapshot is the point. It is immutable for the life of the run, it cannot
change under a sweep, and it archives the question set with the experiment —
the same move `corpus/{fingerprint}/` makes for the corpus.

```
profile   smoke
questions 25 from experiments/questions.json  fingerprint a1b2c3d4
snapshot  gs://…/experiments/layout-check/questions.json
```

Compute and send the fingerprint **always**, including for the default set. It
costs nothing and gives a second, independent guard beside `code_version`.

**Watch:** `test_submit.py::test_every_pipeline_parameter_is_sent_or_deliberately_defaulted`
fails until the CLI sends the new parameter. That is the guard working — do not
add it to `DELIBERATE_DEFAULTS`.

**Tests:** the submitted parameters carry a non-empty fingerprint; the snapshot
lands at the expected path and round-trips; two different question files produce
different fingerprints in the submission.

**Commit:** `feat: snapshot and fingerprint the question set at submission`

---

### Task 5: thread it to the shards, and verify on arrival

**Files:** `src/bq_context/pipeline/{dag,components}.py`,
`src/bq_context/cli.py` (`run_shard`), `tests/test_pipeline.py`, `tests/test_cli.py`

`dag.py` gains `questions_fingerprint: str = ""`, passed to `run_shard` as an
explicit input — **this is the cache-key change and the reason the feature is
safe** — and to `finalize`.

The `run_shard` component adds two flags:

```text
"--questions", f"{out}/{experiment_prefix(experiment_id)}/questions.json",
"--questions-fingerprint", questions_fingerprint,
```

`run-shard` the CLI gains `--questions-fingerprint`. After loading, it
recomputes and **aborts on mismatch** rather than running:

```
FAIL  the question set at gs://…/questions.json has changed since submission
      (expected a1b2c3d4, found 9f8e7d6c). Refusing to mix two question sets
      in one results file.
```

Abort, not warn — unlike the corpus warning. A changed corpus still produces
comparable cells for the same questions; a changed question set produces cells
for *different questions* under the same experiment id, which merge then reads
as both missing and unexpected.

Empty fingerprint means "not checked" (a local `run-shard`), same convention as
`corpus_fingerprint` in `note_experiment_identity`.

**Tests:** matching fingerprints run; a mismatch exits non-zero and names both;
an empty expected fingerprint skips the check; the compiled spec lists
`questions_fingerprint` among `run_shard`'s inputs (mirror
`test_preflight_component_takes_no_service_account`'s exhaustive style).

**Commit:** `feat: the shard cache notices a changed question set`

---

### Task 6: `merge` uses the same set the shards did

**Files:** `src/bq_context/pipeline/publish.py` (`merge_args`),
`src/bq_context/pipeline/components.py` (`finalize`), `tests/test_publish.py`

`merge_args` gains the snapshot URI and passes `--questions`. Without this the
exit task computes expected cells from the built-in 25 and fails a healthy run
for phantom missing cells — the failure `merge_args`' docstring already warns
about for `question_limit`.

**Backward compatibility matters here.** `full-01` and `hard-full-01` predate the
snapshot and have no `questions.json`. Fall back to the packaged default when the
snapshot is absent, so `merge`/`score` against an old experiment keeps working.

**Tests:** the argv carries `--questions` when a snapshot exists and omits it
when not; an old experiment with no snapshot still merges.

**Commit:** `feat: the exit task scores the questions the sweep ran`

---

### Task 7: record which questions produced the results

**Files:** `src/bq_context/runner/resume.py` (`note_experiment_identity`),
`src/bq_context/pipeline/publish.py` (`run_manifest`),
`tests/test_resume.py`, `tests/test_publish.py`

`experiment.json` gains `questions_fingerprint` beside `corpus_fingerprint`, with
the same first-write-wins rule and the same warning on change — extend
`note_experiment_identity` rather than adding a second function.

`runs/{run_id}/manifest.json` gains `questions_fingerprint` and
`question_count`, so a run folder says what was asked as well as what was
measured.

**Tests:** a changed question set warns and names both fingerprints; the first
value is not overwritten; the manifest carries both fingerprints; an experiment
written before this field still reads (absent, not crash).

**Commit:** `feat: record the question set on the experiment and the run`

---

### Task 8: documentation

**Files:** `experiments/README.md`, `docs/notes/gcs-layout.md`,
`docs/notes/README.md`

`experiments/README.md` gets a **Bring your own questions** section: the file
format (`id`, `category`, `question`, `relevance.{must_have,nice_to_have,distractor}`),
the four categories and why the mix matters, `--questions` on both
`submit-pipeline` and the local commands, and the preflight gate.

`docs/notes/gcs-layout.md` gains `experiments/{id}/questions.json` in the tree
and a line on why it is snapshotted rather than referenced.

**Commit:** `docs: how to run the factorial on your own questions`

---

## Verification

```bash
make check
```

Then a real run with a genuinely custom set — this is the whole feature:

```bash
# three questions, one deliberately naming a table that does not exist
uv run bq-context preflight --tier 3 --questions /tmp/mine.json   # must FAIL, naming it
# fix the typo, then
uv run bq-context submit-pipeline --profile smoke -e byoq-01 --skip-infra \
    --questions /tmp/mine.json
```

**Checks that matter more than the tests passing:**

1. **The gate catches a bad table name** before any money is spent, and the
   suggestion is useful.
2. **The snapshot exists** at `experiments/byoq-01/questions.json` and matches
   the submitted fingerprint.
3. **The cells are the custom questions'** — `merged/results.jsonl` carries the
   new question ids, not `single-q1`.
4. **The cache invalidates on a question change.** Resubmit `byoq-01` at the
   same commit with one question edited: shards must **re-run**, not come back
   `SKIPPED`. This is the entire point of the fingerprint — if they skip, the
   feature is unsafe and the work is not done.
5. **The cache still hits when nothing changed.** Resubmit unmodified: shards
   `SKIPPED`. A fingerprint that changes spuriously turns every resume into a
   12-hour resweep.
6. **A mid-flight edit aborts cleanly.** Overwrite the snapshot during a run and
   confirm the next shard exits non-zero with the mismatch message rather than
   producing cells.
7. **Old experiments still work.** `merge` and `score` against `full-01`, which
   has no snapshot.

Mutation-test every new guard (CLAUDE.md): break the order-sensitivity, the
mismatch abort, and the preflight validation on purpose and confirm a test fails.

## Out of scope

- **`questions_fingerprint` on the `Cell` record and the BigQuery sink.**
  `corpus_fingerprint` is there because two corpora produce cells with identical
  `question_id`s; two question sets produce *different* ids, so rows are already
  distinguishable. Revisit only if someone reuses ids across sets.
- **Generating or validating relevance judgments.** Deciding which tables
  *should* answer a question is the experiment's premise, not something to infer.
- **Per-question overrides** of tier, approach or run count. The factorial is
  the design.
- **Removing the baked-in default set.** It stays the default and the smoke
  path, and `code_version` keeps covering it.
