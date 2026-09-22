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
    "smoke": {
        "tiers": [3],
        "runs": 1,
        "require_complete": True,
    },
    "pilot": {
        "tiers": [0, 1, 2, 3],
        "runs": 1,
        "require_complete": True,
    },
    "full": {
        "tiers": [0, 1, 2, 3],
        "runs": 5,
        "require_complete": True,
    },
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
    enable_caching: bool = True,
    wait: bool = False,
) -> Any:
    """Submit the pipeline and return the job.

    ``enable_caching`` is safe here only because every shard takes
    ``code_version`` as an explicit input: KFP keys its cache on component
    inputs, so a changed commit invalidates the shards while an unchanged one
    skips finished work without even starting a VM.
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
