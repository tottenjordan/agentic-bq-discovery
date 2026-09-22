"""The corpus module's pure logic: the enrichment ladder and resource ids.

`corpus/setup.py` is 837 lines at 0% coverage because almost all of it calls
GCP. But the parts that decide *what gets created and what it is called* are
pure, and they are the parts whose failure is silent:

- A duplicate resource id means two tables share one DataScan or entry link, so
  a table gets enrichment computed from a different table's data. Nothing
  errors; the experiment just measures the wrong thing.
- A tier threshold that drifts between setup and cleanup leaves orphaned scans
  and links behind. Stale catalog resources survive a corpus rebuild, so a
  later run silently sees enrichment it was never meant to have.

Neither is visible without provisioning, and provisioning takes 40 minutes.
These tests cover both in milliseconds.
"""

from __future__ import annotations

import re
from unittest import mock

import pytest
from google.cloud import dataplex_v1

from bq_context.corpus import cleanup, setup

#: Dataplex entry-link and DataScan ids: lowercase letters, digits and hyphens,
#: starting with a letter, ending with a letter or digit, at most 63 characters.
DATAPLEX_ID = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")

ID_LIMIT = 63


def _all_scan_ids() -> list[tuple[str, tuple[int, str]]]:
    return [
        (setup.profile_scan_id(tier, view["name"]), (tier, view["name"]))
        for tier in setup.PROFILED_TIERS
        for view in setup.CORPUS
    ]


def _all_link_ids() -> list[tuple[str, tuple[int, str, str, str]]]:
    return [
        (setup.definition_link_id(tier, term_id, table, column), (tier, term_id, table, column))
        for tier in setup.GLOSSARY_TIERS
        for term_id, term in setup.GLOSSARY_TERMS.items()
        for table, columns in term["columns"].items()
        for column in columns
    ]


def _scans_cleanup_targets() -> set[str]:
    """Run `delete_profile_scans` against a stubbed client; return the ids it deletes."""
    deleted: set[str] = set()

    class _FakeScanClient:
        def __init__(self, *_: object, **__: object) -> None: ...

        def delete_data_scan(self, request: object = None, **_: object) -> mock.MagicMock:
            deleted.add(request.name.rsplit("/", 1)[-1])  # type: ignore[attr-defined]
            return mock.MagicMock()

    with mock.patch.object(dataplex_v1, "DataScanServiceClient", _FakeScanClient):
        cleanup.delete_profile_scans()
    return deleted


def _links_cleanup_targets() -> set[str]:
    """Run `delete_entry_links` against a stubbed client; return the ids it deletes."""
    deleted: set[str] = set()

    class _FakeCatalogClient:
        def __init__(self, *_: object, **__: object) -> None: ...

        def delete_entry_link(self, name: str = "", **_: object) -> mock.MagicMock:
            deleted.add(name.rsplit("/", 1)[-1])
            return mock.MagicMock()

    with mock.patch.object(dataplex_v1, "CatalogServiceClient", _FakeCatalogClient):
        cleanup.delete_entry_links()
    return deleted


# ---------------------------------------------------------------------------
# The enrichment ladder
# ---------------------------------------------------------------------------
def test_the_ladder_is_nested() -> None:
    """Each rung is a subset of the one below, because enrichment accumulates."""
    assert set(setup.GUIDELINES_TIERS) <= set(setup.GLOSSARY_TIERS)
    assert set(setup.GLOSSARY_TIERS) <= set(setup.PROFILED_TIERS)
    assert set(setup.PROFILED_TIERS) <= set(setup.TIERS)


def test_tier_zero_is_the_unenriched_control() -> None:
    """Tier 0 must receive no catalog enrichment at all, or the ablation has no floor."""
    assert 0 not in setup.PROFILED_TIERS
    assert 0 not in setup.GLOSSARY_TIERS
    assert 0 not in setup.GUIDELINES_TIERS


def test_every_rung_is_non_empty() -> None:
    """A silently empty rung makes two tiers identical and the comparison meaningless."""
    assert setup.PROFILED_TIERS
    assert setup.GLOSSARY_TIERS
    assert setup.GUIDELINES_TIERS


def test_cleanup_deletes_exactly_the_profile_scans_setup_creates() -> None:
    """Regression: both modules used to spell `if tier < 1` inline.

    Moving a rung in setup.py would then leave cleanup.py deleting the wrong
    set, orphaning DataScans that survive a corpus rebuild. Asserted by running
    the deletion against a stubbed client and comparing the ids it targets —
    checking that the two modules merely *import* the same constant passes even
    when the function body ignores it.
    """
    deleted = _scans_cleanup_targets()
    expected = {scan_id for scan_id, _ in _all_scan_ids()}
    assert deleted == expected, (
        f"orphaned by cleanup: {sorted(expected - deleted)}; "
        f"deleted but never created: {sorted(deleted - expected)}"
    )


def test_cleanup_deletes_exactly_the_entry_links_setup_creates() -> None:
    """Same invariant for the glossary rung, which uses a different threshold."""
    deleted = _links_cleanup_targets()
    expected = {link_id for link_id, _ in _all_link_ids()}
    assert deleted == expected, (
        f"orphaned by cleanup: {sorted(expected - deleted)}; "
        f"deleted but never created: {sorted(deleted - expected)}"
    )


# ---------------------------------------------------------------------------
# Resource ids are unique and valid
# ---------------------------------------------------------------------------
def test_profile_scan_ids_are_unique_across_the_corpus() -> None:
    ids = _all_scan_ids()
    seen: dict[str, tuple[int, str]] = {}
    for scan_id, origin in ids:
        assert scan_id not in seen, f"{scan_id} generated by both {seen[scan_id]} and {origin}"
        seen[scan_id] = origin
    assert len(ids) == len(setup.PROFILED_TIERS) * len(setup.CORPUS)


def test_definition_link_ids_are_unique_across_the_corpus() -> None:
    ids = _all_link_ids()
    seen: dict[str, tuple[int, str, str, str]] = {}
    for link_id, origin in ids:
        assert link_id not in seen, f"{link_id} generated by both {seen[link_id]} and {origin}"
        seen[link_id] = origin
    assert ids, "the glossary defines no links; tiers 2 and 3 would equal tier 1"


@pytest.mark.parametrize("resource_id", [i for i, _ in _all_scan_ids() + _all_link_ids()])
def test_every_generated_id_is_dataplex_valid(resource_id: str) -> None:
    assert len(resource_id) <= ID_LIMIT
    assert DATAPLEX_ID.match(resource_id), resource_id


def test_tier_is_part_of_every_id() -> None:
    """The same table exists in all four tier datasets; ids must not collide across them."""
    for view in setup.CORPUS[:3]:
        ids = {setup.profile_scan_id(t, view["name"]) for t in setup.PROFILED_TIERS}
        assert len(ids) == len(setup.PROFILED_TIERS)


# ---------------------------------------------------------------------------
# _bounded_id: the hash-suffix branch the real corpus does not reach
# ---------------------------------------------------------------------------
def test_the_real_corpus_does_not_yet_need_hash_suffixing() -> None:
    """Canary. The longest real id is 62 of 63 allowed characters.

    One longer table or column name pushes ids into the hash-suffix branch
    below, which has never run against this project. This test is not a
    requirement — it is a tripwire. If it fails, the branch is now load-bearing
    and the tests that follow are the ones that matter.
    """
    longest = max((i for i, _ in _all_scan_ids() + _all_link_ids()), key=len)
    assert len(longest) < ID_LIMIT, (
        f"{longest!r} is {len(longest)} of {ID_LIMIT} characters. Ids now reach the "
        "cap, so _bounded_id's hash-suffix path is live for the first time."
    )


def test_over_long_ids_are_hash_suffixed_and_stay_valid() -> None:
    long_base = "def-t3-" + "a" * 100
    result = setup._bounded_id(long_base)
    assert len(result) <= ID_LIMIT
    assert DATAPLEX_ID.match(result), result


def test_ids_sharing_a_long_prefix_do_not_collide() -> None:
    """Truncation alone would collide; the sha1 suffix is what prevents it."""
    shared = "def-t3-" + "a" * 60
    first, second = (
        setup._bounded_id(shared + "-column-one"),
        setup._bounded_id(shared + "-column-two"),
    )
    assert first != second
    assert len({len(first), len(second)}) == 1


def test_the_hash_is_taken_before_sanitizing() -> None:
    """Two bases that sanitize identically must still differ once hashed.

    `_bounded_id` digests the raw base, not the sanitized form, so inputs that
    differ only in punctuation stay distinct *on the long path*.
    """
    base = "x" * 70
    assert setup._bounded_id(base + "_A") != setup._bounded_id(base + "-A")


def test_punctuation_only_differences_collide_on_the_short_path() -> None:
    """A sharp edge, pinned deliberately rather than discovered in production.

    Below 63 characters `_bounded_id` returns the sanitized string with no
    disambiguation, so `a_b`, `a-b` and `a.b` are one id. The current corpus
    has no such pair. Anything added that differs only in punctuation will
    silently share a resource, and the uniqueness tests above are what catch it.
    """
    assert setup._bounded_id("a_b") == setup._bounded_id("a-b") == setup._bounded_id("a.b")
