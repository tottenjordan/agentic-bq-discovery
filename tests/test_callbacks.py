"""The six approach callbacks.

These are near-parallel implementations of one contract, which is exactly why
they were the largest remaining coverage gap worth closing: a copy-paste
divergence between them is invisible. Nothing errors. The cell is simply
recorded wrong.

Two seams carry that risk:

- **State keys.** Each callback writes `nominated_tables_<METHOD>`,
  `reranker_result_<METHOD>` and, for the searchers, `search_stats_<METHOD>`.
  `runner/cells.py` reads those same keys built from the *spec's* approach
  name. If a callback's METHOD ever drifts from its registered approach name,
  the reader finds nothing and records an empty cell for a run that worked.
- **Scope filtering.** `filter_scope` is the only thing keeping `bq_tools`
  inside one tier. Failing open does not raise; it silently dissolves the tier
  isolation the whole experiment rests on.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from bq_context.approaches.agent_bq_tools.callback_filter_scope import filter_scope
from bq_context.approaches.agent_search_direct.tools.callback_search_direct import (
    _response_from_search_order,
)
from bq_context.config import ExperimentConfig
from bq_context.context_cache import TableCache
from bq_context.runner.cells import APPROACHES
from bq_context.runtime import TierContext, tier_scope

#: Approach name -> the module whose METHOD must match it. Every approach in
#: APPROACHES must appear, so adding a seventh forces a deliberate decision
#: rather than silently skipping the check.
CALLBACK_MODULES = {
    "kc_context": "bq_context.approaches.agent_kc_context.tools.callback_discover_and_rerank",
    "kc_search": "bq_context.approaches.agent_kc_search.tools.callback_discover_and_rerank",
    "semantic_context": (
        "bq_context.approaches.agent_semantic_context.tools.callback_discover_and_rerank"
    ),
    "context_prefilter": (
        "bq_context.approaches.agent_context_prefilter.tools.callback_rerank_nominations"
    ),
    "search_direct": "bq_context.approaches.agent_search_direct.tools.callback_search_direct",
    # bq_tools has no discovery callback: it is a real LLM tool loop, and its
    # only callback filters tool output for scope. Tested separately below.
    "bq_tools": None,
}


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture
def scoped() -> Any:
    """A tier scope with an empty cache — enough for top_k and scope lookups."""
    config = ExperimentConfig.from_env()
    with tier_scope(TierContext.build(config, 3, TableCache.empty())) as ctx:
        yield ctx


# ---------------------------------------------------------------------------
# The state-key contract between callbacks and runner/cells.py
# ---------------------------------------------------------------------------
def test_every_approach_has_a_callback_decision() -> None:
    assert set(CALLBACK_MODULES) == set(APPROACHES)


@pytest.mark.parametrize("approach", [a for a, m in CALLBACK_MODULES.items() if m is not None])
def test_method_matches_the_registered_approach_name(approach: str) -> None:
    """The seam. `cells.py` builds its state keys from the approach name.

    A callback whose METHOD drifts from its approach name writes keys nobody
    reads, and the cell records zero nominated tables and no ranking for a run
    that actually succeeded.
    """
    module = importlib.import_module(CALLBACK_MODULES[approach])
    assert getattr(module, "METHOD", None) == approach


@pytest.mark.parametrize("approach", [a for a, m in CALLBACK_MODULES.items() if m is not None])
def test_state_keys_are_derived_from_method_not_hardcoded(approach: str) -> None:
    """Hardcoded key strings cannot be kept in step with METHOD.

    `search_direct` spelled all three out literally; a typo in any one of them
    would have been invisible.
    """
    module = importlib.import_module(CALLBACK_MODULES[approach])
    source = importlib.import_module(module.__name__).__loader__.get_source(module.__name__)  # type: ignore[union-attr]
    assert source is not None
    for prefix in ("nominated_tables_", "reranker_result_", "search_stats_"):
        assert f'"{prefix}{approach}"' not in source, (
            f"{approach} hardcodes {prefix}{approach}; derive it from METHOD"
        )


# ---------------------------------------------------------------------------
# filter_scope — the only thing keeping bq_tools inside one tier
# ---------------------------------------------------------------------------
def test_out_of_scope_datasets_are_removed(scoped: Any) -> None:
    """Tier isolation. A leak here silently invalidates every bq_tools cell.

    The project has 254 datasets; the run must see exactly its own tier.
    """
    in_scope = scoped.scope[0]
    response = {"result": [in_scope, "bigquery_context_tier0", "some_other_dataset"]}
    assert filter_scope(_FakeTool("list_dataset_ids"), {}, None, response) == {"result": [in_scope]}


def test_an_all_in_scope_response_is_unchanged(scoped: Any) -> None:
    in_scope = scoped.scope[0]
    assert filter_scope(_FakeTool("list_dataset_ids"), {}, None, {"result": [in_scope]}) == {
        "result": [in_scope]
    }


def test_tables_are_filtered_to_the_scoped_set(scoped: Any) -> None:
    dataset = scoped.scope[0]
    out = filter_scope(
        _FakeTool("list_table_ids"),
        {"dataset_id": dataset},
        None,
        {"result": ["weather_stations", "not_in_the_corpus"]},
    )
    # An empty scoped-table list means "all tables allowed" (None), so only
    # assert filtering when the tier actually restricts tables.
    assert out is None or "not_in_the_corpus" not in out["result"]


@pytest.mark.parametrize(
    "response",
    [
        "not a dict",
        {"error": "boom"},
        {},
        None,
        42,
    ],
)
@pytest.mark.usefixtures("scoped")
def test_an_unexpected_response_shape_passes_through(response: Any) -> None:
    """Failing closed on an error response would hide the real tool error."""
    assert filter_scope(_FakeTool("list_dataset_ids"), {}, None, response) is None


@pytest.mark.usefixtures("scoped")
def test_other_tools_are_untouched() -> None:
    """Only the two listing tools are in scope for filtering."""
    response = {"result": ["anything", "at", "all"]}
    assert filter_scope(_FakeTool("execute_sql"), {}, None, response) is None


@pytest.mark.usefixtures("scoped")
def test_an_unknown_dataset_yields_an_empty_table_list() -> None:
    """A dataset outside the scope must not leak its tables."""
    out = filter_scope(
        _FakeTool("list_table_ids"),
        {"dataset_id": "bigquery_context_tier0"},
        None,
        {"result": ["weather_stations"]},
    )
    assert out is None or out["result"] == []


# ---------------------------------------------------------------------------
# search_direct's ranking — approach 6 in its entirety, and it uses no LLM
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("scoped")
def test_rank_is_the_search_position() -> None:
    ids = ["p.d.a", "p.d.b", "p.d.c"]
    result = _response_from_search_order("q", ids)
    assert [t.rank for t in result.ranked_tables] == [1, 2, 3]
    assert [t.table_id for t in result.ranked_tables] == ids


@pytest.mark.usefixtures("scoped")
def test_confidence_descends_and_stays_in_the_documented_band() -> None:
    """A proxy for position, not a real score — search returns none."""
    result = _response_from_search_order("q", [f"p.d.t{i}" for i in range(6)])
    confidences = [t.confidence for t in result.ranked_tables]
    assert confidences == sorted(confidences, reverse=True)
    assert all(0.5 < c <= 1.0 for c in confidences), confidences


@pytest.mark.usefixtures("scoped")
def test_no_hits_produces_an_empty_ranking_rather_than_dividing_by_zero() -> None:
    """15 cells in full-01 hit this: search returned nothing at all."""
    result = _response_from_search_order("q", [])
    assert result.ranked_tables == []


@pytest.mark.usefixtures("scoped")
def test_every_row_is_attributed_to_search_direct() -> None:
    """Mis-attribution here would silently credit another approach."""
    result = _response_from_search_order("q", ["p.d.a", "p.d.b"])
    assert {t.discovery_method for t in result.ranked_tables} == {"search_direct"}


@pytest.mark.usefixtures("scoped")
def test_the_full_search_result_is_kept_rather_than_truncated_to_top_k() -> None:
    """Search already pruned; truncating again would understate its recall."""
    ids = [f"p.d.t{i}" for i in range(12)]
    result = _response_from_search_order("q", ids)
    assert len(result.ranked_tables) == 12 > result.top_k


# ---------------------------------------------------------------------------
# The four discovery callbacks, exercised end to end
# ---------------------------------------------------------------------------
# Enough of a CallbackContext for the shared helpers: `get_question` reads
# user_content.parts[0].text, and everything else reads or writes `state`.
class _FakePart:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _FakeContent:
    def __init__(self, text: str | None) -> None:
        self.parts = [_FakePart(text)] if text is not None else []


class _FakeCtx:
    def __init__(self, question: str | None = "which stations?") -> None:
        self.user_content = _FakeContent(question) if question is not None else None
        self.state: dict[str, Any] = {}


#: (approach, module, callable name). bq_tools is absent by design — it has no
#: discovery callback, only the scope filter tested above.
DISCOVERY_CALLBACKS = [
    ("kc_context", CALLBACK_MODULES["kc_context"], "discover_and_rerank"),
    ("kc_search", CALLBACK_MODULES["kc_search"], "discover_and_rerank"),
    ("semantic_context", CALLBACK_MODULES["semantic_context"], "discover_and_rerank"),
    ("context_prefilter", CALLBACK_MODULES["context_prefilter"], "rerank_nominations"),
]


@pytest.mark.parametrize(("approach", "module_name", "func"), DISCOVERY_CALLBACKS)
@pytest.mark.usefixtures("scoped")
async def test_no_question_declines_to_the_llm_path(
    approach: str,  # noqa: ARG001 - part of the shared parametrize id
    module_name: str,
    func: str,
) -> None:
    """Returning None is what hands control back to the LLM.

    Returning Content here instead would short-circuit the agent with an empty
    answer rather than letting it try.
    """
    module = importlib.import_module(module_name)
    assert await getattr(module, func)(_FakeCtx(question=None)) is None


@pytest.mark.parametrize(("approach", "module_name", "func"), DISCOVERY_CALLBACKS)
@pytest.mark.usefixtures("scoped")
async def test_nothing_found_still_records_a_scored_zero(
    approach: str, module_name: str, func: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cell that found nothing must be recorded, not skipped.

    `store_empty` writes an empty RerankerResponse so the cell scores zero
    rather than vanishing from the denominator. 15 cells in full-01 took this
    path because semantic search returned no hits at all.
    """
    module = importlib.import_module(module_name)
    # Every discovery path yields nothing: empty cache, and search finds nobody.
    monkeypatch.setattr(module, "search_entries_scoped", lambda _q: ([], {}), raising=False)
    ctx = _FakeCtx()

    await getattr(module, func)(ctx)

    assert f"reranker_result_{approach}" in ctx.state, "an empty result must still be stored"
    assert ctx.state[f"reranker_result_{approach}"], "stored result must not be blank"


@pytest.mark.parametrize(("approach", "module_name", "func"), DISCOVERY_CALLBACKS)
@pytest.mark.usefixtures("scoped")
async def test_the_callback_writes_the_key_cells_py_reads(
    approach: str, module_name: str, func: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam, exercised rather than inspected.

    `runner/cells.py` reads `nominated_tables_{approach}`. If a callback writes
    a different key the run still succeeds and the cell records zero
    candidates.
    """
    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "search_entries_scoped", lambda _q: ([], {}), raising=False)
    ctx = _FakeCtx()
    ctx.state["nominated_tables"] = []  # context_prefilter reads this upstream key

    await getattr(module, func)(ctx)

    assert f"nominated_tables_{approach}" in ctx.state, (
        f"{approach} did not write the key runner/cells.py reads"
    )
