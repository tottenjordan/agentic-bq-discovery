"""The artifact store's binary path.

`write_text` hard-codes `content_type="application/json"`, which is right for
the JSONL and JSON this project writes and wrong for everything else. Pushing a
PNG through it would upload bytes that GCS then serves as JSON — the object
exists, the size is right, and a browser downloads it instead of showing it.
That is the kind of wrong that survives a smoke test.

`write_bytes` exists so `finalize` can persist the three matplotlib figures with
a type that makes them viewable in the console.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bq_context.runner.store import ArtifactStore, GcsStore, LocalStore

if TYPE_CHECKING:
    from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_bytes_round_trip_unchanged(tmp_path: Path) -> None:
    """A PNG must survive byte-for-byte; no text encoding anywhere in the path."""
    store = LocalStore(tmp_path)
    payload = PNG_MAGIC + bytes(range(256))
    store.write_bytes("plots/x.png", payload, "image/png")
    assert (tmp_path / "plots" / "x.png").read_bytes() == payload


def test_it_creates_missing_parent_directories(tmp_path: Path) -> None:
    """`finalize` writes into `experiments/{id}/plots/`, which will not exist."""
    store = LocalStore(tmp_path)
    store.write_bytes("deep/nested/dir/x.png", PNG_MAGIC, "image/png")
    assert store.exists("deep/nested/dir/x.png")


def test_a_write_is_atomic(tmp_path: Path) -> None:
    """Temp-and-rename, matching write_text: a reader never sees a partial file."""
    store = LocalStore(tmp_path)
    store.write_bytes("x.png", b"first", "image/png")
    store.write_bytes("x.png", b"second", "image/png")
    assert (tmp_path / "x.png").read_bytes() == b"second"
    assert not list(tmp_path.glob("*.tmp")), "a temp file was left behind"


def test_both_stores_satisfy_the_protocol() -> None:
    """A method added to only one implementation is a runtime failure in the pipeline,
    where GcsStore is the one actually used."""
    for impl in (LocalStore, GcsStore):
        assert hasattr(impl, "write_bytes"), impl.__name__
    assert isinstance(LocalStore("/tmp"), ArtifactStore)  # noqa: S108


@pytest.mark.parametrize("name", ["write_text", "write_bytes", "read_text", "exists", "uri"])
def test_the_protocol_declares_what_callers_use(name: str) -> None:
    assert hasattr(ArtifactStore, name)
