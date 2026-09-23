"""The upstream comparison document describes the *original* experiment.

`upstream_build_results.corpus_table()` regenerates the corpus table injected
into the upstream readme, and it imported `CORPUS` — which is now
profile-dependent. Run any doc regeneration with `CORPUS_PROFILE=hard` in the
environment and the document would quietly claim the upstream experiment ran
against 31 tables.

That is worse than a cosmetic error: the whole point of the document is to
compare our numbers against theirs on a corpus they can recognise. A one-line
import change, and easy to miss, because nothing else in the module would fail.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


def test_the_corpus_table_is_always_the_original_fifteen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE guard. The rendered table must not move with the corpus profile."""
    from bq_context.corpus import setup
    from bq_context.scoring import upstream_build_results

    baseline = upstream_build_results.corpus_table()
    assert "= 15**" in baseline, baseline

    monkeypatch.setenv("CORPUS_PROFILE", "hard")
    monkeypatch.setenv("RESOURCE_PREFIX", "bigquery_context_hard")
    importlib.reload(setup)
    assert len(setup.CORPUS) == 31, "the hard profile did not load; the test proves nothing"

    hardened = importlib.reload(upstream_build_results).corpus_table()
    assert hardened == baseline


def test_it_imports_the_base_corpus_by_name() -> None:
    """Belt and braces on the import itself.

    The functional test above only fails if the two corpora differ in *size*. A
    future profile that swapped a table for another of the same count would slip
    past it, so pin the symbol as well.
    """
    import inspect

    from bq_context.scoring import upstream_build_results

    source = inspect.getsource(upstream_build_results)
    assert "BASE_CORPUS" in source
    assert "import CORPUS" not in source
