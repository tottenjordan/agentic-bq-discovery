"""Decide what order the 24 shards run in.

This module exists because of a measurement failure, not a scheduling one.

The first full run built its plan tier-major — ``for t in tiers for a in
approaches`` — so with ``parallelism=8`` the entire tier-0 row entered the
queue first and tier 3 ran hours later. The Dataplex semantic index was still
converging over that window, so discovery recall for the three search
approaches climbed 0.52 -> 0.68 -> 0.97 -> 0.92 across tiers and read as a large
enrichment effect. Re-running every tier once the index had settled gave an
identical 0.967. **Tier was confounded with elapsed time**, and the experiment's
headline variable was measuring the clock.

The fix is deliberately not a shuffle. The comparison that matters is *tier
within approach*, so ordering groups each approach's tiers together: all four
enter the queue in the same wave and see the same index state, which removes the
confound rather than randomising it away. A shuffle would only decorrelate in
expectation, and would scatter the 47-minute ``bq_tools`` shards through the
run — the worst possible thing for makespan.

Ordering approaches longest-first then costs nothing and buys the makespan back:
``bq_tools`` alone is ~47 minutes against ~1 minute for ``search_direct``, so a
sweep finishes no sooner than whenever ``bq_tools`` happens to start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Measured seconds per cell, from the full-01 run's 3,000 cells (2026-09-22).
#: Used only for ordering, so it needs to be roughly right, not exact — but it
#: is real data rather than a guess, and re-measuring is one query against
#: bigquery_context_results.cells.
APPROACH_COST_S: dict[str, float] = {
    "bq_tools": 22.71,
    "context_prefilter": 11.57,
    "kc_search": 4.08,
    "kc_context": 3.65,
    "semantic_context": 3.27,
    "search_direct": 0.39,
}


def corpus_fingerprint(ladder: list[dict]) -> str:
    """Short, stable hash of the corpus's enrichment *shape*.

    Threaded into every shard as a KFP input so that changing the corpus
    invalidates the shard cache exactly as changing the code does. Without it,
    re-running ``ensure-infra`` to alter enrichment and resubmitting under the
    same commit returns cells scored against the *old* corpus — green, plausible,
    wrong.

    **What it deliberately excludes, and why the exclusions matter more than the
    inclusions:**

    ``bytes`` — ``_tier_profile`` reports ``len(cache.all_detailed())``, the raw
    ``lookupContext`` capsule. That payload carries Dataplex entry timestamps and
    per-column ``dataProfile`` float statistics, which shift whenever a DataScan
    re-runs. ``_tier_profile``'s own docstring calls byte deltas "a trap in both
    directions". Hashing them would move the fingerprint on essentially every
    run, so every shard would miss cache — turning a 15-minute resume into a
    12-hour resweep, and making ``code_version`` irrelevant because everything
    was already invalidated.

    The search-convergence probe — hit counts drift while the Dataplex index
    warms, which is the entire reason ``assess_search_convergence`` exists.

    What remains is the shape a human would call "the corpus": how many tables,
    how many columns profiled, how many glossary terms attached, which
    table-level aspects are present, per tier.
    """
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    canonical = [
        [rung["tier"], rung["tables"], rung["profiled"], rung["terms"], sorted(rung["aspects"])]
        for rung in sorted(ladder, key=lambda r: r["tier"])
    ]
    # sha256, not hash(): the builtin is salted per process, so it would differ
    # between the preflight task and anything comparing against it.
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode())
    return digest.hexdigest()[:16]


def questions_fingerprint(questions: Mapping[str, Mapping[str, Any]]) -> str:
    """Short, stable hash of the question set's *meaning*.

    The same job ``corpus_fingerprint`` does, one layer up. ``code_version``
    describes the questions only for as long as they are baked into the image;
    once ``--questions`` can point somewhere else, swapping the file and
    resubmitting under the same commit would return cells scored against the old
    questions — green, plausible, wrong.

    **Order is content here, which is the asymmetry with ``corpus_fingerprint``.**
    That one sorts its ladder, because tier order is presentation. This one must
    not: ``--limit`` takes a deterministic prefix (``list(questions)[:limit]``),
    so reordering the file changes which questions a smoke or pilot run measures
    without editing a single character of any question.

    Order *within* a relevance list carries nothing, so those are sorted. A
    fingerprint that moved when someone reshuffled a ``must_have`` would turn
    every resume into a full resweep, which is the failure mode
    ``corpus_fingerprint``'s exclusions exist to avoid.

    ``distractor`` is hashed alongside the other two. The trap questions exist to
    catch a retriever that takes the bait, so swapping a distractor is a
    different experiment even though no expected answer changed.

    Missing fields are tolerated rather than rejected. A hand-written set with no
    ``relevance`` is a question with no expected answer, which ``preflight``
    should refuse — but a hasher is the wrong place to raise.
    """
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    canonical = [
        [
            qid,
            str(q.get("category", "")),
            str(q.get("question", "")),
            *(
                sorted(str(t) for t in (q.get("relevance") or {}).get(field, []))
                for field in ("must_have", "nice_to_have", "distractor")
            ),
        ]
        for qid, q in questions.items()
    ]
    # sha256, not hash(): the builtin is salted per process, so the submitting
    # CLI and the shard re-checking it would disagree every time.
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode())
    return digest.hexdigest()[:16]


def order_shards(tiers: list[int], approaches: list[str]) -> list[tuple[int, str]]:
    """Return ``(tier, approach)`` pairs in the order they should be dispatched.

    Approach-major and longest-first, with the tier sequence reversed on every
    other approach. Deterministic and independent of the caller's argument
    order, so ``--tier 3 --tier 0`` schedules identically to ``--tier 0 --tier
    3`` and a resumed run matches the run it resumes.

    The alternating direction is not decoration. Running tiers ascending inside
    every block leaves tier drifting upward with position across the whole plan
    — a residual correlation of 0.16, small but in exactly the direction that
    produced the original artifact. Serpentine order makes consecutive blocks
    contribute equal and opposite amounts, cancelling to zero for an even number
    of approaches and staying negligible for an odd one.
    """
    ordered_tiers = sorted(set(tiers))
    # Unknown approaches sort last but keep a stable alphabetical order among
    # themselves; a test asserts the six known ones are all in the table.
    ordered_approaches = sorted(set(approaches), key=lambda a: (-APPROACH_COST_S.get(a, 0.0), a))
    return [
        (tier, approach)
        for index, approach in enumerate(ordered_approaches)
        for tier in (ordered_tiers if index % 2 == 0 else list(reversed(ordered_tiers)))
    ]
