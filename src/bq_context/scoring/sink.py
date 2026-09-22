"""Load merged results into BigQuery for ad-hoc querying.

**A sink, not the system of record.** `merged/results.jsonl` in GCS is
authoritative; this table is a convenience for slicing 3,000 cells with SQL. So
a load failure is reported and swallowed rather than failing a sweep — the data
is already durable, and re-running `merge` re-loads it.

Loaded once per experiment by a load job, never streamed. Streaming from 24
concurrent shards would add insert quotas and partial-commit semantics to a
problem append-only JSONL already solves.

Shape: the scalar fields every query filters or aggregates on get real columns;
the variable-width parts (`relevance`, `nominated`, `ranked_tables`,
`search_stats`) go into one `JSON` column. That keeps the table stable when a
new approach reports a new statistic, which would otherwise be a schema
migration.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from google.auth.credentials import Credentials

    from bq_context.config import ExperimentConfig

logger = logging.getLogger(__name__)

#: Dataset holding every experiment's cells. One table, `experiment_id` scoped.
DATASET = "bigquery_context_results"
TABLE = "cells"

#: Columns promoted out of the payload because queries filter or aggregate on
#: them. Everything else stays in `payload` so a new statistic is not a
#: schema migration.
SCALAR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("experiment_id", "STRING"),
    ("cell_key", "STRING"),
    ("question_id", "STRING"),
    ("approach", "STRING"),
    ("tier", "INTEGER"),
    ("run_idx", "INTEGER"),
    ("status", "STRING"),
    ("written_at", "TIMESTAMP"),
    ("code_version", "STRING"),
    ("category", "STRING"),
    ("question", "STRING"),
    ("nominated_count", "INTEGER"),
    ("ranked_count", "INTEGER"),
    ("latency_s", "FLOAT"),
    ("reranker_prompt_tokens", "INTEGER"),
    ("reranker_output_tokens", "INTEGER"),
    ("reranker_total_tokens", "INTEGER"),
    ("reranker_calls", "INTEGER"),
    ("adk_tool_calls", "INTEGER"),
    ("cache_warm_s", "FLOAT"),
    ("error_type", "STRING"),
    ("error_message", "STRING"),
    ("attempts", "INTEGER"),
)

#: Nested fields folded into the single JSON column.
PAYLOAD_FIELDS = ("relevance", "nominated", "ranked_tables", "search_stats")

PARTITION_FIELD = "written_at"
CLUSTER_FIELDS = ("approach", "tier")


def table_id(config: ExperimentConfig) -> str:
    return f"{config.project}.{DATASET}.{TABLE}"


def to_row(cell: dict[str, Any], experiment_id: str) -> dict[str, Any]:
    """Reshape one merged cell into a table row.

    Unknown keys are dropped rather than silently widening the payload: the
    payload is for the four known nested fields, not a dumping ground.
    """
    row: dict[str, Any] = {"experiment_id": experiment_id}
    for name, _type in SCALAR_COLUMNS:
        if name in cell:
            row[name] = cell[name]
    # A dict, not json.dumps(...). Handing a string to a JSON column stores it
    # as a JSON *string scalar*, so JSON_VALUE(payload.x) returns NULL for every
    # path and the column is write-only. JSON_TYPE(payload) must be 'object'.
    row["payload"] = {k: cell.get(k) for k in PAYLOAD_FIELDS}
    return row


def rows_from(records: Iterable[dict[str, Any]], experiment_id: str) -> Iterator[dict[str, Any]]:
    for record in records:
        yield to_row(record, experiment_id)


def _schema() -> list[Any]:
    from google.cloud import bigquery  # noqa: PLC0415

    fields = [
        bigquery.SchemaField(
            name,
            field_type,
            mode="REQUIRED" if name in {"experiment_id", "cell_key"} else "NULLABLE",
        )
        for name, field_type in SCALAR_COLUMNS
    ]
    fields.append(bigquery.SchemaField("payload", "JSON"))
    return fields


def ensure_table(config: ExperimentConfig, credentials: Credentials | None = None) -> str:
    """Create the dataset and table if absent. Idempotent. Returns the table id."""
    from google.api_core import exceptions  # noqa: PLC0415
    from google.cloud import bigquery  # noqa: PLC0415

    client = bigquery.Client(project=config.project, credentials=credentials)

    dataset = bigquery.Dataset(f"{config.project}.{DATASET}")
    dataset.location = config.locations.bigquery
    dataset.description = (
        "Query sink for the six-approach BigQuery table-discovery experiment. "
        "GCS merged/results.jsonl is the system of record; this is loaded from it."
    )
    client.create_dataset(dataset, exists_ok=True)

    table = bigquery.Table(table_id(config), schema=_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field=PARTITION_FIELD
    )
    table.clustering_fields = list(CLUSTER_FIELDS)
    table.description = "One row per (experiment, question, approach, tier, run)."
    # Already there is the normal case; partitioning and clustering cannot be
    # altered after creation anyway, so there is nothing to reconcile.
    with contextlib.suppress(exceptions.Conflict):
        client.create_table(table)
    return table_id(config)


def load_experiment(
    config: ExperimentConfig,
    experiment_id: str,
    records: list[dict[str, Any]],
    credentials: Credentials | None = None,
) -> int:
    """Replace this experiment's rows with ``records``. Returns rows loaded.

    Delete-then-append rather than a MERGE: re-running `merge` must not double
    every cell, and the table is scoped by `experiment_id` so the delete cannot
    touch another run. The brief window where the experiment has no rows is
    acceptable for a sink whose source of truth is a file in GCS.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    client = bigquery.Client(project=config.project, credentials=credentials)
    ensure_table(config, credentials)

    client.query_and_wait(
        f"DELETE FROM `{table_id(config)}` WHERE experiment_id = @eid",  # noqa: S608
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("eid", "STRING", experiment_id)]
        ),
    )

    rows = list(rows_from(records, experiment_id))
    job = client.load_table_from_json(
        rows,
        table_id(config),
        job_config=bigquery.LoadJobConfig(
            schema=_schema(), write_disposition=bigquery.WriteDisposition.WRITE_APPEND
        ),
    )
    job.result()
    return len(rows)
