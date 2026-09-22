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
