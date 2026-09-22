"""Compile the pipeline to YAML. One place, deliberately.

A compiled pipeline YAML is a **build artifact**, and repositories keep treating
it as a source file. The pattern and the reasoning here are adopted from
`/home/user/novastorm/bq_insights_agent/src/pipelines/compilation.py`, whose
header documents what that costs: two tracked copies both went stale, one six
months out of date pinning another project's container image, the other four
months behind the DAG and missing seventeen parameters.

The dangerous part is *why it stayed invisible*. *A stale template still
submits.* Every parameter the callers passed existed in both the old and the new
spec, so Vertex accepted the job and ran an obsolete pipeline that returned
plausible-looking results. Nothing errored.

So the rule is: **compile at the point of use, never read a YAML somebody else
wrote.** `*.pipeline.yaml` and `pipeline.yaml` are gitignored to make that
difficult to get wrong by accident.

Compiling is cheap enough that caching it would be a mistake rather than an
optimisation — novastorm measured 0.3s for a 332 KB spec.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["compile_pipeline"]


def compile_pipeline(dest: str | Path | None = None) -> Path:
    """Compile the factorial pipeline; return the path written.

    Requires ``BQ_CONTEXT_IMAGE`` to be set, because ``base_image`` is resolved
    at compile time. That is enforced by ``components.py`` raising ``KeyError``
    on import rather than defaulting, so a spec can never be produced without an
    explicit, immutable image reference.
    """
    # Imported inside the function: pulling in the DAG drags the whole component
    # graph and the kfp compiler, which callers that merely import this module
    # should not pay for.
    from kfp import compiler  # noqa: PLC0415

    from bq_context.pipeline.dag import PIPELINE_NAME, bq_context_pipeline  # noqa: PLC0415

    path = Path(dest) if dest else Path(f"{PIPELINE_NAME}.pipeline.yaml")
    compiler.Compiler().compile(pipeline_func=bq_context_pipeline, package_path=str(path))
    logger.info("Compiled %s (%d bytes)", path, path.stat().st_size)
    return path
