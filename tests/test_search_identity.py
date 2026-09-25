"""Semantic search answers different principals differently.

Dataplex returned different tables to ``bq-context-pipeline`` than to the
developer, for the same query at the same moment: tier-0 recall 0.292 against
0.625 on the enrichment set. Every pipeline number is what the SA sees and every
local re-check is what the developer sees, so a local re-run could neither
confirm nor refute a pipeline result. That is how `full-01`'s tier effect came to
be blamed on index warming.

Preflight already had `--impersonate` to "check as the pipeline SA". It reached
the table cache and not the search, so the one check that could have seen this
searched as the developer and reported agreement. These tests pin both halves:
the search really runs as the SA, and a disagreement is reported. The preflight
wiring tests live in `test_cli.py` beside the other preflight wiring tests.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from bq_context import discovery_common
from bq_context.cli import assess_search_identity

if TYPE_CHECKING:
    import pytest

SA = "bq-context-pipeline@test-project.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------
def test_agreement_produces_no_warning() -> None:
    same = {"tier0/q1": ("hurricanes",), "tier3/q1": ("hurricanes", "us_counties")}
    assert assess_search_identity(same, dict(same), SA) == []


def test_disagreement_names_the_pair_and_the_identity() -> None:
    """The observed case: `cat-knots` at tier 0 found `hurricanes` for the
    developer and two wrong tables for the SA."""
    mine = {"tier0/cat-knots": ("hurricanes",), "tier1/cat-knots": ("hurricanes",)}
    theirs = {
        "tier0/cat-knots": ("air_quality_annual_summary", "austin_crime"),
        "tier1/cat-knots": ("hurricanes",),
    }
    (warning,) = assess_search_identity(mine, theirs, SA)
    assert "tier0/cat-knots" in warning
    assert "tier1/cat-knots" not in warning, "an agreeing pair must not be named"
    assert SA in warning
    assert "1 of 2" in warning


def test_pairs_missing_from_one_side_are_not_evidence() -> None:
    assert assess_search_identity({"tier0/q1": ("a",)}, {"tier1/q1": ("b",)}, SA) == []


def test_a_long_list_is_summarised_rather_than_dumped() -> None:
    mine = {f"tier0/q{i:02}": ("a",) for i in range(20)}
    theirs = dict.fromkeys(mine, ("b",))
    (warning,) = assess_search_identity(mine, theirs, SA)
    assert "20 of 20" in warning
    assert "(+12 more)" in warning


# ---------------------------------------------------------------------------
# Credentials reach the Dataplex client
# ---------------------------------------------------------------------------
def test_search_runs_as_the_credentials_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug: `CatalogServiceClient()` took ADC unconditionally."""
    built: list[object] = []

    class _Client:
        def __init__(self, credentials: object = None) -> None:
            built.append(credentials)

        def search_entries(self, request: object) -> list:  # noqa: ARG002
            return []

    monkeypatch.setattr(discovery_common.dataplex_v1, "CatalogServiceClient", _Client)
    monkeypatch.setattr(
        discovery_common,
        "current_tier",
        lambda: SimpleNamespace(config=SimpleNamespace(project="p")),
    )
    monkeypatch.setattr(discovery_common, "get_datasets", lambda: ["d"])

    sentinel = object()
    discovery_common.search_entries_scoped("q", sentinel)
    discovery_common.search_entries_scoped("q")
    assert built == [sentinel, None]


def test_the_bound_search_forwards_its_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """The middle link, between preflight and the client. Found by mutation:
    with the client test and the preflight wiring test both green, dropping the
    argument here still made every search run as ADC."""
    import contextlib

    from bq_context import cli, runtime

    seen: list[object] = []
    monkeypatch.setattr(
        discovery_common,
        "search_entries_scoped",
        lambda _q, credentials=None: (seen.append(credentials), ([], {}))[1],
    )
    monkeypatch.setattr(runtime.TierContext, "build", classmethod(lambda *_a, **_k: object()))
    monkeypatch.setattr(runtime, "tier_scope", lambda _ctx: contextlib.nullcontext())

    sentinel = object()
    cli._live_search(SimpleNamespace(), sentinel)(0, "q")  # ty: ignore[invalid-argument-type]
    assert seen == [sentinel]
