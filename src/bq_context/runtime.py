"""The per-run tier context, and the ambient scope the ADK agents read.

Upstream flipped ``config.SCOPE`` and ``config.ACTIVE_TIER`` as module globals
between tiers, which made the factorial un-parallelizable. The obvious fix —
pass a ``TierContext`` argument everywhere — is not available: ADK fixes the
callback signature to ``(CallbackContext)``, so the five callback-driven
approaches have nowhere to receive it.

A ``ContextVar`` gives the same explicitness without fighting ADK. Callbacks read
the ambient context, ``asyncio`` tasks inherit whatever was active when they were
created, and two shards in one process cannot see each other's tier.

The correctness property this exists to protect: **a run must see exactly one
tier dataset.** All four tier datasets hold identically-named tables and scoring
matches on the short table name, so a scope spanning two tiers would score
against the wrong corpus without erroring.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from bq_context.config import ExperimentConfig, tier_dataset

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bq_context.context_cache.cache import TableCache

__all__ = [
    "NoTierContextError",
    "TierContext",
    "current_tier",
    "default_config",
    "get_datasets",
    "get_scoped_tables",
    "is_table_in_scope",
    "tier_scope",
]


@cache
def default_config() -> ExperimentConfig:
    """The process-wide config, read from the environment once.

    Only for things fixed before any tier is chosen — chiefly the model id an
    ADK ``Agent`` is constructed with at import time. Anything that varies per
    run must come from ``current_tier().config`` instead, so a shard cannot be
    silently influenced by ambient environment.

    Memoized and immutable, so this is a constant rather than mutable state.
    """
    return ExperimentConfig.from_env()


class NoTierContextError(RuntimeError):
    """Raised when tier-scoped state is read outside a ``tier_scope`` block."""

    def __init__(self) -> None:
        super().__init__(
            "No TierContext is active. Wrap the call in `with tier_scope(ctx):`. "
            "This raises rather than defaulting to a tier because a silent "
            "default would mislabel every result produced under it."
        )


@dataclass(frozen=True, slots=True)
class TierContext:
    """Everything that varies by enrichment tier, for one shard."""

    tier: int
    config: ExperimentConfig
    scope: tuple[str, ...]
    cache: TableCache

    @classmethod
    def build(cls, config: ExperimentConfig, tier: int, cache: TableCache) -> TierContext:
        """Build a context scoped to exactly one tier dataset."""
        return cls(
            tier=tier,
            config=config,
            scope=(tier_dataset(config.resource_prefix, tier),),
            cache=cache,
        )


_CURRENT: ContextVar[TierContext | None] = ContextVar("bq_context_tier", default=None)


@contextmanager
def tier_scope(ctx: TierContext) -> Iterator[TierContext]:
    """Make ``ctx`` the ambient tier context for the duration of the block."""
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def current_tier() -> TierContext:
    """Return the active tier context, or raise if none is set."""
    ctx = _CURRENT.get()
    if ctx is None:
        raise NoTierContextError
    return ctx


# ---------------------------------------------------------------------------
# Scope helpers — the shapes the vendored agents already expect.
#
# Each scope entry is either "dataset" (all tables) or "dataset.table" (one
# table). In practice scope is always a single bare dataset; the parsing is
# retained from upstream so a future narrower scope stays expressible.
# ---------------------------------------------------------------------------


def get_datasets() -> list[str]:
    """Unique dataset names in the active scope, preserving order."""
    scope = current_tier().scope
    return list(dict.fromkeys(entry.split(".")[0] for entry in scope))


def get_scoped_tables(dataset: str) -> list[str] | None:
    """Specific table names scoped for a dataset, or None meaning all of them."""
    scope = current_tier().scope
    if dataset in scope:
        return None
    tables = [
        entry.split(".", 1)[1]
        for entry in scope
        if "." in entry and entry.split(".", 1)[0] == dataset
    ]
    return tables or None


def is_table_in_scope(dataset: str, table: str) -> bool:
    """Whether a specific table is in the active scope."""
    scope = current_tier().scope
    return dataset in scope or f"{dataset}.{table}" in scope
