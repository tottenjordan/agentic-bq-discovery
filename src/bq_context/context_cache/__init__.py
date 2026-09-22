"""Per-tier table metadata cache, built from Knowledge Catalog context capsules.

Unlike upstream, nothing here populates at import time. A ``TableCache`` is built
explicitly by the shard runner and owned by a ``TierContext``.
"""

from .cache import TableCache, TableContext

__all__ = ["TableCache", "TableContext"]
