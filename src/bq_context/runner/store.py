"""Artifact storage, over GCS or the local filesystem.

The sweep writes per-shard JSONL and a handful of marker files. Both backends
implement the same tiny interface so a shard can be developed and tested against
``/tmp`` and then run unchanged against ``gs://``. Tests exercise the real
``LocalStore`` rather than a mock, so path handling is covered too.

**"Append-only" needs a caveat.** GCS objects are immutable — there is no true
append. The shard therefore appends to a local file (real append, fsync'd per
line) and periodically overwrites the whole remote object. A full overwrite of a
small object is atomic in GCS, so a reader never sees a torn file, and the worst
case on a crash is losing whatever was written since the last upload.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

logger = logging.getLogger(__name__)

__all__ = ["ArtifactStore", "GcsStore", "LocalStore", "store_for"]


@runtime_checkable
class ArtifactStore(Protocol):
    """Minimal blob interface: list, read, write, exists."""

    def list_paths(self, prefix: str) -> list[str]:
        """Paths under ``prefix``, sorted. Relative to the store root."""
        ...

    def read_text(self, path: str) -> str:
        """Whole object as text. Raises FileNotFoundError if absent."""
        ...

    def write_text(self, path: str, text: str) -> None:
        """Create or overwrite an object atomically."""
        ...

    def write_bytes(self, path: str, data: bytes, content_type: str) -> None:
        """Create or overwrite a binary object atomically.

        Separate from ``write_text`` because that one hard-codes
        ``application/json``: a PNG uploaded through it would be served with the
        wrong type and render as a download rather than an image.
        """
        ...

    def exists(self, path: str) -> bool: ...

    def delete(self, path: str) -> None:
        """Remove one object. Absent is not an error.

        Deliberately the narrowest delete that works: one path, no prefix, no
        recursion. This store holds twelve hours of irreplaceable agent output,
        and the only thing the code ever removes is a shard's terminal marker —
        seventeen bytes it wrote itself — so that ``_SUCCESS`` and ``_FAILED``
        cannot both be present. Resist widening it.
        """
        ...

    def uri(self, path: str) -> str:
        """Fully-qualified location, for logs and run reports."""
        ...


class LocalStore:
    """Filesystem-backed store, for local runs and tests."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _full(self, path: str) -> Path:
        return self.root / path

    def list_paths(self, prefix: str) -> list[str]:
        base = self._full(prefix)
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def read_text(self, path: str) -> str:
        return self._full(path).read_text()

    def write_text(self, path: str, text: str) -> None:
        full = self._full(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        # Temp + rename so a reader never observes a partial file, matching the
        # atomicity GCS gives us for free.
        tmp = full.with_suffix(full.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(full)

    def write_bytes(self, path: str, data: bytes, content_type: str = "") -> None:
        del content_type  # a local file has no content type to carry
        full = self._full(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_suffix(full.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(full)

    def exists(self, path: str) -> bool:
        return self._full(path).exists()

    def delete(self, path: str) -> None:
        self._full(path).unlink(missing_ok=True)

    def uri(self, path: str) -> str:
        return str(self._full(path))


class GcsStore:
    """Cloud Storage-backed store."""

    def __init__(
        self, bucket: str, prefix: str = "", credentials: Credentials | None = None
    ) -> None:
        # Imported here so LocalStore users never pay the ~0.4s
        # google-cloud-storage import, and tests need no GCP creds.
        from google.cloud import storage  # noqa: PLC0415

        # Credentials must be threaded through: without this, a --impersonate
        # check would build a client from ambient ADC and report the caller's
        # access as though it were the service account's.
        self._client = storage.Client(credentials=credentials)
        self._bucket = self._client.bucket(bucket)
        self.bucket_name = bucket
        self.prefix = prefix.strip("/")

    def _blob_name(self, path: str) -> str:
        return f"{self.prefix}/{path}".lstrip("/") if self.prefix else path

    def list_paths(self, prefix: str) -> list[str]:
        full = self._blob_name(prefix)
        offset = len(self.prefix) + 1 if self.prefix else 0
        return sorted(b.name[offset:] for b in self._client.list_blobs(self._bucket, prefix=full))

    def read_text(self, path: str) -> str:
        blob = self._bucket.blob(self._blob_name(path))
        if not blob.exists():
            msg = self.uri(path)
            raise FileNotFoundError(msg)
        return blob.download_as_text()

    def write_text(self, path: str, text: str) -> None:
        # A single upload replaces the object atomically; readers see either the
        # old object or the new one, never a partial write.
        self._bucket.blob(self._blob_name(path)).upload_from_string(
            text, content_type="application/json"
        )

    def write_bytes(
        self, path: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        self._bucket.blob(self._blob_name(path)).upload_from_string(data, content_type=content_type)

    def exists(self, path: str) -> bool:
        return self._bucket.blob(self._blob_name(path)).exists()

    def delete(self, path: str) -> None:
        from google.cloud.exceptions import NotFound  # noqa: PLC0415

        # `if_generation_match=0` is not usable here and a pre-check would race,
        # so swallow the 404 instead. Deleting a marker that was never written
        # is the normal first-run case, not an exceptional one.
        with contextlib.suppress(NotFound):
            self._bucket.blob(self._blob_name(path)).delete()

    def uri(self, path: str) -> str:
        return f"gs://{self.bucket_name}/{self._blob_name(path)}"


def store_for(location: str, credentials: Credentials | None = None) -> ArtifactStore:
    """Build a store from a ``gs://bucket/prefix`` URI or a local path."""
    if location.startswith("gs://"):
        without_scheme = location[len("gs://") :]
        bucket, _, prefix = without_scheme.partition("/")
        return GcsStore(bucket=bucket, prefix=prefix, credentials=credentials)
    return LocalStore(location)
