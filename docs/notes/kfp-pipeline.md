# The Vertex AI Pipeline

Built 2026-09-22. Spec compiles to ~24 KB; see `src/bq_context/pipeline/`.

```
validate-config  →  ensure-infra  →  preflight  →  plan-shards
                                                        ↓
                            ┌─ ExitHandler ─────────────────────┐
                            │  ParallelFor(parallelism=8)       │
                            │     run-shard  ×24  retries=2     │
                            └───────────────────────────────────┘
                                                        ↓
                                      finalize  (merge, score, plot, verify)
```

`finalize` carries `trigger=ALL_UPSTREAM_TASKS_COMPLETED` — it runs whether or
not the sweep succeeded, which is the whole point of the `ExitHandler`.

## Three KFP constraints that cost real time

**1. `from __future__ import annotations` breaks compilation.** KFP introspects
`__annotations__` at runtime to build the component interface. Under PEP 563
every annotation is a string, so KFP reads the literal `"str"` and tries to
parse it as an artifact type:

```
TypeError: Artifacts must have both a schema_title and a schema_version,
separated by `@`. Got: str
```

Nothing in that message points at the cause. It applies to **both**
`components.py` and `dag.py` — `@dsl.pipeline` introspects the pipeline
signature exactly the way `@dsl.component` introspects a component's. Fixing
only one of them reproduces the identical error. Both files carry a comment
saying why the import is absent, and a test asserts it stays absent.

**2. `ParallelFor(parallelism=)` rejects pipeline parameters.**

```
ValueError: ParallelFor parallelism must be >= 0.
Got: {{channel:task=;name=parallelism;type=Integer;}}
```

Same constraint as `set_retry`'s arguments: compile-time constants only. So
parallelism lives in `dag.PARALLELISM`, not in the parameter list or the run
profiles. No real loss — the spec is compiled at submission anyway, and every
profile wants 8.

**3. A `dsl.If` group cannot be depended on from outside it.** `ensure_infra`
was initially wrapped in `dsl.If(skip_infra == False)`, which meant `preflight`
could only be ordered after `validate_config` — so on a cold project it would
race provisioning and fail with "dataset does not exist". Caught by inspecting
the compiled `dependentTasks`, not by reading the DAG source.

The fix is to always run `ensure_infra` as a real task and let the component
honour `skip` itself. It is idempotent, so the only cost is one VM start.

## Why finalize is one component, not four

The plan sketched merge / score / verify / publish as separate tasks. An
`ExitHandler` accepts exactly **one** exit task, and anything placed after the
handler block cannot depend on the exit task — so a separate verifier would race
the merge for `missing.json`.

Nothing is lost by collapsing them: iterating on scoring happens through the CLI
against a frozen `merged/results.jsonl`, which is faster than rerunning a
pipeline anyway.

`finalize` is **the only task allowed to turn the run red**, and only for
missing cells. Merge and scoring hiccups are logged as warnings. A 12-hour sweep
with three bad cells still yields a scored dataset plus the exact list of what
is missing.

## What the compiled spec is asserted to contain

`tests/test_pipeline.py` compiles the pipeline and checks the guarantees rather
than trusting the source:

- execution order, and specifically that `preflight` waits on `ensure-infra`
- `finalize` triggers on `ALL_UPSTREAM_TASKS_COMPLETED`
- `parallelism == 8`; retry is 2 attempts, 120s × 2 capped at 600s
- `code_version` is an input to every shard — KFP keys its cache on component
  inputs, so without it a prompt edit would silently return cached results from
  the old code
- every component pins the same immutable SHA tag, never `:latest`
- caching is off for `ensure-infra`, `preflight`, and `finalize`, because cloud
  state changes outside the pipeline and a cached "enrichment is fine" from last
  week is worse than useless

## Running it

```bash
make image                                   # build + push, refuses a dirty tree
bq-context submit-pipeline -e pilot-01 --profile pilot \
  --image "$(make -s image-ref)" --dry-run   # compile and print
```

Profiles differ only in parameters, so smoke exercises the same machinery the
full run will: **smoke** tier 3 only / 1 run; **pilot** all tiers / 1 run;
**full** all tiers / 5 runs.

Smoke runs tier 3 rather than tier 0 deliberately — tier 0 is unenriched, so a
green tier-0 smoke would pass even if every scan, term, link, and aspect were
missing.

`experiment_id` is load-bearing: the GCS prefix derives from it, so resubmitting
with the same value resumes. Never derive it from a timestamp inside the
pipeline.

Related: [[container]], [[prior-art-novastorm-kfp]], [[pipeline-service-account]].
