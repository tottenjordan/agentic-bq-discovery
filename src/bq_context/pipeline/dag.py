"""The pipeline topology.

    validate_config          ~30s   retries=0   fail fast on IAM and models
        ↓
    ensure_infra          12-40m   retries=1   idempotent corpus creation
        ↓
    preflight               ~3m    retries=0   assert enrichment is real
        ↓
    ┌─ ExitHandler(exit_task=finalize) ─────────────────────────┐
    │   ParallelFor(shard_plan, parallelism=8)                  │
    │       run_shard    5m-1.5h   retries=2   one (tier,approach)
    └───────────────────────────────────────────────────────────┘
        ↓
    finalize                ~4m    retries=0   merge, score, plot, verify

**Shards never fail the run.** ``run_shard`` records a failed cell as data and
exits 0; only ``finalize`` turns the run red, and only for missing cells. That
way a 12-hour sweep with three bad cells still yields a scored dataset plus an
actionable list, rather than a red run and nothing to look at.

``parallelism=8`` is where the makespan curve bends. Per-approach cost is
wildly uneven — ``bq_tools`` is ~1.5 h per shard against ``search_direct``'s
0.08 h — so makespan is ``max(longest_shard, total_work / P)``:
``P=4`` → 2.84 h, ``P=8`` → 1.50 h, ``P=12`` → 1.50 h. Above 8 the single
``bq_tools`` shard pins it, and extra concurrency buys nothing while costing
linear 429 exposure against a shared Dynamic Shared Quota pool.
"""

# NB: deliberately NO `from __future__ import annotations` here either. The
# @dsl.pipeline decorator introspects this function's signature the same way
# @dsl.component does, so PEP 563 string annotations break compilation with the
# same misleading "Artifacts must have both a schema_title and a
# schema_version" error. See components.py.

from kfp import dsl

from bq_context.pipeline import components

__all__ = ["PIPELINE_NAME", "bq_context_pipeline"]

PIPELINE_NAME = "bq-context-factorial"

#: Concurrent shards. A **compile-time constant**, not a pipeline parameter:
#: KFP rejects a parameter here with "ParallelFor parallelism must be >= 0",
#: the same constraint that applies to set_retry's arguments. Since the spec is
#: compiled at submission anyway, changing this means editing and recompiling.
#:
#: 8 is where the makespan curve bends — see the module docstring. No profile
#: wants a different value: smoke has only 6 shards, so 8 simply means all of
#: them run at once.
PARALLELISM = 8

DEFAULT_TIERS = [0, 1, 2, 3]
DEFAULT_APPROACHES = [
    "bq_tools",
    "kc_search",
    "kc_context",
    "context_prefilter",
    "semantic_context",
    "search_direct",
]


@dsl.pipeline(
    name=PIPELINE_NAME,
    description="Six BigQuery table-discovery approaches across four catalog enrichment tiers.",
)
def bq_context_pipeline(
    project: str = "hybrid-vertex",
    experiment_id: str = "pilot-01",
    out: str = "gs://hybrid-vertex-bq-context",
    code_version: str = "unknown",
    service_account: str = "",
    runs: int = 5,
    question_limit: int = 0,
    # KFP resolves these defaults into the spec; they are never mutated.
    tiers: list = DEFAULT_TIERS,
    approaches: list = DEFAULT_APPROACHES,
    skip_infra: bool = False,
    require_complete: bool = True,
    refresh_figures: bool = False,
) -> None:
    """Run the factorial.

    ``experiment_id`` is load-bearing: the GCS prefix derives from it, so
    resubmitting with the same value resumes rather than restarting. Never
    derive it from a timestamp inside the pipeline.
    """
    validate = components.validate_config(project=project, out=out, expect_identity=service_account)
    validate.set_display_name("validate config")
    validate.set_retry(num_retries=0)
    # Identity, IAM grants and model availability all change outside this
    # pipeline. A cached "config is fine" is the same false comfort as a cached
    # preflight, just cheaper to get wrong.
    validate.set_caching_options(enable_caching=False)

    # Always a real task rather than wrapped in dsl.If: a conditional group
    # cannot be depended on from outside it, so preflight could not be ordered
    # after the provisioning and would race it into "dataset does not exist".
    # ensure_infra is idempotent and honours skip_infra itself, so the only cost
    # of always running it is one VM start.
    infra = components.ensure_infra(project=project, out=out, skip=skip_infra)
    infra.set_display_name("ensure infra")
    infra.set_retry(num_retries=1, backoff_duration="60s")
    infra.after(validate)
    infra.set_caching_options(enable_caching=False)

    check = components.preflight(project=project, tier=3, baseline=0)
    check.set_display_name("preflight: enrichment is real")
    check.set_retry(num_retries=0)
    check.after(infra)
    # Catalog state changes outside this pipeline, so a cached "enrichment is
    # fine" from last week is worse than useless.
    check.set_caching_options(enable_caching=False)

    plan = components.plan_shards(tiers=tiers, approaches=approaches)
    plan.set_display_name("plan shards")
    # Cacheable, and the only task here that is: a pure function of its inputs
    # with no external state behind it.
    plan.set_caching_options(enable_caching=True)
    plan.after(check)

    finalize = components.finalize(
        project=project,
        experiment_id=experiment_id,
        out=out,
        runs=runs,
        tiers=tiers,
        approaches=approaches,
        require_complete=require_complete,
        question_limit=question_limit,
        refresh_figures=refresh_figures,
    )
    finalize.set_display_name("merge, score, verify")
    finalize.set_caching_options(enable_caching=False)
    # Only this task reads it: `finalize` shells out to `bq-context figures`, and
    # the 24 shards never generate figures. Set unconditionally rather than only
    # when non-empty, so the variable is always visible in the compiled spec and
    # the console — an absent key is then obvious rather than looking like a task
    # that was never configured. The value is the secret's *name*; see
    # components.SECRET_ID for why it cannot be a pipeline parameter.
    finalize.set_env_variable("SECRET_ID", components.SECRET_ID)

    # Kept nested rather than combined: the indentation mirrors the pipeline's
    # actual structure, which is the thing a reader needs to see.
    with dsl.ExitHandler(exit_task=finalize, name="sweep"):  # noqa: SIM117
        with dsl.ParallelFor(items=plan.output, parallelism=PARALLELISM) as shard:
            cell = components.run_shard(
                project=project,
                experiment_id=experiment_id,
                # ty sees ParallelFor's item as a union including
                # LoopArtifactArgument; ours is always a dict parameter.
                tier=shard.tier,  # ty: ignore[unresolved-attribute]
                approach=shard.approach,  # ty: ignore[unresolved-attribute]
                runs=runs,
                out=out,
                code_version=code_version,
                # Corpus shape as a cache-key input. code_version alone lets a
                # changed corpus return cells scored against the old one.
                corpus_fingerprint=check.outputs["fingerprint"],
                question_limit=question_limit,
            )
            cell.set_display_name("run shard")
            # Retries are only worth enabling because shards resume: a retry
            # picks up from the JSONL rather than redoing 90 minutes of work.
            # These arguments must be compile-time constants — KFP rejects
            # pipeline parameters here.
            cell.set_retry(
                num_retries=2,
                backoff_duration="120s",
                backoff_factor=2.0,
                backoff_max_duration="600s",
            )
            # Cacheable, and safe *only* because both code_version and
            # corpus_fingerprint are explicit inputs. Drop either and a rerun
            # silently returns cells produced by different code, or scored
            # against a different corpus.
            cell.set_caching_options(enable_caching=True)
