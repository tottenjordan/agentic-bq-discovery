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
    ("corpus_fingerprint", "STRING"),
    ("principal", "STRING"),
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

#: Field documentation, attached to the BigQuery schema. Kept separate from the
#: column list because it is documentation, not shape — and because this is
#: where the non-obvious caveats live. An experiment about metadata quality
#: should not ship an undocumented results table.
COLUMN_DESCRIPTIONS: dict[str, str] = {
    "experiment_id": (
        "Experiment this cell belongs to, e.g. 'full-01'. The table holds every "
        "run, so essentially every query should filter on this."
    ),
    "cell_key": (
        "Globally unique '{question_id}|{approach}|tier{tier}|run{run_idx}'. "
        "Carries no shard identity, which is what makes resume correct even when "
        "the shard plan changes between runs."
    ),
    "question_id": "Question id from experiments/questions.json.",
    "approach": (
        "One of the six discovery strategies: bq_tools, kc_search, kc_context, "
        "context_prefilter, semantic_context, search_direct. Clustering key."
    ),
    "tier": (
        "Catalog enrichment level. 0 schema only, 1 adds data profiling, 2 adds "
        "glossary term links, 3 adds a table-level aspect. Each tier is a separate "
        "dataset holding an identical corpus, so tier is the only thing that "
        "differs. Clustering key."
    ),
    "run_idx": (
        "Repeat index 0-4. Five runs per (question, approach, tier) to average "
        "over LLM non-determinism."
    ),
    "status": (
        "'ok' or 'error'. Error rows carry error_type and error_message and have "
        "no ranking; they are recorded rather than dropped so a sweep with a few "
        "bad cells is still scoreable."
    ),
    "written_at": ("When the cell finished, not when the run started. Partition key (DAY)."),
    "code_version": (
        "Short git SHA that produced the cell. Threaded into the KFP cache key so "
        "a code change cannot silently return cached results. full-01 mixes "
        "5de0dde (2,993 cells) and 28a7b2c (7 re-run on resume)."
    ),
    "corpus_fingerprint": (
        "Short hash of the corpus's enrichment shape -- table count, profiled "
        "columns, glossary terms and aspects per tier. Identifies *which corpus* "
        "produced the row, so results from different corpora in this table can be "
        "told apart and compared. Filter or group on it alongside experiment_id; "
        "an experiment id is a naming convention, this is a measured fact. "
        "Deliberately excludes capsule bytes and search-hit counts, which drift "
        "between runs without the corpus changing."
    ),
    "principal": (
        "The account that ran the search, e.g. the pipeline service account. "
        "Dataplex semantic search returns different tables to different "
        "principals for the same query, so rows measured by different principals "
        "are not comparable. Group on this alongside corpus_fingerprint. Empty "
        "means unknown: cells written before the column existed."
    ),
    "category": (
        "Question class: single-table, multi-table-related, multi-table-disparate, "
        "or trap. Trap questions name a table that does not actually answer them."
    ),
    "question": "The natural-language question put to the agent.",
    "nominated_count": (
        "Candidate tables considered before reranking. Means different things per "
        "approach: the whole corpus for kc_context (15), semantic search hits for "
        "the three search approaches, and an LLM shortlist for context_prefilter."
    ),
    "ranked_count": (
        "Tables in the final ranking. Zero has two indistinguishable causes here: "
        "search returned nothing, or the reranker's output failed to parse. Only "
        "the shard log's parse warning separates them."
    ),
    "latency_s": "End-to-end wall clock for this cell, in seconds.",
    "reranker_prompt_tokens": (
        "Prompt tokens billed to the reranker call. See reranker_total_tokens for "
        "what this excludes."
    ),
    "reranker_output_tokens": "Output tokens billed to the reranker call.",
    "reranker_total_tokens": (
        "Total reranker tokens. IMPORTANT: agent-side LLM tokens are NOT counted, "
        "so cost for bq_tools and context_prefilter - the two approaches running "
        "their own LLM loop - is understated. Retries are excluded, so a retried "
        "call counts once."
    ),
    "reranker_calls": (
        "Reranker invocations, normally 1. Zero for search_direct by design, since "
        "it uses raw search order and calls no LLM at all, and zero for the few "
        "cells where search returned nothing to rank."
    ),
    "adk_tool_calls": (
        "ADK tool invocations. Only bq_tools runs a real tool loop (~5 per cell); "
        "context_prefilter makes 1; the other four make none."
    ),
    "cache_warm_s": (
        "Always 0 - vestigial. Context-cache warm is measured once per shard and "
        "recorded on the shard summary, never attributed to a cell. Do not read "
        "this as 'warming was free'."
    ),
    "error_type": "Exception class when status='error'; empty string otherwise.",
    "error_message": "Exception message when status='error'; empty string otherwise.",
    "attempts": (
        "Attempts made for this cell. Greater than 1 means it was retried after a "
        "transient failure such as a 429."
    ),
    "payload": (
        "Variable-width nested fields as a JSON object (JSON_TYPE is 'object', so "
        "JSON_VALUE(payload.a.b) works). Keys: relevance {must_have, nice_to_have, "
        "distractor}, the graded ground truth; nominated, candidate table ids; "
        "ranked_tables, [{table_id, rank, confidence}] in final order; "
        "search_stats {raw_search_count, out_of_scope_dropped, page_size}, null "
        "for approaches that do not search. Kept as JSON so a new per-approach "
        "statistic is not a schema migration."
    ),
}

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
            description=COLUMN_DESCRIPTIONS.get(name, ""),
        )
        for name, field_type in SCALAR_COLUMNS
    ]
    fields.append(
        bigquery.SchemaField("payload", "JSON", description=COLUMN_DESCRIPTIONS["payload"])
    )
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
    try:
        client.create_table(table)
    except exceptions.Conflict:
        # Descriptions *can* change on an existing table, and a wording fix
        # should not need the table dropped. Patch them in place; create_table
        # would have been a no-op and left the old text behind.
        existing = client.get_table(table_id(config))
        existing.schema = _schema()
        existing.description = table.description
        client.update_table(existing, ["schema", "description"])
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
