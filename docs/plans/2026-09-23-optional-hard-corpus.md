# Optional Hard Corpus Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this task-by-task.
> First action in execution: copy this file to `docs/plans/2026-09-23-optional-hard-corpus.md`.

**Goal:** Give the experiment an opt-in corpus that is hard enough to separate the
six approaches and the four enrichment tiers, without changing the existing
15-table run in any way.

**Architecture:** Three independent switches, all default OFF, selected by
environment variables that already flow to the pipeline. `CORPUS_PROFILE=hard`
appends 16 verified near-neighbour tables; `BARE_TIER0=1` makes tier 0 a genuine
schema-only baseline; `TOP_K` is already configurable and just needs surfacing.
`RESOURCE_PREFIX` keeps variants side by side as separate datasets.

**Tech Stack:** BigQuery public datasets · Dataplex (scans, glossary, aspects) ·
uv / ruff / pytest / ty

---

## Context

The experiment cannot currently answer its own headline question. On a converged
index, all three search approaches score **0.967 discovery recall at every tier**
(`docs/notes/full-run-results.md`) — enrichment produces no measurable effect.
Two causes, both verified against the live corpus during planning:

**1. Tier 0 is not a baseline.** `corpus/setup.py` sets
`view.description = view_def["description"]` inside the loop over *all* tiers and
copies column descriptions from the source table. Queried directly:

```
tier0: description='Bike share trip records from Austin, Texas. Each row is a
        single bike trip with start/end times, stations, duration, and
        subscriber type.'
        columns=10  with descriptions=10
tier3: ...identical...
```

The BigQuery metadata a semantic search matches against is **the same at tier 0
and tier 3**. A question like *"busiest bike share stations in Austin"* matches
the tier-0 description almost verbatim, so the rungs above it have nothing left
to add.

**2. The haystack is too small.** 15 tables with `TOP_K=5` lets a retriever
return a third of the corpus and still look precise.

**The constraint that shapes this plan:** everything is additive and optional.
`bq-context ensure-infra` with no environment changes must keep producing exactly
today's corpus, so `full-01` stays reproducible.

**What makes this cheap:** `gain_for()` in `scoring/metrics.py:162` returns `0.0`
for any table not in `must_have`/`nice_to_have`, and precision already counts
"distractors and unlabelled tables" alike. So **added tables need no ground-truth
edits at all** — recall, precision and nDCG stay correct. And datasets, the
glossary id and profile-scan ids all derive from `RESOURCE_PREFIX`, which #28
already forwards to every pipeline task, so two corpora coexist with no new
plumbing.

---

## Tasks

### Task 1: Corpus profiles

**Files:** `src/bq_context/corpus/setup.py`, `tests/test_corpus.py`

Split the existing list into `BASE_CORPUS` (today's 15, unchanged) and
`NEAR_NEIGHBOUR_CORPUS` (the 16 below). `CORPUS` becomes the selection:

```python
CORPUS_PROFILE = os.getenv("CORPUS_PROFILE", "base").strip().lower()
_PROFILES = {"base": [], "hard": NEAR_NEIGHBOUR_CORPUS}
if CORPUS_PROFILE not in _PROFILES:
    msg = f"Unknown CORPUS_PROFILE {CORPUS_PROFILE!r}. Valid: {', '.join(_PROFILES)}"
    raise ValueError(msg)
CORPUS = BASE_CORPUS + _PROFILES[CORPUS_PROFILE]
```

**Fail on an unknown value, never fall back to base.** A typo that silently
selects the small corpus produces a run that looks fine and measures the wrong
thing — the exact failure mode this project keeps hitting.

The 16 tables, all verified to exist during planning. Each is a genuine "looks
right, is wrong" neighbour for at least one existing question:

| table | attacks |
|---|---|
| `austin_incidents.incidents_2016` | `austin_crime` |
| `san_francisco_bikeshare.bikeshare_trips` / `.bikeshare_station_info` | the Austin bike share pair |
| `new_york_citibike.citibike_trips` | pairs with the existing `citibike_stations` distractor |
| `chicago_taxi_trips.taxi_trips`, `new_york_taxi_trips.tlc_green_trips_2022` | `nyc_taxi_trips_2022` |
| `chicago_crime.crime`, `san_francisco.sfpd_incidents` | `austin_crime` |
| `noaa_gsod.stations` | `weather_stations` |
| `epa_historical_air_quality.o3_daily_summary` | `air_quality_annual_summary` |
| `census_bureau_acs.county_2018_5yr` | `population_by_zip_2010` |
| `geo_us_boundaries.states` | `us_counties`, `zip_codes` |
| `sdoh_cdc_wonder_natality.county_natality_by_mother_race` | `county_natality` |
| `bls.employment_hours_earnings` | `unemployment_cps` |
| `new_york.nypd_mv_collisions`, `austin_waste.waste_and_diversion` | thematic noise |

Do **not** add `epa_historical_air_quality.air_quality_daily_summary` or
`geo_us_boundaries.census_tracts_texas` — both were checked and do not exist.

Each entry needs the same three keys as the existing ones (`name`, `source`,
`description`). Write descriptions in the same register as the current corpus:
factual, one or two sentences, no hints about which questions they answer.

**Tests:** default profile yields exactly the original 15 names; `hard` yields 31
with no duplicate `name`; an unknown profile raises; every `source` is unique.
Mutation-check by pointing one entry at a nonexistent table and confirming a
test notices, and by making an unknown profile fall through to base.

**Commit:** `feat: optional near-neighbour corpus profile`

---

### Task 2: A guard against overwriting the baseline corpus

**Files:** `src/bq_context/corpus/setup.py`, `tests/test_corpus.py`

This is the task most likely to be skipped and most expensive to skip. Running
`CORPUS_PROFILE=hard` with the default `RESOURCE_PREFIX` would add 16 views to
`bigquery_context_tier0..3` — silently destroying the reproducibility this whole
plan exists to protect, and the damage is not obvious afterwards.

Refuse it:

```python
if CORPUS_PROFILE != "base" and RESOURCE_PREFIX == _DEFAULT_RESOURCE_PREFIX:
    msg = (
        f"CORPUS_PROFILE={CORPUS_PROFILE} with the default RESOURCE_PREFIX would add "
        f"tables to the baseline corpus and make full-01 irreproducible. Set "
        f"RESOURCE_PREFIX to something else, e.g. {_DEFAULT_RESOURCE_PREFIX}_hard."
    )
    raise ValueError(msg)
```

The same reasoning applies to `BARE_TIER0` once Task 3 lands — include it in the
condition.

**Watch:** `cleanup.py` imports `CORPUS`, `PROFILED_TIERS` and `GLOSSARY_TIERS`
from `setup.py`, so it deletes whatever the *current environment* selects. Run it
with the wrong `RESOURCE_PREFIX`/`CORPUS_PROFILE` and it orphans scans and links
rather than removing them. Add a test that both modules resolve the same dataset
ids and the same corpus under a given environment, and say so in the docstring.

**Commit:** `feat: refuse to mix a corpus profile into the baseline datasets`

---

### Task 3: Optional bare tier 0

**Files:** `src/bq_context/corpus/setup.py` (`create_datasets_and_views`),
`tests/test_corpus.py`

`BARE_TIER0=1` omits the table description and strips column descriptions at
tier 0 only. This is the change most likely to break the flat 0.967 line, because
today tier 0 already carries a task-revealing description.

Column descriptions must be stripped recursively — some public tables have
`RECORD` fields:

```python
def _without_descriptions(schema):
    from google.cloud import bigquery

    return [
        bigquery.SchemaField(
            f.name,
            f.field_type,
            mode=f.mode,
            description=None,
            fields=_without_descriptions(f.fields) if f.fields else (),
        )
        for f in schema
    ]
```

Apply it where the view is created and where the schema is copied — both sites
currently set `description`, and the second overwrites the first, so changing
only one is a silent no-op.

**Tests:** with the flag off, tier 0 keeps its description (today's behaviour);
with it on, tier 0 has none and tiers 1-3 are untouched; the strip helper handles
a nested `RECORD` field. Assert on a fake schema rather than calling BigQuery.

**Commit:** `feat: optional schema-only tier 0`

---

### Task 4: Make the variant visible

**Files:** `src/bq_context/cli.py` (`preflight`), `src/bq_context/pipeline/components.py`

Two things, both small and both about not being misled later.

`preflight` already prints the ladder and `tables` per rung, which makes a
31-table corpus self-evident. Add `TOP_K` and the corpus profile to that output
so a saved log says what was measured. `config.top_k` already exists
(`config.py:68`) and is forwarded by #28 — this is reporting, not new config.

Add `CORPUS_PROFILE` and `BARE_TIER0` to `components.CONFIG_ENV_KEYS` so the
pipeline tasks see them.

**Watch — the existing guard will not catch this.**
`test_the_forwarded_set_covers_everything_from_env_reads` parses
`ExperimentConfig.from_env`, and these two are read in `corpus/setup.py`, not
`config.py`. Extend that test to also scan `setup.py`'s module-level
`os.getenv` calls, or the next variable added there is silently not forwarded —
which is precisely the bug #28 fixed for the other six.

**Commit:** `feat: report the corpus profile and TOP_K in preflight`

---

### Task 5: Keep the upstream comparison pinned

**Files:** `src/bq_context/scoring/upstream_build_results.py:42`

It imports `CORPUS` to generate the corpus table in the upstream comparison doc.
That doc describes the *original* experiment, so it must import `BASE_CORPUS` and
never vary with the profile. One-line change, easy to miss, and the symptom is a
comparison document that quietly describes a different corpus than the one it
compares against.

**Commit:** `fix: pin the upstream comparison to the base corpus`

---

### Task 6: Documentation

**Files:** `.env.example`, `README.md`, `experiments/README.md`,
`docs/notes/gcp/corpus-provisioning.md`

`.env.example` gains `CORPUS_PROFILE`, `BARE_TIER0` and a note that the harder
variant requires its own `RESOURCE_PREFIX`. Note that
`tests/test_config_env.py::test_every_variable_that_changes_a_run_is_documented`
will fail until they are added, which is the guard working.

The note should record *why* the variant exists — the two measured causes in the
Context section above — not just how to switch it on.

**Commit:** `docs: the optional hard corpus and why it exists`

---

## Verification

Offline:

```bash
make check
CORPUS_PROFILE=hard uv run python -c "
from bq_context.corpus import setup
print(len(setup.CORPUS), 'tables')"      # 31
CORPUS_PROFILE=typo uv run python -c "import bq_context.corpus.setup"   # must raise
```

Then the part that actually matters — provision the variant alongside the
original and compare. Budget 30-60 minutes for `ensure-infra`; it creates ~124
views and ~93 profile scans.

```bash
export RESOURCE_PREFIX=bigquery_context_hard CORPUS_PROFILE=hard BARE_TIER0=1
bq-context ensure-infra --yes
bq-context preflight --tier 3 --baseline 0        # expect tables=31, and a
                                                  # visible byte gap tier0 -> tier1
bq-context submit-pipeline --profile pilot -e hard-pilot-01
```

**Checks that matter more than the tests passing:**

1. **The baseline is untouched.** `bq-context preflight` with no environment
   overrides still reports 15 tables and fingerprint `861648cc513c3862`. If that
   moves, Task 2's guard failed and `full-01` is no longer reproducible.
2. **Tier 0 is actually bare.** Query
   `{prefix}_tier0.austin_bikeshare_trips` and confirm `description is None` and
   no column carries one, while `_tier1` still does.
3. **The ceiling breaks, or it does not.** Compare `hard-pilot-01`'s
   `recall_vs_tier` against the flat 0.967. A rising curve is the result this
   plan is for. **A still-flat curve is also a result** — it would mean
   enrichment genuinely does not help retrieval on this task, which is upstream's
   published finding, and it should be reported as such rather than tuned away.
4. **`search_direct` should get worse.** It has no reranker, so a corpus with 16
   near-neighbours is exactly what should hurt it. If its 77.2% does not drop,
   the added tables are not the confusable neighbours they are meant to be.
5. **Cost per cell.** `kc_context` sends `all_detailed()`
   (`approaches/agent_kc_context/tools/callback_discover_and_rerank.py:32`), so
   its capsule roughly doubles: measured ~8.2 KB/table, 15 -> 31 tables is
   ~122 KB -> ~254 KB, about 64K tokens per cell. Check the reranker-token column
   before committing to a 3,000-cell sweep.

## Out of scope

New questions or relevance labels — unnecessary, since unlabelled tables already
score correctly, and the existing 25 become harder for free. Changing the tier
definitions of the base corpus. Raising the corpus past ~50 tables, where
`kc_context`'s capsule stops being affordable. Making the index-drift warning
fatal.
