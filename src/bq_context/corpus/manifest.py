"""What ``ensure-infra`` provisioned, as data a bucket can hold.

The corpus lived only as a Python list in ``setup.py``, which is vendored and
kept re-syncable with upstream (see ``NOTICE``) — so this is beside it rather
than in it, the way ``bucket.py`` is.

The problem it solves: a run's bucket recorded 3,000 cells and said nothing
about what they were measured against. Answering "what was in ``hard-full-01``'s
corpus?" meant finding the commit and reading ``setup.py`` at it. Every cell now
carries a ``corpus_fingerprint``, and ``corpus/{fingerprint}/`` is where a reader
lands after grouping by one.

Keyed by fingerprint rather than by resource prefix, deliberately. A prefix is
reused as enrichment changes, so a record under it would be overwritten by the
next provisioning; fingerprints accumulate.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["corpus_manifest", "corpus_prefix", "provisioned_path", "provisioned_record"]

#: A fingerprint is a path segment, so it must not contain one. `planner.py`
#: produces a hex digest; anything else is a caller error, not something to
#: sanitise into a plausible-looking folder.
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def corpus_prefix(fingerprint: str) -> str:
    """Where a corpus of this shape is described."""
    if not _FINGERPRINT_RE.match(fingerprint):
        msg = f"not a usable corpus fingerprint: {fingerprint!r}"
        raise ValueError(msg)
    return f"corpus/{fingerprint}"


def provisioned_path(resource_prefix: str) -> str:
    """Where ``ensure-infra`` records what it built.

    Keyed by resource prefix, not fingerprint, because ``ensure-infra`` cannot
    know the fingerprint: it is a hash of the *provisioned* state, which does not
    exist until provisioning finishes. ``preflight`` runs next, computes it, and
    writes the fingerprint-keyed manifest.
    """
    return f"corpus/provisioned/{resource_prefix}.json"


def corpus_manifest(setup: ModuleType) -> dict[str, Any]:
    """Describe the corpus the given ``setup`` module would provision.

    Takes the module rather than importing it, so a test can hand in a copy
    reloaded under a different environment — ``CORPUS_PROFILE`` and
    ``RESOURCE_PREFIX`` are both resolved at ``setup``'s import time.

    Descriptions are included: in this experiment a description *is* tier-0
    enrichment, the same category as schema, so it is part of what the corpus is
    rather than commentary about it.
    """
    return {
        "corpus_profile": setup.CORPUS_PROFILE,
        "resource_prefix": setup.RESOURCE_PREFIX,
        "tiers": list(setup.TIERS),
        "table_count": len(setup.CORPUS),
        "glossary_term_count": len(setup.GLOSSARY_TERMS),
        "tables": [
            {
                "name": view["name"],
                "source": view["source"],
                "description": view["description"],
            }
            for view in setup.CORPUS
        ],
    }


def provisioned_record(
    setup: ModuleType, project: str, identity: str, now: datetime | None = None
) -> dict[str, Any]:
    """What one ``ensure-infra`` run built, and who ran it.

    Separate from the manifest because the two answer different questions. The
    manifest says what the corpus *is* and is the same for anyone who builds it;
    this says when and where this particular copy came from.
    """
    return {
        **corpus_manifest(setup),
        "project": project,
        "provisioned_by": identity,
        "provisioned_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }
