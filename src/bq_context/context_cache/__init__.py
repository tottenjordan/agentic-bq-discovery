"""Per-tier table metadata cache, built from Knowledge Catalog context capsules.

Unlike upstream, nothing here populates at import time. A ``TableCache`` is built
explicitly by the shard runner and owned by a ``TierContext``.
"""

from .cache import TableCache, TableContext, is_empty_payload

__all__ = ["TableCache", "TableContext", "is_empty_payload"]
