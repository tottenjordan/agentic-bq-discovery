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

from bq_context.runner.planner import corpus_fingerprint, questions_fingerprint

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
        "from bq_context.runner.planner import corpus_fingerprint, questions_fingerprint;"
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


# ---------------------------------------------------------------------------
# The question-set fingerprint
#
# Same job as the corpus one, one layer up: `code_version` describes the
# questions only while they are baked into the image. Once `--questions` can
# point somewhere else, swapping the file and resubmitting at the same commit
# would return cells scored against the *old* questions.
#
# The asymmetry with `corpus_fingerprint` is the thing to get right. That one
# sorts its ladder because tier order is presentation. Here order is *content*:
# `--limit` takes a deterministic prefix (`list(questions)[:limit]`), so
# reordering the file changes which questions a smoke or pilot run measures
# while editing nothing.
# ---------------------------------------------------------------------------
def _q(qid: str, must: list[str], *, text: str = "t", distractor: list[str] | None = None) -> dict:
    return {
        "id": qid,
        "category": "single-table",
        "question": text,
        "relevance": {
            "must_have": must,
            "nice_to_have": [],
            "distractor": distractor or [],
        },
    }


QUESTIONS = {
    "q1": _q("q1", ["trips"], text="busiest stations?"),
    "q2": _q("q2", ["taxi"], text="tips by hour?", distractor=["taxi_zone_geom"]),
}


def test_the_same_question_set_fingerprints_the_same() -> None:
    assert questions_fingerprint(QUESTIONS) == questions_fingerprint(dict(QUESTIONS))


def test_editing_a_question_changes_it() -> None:
    edited = {**QUESTIONS, "q1": _q("q1", ["trips"], text="quietest stations?")}
    assert questions_fingerprint(edited) != questions_fingerprint(QUESTIONS)


def test_reordering_the_file_changes_it() -> None:
    """THE asymmetry with corpus_fingerprint, and the reason this is not a
    sorted hash. `--limit` takes a deterministic prefix, so `--limit 1` against
    a reordered file measures a different question with no edit anywhere."""
    reordered = {"q2": QUESTIONS["q2"], "q1": QUESTIONS["q1"]}
    assert questions_fingerprint(reordered) != questions_fingerprint(QUESTIONS)


def test_reordering_a_relevance_list_does_not_change_it() -> None:
    """The other half of the same decision. Order inside `must_have` carries
    nothing, so a reshuffled list is the same experiment and must stay a cache
    hit — a fingerprint that moves spuriously turns every resume into a
    12-hour resweep."""
    shuffled = {**QUESTIONS, "q1": _q("q1", ["b", "a"], text="busiest stations?")}
    ordered = {**QUESTIONS, "q1": _q("q1", ["a", "b"], text="busiest stations?")}
    assert questions_fingerprint(shuffled) == questions_fingerprint(ordered)


def test_changing_a_distractor_changes_it() -> None:
    """Distractors are not decoration: the trap questions exist to catch a
    retriever that takes the bait, so swapping one is a different experiment."""
    swapped = {**QUESTIONS, "q2": _q("q2", ["taxi"], text="tips by hour?", distractor=["other"])}
    assert questions_fingerprint(swapped) != questions_fingerprint(QUESTIONS)


def test_renaming_a_question_id_changes_it() -> None:
    """`cell_key` is built from the id, so a rename makes every prior cell
    unmatchable — a different experiment by any useful definition."""
    renamed = {"q9": QUESTIONS["q1"], "q2": QUESTIONS["q2"]}
    assert questions_fingerprint(renamed) != questions_fingerprint(QUESTIONS)


def test_the_question_fingerprint_is_stable_across_processes() -> None:
    """Same reason as the corpus one: the submitting CLI computes it and the
    shard re-checks it, in different processes. A salted hash() would read as
    flakiness rather than a bug."""
    import subprocess
    import sys

    code = (
        "from bq_context.runner.planner import questions_fingerprint;"
        f"print(questions_fingerprint({QUESTIONS!r}))"
    )
    runs = {
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(runs) == 1
    assert runs == {questions_fingerprint(QUESTIONS)}


def test_the_question_fingerprint_is_short_and_hex() -> None:
    fp = questions_fingerprint(QUESTIONS)
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_an_empty_question_set_is_handled() -> None:
    """Not a crash: `--limit` can produce one, and a clear downstream failure
    beats a traceback from the hasher."""
    assert questions_fingerprint({})


def test_a_question_missing_optional_fields_is_handled() -> None:
    """A hand-written set may omit `nice_to_have` or `relevance` entirely. That
    is a question with no expected answer, which preflight should reject — but
    the hasher is not the place to raise."""
    assert questions_fingerprint({"q1": {"id": "q1", "question": "t"}})
