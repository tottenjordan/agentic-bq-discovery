"""Submit the compiled pipeline to Vertex AI Pipelines.

Two choices here are load-bearing.

``job.submit()``, not ``job.run()``. ``run()`` blocks until the pipeline
finishes, so a dropped workstation session — closing a laptop, a timed-out SSH —
looks like a failed submission even though the pipeline is running fine. For a
job measured in hours that is the difference between a tool you can walk away
from and one you cannot.

``service_account=`` passed explicitly. Omit it and Vertex silently falls back
to the Compute Engine default service account, which in a long-lived sandbox
often carries Editor. Everything works, and then breaks in any project that
enforces least privilege.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["PROFILES", "submit_pipeline"]

#: Run profiles. One code path; only the parameters differ, so the smoke run
#: exercises exactly the machinery the full run will use.
#:
#: Parallelism is not here: KFP requires it as a compile-time constant, so it
#: lives in ``dag.PARALLELISM``.
#:
#: Smoke deliberately runs **tier 3, not tier 0**. Tier 0 is the unenriched
#: baseline: a green tier-0 smoke proves nothing about Dataplex and would pass
#: even if every scan, term, link, and aspect were missing. Tier 3 is maximal
#: enrichment, so it has the most ways to fail informatively.
PROFILES: dict[str, dict[str, Any]] = {
    # 6 shards, 18 cells. Proves the machinery, not the science.
    "smoke": {"tiers": [3], "runs": 1, "question_limit": 3, "require_complete": True},
    # The exact 24-shard topology of the full run, at 1/25th the cost. This is
    # where P=8 contention, cache warm, and serialization problems surface.
    #
    # NB it exercises the *machinery*, not the science. `question_limit` takes the
    # first five questions and all five are `single-table`, so a pilot touches
    # none of the four traps, none of the multi-table questions, and none of the
    # twelve that name no place. Its tier response is close to meaningless -- a
    # hard-corpus pilot reported +0.000 across every approach, on questions whose
    # answer was never in doubt.
    "pilot": {"tiers": [0, 1, 2, 3], "runs": 1, "question_limit": 5, "require_complete": True},
    # 600 cells. Every question, every tier, once -- the cheapest run that is
    # actually about the experiment rather than the plumbing. Use it to decide
    # whether a corpus or enrichment change moved anything before paying for
    # `full`; the traps and the underspecified questions only appear here.
    #
    # One run, so per-cell variance is not averaged out. Read a difference here as
    # a signal worth confirming, never as a measurement.
    "survey": {"tiers": [0, 1, 2, 3], "runs": 1, "question_limit": 0, "require_complete": True},
    # 3,000 cells.
    "full": {"tiers": [0, 1, 2, 3], "runs": 5, "question_limit": 0, "require_complete": True},
}


def submit_pipeline(  # noqa: PLR0913 - submission parameters are the API
    *,
    template_path: str | Path,
    project: str,
    location: str,
    pipeline_root: str,
    service_account: str,
    experiment_id: str,
    parameter_values: dict[str, Any],
    enable_caching: bool | None = None,
    wait: bool = False,
) -> Any:
    """Submit the pipeline and return the job.

    ``enable_caching=None`` is load-bearing, not a lazy default. The Vertex SDK
    rewrites the compiled spec whenever it is *not* None::

        for task in component["dag"]["tasks"].values():
            task["cachingOptions"] = {"enableCache": enable_caching}

    That is a blunt overwrite of every task in every DAG. This used to default to
    ``True``, which silently discarded every ``set_caching_options(False)`` in
    ``dag.py`` — including the one on ``preflight``, the gate that decides whether
    catalog enrichment is real. ``tests/test_pipeline.py`` inspects the compiled
    spec and so could not see it; ``tests/test_submit.py`` asserts it here.

    ``False`` remains a safe override because it can only ever disable caching.
    ``True`` is not offered: it cannot be expressed without overriding tasks that
    deliberately opted out.

    Shard-level caching is still safe, and still worth having: every shard takes
    ``code_version`` and ``corpus_fingerprint`` as explicit inputs, so a changed
    commit or a changed corpus invalidates them while an unchanged pair skips
    finished work without starting a VM.
    """
    from google.cloud import aiplatform  # noqa: PLC0415

    aiplatform.init(project=project, location=location)
    job = aiplatform.PipelineJob(
        display_name=f"bq-context-{experiment_id}",
        template_path=str(template_path),
        pipeline_root=pipeline_root,
        parameter_values=parameter_values,
        enable_caching=enable_caching,
        # 'slow' (the default) lets sibling shards finish when one dies, rather
        # than cancelling 23 of them over a single failure.
        failure_policy="slow",
    )
    job.submit(service_account=service_account)
    logger.info("Submitted %s", job.resource_name)
    logger.info("Console: %s", job._dashboard_uri())  # noqa: SLF001 - no public accessor

    if wait:
        job.wait()
    return job
