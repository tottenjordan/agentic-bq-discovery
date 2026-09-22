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

## The hermetic-body constraint includes the annotations

KFP does not ship your module. It extracts each component function's source into
a standalone `ephemeral_component.py` and re-evaluates the `def` — **annotations
included** — with only this in scope:

```python
import kfp
from kfp import dsl
from kfp.dsl import *
from typing import *
```

`preflight` was annotated `-> Preflight`, a module-level
`class Preflight(NamedTuple)`. It compiled cleanly, passed every test, and died
at task startup in Vertex:

```
NameError: name 'Preflight' is not defined
```

Compilation introspects the *original* module, where the class exists. Only the
extracted file matters at runtime. The functional form
`NamedTuple("Preflight", [("fingerprint", str)])` works because `NamedTuple`
comes from `typing`. ty rejects a call in a return annotation, so it carries a
`# ty: ignore[invalid-type-form]` — that is the price of the only form KFP can
execute.

`tests/test_pipeline.py::test_every_component_body_resolves_in_the_namespace_kfp_gives_it`
now execs every embedded definition in that namespace, which is the only offline
check for this.

### Writing that test has its own trap

`compile()` **inherits the `__future__` flags of the calling module** unless you
pass `dont_inherit=True`. `tests/test_pipeline.py` opens with
`from __future__ import annotations`, so the first version of the test compiled
the extracted def with PEP 563 lazy annotations — they became strings, were never
evaluated, and the test passed no matter what the annotation referenced. The
identical code raised `NameError` when run as a standalone script, which is what
eventually gave it away.

## A task's image *can* be a runtime value — `set_container_image`

`base_image` on `@dsl.component` is compile-time only, and so is
`ContainerSpec(image=...)` inside a `@dsl.container_component` — the latter
rejects a channel with an unhelpful `TypeError: bad argument type for built-in
operation`. That is easy to over-generalise into "a task's image is fixed at
compile time". **It is not.**

`PipelineTask.set_container_image()` takes a static string *or* a
`PipelineChannel`, and its own docstring is explicit: *"Unlike `base_image`, this
method supports dynamic values such as Pipeline Parameters or outputs from
previous tasks, which are resolved at runtime."*

From an upstream task's output:

```python
built = build_report_image(tag=tag)  # e.g. triggers Cloud Build, returns a ref
task = shard(i=1)
task.set_container_image(built.output)
```

compiles to a genuine runtime placeholder:

```
exec-shard  image={{$.inputs.parameters['pipelinechannel--build-report-image-Output']}}
```

### The catch, and the way around it

Passing an **upstream output** makes the consuming task *depend* on the producer.
An `ExitHandler` exit task cannot depend on anything, so this fails on `finalize`
specifically:

```
finalize_().set_container_image(built.output)
  → ValueError: finalize does not exist.
```

A **pipeline parameter** creates no dependency, so it works even there:

```python
def pipeline(report_image: str = DEFAULT):
    fin = finalize_()
    fin.set_container_image(report_image)
```

```
exec-finalize  image={{$.inputs.parameters['pipelinechannel--report_image']}}
```

That is submit-time image selection without recompiling — strictly more flexible
than reading the reference from an environment variable at compile time.

### Why we do not use it

The one candidate was PaperBanana in `finalize`. A cold `uv pip install
paperbanana` measures **3.35s** (237 MB, 51 packages), and a dynamic image still
requires building and pushing one — the technique removes the *recompile*, not
the *build*. Three seconds does not justify a second Dockerfile, a Cloud Build
config and a registry artifact to keep in step with the runner.

It would earn its place if the extra were genuinely heavy (a CUDA base, a large
model), if the environment blocked PyPI egress (VPC-SC), or if per-run pinned
environments were a requirement. None holds today.

### Do not generalise it: `set_env_variable` is compile-time only

The natural next thought — "if the image can be a runtime value, so can an env
var" — is wrong, and the two methods look identical from the outside. Both are
annotated `(name: str, value: str)`; only one accepts a channel.

```python
@dsl.pipeline
def p(secret_id: str = "from-param"):
    t().set_env_variable("SECRET_ID", secret_id)
```

```
TypeError: bad argument type for built-in operation
```

The failure is at **compile** time, from `build_container_spec_for_task`
(`pipeline_spec_builder.py:751`) handing a `PipelineChannel` to a protobuf
`EnvVar`. Note that is the same message `ContainerSpec(image=...)` gives, so the
error does not distinguish "this method never takes a channel" from "you passed
the wrong kind of thing".

Consequence for us: `SECRET_ID` on `finalize` is resolved from the environment at
compile time (`components.SECRET_ID`), not threaded as a pipeline parameter. The
submitting shell's `.env` therefore decides it, and changing it means
recompiling — which costs nothing here, since the spec is compiled at every
submission anyway.

Related: [[pipeline-service-account]] for why only the secret's *name* goes in
the spec and the key itself never does.
