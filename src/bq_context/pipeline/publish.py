"""Persist what ``finalize`` produces, and report what the sweep actually did.

Extracted from the component body for two reasons. The obvious one is that
``finalize`` had grown past the complexity limit. The better one is that inline
component code is effectively untestable — a KFP body is extracted and run
standalone, so nothing here could be exercised without a pipeline run. As a
library function it is covered by ``tests/test_publish.py``.

Components import the library for plumbing like this already (``store_for``,
``missing_path``); they shell out to the CLI for *work*. This is plumbing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bq_context.runner.resume import run_prefix
from bq_context.scoring.merge import missing_path

if TYPE_CHECKING:
    from bq_context.runner.store import ArtifactStore

logger = logging.getLogger(__name__)

#: What a sweep reports when merge never produced a record. Zeroes rather than
#: an exception: the exit task must still publish artifacts on a run where the
#: merge itself failed, and a missing report is information, not a crash.
EMPTY_REPORT: dict[str, Any] = {"expected": 0, "present": 0, "missing_count": 0, "missing": []}


def publish_report(
    store: ArtifactStore, experiment_id: str, run_id: str, report_path: str
) -> str | None:
    """Copy the markdown report to this execution's folder. Returns its path.

    Not a KFP artifact path: those embed the pipeline job id and change every
    run, so the copy a human goes looking for weeks later would not be there.
    Not the bare experiment prefix either — that is one path per *experiment*,
    and the second execution of an experiment id silently overwrote the first.
    """
    source = Path(report_path)
    if not source.exists():
        logger.warning("No report at %s; nothing to publish", report_path)
        return None
    target = f"{run_prefix(experiment_id, run_id)}/scoring/report.md"
    store.write_text(target, source.read_text())
    return target


def publish_figures(
    store: ArtifactStore, experiment_id: str, run_id: str, plots_dir: str
) -> list[str]:
    """Copy every PNG to this execution's folder. Returns the paths written.

    ``write_bytes`` rather than ``write_text``: the latter hard-codes
    ``application/json``, which uploads the right bytes under a type that makes a
    browser download the figure instead of showing it.
    """
    prefix = run_prefix(experiment_id, run_id)
    written = []
    for png in sorted(Path(plots_dir).glob("*.png")):
        target = f"{prefix}/plots/{png.name}"
        store.write_bytes(target, png.read_bytes(), "image/png")
        written.append(target)
    return written


def merge_report(store: ArtifactStore, experiment_id: str) -> dict[str, Any]:
    """The sweep's completeness record, or ``EMPTY_REPORT`` if there is none.

    Never raises. This runs in the exit task, which must publish its artifacts
    even when the thing that would have written this record is what failed.
    """
    try:
        return json.loads(store.read_text(missing_path(experiment_id)))
    except Exception:  # noqa: BLE001 - a missing or torn report is not fatal here
        logger.warning("No merge report for %s; treating the sweep as empty", experiment_id)
        return dict(EMPTY_REPORT)


def merge_args(  # noqa: PLR0913 - these are merge's own six parameters; a dataclass
    # wrapper would add a type for a single call site and hide nothing.
    experiment_id: str,
    out: str,
    *,
    runs: int,
    tiers: list,
    approaches: list,
    question_limit: int = 0,
) -> list[str]:
    """Build the `bq-context merge` argv the exit task runs.

    Pure, and extracted so it can be tested directly — the same construction
    inline in a component body is only reachable through a pipeline run.

    ``question_limit`` must match what the shards actually ran. Omitting it makes
    merge expect the full question set and report phantom missing cells, which
    then fails the run for a completeness problem that does not exist.
    """
    args = [
        "bq-context",
        "merge",
        "--experiment-id",
        experiment_id,
        "--out",
        out,
        "--runs",
        str(runs),
    ]
    if question_limit:
        args += ["--limit", str(question_limit)]
    for tier in tiers:
        args += ["--tier", str(tier)]
    for approach in approaches:
        args += ["--approach", str(approach)]
    return args


def ensure_placeholder(path: str, body: str) -> None:
    """Guarantee an artifact file exists, even when the step that writes it failed.

    KFP creates only the *parent* directory of an artifact path. Leaving the file
    unwritten registers an artifact pointing at nothing, and the UI shows a dead
    link — on exactly the runs someone needs to read.
    """
    target = Path(path)
    if not target.exists():
        target.write_text(body)


def publish_summary(
    store: ArtifactStore, experiment_id: str, run_id: str, summary_path: str
) -> str | None:
    """Copy the executive HTML to this execution's folder. Returns its path."""
    source = Path(summary_path)
    if not source.exists():
        return None
    target = f"{run_prefix(experiment_id, run_id)}/scoring/executive.html"
    store.write_text(target, source.read_text())
    return target
