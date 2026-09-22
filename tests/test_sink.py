"""The BigQuery query sink.

A sink, not the system of record — GCS `merged/results.jsonl` is authoritative.
The tests that matter here are about shape, because a shape mistake makes the
table *write-only*: it loads without error and then answers every query with
NULL. That happened once already, and `test_payload_is_an_object_not_a_string`
is the regression.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bq_context.config import ExperimentConfig
from bq_context.scoring import sink

CELL: dict[str, Any] = {
    "cell_key": "q1|kc_search|tier3|run0",
    "question_id": "q1",
    "approach": "kc_search",
    "tier": 3,
    "run_idx": 0,
    "status": "ok",
    "written_at": "2026-09-22T07:00:00+00:00",
    "code_version": "abc1234",
    "category": "single-table",
    "question": "which stations?",
    "relevance": {"must_have": ["weather_stations"], "nice_to_have": []},
    "nominated": ["weather_stations"],
    "nominated_count": 1,
    "ranked_tables": [{"table_id": "p.d.weather_stations", "rank": 1, "confidence": 0.9}],
    "ranked_count": 1,
    "search_stats": {"raw_search_count": 6, "out_of_scope_dropped": 0},
    "latency_s": 4.1,
    "reranker_prompt_tokens": 100,
    "reranker_output_tokens": 20,
    "reranker_total_tokens": 120,
    "reranker_calls": 1,
    "adk_tool_calls": 0,
    "cache_warm_s": 0.0,
    "error_type": "",
    "error_message": "",
    "attempts": 1,
}


@pytest.fixture
def config() -> ExperimentConfig:
    return ExperimentConfig.from_env()


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------
def test_payload_is_an_object_not_a_string() -> None:
    """Regression. Handing a JSON column a `str` makes the table write-only.

    BigQuery stores it as a JSON *string scalar*, so `JSON_TYPE(payload)` is
    'string' and every `JSON_VALUE(payload.x)` returns NULL. It loads cleanly,
    which is what makes it dangerous — it was only caught by querying the table
    rather than trusting the load job's success.
    """
    payload = sink.to_row(CELL, "e1")["payload"]
    assert isinstance(payload, dict), "must be a dict; json.dumps() here is the bug"
    assert not isinstance(payload, str)


def test_payload_carries_exactly_the_nested_fields() -> None:
    payload = sink.to_row(CELL, "e1")["payload"]
    assert set(payload) == set(sink.PAYLOAD_FIELDS)
    assert payload["search_stats"]["raw_search_count"] == 6
    assert payload["relevance"]["must_have"] == ["weather_stations"]


def test_nested_fields_are_not_also_left_as_top_level_columns() -> None:
    """Storing them twice would double the table for no query benefit."""
    row = sink.to_row(CELL, "e1")
    assert not set(sink.PAYLOAD_FIELDS) & set(row) - {"payload"}


def test_experiment_id_is_stamped_on_every_row() -> None:
    """The table holds every experiment; without this they are indistinguishable."""
    assert sink.to_row(CELL, "full-01")["experiment_id"] == "full-01"


def test_scalar_columns_are_promoted_out_of_the_payload() -> None:
    row = sink.to_row(CELL, "e1")
    for name, _type in sink.SCALAR_COLUMNS:
        assert name in row, f"{name} should be a real column"


def test_unknown_keys_are_dropped() -> None:
    """The payload is four known fields, not a dumping ground for new keys."""
    row = sink.to_row({**CELL, "some_future_field": 1}, "e1")
    assert "some_future_field" not in row
    assert "some_future_field" not in row["payload"]


def test_a_missing_optional_field_is_simply_absent() -> None:
    """Load jobs treat an absent key as NULL; emitting None would be equivalent
    but makes rows larger for 3,000 cells."""
    sparse = {k: v for k, v in CELL.items() if k != "error_type"}
    assert "error_type" not in sink.to_row(sparse, "e1")


def test_rows_from_preserves_order_and_count() -> None:
    rows = list(sink.rows_from([CELL, {**CELL, "cell_key": "k2"}], "e1"))
    assert [r["cell_key"] for r in rows] == ["q1|kc_search|tier3|run0", "k2"]


def test_every_row_is_json_serialisable() -> None:
    """load_table_from_json serialises these; a stray object fails mid-load."""
    json.dumps(list(sink.rows_from([CELL], "e1")))


# ---------------------------------------------------------------------------
# Table design
# ---------------------------------------------------------------------------
def test_partitioned_by_write_time_and_clustered_for_the_common_filter() -> None:
    """Almost every query is `WHERE approach = ... AND tier = ...`."""
    assert sink.PARTITION_FIELD == "written_at"
    assert sink.CLUSTER_FIELDS == ("approach", "tier")


def test_the_scalar_columns_match_the_cell_model() -> None:
    """Catches a Cell field added without deciding column-or-payload.

    A new field silently landing in neither is invisible: the load succeeds and
    the data is only in GCS.
    """
    from bq_context.runner.models import Cell

    covered = {name for name, _ in sink.SCALAR_COLUMNS} | set(sink.PAYLOAD_FIELDS)
    uncovered = set(Cell.model_fields) - covered
    assert not uncovered, f"Cell fields in neither a column nor the payload: {sorted(uncovered)}"


def test_table_id_is_fully_qualified(config: ExperimentConfig) -> None:
    assert sink.table_id(config) == f"{config.project}.{sink.DATASET}.{sink.TABLE}"
    assert sink.DATASET == "bigquery_context_results"
