"""Bootstrapping the results bucket.

The bucket is the one resource `ensure-infra` did not create. It existed only
because it was made by hand during preflight, so a fresh project got a green
`ensure-infra` followed by a `run-shard` that failed on write.

It is deliberately a *local bootstrap* step rather than a pipeline one. The
pipeline cannot reach this code without the bucket already existing — its
`pipeline_root` lives there — and the pipeline service account holds
`roles/storage.objectAdmin` scoped to the bucket, which does not include
`storage.buckets.create`. So in a pipeline run this is always a no-op existence
check, and the 403 path below is what explains that to whoever hits it.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from google.api_core import exceptions
from google.cloud import storage

from bq_context.config import ExperimentConfig
from bq_context.corpus.bucket import ensure_bucket


@pytest.fixture
def config() -> ExperimentConfig:
    return ExperimentConfig.from_env()


class _FakeClient:
    """Records what was asked of it; `existing` seeds lookup_bucket."""

    def __init__(
        self, existing: set[str] | None = None, create_raises: Exception | None = None
    ) -> None:
        self.existing = existing or set()
        self.create_raises = create_raises
        self.created: list[tuple[str, str]] = []
        self.instances_made = 0

    def __call__(self, *_: Any, **__: Any) -> _FakeClient:
        self.instances_made += 1
        return self

    def lookup_bucket(self, name: str) -> object | None:
        return mock.MagicMock() if name in self.existing else None

    def bucket(self, name: str) -> mock.MagicMock:
        bucket = mock.MagicMock()
        bucket.name = name
        return bucket

    def create_bucket(self, bucket: Any, location: str = "", **_: Any) -> mock.MagicMock:
        if self.create_raises:
            raise self.create_raises
        self.created.append((bucket.name, location))
        return mock.MagicMock()


def _run(config: ExperimentConfig, out: str, client: _FakeClient) -> str:
    with mock.patch.object(storage, "Client", client):
        return ensure_bucket(config, out)


# ---------------------------------------------------------------------------
# The ordinary paths
# ---------------------------------------------------------------------------
def test_an_existing_bucket_is_left_alone(config: ExperimentConfig) -> None:
    """The overwhelmingly common case, including every pipeline run."""
    client = _FakeClient(existing={"b"})
    assert _run(config, "gs://b", client) == "exists"
    assert client.created == []


def test_a_missing_bucket_is_created_in_the_compute_region(config: ExperimentConfig) -> None:
    """Regional and co-located with pipeline compute, not multi-region.

    A multi-region bucket would work but costs more and adds cross-region reads
    for 24 shards that all run in us-central1.
    """
    client = _FakeClient()
    assert _run(config, "gs://b", client) == "created"
    assert client.created == [("b", "us-central1")]
    assert config.locations.gcs == "us-central1"


def test_only_the_bucket_part_of_the_uri_is_used(config: ExperimentConfig) -> None:
    """`--out` carries a prefix; the prefix is not part of the bucket name."""
    client = _FakeClient()
    assert _run(config, "gs://b/experiments/full-01", client) == "created"
    assert client.created == [("b", "us-central1")]


def test_a_local_out_path_needs_no_bucket(config: ExperimentConfig) -> None:
    """`--out out/x` is a supported mode; it must not construct a GCS client."""
    client = _FakeClient()
    assert _run(config, "out/results", client) == "skipped"
    assert client.instances_made == 0
    assert client.created == []


# ---------------------------------------------------------------------------
# Idempotence and failure
# ---------------------------------------------------------------------------
def test_losing_a_creation_race_still_succeeds(config: ExperimentConfig) -> None:
    """Two operators bootstrapping at once must not turn into a hard failure."""
    client = _FakeClient(create_raises=exceptions.Conflict("already exists"))
    assert _run(config, "gs://b", client) == "exists"


def test_a_permission_failure_explains_that_this_is_a_bootstrap_step(
    config: ExperimentConfig,
) -> None:
    """The pipeline SA cannot create buckets, and that is deliberate.

    Without this message the 403 reads as a broken grant set, and the obvious
    "fix" is to widen the SA to roles/storage.admin — which is exactly wrong.
    """
    client = _FakeClient(create_raises=exceptions.Forbidden("denied"))
    with pytest.raises(RuntimeError) as err, mock.patch.object(storage, "Client", client):
        ensure_bucket(config, "gs://b")
    message = str(err.value)
    assert "storage.buckets.create" in message
    assert "gs://b" in message
