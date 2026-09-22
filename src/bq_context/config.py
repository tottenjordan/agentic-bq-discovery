"""Experiment configuration as immutable data.

Vendored from statmike/vertex-ai-mlops (Apache-2.0) and substantially rewritten:
upstream kept scope and the active tier in module globals, which is why its
factorial had to run serially. Here the configuration is a frozen dataclass and
the per-run tier lives in a context variable (see ``bq_context.runtime``).

Pure data — no SDK imports, no environment mutation at import time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Locations:
    """Every location this experiment touches, in one place.

    There are seven of them and they are not interchangeable. Getting one wrong
    fails ten seconds into a local run and twenty minutes into a pipeline run,
    after a container pull, which is why they are frozen together and printed by
    ``validate-config`` rather than scattered across call sites.

    ``gemini`` is the one that surprises people: ``gemini-3.6-flash`` and
    ``gemini-3.5-flash-lite`` return 404 in ``us-central1`` and 200 at
    ``global`` (verified 2026-09-22). The model endpoint and the compute region
    are different things.

    ``catalog`` must track ``bigquery`` (lowercased), *not* ``datascan``: entry
    links require every referenced entry to live in the link's own region, and
    the entries being linked are BigQuery entries. Upstream's readme says
    otherwise; upstream's code agrees with this and is authoritative.
    """

    pipeline: str = "us-central1"  # Vertex AI Pipelines compute
    gemini: str = "global"  # model endpoint — NOT regional
    bigquery: str = "US"  # dataset multi-region
    datascan: str = "us-central1"  # Dataplex DataScans must be single-region
    artifact_registry: str = "us-central1"  # must match `pipeline`
    gcs: str = "us-central1"  # bucket region

    @property
    def catalog(self) -> str:
        """Location of the glossary, its terms, and the definition entry links."""
        return self.bigquery.lower()


# ---------------------------------------------------------------------------
# Experiment configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Everything a run needs that does not vary per tier or per cell."""

    project: str
    locations: Locations = field(default_factory=Locations)
    agent_model: str = "gemini-3.6-flash"
    tool_model: str = "gemini-3.5-flash-lite"
    resource_prefix: str = "bigquery_context"
    top_k: int = 5

    @classmethod
    def from_env(cls) -> ExperimentConfig:
        """Build from environment variables, matching upstream's names."""
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            msg = (
                "GOOGLE_CLOUD_PROJECT is not set. Every BigQuery, Dataplex, and "
                "Vertex call needs it; defaulting would silently target the "
                "wrong project."
            )
            raise ValueError(msg)
        return cls(
            project=project,
            locations=Locations(
                bigquery=os.environ.get("BQ_LOCATION", "US"),
                datascan=os.environ.get("DATAPLEX_LOCATION", "us-central1"),
            ),
            agent_model=os.environ.get("AGENT_MODEL", "gemini-3.6-flash"),
            tool_model=os.environ.get("TOOL_MODEL", "gemini-3.5-flash-lite"),
            resource_prefix=os.environ.get("RESOURCE_PREFIX", "bigquery_context"),
            top_k=int(os.environ.get("TOP_K", "5")),
        )

    def configure_adk_env(self) -> None:
        """Export the environment variables ADK builds its own client from.

        Our reranker constructs ``genai.Client(vertexai=True, ...)`` explicitly,
        but ADK does not: for an LLM-driven agent it builds a client from
        ``GOOGLE_GENAI_USE_VERTEXAI``, ``GOOGLE_CLOUD_PROJECT``, and
        ``GOOGLE_CLOUD_LOCATION``. Without them it falls back to the Gemini
        Developer API and fails with "No API key was provided".

        Upstream did this as an import-time side effect in ``config.py``. Making
        it an explicit call is better, but it does mean it can be forgotten —
        so ``execute_shard`` calls it, which is the single path through which
        any agent runs.

        Only ``bq_tools`` and ``context_prefilter`` actually reach the agent
        LLM; the other four short-circuit it in a callback. That is why a
        missing setting here stays invisible until one of those two runs.
        """
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        os.environ["GOOGLE_CLOUD_PROJECT"] = self.project
        # The model endpoint, NOT the compute region: gemini-3.x flash models
        # return 404 in us-central1 and 200 at global.
        os.environ["GOOGLE_CLOUD_LOCATION"] = self.locations.gemini

    def tier_dataset(self, tier: int) -> str:
        """Dataset id holding the corpus at a given enrichment tier."""
        return tier_dataset(self.resource_prefix, tier)

    def dataplex_entry_name(self, dataset: str, table: str) -> str:
        """Dataplex entry name for a BigQuery table in this project."""
        return (
            f"projects/{self.project}/locations/{self.locations.catalog}"
            f"/entryGroups/@bigquery/entries/bigquery.googleapis.com"
            f"/projects/{self.project}/datasets/{dataset}/tables/{table}"
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

TIERS: tuple[int, ...] = (0, 1, 2, 3)


def tier_dataset(resource_prefix: str, tier: int) -> str:
    """Dataset id holding the corpus at a given enrichment tier.

    setup.py replicates an identical 15-table corpus into one dataset per tier;
    only the Knowledge Catalog enrichment differs between them.
    """
    return f"{resource_prefix}_tier{tier}"
