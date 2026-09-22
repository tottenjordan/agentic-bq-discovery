"""Utility for calling Gemini with structured output to produce a ranked table list."""

import json
import re

from google import genai
from google.genai import types

from bq_context.config import ExperimentConfig
from bq_context.schemas import RerankerResponse
from bq_context.usage import record_usage_response


class RerankerEmptyResponseError(RuntimeError):
    """Gemini returned a response with no text (safety block, truncation, ...)."""


def _normalize_table_id(raw_id: str) -> str:
    """Normalize a table ID to project.dataset.table format.

    Handles variants the reranker sometimes produces from Dataplex paths:
      - "project.dataset.table" → keep as-is
      - "projects.project.datasets.dataset.tables.table" → strip path components
      - "projects.project.datasets.dataset.table" → strip partial path
    """
    return re.sub(
        r"^projects[./]([^./]+)[./](?:datasets[./])?([^./]+)[./](?:tables[./])?([^./]+)$",
        r"\1.\2.\3",
        raw_id,
    )


RERANKER_SYSTEM_PROMPT = """\
You are a BigQuery table relevance ranker. Given a user's question and metadata
about candidate BigQuery tables, you rank the tables by relevance.

For each table, evaluate:
1. How directly the table's columns and description answer the question
2. Which specific columns would be used in a SQL query to answer it
3. Whether joins to other candidate tables would help
4. What SQL patterns (WHERE filters, GROUP BY, aggregations) would be needed

Some candidate metadata comes from the Knowledge Catalog context capsule and may
include richer signals — use them when present:
- `frequent_joins` (or join hints): real observed join relationships. Prefer
  these when proposing join_suggestions rather than guessing join keys.
- glossary terms / `related_terms` / `business_descriptions`: business meaning of
  columns. Use them to disambiguate columns and improve reasoning.
- `guidelines`: authored NL→SQL guidance for the table. Fold applicable
  guidance directly into sql_hints.

Return ALL tables with confidence >= 0.5, ranked by relevance (most relevant
first). Do not omit borderline tables — include anything that could
plausibly help answer the question, even indirectly (e.g., a population
table needed for a per-capita calculation). Set confidence scores honestly:
0.9+ for highly relevant, 0.5-0.8 for partially relevant. Only exclude
tables below 0.5 confidence.

For table_id, always use the standard BigQuery fully-qualified format:
`project.dataset.table` (e.g., `my-project.my_dataset.my_table`).
Extract the project, dataset, and table name from whatever identifier
format appears in the metadata.

For key_columns, identify the most important columns for answering the question
and flag which are useful for filtering vs aggregation.

For sql_hints, provide concrete SQL guidance like:
- "Filter on weather_date for time-based questions"
- "GROUP BY subscriber_type to compare rider categories"
- "JOIN to stations table on start_station_id = station_id"
"""


# Token accounting for these calls lives in ``bq_context.usage`` as a
# ContextVar. ``call_reranker`` invokes Gemini directly rather than as an ADK
# tool, so its ``usage_metadata`` never reaches the ADK event stream and has to
# be collected on the side. Upstream used a module-level list, which was exact
# only because runs were strictly sequential.

_CLIENTS: dict[tuple[str, str], genai.Client] = {}


def _get_client(config: ExperimentConfig) -> genai.Client:
    """Return a genai client for this project/location, cached per pair.

    Keyed rather than a single global so two configs in one process (tests, or a
    future multi-project run) cannot silently share a client pointed at the
    wrong endpoint.
    """
    key = (config.project, config.locations.gemini)
    client = _CLIENTS.get(key)
    if client is None:
        client = genai.Client(
            vertexai=True,
            project=config.project,
            location=config.locations.gemini,
        )
        _CLIENTS[key] = client
    return client


def call_reranker(
    config: ExperimentConfig,
    question: str,
    candidate_metadata: str,
    discovery_method: str,
    top_k: int,
) -> RerankerResponse:
    """Call Gemini with structured output to rank candidate tables.

    Args:
        config: Project, location, and model settings.
        question: The user's original question.
        candidate_metadata: String containing table metadata (schemas,
            descriptions, profiles, etc.) from the discovery approach.
        discovery_method: Which approach produced the candidates
            ("bq_tools", "kc_search", "kc_context", "context_prefilter",
            "semantic_context").
        top_k: Maximum number of tables to return.

    Returns:
        RerankerResponse with ranked tables.
    """
    client = _get_client(config)

    user_prompt = f"""\
## User Question
{question}

## Discovery Method
{discovery_method}

## Ranking Instructions
Return ALL tables with confidence >= 0.5 (up to {top_k} maximum).
Do not drop borderline tables — if a table could contribute to answering
the question (even as a supporting join), include it.

## Candidate Table Metadata
{candidate_metadata}
"""

    response = client.models.generate_content(
        model=config.tool_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=RERANKER_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RerankerResponse.model_json_schema(),
            temperature=0.0,
        ),
    )

    # Recorded only for the response we keep. Any retry must wrap *this whole
    # function* from outside, so a retried-away attempt is never counted.
    record_usage_response(response)

    # response.text is None when the model returns no candidate — a safety block
    # or a finish_reason like MAX_TOKENS. Upstream passed it straight to
    # json.loads, which raises `TypeError: the JSON object must be str, bytes or
    # bytearray, not NoneType` and tells you nothing about why. Fail with the
    # finish_reason instead, so a shard's error cell is diagnosable.
    if response.text is None:
        finish = "unknown"
        if response.candidates:
            finish = str(getattr(response.candidates[0], "finish_reason", "unknown"))
        msg = (
            f"Reranker returned no text for discovery_method={discovery_method!r} "
            f"(finish_reason={finish}). Prompt feedback: {response.prompt_feedback!r}"
        )
        raise RerankerEmptyResponseError(msg)

    result = RerankerResponse.model_validate(json.loads(response.text))

    # Normalize table IDs — the reranker sometimes picks up Dataplex path
    # format from the metadata (e.g., "projects.proj.datasets.ds.tables.tbl")
    for t in result.ranked_tables:
        t.table_id = _normalize_table_id(t.table_id)

    return result


def format_reranker_markdown(result: RerankerResponse, label: str) -> str:
    """Format a RerankerResponse as readable markdown for agent output.

    Args:
        result: The reranker result to format.
        label: Approach label, e.g. "Approach 2: Knowledge Catalog Search".
    """
    lines = [f"**[{label}]**", ""]

    if not result.ranked_tables:
        lines.append(result.notes or "No relevant tables found.")
        return "\n".join(lines)

    lines.append(f"Found **{len(result.ranked_tables)}** relevant table(s):")
    lines.append("")

    for t in result.ranked_tables:
        lines.append(f"**#{t.rank}. `{t.table_id}`** — confidence: {t.confidence:.2f}")
        lines.append(f"> {t.reasoning}")
        if t.key_columns:
            col_strs = []
            for c in t.key_columns:
                tags = []
                if c.useful_for_filtering:
                    tags.append("filter")
                if c.useful_for_aggregation:
                    tags.append("aggregate")
                if c.is_key:
                    tags.append("key")
                tag_str = f" ({', '.join(tags)})" if tags else ""
                col_strs.append(f"`{c.name}` {c.data_type}{tag_str}")
            lines.append(f"- **Key columns:** {', '.join(col_strs)}")
        if t.sql_hints:
            lines.append(f"- **SQL hints:** {t.sql_hints}")
        if t.join_suggestions:
            for j in t.join_suggestions:
                lines.append(
                    f"- **Join:** → `{j.target_table}` "
                    f"ON {', '.join(j.join_keys)} ({j.relationship})"
                )
        lines.append("")

    if result.notes:
        lines.append(f"**Notes:** {result.notes}")

    return "\n".join(lines)
