"""Persist and read per-shard ``ShardResult`` summaries.

``ShardResult`` holds the only record of several facts that vanish when the
process exits: how long the context cache took to warm, whether the circuit
breaker tripped and why, and the planned/done/executed/failed split. Its
docstring already said "written alongside its attempt files" — this is the code
that makes that true.

The gap was not theoretical. ``cache_warm_s`` is the number the design named as
the one that would flip the sharding topology decision, and answering it after
the first full run meant scraping Cloud Logging. ``abort_reason`` is worse: for
a shard stopped by the circuit breaker, that string is the entire diagnosis, and
it was only ever printed to a console nobody was watching.

Summaries are diagnostics, never the system of record. Writing one must not be
able to fail a shard that otherwise worked, so every failure here is swallowed
and logged.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from bq_context.runner.models import ShardResult
from bq_context.runner.resume import experiment_prefix, summary_path

if TYPE_CHECKING:
    from bq_context.runner.store import ArtifactStore

logger = logging.getLogger(__name__)

#: Matched against the *basename*, not the whole path. The experiment id is part
#: of the path, so a substring test finds "/summary-" inside an experiment
#: literally named "summary-smoke" and then tries to parse _SUCCESS as JSON.
SUMMARY_RE = re.compile(r"^summary-\d{4}\.json$")


def write_summary(store: ArtifactStore, result: ShardResult) -> None:
    """Write ``result`` next to its attempt file. Never raises."""
    path = summary_path(result.attempt_path)
    try:
        store.write_text(path, result.model_dump_json(indent=2) + "\n")
    except Exception:  # noqa: BLE001 - diagnostics must not fail the shard
        logger.warning("[%s] could not write shard summary to %s", result.shard_id, path)
    else:
        logger.info("[%s] summary -> %s", result.shard_id, store.uri(path))


def load_summaries(store: ArtifactStore, experiment_id: str) -> list[ShardResult]:
    """Every shard summary for one experiment, skipping unreadable ones.

    Runs made before summaries existed simply have none, so an empty list is a
    normal result rather than a problem.
    """
    summaries: list[ShardResult] = []
    for path in sorted(store.list_paths(f"{experiment_prefix(experiment_id)}/shards")):
        if not SUMMARY_RE.match(path.rsplit("/", 1)[-1]):
            continue
        try:
            summaries.append(ShardResult.model_validate_json(store.read_text(path)))
        except Exception:  # noqa: BLE001 - one torn file must not hide the rest
            logger.warning("Skipping unreadable shard summary %s", path)
    return summaries
