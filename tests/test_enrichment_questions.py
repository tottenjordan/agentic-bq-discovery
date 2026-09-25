"""The enrichment-dependent question set must stay enrichment-dependent.

`experiments/questions-enrichment.json` exists to test one thing: whether
catalog enrichment helps an agent find a table *when the question's vocabulary
lives only in the catalog*. That property is fragile in a way nothing else here
is — reword one question with a word from the table's description and it becomes
answerable at tier 0, the tier response flattens for that cell, and the run
reads as evidence about enrichment when it is really evidence about the wording.

The first draft of this set failed exactly that way: 12 of 15 target pairs had a
tier-0 lexical path, because the corpus descriptions are rich enough to pre-empt
most of the glossary. See docs/notes/enrichment-dependent-questions.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bq_context.corpus import setup

SET_PATH = Path("experiments/questions-enrichment.json")

#: Words too common to imply a lexical path to any particular table.
STOP = frozenset(
    # A prose list, not a literal: ruff rewrites `.split()` into 40 lines of
    # quoted strings, which is unreadable for something whose whole job is to
    # be skimmed and edited.
    "a an the of in on for and or to with by from is are as at each do does how which what "  # noqa: SIM905
    "many more than most their there where who whose into over per show map include its "
    "only ever people same against also between".split()
)


def _tier0_text() -> dict[str, str]:
    """What an agent can read at tier 0: the view name and its description.

    Descriptions count as schema in this experiment — that was a deliberate
    decision, and it is the reason this guard has to exist.
    """
    return {
        v["name"]: f"{v['name'].replace('_', ' ')} {v['description']}".lower() for v in setup.CORPUS
    }


def _content_words(question: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", question.lower()) if w not in STOP}


def _questions() -> list[dict]:
    raw = json.loads(SET_PATH.read_text())
    return raw["questions"] if isinstance(raw, dict) else raw


def test_the_set_exists_and_is_not_empty() -> None:
    """Guards the guard: a renamed file would make every test below vacuous."""
    assert _questions()


@pytest.mark.parametrize("question", _questions(), ids=lambda q: q["id"])
def test_no_question_has_a_tier_zero_path_to_its_answer(question: dict) -> None:
    """THE guard. A content word shared with the target's tier-0 text means the
    question is findable by lexical match before any enrichment is applied."""
    if question["id"].startswith("ctrl"):
        pytest.skip("controls are deliberately answerable at tier 0")

    tier0 = _tier0_text()
    words = _content_words(question["question"])
    for table in question["relevance"]["must_have"]:
        shared = sorted(w for w in words if w in tier0[table])
        assert not shared, (
            f"{question['id']} shares {shared} with {table}'s tier-0 text, so it can "
            f"be found by lexical match without the catalog. Reword it using "
            f"vocabulary that appears only in the glossary definition."
        )


def test_the_controls_are_answerable_at_tier_zero() -> None:
    """The other half. Without a control that *is* findable at tier 0, a flat
    result cannot distinguish "enrichment did not help" from "this set is just
    harder everywhere"."""
    tier0 = _tier0_text()
    controls = [q for q in _questions() if q["id"].startswith("ctrl")]
    assert controls, "the set has no control questions"
    for q in controls:
        words = _content_words(q["question"])
        assert any(w in tier0[t] for w in words for t in q["relevance"]["must_have"]), q["id"]


def test_every_referenced_table_is_in_the_corpus() -> None:
    """`preflight` enforces this against the live corpus; this catches it in CI,
    where there is no project to ask."""
    known = {v["name"] for v in setup.CORPUS}
    for q in _questions():
        for field in ("must_have", "nice_to_have", "distractor"):
            unknown = set(q["relevance"].get(field, [])) - known
            assert not unknown, f"{q['id']}.{field} references {sorted(unknown)}"


def test_every_question_has_an_expected_answer() -> None:
    for q in _questions():
        assert q["relevance"].get("must_have"), f"{q['id']} has no must_have tables"
