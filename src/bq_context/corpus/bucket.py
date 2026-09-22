"""Create the results bucket if it is missing.

Deliberately *not* part of ``setup.py``. That module is vendored near-verbatim
from upstream (see NOTICE) and kept re-syncable; upstream has no bucket because
its harness writes locally. This is ours, so it lives beside it rather than in
it.

Why this is a local bootstrap step and not a pipeline one: a pipeline run cannot
reach this code without the bucket already existing, because ``pipeline_root``
lives in it. And the pipeline service account holds ``roles/storage.objectAdmin``
scoped to that bucket, which does not include ``storage.buckets.create``. In the
pipeline this is therefore always a no-op existence check — which is the correct
outcome, not a limitation to engineer around.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from bq_context.config import ExperimentConfig

logger = logging.getLogger(__name__)

GCS_SCHEME = "gs://"


def bucket_name(out: str) -> str | None:
    """Bucket in a ``gs://bucket/prefix`` URI, or None for a local path."""
    if not out.startswith(GCS_SCHEME):
        return None
    return out.removeprefix(GCS_SCHEME).split("/", 1)[0] or None


def ensure_bucket(
    config: ExperimentConfig,
    out: str,
    credentials: Credentials | None = None,
) -> str:
    """Create the bucket behind ``out`` if absent. Returns what it did.

    One of ``"skipped"`` (``out`` is a local path), ``"exists"`` or
    ``"created"``. Safe to call repeatedly and safe to lose a race with another
    operator running the same command.
    """
    from google.api_core import exceptions
    from google.cloud import storage

    name = bucket_name(out)
    if name is None:
        logger.debug("out=%s is a local path; no bucket needed", out)
        return "skipped"

    client = storage.Client(project=config.project, credentials=credentials)
    if client.lookup_bucket(name) is not None:
        return "exists"

    bucket = client.bucket(name)
    # Uniform access: per-object ACLs are legacy, and mixing the two models is a
    # reliable way to produce objects a correctly-roled principal cannot read.
    bucket.iam_configuration.uniform_bucket_level_access_enabled = True
    try:
        client.create_bucket(bucket, location=config.locations.gcs)
    except exceptions.Conflict:
        # Either another operator just created it, or the name is taken
        # globally. Both mean "do not create"; the next write says which.
        return "exists"
    except exceptions.Forbidden as exc:
        msg = (
            f"Cannot create {out}: this principal lacks storage.buckets.create.\n"
            "Creating the bucket is a one-time local bootstrap, not a pipeline "
            "step — the pipeline service account is scoped to "
            "roles/storage.objectAdmin on the bucket by design, so widening it "
            "to roles/storage.admin is the wrong fix. Either run this as a "
            "principal that can create buckets, or create it once by hand:\n"
            f"  gcloud storage buckets create {GCS_SCHEME}{name} "
            f"--location={config.locations.gcs} --uniform-bucket-level-access"
        )
        raise RuntimeError(msg) from exc
    return "created"
