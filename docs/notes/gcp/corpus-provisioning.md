# Provisioning the four-tier corpus

`bq-context ensure-infra` run against `hybrid-vertex` on **2026-09-22**.
Duration **19m22s**, exit 0. Re-verify before relying on any figure here.

## What got created

| resource | count | location |
|---|---|---|
| BigQuery datasets `bigquery_context_tier0..3` | 4 | `US` |
| Views over `bigquery-public-data` | 60 (15 × 4) | `US` |
| Dataplex profile scans | 45 (tiers 1–3) | `us-central1` |
| Business glossary `bigquery-context-glossary` | 1 | `us` |
| Glossary terms | 11 | `us` |
| Definition entry links in `@bigquery` | 48 | `us` |
| `overview` aspects on tier-3 tables | 4 | — |

Rough phase timings from the log: datasets + 60 views ≈ 2 min; 45 scan
creations at the hardcoded 5s throttle ≈ 7 min; scan polling ≈ 8 min; glossary,
terms, and links ≈ 2 min.

## The important finding: the tier ladder is not what it claims

`bq-context preflight --tier 3` reports what actually reached the capsule:

```
tier   tables     bytes  profiled  aspects
0          15    49,089         0  —
1          15   118,275        15  —
2          15   119,882        15  —
3          15   122,346        15  overview
```

| tier | intended | actual |
|---|---|---|
| 0 | schema only | ✅ as intended |
| 1 | + data profiling | ✅ 0 → 15 profiled tables, +69 KB |
| 2 | + business glossary | ❌ **indistinguishable from tier 1** |
| 3 | + authored guidelines | ⚠️ `overview` aspect on 4 tables, not `guidelines` |

### Tier 2: glossary links exist but never reach the capsule

This is not a setup failure. The links are created successfully and are
correctly formed — `get_entry_link` confirms:

```
def-t2-subscriber-type-austin-bikeshare-trips-subscriber-type
  references: [('austin_bikeshare_trips', 'Schema.subscriber_type', 'SOURCE'),
               ('subscriber-type', '', 'TARGET')]
```

But **neither `lookup_entry` nor `lookup_context` surfaces them.** Tier 1 and
tier 2 return identical aspect keys:

```
['655216118709.global.bigquery-view',
 '655216118709.global.data-profile',
 '655216118709.global.schema']
```

Searching the tier-2 capsule for `related_terms`, `glossar`, or the term id
`subscriber-type` returns zero hits. The 1,607-byte difference from tier 1 is
timestamps and entry ids, not content.

**Consequence:** tier 2 is not a distinct factor level. Every approach sees
exactly what it sees at tier 1, so 750 of the 3,000 cells measure tier 1 twice.
Any tier-2 result must be read as a replicate of tier 1, not as a glossary arm.

**This is a strong candidate explanation for part of upstream's flat tier
response** (see [[upstream-experiment]]). If glossary enrichment never reaches
the capsule that the agents consume, tier 2 contributes nothing *by
construction*, and no amount of replication would reveal it.

Unresolved: whether this is a `lookupContext` preview limitation, a missing
request option, or propagation lag longer than the ~20 minutes observed here.
Worth re-checking before the full run — if links do eventually surface, the
tier-2 arm becomes valid and the timing matters for the pipeline's ordering.

### Tier 3: the guidelines aspect type is not readable

```
Aspect type unavailable: projects/dataplex-types/locations/global/aspectTypes/guidelines
  - 403 Permission 'dataplex.aspectTypes.get' denied
Using aspect: projects/dataplex-types/locations/global/aspectTypes/overview (field 'content')
```

Upstream's `_resolve_guidelines_aspect` falls back to `overview` when
`guidelines` is unavailable, so setup succeeded — quietly. Tier 3 *is* distinct
from tier 2 (+2,464 bytes, `overview` on 4 tables), so the arm is real, but it
is testing an overview blob rather than authored NL→SQL guidance.

If the intent is to test guidelines specifically, the SA needs
`dataplex.aspectTypes.get` on the Google-published `dataplex-types` project. It
is a cross-project read, so a project-level grant in `hybrid-vertex` will not
cover it.

## The gate was too weak, and this is why it now checks every rung

The first version of `preflight` compared only tier 3 against tier 0. That
passed — tier 3 genuinely carries 73 KB more context — while tier 2 was dead.
An endpoint check cannot see a flat rung in the middle, and a flat middle rung
is worse than a missing tier, because the factorial still reports the two levels
as distinct.

`preflight` now walks the whole ladder and warns per transition. See
`assess_ladder` in `cli.py`, tested in `tests/test_cli.py`.

## Teardown

`bq-context cleanup` removes everything above. Views cost nothing to store, but
the 45 profile scans are billable BigQuery work each time they run, so leave
them idle rather than rescanning.

Related: [[dataplex-catalog-gotchas]], [[hybrid-vertex-environment]].
