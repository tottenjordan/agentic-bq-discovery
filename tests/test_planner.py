"""Shard ordering: tier must not be confounded with execution time.

The first full run ordered shards tier-major — all of tier 0, then tier 1, and
so on. With parallelism 8 that put the whole of tier 0 in the first wave and
tier 3 hours later, while the Dataplex semantic index was still converging.
Discovery recall duly "improved" 0.52 -> 0.97 across tiers and none of it was
real: re-running every tier later gave an identical 0.967. Tier was measuring
the clock.

The fix is not a random shuffle. The comparison that matters is *tier within
approach*, so the ordering groups each approach's four tiers together: they
launch in the same wave and see the same index state. Randomisation would only
decorrelate in expectation, and would scatter the 47-minute `bq_tools` shards
across the run, which is also the worst thing for makespan.

Ordering approaches by descending cost then serves both goals at once — longest
first is the standard makespan heuristic, and it costs the confound nothing.
"""

from __future__ import annotations

import pytest

from bq_context.runner.cells import APPROACHES
from bq_context.runner.planner import APPROACH_COST_S, order_shards

TIERS = [0, 1, 2, 3]
ALL = sorted(APPROACHES)


def _correlation(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    return cov / (vx * vy) if vx and vy else 0.0


# ---------------------------------------------------------------------------
# The property the whole thing exists for
# ---------------------------------------------------------------------------
def test_each_approach_runs_its_tiers_together() -> None:
    """The load-bearing assertion.

    Consecutive positions means the four tiers of an approach enter the queue
    in the same wave, so the search index is in the same state for all of them
    and tier cannot pick up an elapsed-time effect.
    """
    plan = order_shards(TIERS, ALL)
    for approach in ALL:
        positions = [i for i, (_t, a) in enumerate(plan) if a == approach]
        assert positions == list(range(min(positions), min(positions) + len(TIERS))), (
            f"{approach} tiers are scattered across the run: {positions}"
        )


def test_tier_does_not_correlate_with_position() -> None:
    """The statistical restatement of the same thing.

    Tier-major ordering scores ~0.9 here, which is the confound in one number.
    """
    plan = order_shards(TIERS, ALL)
    r = _correlation([float(i) for i in range(len(plan))], [float(t) for t, _a in plan])
    assert abs(r) < 0.1, f"tier still correlates with execution order (r={r:.3f})"


def test_the_old_tier_major_order_would_fail_that() -> None:
    """Pins the bug, so the guard above cannot be quietly weakened.

    If this stops showing a large correlation, the test above has lost its
    teeth and the ordering is no longer being checked against anything.
    """
    tier_major = [(t, a) for t in TIERS for a in ALL]
    r = _correlation([float(i) for i in range(len(tier_major))], [float(t) for t, _a in tier_major])
    assert r > 0.8, f"expected the old order to be badly confounded, got r={r:.3f}"


# ---------------------------------------------------------------------------
# Makespan
# ---------------------------------------------------------------------------
def test_the_most_expensive_approach_starts_first() -> None:
    """bq_tools is ~47 min against ~1 min for search_direct and pins the makespan.

    Starting it late extends the whole sweep by however late it starts.
    """
    plan = order_shards(TIERS, ALL)
    assert plan[0][1] == "bq_tools"


def test_approaches_are_ordered_by_descending_cost() -> None:
    plan = order_shards(TIERS, ALL)
    seen: list[str] = []
    for _tier, approach in plan:
        if approach not in seen:
            seen.append(approach)
    costs = [APPROACH_COST_S[a] for a in seen]
    assert costs == sorted(costs, reverse=True), f"not longest-first: {seen}"


def test_every_approach_has_a_measured_cost() -> None:
    """An approach missing from the table would silently sort last."""
    assert set(APPROACH_COST_S) == set(APPROACHES)


# ---------------------------------------------------------------------------
# It is still a complete, deterministic plan
# ---------------------------------------------------------------------------
def test_the_plan_is_complete_and_has_no_duplicates() -> None:
    plan = order_shards(TIERS, ALL)
    assert len(plan) == len(TIERS) * len(ALL)
    assert len(set(plan)) == len(plan)
    assert set(plan) == {(t, a) for t in TIERS for a in ALL}


def test_ordering_is_deterministic() -> None:
    """Resume and cross-run comparability both depend on this."""
    assert order_shards(TIERS, ALL) == order_shards(TIERS, ALL)


def test_input_order_does_not_change_the_plan() -> None:
    """A caller passing --tier 3 --tier 0 must not get a different schedule."""
    assert order_shards([3, 1, 0, 2], list(reversed(ALL))) == order_shards(TIERS, ALL)


@pytest.mark.parametrize(
    ("tiers", "approaches"),
    [
        ([3], ALL),
        (TIERS, ["bq_tools"]),
        ([0, 3], ["kc_search", "search_direct"]),
        ([2], ["kc_context"]),
    ],
)
def test_subsets_still_produce_a_valid_plan(tiers: list[int], approaches: list[str]) -> None:
    """Smoke and pilot profiles run subsets; so does re-running one shard."""
    plan = order_shards(tiers, approaches)
    assert set(plan) == {(t, a) for t in tiers for a in approaches}
    assert len(set(plan)) == len(plan)


def test_an_empty_selection_is_an_empty_plan() -> None:
    assert order_shards([], []) == []
