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
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from google.cloud import dataplex_v1

if TYPE_CHECKING:
    from types import ModuleType

from bq_context.corpus import cleanup, setup
from bq_context.corpus.manifest import (
    corpus_manifest,
    corpus_prefix,
    provisioned_path,
)

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


# ---------------------------------------------------------------------------
# Corpus profiles
#
# The 15-table corpus cannot separate the tiers: on a converged index all three
# search approaches score 0.967 discovery recall at *every* rung. `hard` appends
# near-neighbour tables that look right and are wrong, making discovery
# selective again.
#
# It is additive and opt-in on purpose. `full-01` has to stay reproducible, so
# the default profile must keep producing byte-for-byte today's corpus.
#
# No ground-truth edits are needed: `metrics.gain_for` returns 0.0 for any table
# that is in neither `must_have` nor `nice_to_have`, and precision already counts
# distractors and unlabelled tables alike.
# ---------------------------------------------------------------------------
def _reload_setup(monkeypatch: pytest.MonkeyPatch, **env: str) -> ModuleType:
    """Re-import setup.py under a given environment.

    The profile is resolved at import, like RESOURCE_PREFIX, so a fixture that
    only sets the variable is too late.
    """
    import importlib

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(setup)


def test_the_default_profile_is_todays_corpus_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE reproducibility guard. Anything that changes this invalidates full-01."""
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="base")
    assert [v["name"] for v in mod.CORPUS] == [v["name"] for v in mod.BASE_CORPUS]
    assert len(mod.CORPUS) == 15


def test_no_profile_set_behaves_as_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORPUS_PROFILE", raising=False)
    import importlib

    mod = importlib.reload(setup)
    assert len(mod.CORPUS) == 15


def test_the_hard_profile_appends_near_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    assert len(mod.CORPUS) == 24
    # Additive: the base tables are still there, unchanged and first.
    assert [v["name"] for v in mod.CORPUS[:15]] == [v["name"] for v in mod.BASE_CORPUS]


def test_an_unknown_profile_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never fall back to base. A typo that silently selects the small corpus
    produces a run that looks fine and measured the wrong thing."""
    import importlib

    monkeypatch.setenv("CORPUS_PROFILE", "haard")
    with pytest.raises(ValueError, match="Unknown CORPUS_PROFILE"):
        importlib.reload(setup)


@pytest.mark.parametrize("profile", ["base", "hard"])
def test_names_and_sources_are_unique(monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    """A duplicate name would silently overwrite a view; a duplicate source would
    put the same table in the corpus twice under two names."""
    mod = _reload_setup(
        monkeypatch, CORPUS_PROFILE=profile, RESOURCE_PREFIX=f"bigquery_context_{profile}"
    )
    names = [v["name"] for v in mod.CORPUS]
    sources = [v["source"] for v in mod.CORPUS]
    assert len(set(names)) == len(names)
    assert len(set(sources)) == len(sources)


def test_every_entry_is_fully_specified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing description would make a hard-corpus table strictly easier to
    ignore than a base one, which biases the very thing being measured."""
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    for view in mod.CORPUS:
        assert set(view) >= {"name", "source", "description"}, view
        assert view["source"].startswith("bigquery-public-data."), view["source"]
        assert view["description"].strip()


def test_the_tables_that_do_not_exist_are_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both were checked against BigQuery during planning and are absent. Listing
    either fails `ensure-infra` forty minutes in, on view creation."""
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    sources = {v["source"] for v in mod.CORPUS}
    assert (
        "bigquery-public-data.epa_historical_air_quality.air_quality_daily_summary" not in sources
    )
    assert "bigquery-public-data.geo_us_boundaries.census_tracts_texas" not in sources


# ---------------------------------------------------------------------------
# Guarding the baseline datasets
# ---------------------------------------------------------------------------
def test_a_profile_cannot_be_mixed_into_the_baseline_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE guard, and the one whose absence is expensive.

    `CORPUS_PROFILE=hard` with the default RESOURCE_PREFIX would add 16 views to
    `bigquery_context_tier0..3`, destroying the reproducibility this whole option
    exists to protect. The damage is not obvious afterwards: the datasets still
    look healthy, preflight still passes, and only the table count betrays it.
    """
    import importlib

    monkeypatch.setenv("CORPUS_PROFILE", "hard")
    monkeypatch.delenv("RESOURCE_PREFIX", raising=False)
    with pytest.raises(ValueError, match="would add tables to the baseline corpus"):
        importlib.reload(setup)


def test_a_distinct_prefix_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    assert mod.tier_dataset(0) == "bigquery_context_hard_tier0"


def test_setup_and_cleanup_agree_on_what_to_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """cleanup.py imports CORPUS and the tier thresholds from setup.py, so it
    removes whatever the *current* environment selects. Run under a different
    profile or prefix than the one that provisioned, and it orphans scans and
    entry links instead of deleting them -- and stale catalog resources survive a
    rebuild, which is how a later run inherits enrichment it was never meant to
    see.
    """
    import importlib

    from bq_context.corpus import cleanup

    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    importlib.reload(cleanup)
    assert [v["name"] for v in cleanup.CORPUS] == [v["name"] for v in mod.CORPUS]
    assert cleanup.PROFILED_TIERS == mod.PROFILED_TIERS
    assert cleanup.GLOSSARY_TIERS == mod.GLOSSARY_TIERS


#: Near-neighbour candidates deliberately left out, and the question each would
#: have corrupted. They are not distractors: for a question that names no place
#: or no grain, each is a *legitimate* answer, so including it would score a
#: defensible retrieval as wrong and make any tier effect uninterpretable.
AMBIGUOUS_RIVALS = {
    "san_francisco_bikeshare.bikeshare_trips": "multi-rel-q1 names no city",
    "san_francisco_bikeshare.bikeshare_station_info": "multi-rel-q1 names no city",
    "new_york_citibike.citibike_trips": "multi-rel-q1 names no city",
    "noaa_gsod.stations": "multi-rel-q2 — also a weather-station registry",
    "epa_historical_air_quality.o3_daily_summary": "multi-rel-q3 — also air quality",
    "sdoh_cdc_wonder_natality.county_natality_by_mother_race": "single-q5 — same measure",
    # The sharpest: multi-disp-q11 asks per-capita *by county*, the labelled
    # answer is ZIP-level and needs a crosswalk, and this is county-level
    # directly -- arguably the better answer, which we would have scored as wrong.
    "census_bureau_acs.county_2018_5yr": "multi-disp-q11 — better grain than the label",
}


def test_ambiguous_rivals_stay_out_of_the_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE integrity guard.

    Adding tables needs no ground-truth edits *only* while every added table is
    plausible-but-wrong. Ten of the 25 questions name no place or no grain, and
    for those a near-neighbour is a legitimate answer rather than a distractor.
    Re-adding one silently corrupts the labels: recall drops for a correct
    retrieval and precision drops with it.

    If one of these is wanted, label it `nice_to_have` on the affected questions
    first -- `experiments/GROUND_TRUTH.md` already defines that as "genuinely
    helps but the question is answerable without it", which is exactly what a
    peer table is.
    """
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    sources = {v["source"] for v in mod.CORPUS}
    for rival, why in AMBIGUOUS_RIVALS.items():
        assert f"bigquery-public-data.{rival}" not in sources, f"{rival}: {why}"


def test_every_added_table_is_wrong_for_every_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """No added table may appear in any question's must_have or nice_to_have.

    That is the property which keeps the existing labels correct: `gain_for`
    returns 0.0 for an unlabelled table, which is right only if the table really
    is never a correct answer.
    """
    import json
    from pathlib import Path

    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    added = {v["name"] for v in mod.NEAR_NEIGHBOUR_CORPUS}
    labelled: set[str] = set()
    for question in json.loads(Path("experiments/questions.json").read_text()):
        relevance = question["relevance"]
        labelled |= set(relevance["must_have"]) | set(relevance.get("nice_to_have", []))
    assert not (added & labelled), f"added tables that are also answers: {sorted(added & labelled)}"


# ---------------------------------------------------------------------------
# Entry-link ids must be scoped to the corpus
#
# Found by provisioning the hard corpus for real. Entry links live in the shared
# `@bigquery` entry group and their id carried no corpus marker, only a tier:
#
#     def-t3-county-fips-us-counties-geo-id
#
# The baseline corpus had already created that id, so setup found "Link exists"
# and skipped -- leaving the hard corpus with *zero* glossary links. preflight
# reported `terms=0` at every rung and warned that tier 2 adds nothing over
# tier 1, which is exactly right: without term links, the tier-2 rung does not
# exist. A whole factor level, silently missing.
#
# RESOURCE_PREFIX already scopes datasets, scan ids and the glossary itself. The
# entry-link namespace was the one place it did not reach.
# ---------------------------------------------------------------------------
def test_the_default_corpus_keeps_its_existing_link_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unprefixed for the default corpus, deliberately.

    Those links already exist in the project. Changing their ids would orphan
    them: cleanup reconstructs ids from this same function, so it would compute
    names that do not match what is deployed and quietly fail to delete them.
    """
    mod = _reload_setup(monkeypatch, RESOURCE_PREFIX="bigquery_context")
    assert mod.definition_link_id(3, "county-fips", "us-counties", "geo-id") == (
        "def-t3-county-fips-us-counties-geo-id"
    )


def test_a_variant_corpus_gets_its_own_link_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fix. Two corpora in one project must not collide in @bigquery."""
    base = _reload_setup(monkeypatch, RESOURCE_PREFIX="bigquery_context").definition_link_id(
        3, "county-fips", "us-counties", "geo-id"
    )
    hard = _reload_setup(monkeypatch, RESOURCE_PREFIX="bigquery_context_hard").definition_link_id(
        3, "county-fips", "us-counties", "geo-id"
    )
    assert hard != base
    assert "bigquery-context-hard" in hard


def test_link_ids_stay_valid_under_a_longer_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The longest id was already 62 of 63 characters. `_bounded_id` hashes on
    overflow, so prefixing must not produce an invalid id."""
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    ids = [
        mod.definition_link_id(tier, "air-quality-measure", view["name"], "arithmetic-mean")
        for tier in mod.GLOSSARY_TIERS
        for view in mod.CORPUS
    ]
    assert len(set(ids)) == len(ids), "prefixing collapsed two ids into one"
    for link_id in ids:
        assert DATAPLEX_ID.match(link_id), link_id
        assert len(link_id) <= ID_LIMIT, f"{link_id} is {len(link_id)}"


# ---------------------------------------------------------------------------
# Recording what was provisioned
#
# The corpus existed only as a Python list in a vendored file. A run's bucket
# recorded its results but not what they were measured against, so answering
# "what was in hard-full-01's corpus?" meant finding the commit and reading
# setup.py at it. The fingerprint is on every cell and in the BigQuery sink;
# this is what a reader lands on after grouping by it.
# ---------------------------------------------------------------------------
def test_the_manifest_describes_every_table_in_the_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    manifest = corpus_manifest(mod)

    assert manifest["table_count"] == len(mod.CORPUS)
    assert [t["name"] for t in manifest["tables"]] == [v["name"] for v in mod.CORPUS]
    assert all(t["source"].startswith("bigquery-public-data.") for t in manifest["tables"])


def test_the_manifest_keeps_the_descriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A description is tier-0 enrichment — schema, in this experiment's terms —
    so it is part of what the corpus *is*, not commentary about it."""
    manifest = corpus_manifest(_reload_setup(monkeypatch, CORPUS_PROFILE="base"))
    assert all(t["description"] for t in manifest["tables"])


def test_the_manifest_names_the_profile_and_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both are environment variables set in a shell; without them recorded, the
    two things that decide which corpus got built leave no trace."""
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    manifest = corpus_manifest(mod)
    assert manifest["corpus_profile"] == "hard"
    assert manifest["resource_prefix"] == "bigquery_context_hard"
    assert manifest["tiers"] == mod.TIERS


def test_two_profiles_produce_different_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    base = corpus_manifest(_reload_setup(monkeypatch, CORPUS_PROFILE="base"))
    hard = corpus_manifest(
        _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    )
    assert hard["table_count"] > base["table_count"]
    assert {t["name"] for t in base["tables"]} < {t["name"] for t in hard["tables"]}


def test_the_manifest_is_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is written to GCS, so a value json cannot serialise is a runtime
    failure in the one command whose job is to leave a record behind."""
    import json

    manifest = corpus_manifest(_reload_setup(monkeypatch, CORPUS_PROFILE="base"))
    assert json.loads(json.dumps(manifest)) == manifest


def test_the_corpus_prefix_is_keyed_by_fingerprint() -> None:
    """Not by resource prefix. A prefix is reused as enrichment changes and the
    record would be overwritten; fingerprints accumulate instead."""
    assert corpus_prefix("13f9fcb4") == "corpus/13f9fcb4"


@pytest.mark.parametrize("hostile", ["", "  ", "a/b", ".."])
def test_a_fingerprint_that_would_escape_its_folder_is_refused(hostile: str) -> None:
    with pytest.raises(ValueError, match="fingerprint"):
        corpus_prefix(hostile)


def test_the_provisioned_record_is_keyed_by_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ensure-infra` cannot know the fingerprint: that is a hash of the
    *provisioned* state, which does not exist until it finishes. It records what
    it built under the prefix it built it in, and preflight adds the fingerprint
    afterwards."""
    mod = _reload_setup(monkeypatch, CORPUS_PROFILE="hard", RESOURCE_PREFIX="bigquery_context_hard")
    assert provisioned_path(mod.RESOURCE_PREFIX) == "corpus/provisioned/bigquery_context_hard.json"
