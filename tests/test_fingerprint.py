"""The corpus fingerprint that closes the last stale-cache hole.

`run_shard` is keyed on `code_version`, so changing the code invalidates it. The
*corpus* is invisible to that key: re-run `ensure-infra` to change enrichment,
resubmit with the same SHA, and KFP returns cells scored against the old corpus —
a green run with a plausible, wrong answer.

The fingerprint closes that. But the obvious implementation closes it by
destroying the cache instead, which is why `test_it_ignores_capsule_bytes` is the
load-bearing test here rather than an edge case.
"""

from __future__ import annotations

import pytest

from bq_context.runner.planner import corpus_fingerprint

LADDER = [
    {"tier": 0, "tables": 15, "bytes": 49089, "profiled": 0, "terms": 0, "aspects": []},
    {"tier": 1, "tables": 15, "bytes": 118275, "profiled": 209, "terms": 0, "aspects": []},
    {"tier": 2, "tables": 15, "bytes": 119882, "profiled": 209, "terms": 18, "aspects": []},
    {
        "tier": 3,
        "tables": 15,
        "bytes": 122346,
        "profiled": 209,
        "terms": 18,
        "aspects": ["overview"],
    },
]


# ---------------------------------------------------------------------------
# Stability — the half that protects the cache
# ---------------------------------------------------------------------------
def test_it_ignores_capsule_bytes() -> None:
    """THE test. Including `bytes` would make every shard miss cache, every run.

    `bytes` is `len(cache.all_detailed())` — the raw lookupContext capsule,
    carrying Dataplex entry timestamps and dataProfile float statistics that
    shift as DataScans re-run. `_tier_profile`'s own docstring calls byte deltas
    "a trap in both directions". A fingerprint that moves on every run turns a
    15-minute resume into a 12-hour resweep, and silently makes `code_version`
    irrelevant because everything is already invalidated.
    """
    drifted = [{**rung, "bytes": rung["bytes"] + 137} for rung in LADDER]
    assert corpus_fingerprint(LADDER) == corpus_fingerprint(drifted)


def test_it_is_stable_across_calls() -> None:
    assert corpus_fingerprint(LADDER) == corpus_fingerprint(list(LADDER))


def test_it_does_not_depend_on_row_order() -> None:
    """Callers build the ladder from a dict; iteration order must not leak in."""
    assert corpus_fingerprint(LADDER) == corpus_fingerprint(list(reversed(LADDER)))


def test_it_does_not_depend_on_aspect_order() -> None:
    rung = {"tier": 3, "tables": 15, "bytes": 1, "profiled": 9, "terms": 2, "aspects": ["b", "a"]}
    assert corpus_fingerprint([rung]) == corpus_fingerprint([{**rung, "aspects": ["a", "b"]}])


def test_it_is_stable_across_processes() -> None:
    """Must not use builtin hash(), which is salted per process by default.

    A per-process value would change on every pipeline task and defeat the cache
    in a way that looks like flakiness rather than a bug.
    """
    import subprocess
    import sys

    code = (
        "from bq_context.runner.planner import corpus_fingerprint;"
        f"print(corpus_fingerprint({LADDER!r}))"
    )
    runs = {
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1
    assert runs.pop() == corpus_fingerprint(LADDER)


# ---------------------------------------------------------------------------
# Sensitivity — the half that closes the hole
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("terms", 24),  # e.g. all_schema_fields=true recovering truncated links
        ("profiled", 150),  # a DataScan failed to attach
        ("tables", 14),  # a view went missing
        ("aspects", ["guidelines"]),  # tier 3 aspect changed
    ],
)
def test_it_moves_when_enrichment_shape_changes(field: str, value: object) -> None:
    changed = [{**LADDER[0]}, *LADDER[1:-1], {**LADDER[-1], field: value}]
    assert corpus_fingerprint(LADDER) != corpus_fingerprint(changed)


def test_a_missing_tier_changes_it() -> None:
    """Running a 2-tier subset must not collide with the full 4-tier ladder."""
    assert corpus_fingerprint(LADDER) != corpus_fingerprint(LADDER[:2])


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------
def test_it_is_short_and_hex() -> None:
    """It travels as a KFP input parameter and appears in logs; keep it legible."""
    value = corpus_fingerprint(LADDER)
    assert len(value) == 16
    assert all(c in "0123456789abcdef" for c in value)


def test_an_empty_ladder_is_handled() -> None:
    """preflight can be run for a single tier, or fail before building rungs."""
    assert corpus_fingerprint([])
