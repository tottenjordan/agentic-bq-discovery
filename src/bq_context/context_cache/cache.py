"""Pre-fetched table metadata for one tier, in brief and detailed views.

Vendored from statmike/vertex-ai-mlops (Apache-2.0) and refactored: upstream kept
a module-level ``_CACHE`` dict that auto-populated **at import time** and was
cleared and rebuilt to switch tiers. That made importing any agent hit BigQuery
and Dataplex as a side effect, and made two tiers in one process impossible.

Here the cache is an ordinary object owned by a ``TierContext``. Building it is
an explicit, timed step in the shard runner.

Both views come from the Knowledge Catalog ``lookupContext`` API (JSON format):

- **brief**: everything except per-column ``dataProfile`` — cheap enough to put
  in a prompt, rich enough to pre-filter on
- **detailed**: the full capsule including ``dataProfile`` (nullRatio,
  distinctValues, sampleValues)

Keyed by ``project.dataset.table``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from google.cloud import bigquery

from .util_lookup_context import lookup_context_batched

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from bq_context.config import ExperimentConfig

logger = logging.getLogger(__name__)

# Top-level capsule keys dropped from briefs (noise, not signal for pre-filtering).
_BRIEF_DROP_KEYS = frozenset({"system", "type"})


@dataclass(frozen=True, slots=True)
class TableContext:
    """Brief and detailed metadata for a single BigQuery table."""

    full_id: str  # "project.dataset.table"
    brief: str  # JSON without dataProfile sections
    detailed: str  # Full JSON with dataProfile sections


def _entry_name_to_full_id(entry_name: str) -> str | None:
    """Extract ``project.dataset.table`` from a lookupContext entry name."""
    parts = entry_name.split("/")
    try:
        proj_idx = parts.index("projects")
        ds_idx = parts.index("datasets")
        tbl_idx = parts.index("tables")
    except ValueError:
        return None
    try:
        return f"{parts[proj_idx + 1]}.{parts[ds_idx + 1]}.{parts[tbl_idx + 1]}"
    except IndexError:
        return None


def _build_brief(entry: dict) -> dict:
    """Strip the heavy per-column ``dataProfile`` stats, keep everything else.

    Subtractive by design. The Knowledge Catalog capsule bundles cheap
    high-signal enrichments — ``related_terms``, ``guidelines``,
    ``frequent_joins``, ``business_descriptions`` — whose exact key names are a
    preview-API detail. Dropping a denylist rather than copying an allowlist
    means new enrichment keys flow into briefs with no code change.
    """
    brief: dict = {}
    for key, value in entry.items():
        if key in _BRIEF_DROP_KEYS:
            continue
        if key == "schema":
            brief["schema"] = [
                {k: v for k, v in col.items() if k != "dataProfile"} for col in value
            ]
        else:
            brief[key] = value
    return brief


@dataclass(slots=True)
class TableCache:
    """Table metadata for the tables in one tier's scope."""

    entries: dict[str, TableContext] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def empty(cls) -> TableCache:
        """An empty cache. Used by the search-only approaches and by tests."""
        return cls()

    @classmethod
    def build(
        cls,
        config: ExperimentConfig,
        datasets: list[str],
        scoped_tables: dict[str, list[str] | None] | None = None,
        credentials: Credentials | None = None,
    ) -> TableCache:
        """Fetch context capsules for every in-scope table.

        Args:
            config: Project and location settings.
            datasets: Datasets to populate from — in practice exactly one.
            scoped_tables: Optional per-dataset table allowlist. ``None`` for a
                dataset means "all tables", discovered via the BigQuery API.

        Raises rather than swallowing errors: upstream caught everything here and
        logged a warning, so a permissions problem produced an empty cache and a
        green run that silently measured nothing. The caller decides what an
        empty cache means.
        """
        cache = cls()
        bq_client = bigquery.Client(project=config.project, credentials=credentials)
        scoped_tables = scoped_tables or {}

        for dataset in datasets:
            names = scoped_tables.get(dataset)
            if names is None:
                names = [t.table_id for t in bq_client.list_tables(f"{config.project}.{dataset}")]
            if not names:
                continue

            wanted = {f"{config.project}.{dataset}.{name}" for name in names}
            entry_names = [config.dataplex_entry_name(dataset, name) for name in names]

            # lookupContext accepts at most 10 entries per call; the helper batches.
            context_json = lookup_context_batched(config, entry_names, credentials=credentials)
            if not context_json or context_json == "[]":
                logger.warning(
                    "lookupContext returned nothing for %s. Note it returns an "
                    "EMPTY response rather than 403 when permissions are missing.",
                    dataset,
                )
                continue

            try:
                capsules = json.loads(context_json)
            except json.JSONDecodeError:
                logger.warning("Failed to parse lookupContext JSON for dataset %s", dataset)
                continue

            for capsule in capsules:
                full_id = _entry_name_to_full_id(capsule.get("name", ""))
                if not full_id or full_id not in wanted:
                    logger.warning("Unmatched lookupContext entry: %s", capsule.get("name", ""))
                    continue
                # Carry a dotted id so the reranker sees consistent table_ids;
                # the raw `name` field uses the catalog path format.
                capsule["table_id"] = full_id
                cache.entries[full_id] = TableContext(
                    full_id=full_id,
                    brief=json.dumps(_build_brief(capsule), indent=2),
                    detailed=json.dumps(capsule, indent=2),
                )

        return cache

    # -- access -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def table_ids(self) -> list[str]:
        """All cached fully-qualified table ids."""
        return list(self.entries)

    def all_briefs(self) -> str:
        """Every brief as a JSON array. Approach 4 puts this in the prompt."""
        return json.dumps([json.loads(tc.brief) for tc in self.entries.values()], indent=2)

    def all_detailed(self) -> str:
        """Every full capsule as a JSON array. Approach 3 sends this to rerank."""
        return json.dumps(
            [json.loads(tc.detailed) for tc in self.entries.values() if tc.detailed],
            indent=2,
        )

    def detailed_for(self, table_ids: list[str]) -> str:
        """Full capsules for specific tables, as a JSON array.

        Used by Approach 4 (after nomination) and Approach 5 (after search).
        """
        out = []
        for tid in table_ids:
            tc = self.entries.get(tid)
            if tc is None and "/" in tid:
                # The nominating LLM sometimes returns the catalog entry-path
                # form instead of the dotted key the cache uses — normalize.
                normalized = _entry_name_to_full_id(tid)
                if normalized:
                    tc = self.entries.get(normalized)
            if tc and tc.detailed:
                out.append(json.loads(tc.detailed))
        return json.dumps(out, indent=2)
