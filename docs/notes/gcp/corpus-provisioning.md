# Provisioning the four-tier corpus

`bq-context ensure-infra` run against `hybrid-vertex` on **2026-09-22**.
Duration **19m22s**, exit 0. Re-verify before relying on any figure here.

## What got created

| resource | count | location |
|---|---|---|
| GCS results bucket | 1 | `us-central1` (regional) |
| BigQuery datasets `bigquery_context_tier0..3` | 4 | `US` |
| Views over `bigquery-public-data` | 60 (15 × 4) | `US` |
| Dataplex profile scans | 45 (tiers 1–3) | `us-central1` |
| Business glossary `bigquery-context-glossary` | 1 | `us` |
| Glossary terms | 11 | `us` |
| Definition entry links in `@bigquery` | 48 | `us` |
| `overview` aspects on tier-3 tables | 4 | — |

The bucket was **not** created by this run — it was made by hand during
preflight, and `ensure-infra` only started creating it later (see below).

Rough phase timings from the log: datasets + 60 views ≈ 2 min; 45 scan
creations at the hardcoded 5s throttle ≈ 7 min; scan polling ≈ 8 min; glossary,
terms, and links ≈ 2 min.

## The bucket is a local bootstrap step

`ensure-infra` now creates the results bucket first, regional in
`config.locations.gcs` (`us-central1`, matching pipeline compute) with uniform
bucket-level access. Idempotent: an existing bucket reports `exists`, and losing
a creation race to another operator is treated as success.

It lives in `corpus/bucket.py` rather than `corpus/setup.py` because that module
is vendored near-verbatim from upstream and kept re-syncable; upstream has no
bucket, since its harness writes locally.

**This can only ever be a local step.** A pipeline run cannot reach the code
without the bucket already existing — `pipeline_root` is inside it — and the
pipeline service account holds `roles/storage.objectAdmin` scoped to that
bucket, which does not include `storage.buckets.create`. In the pipeline the
call is therefore always a no-op existence check. If it ever 403s, the error
says so explicitly, because the tempting fix is to widen the SA to
`roles/storage.admin` and that is wrong.

## The tier ladder

`bq-context preflight --tier 3` reports what actually reached the capsule:

```
tier   tables     bytes  profiled  terms  aspects
0          15    49,089         0      0  —
1          15   118,275       209      0  —
2          15   119,882       209     18  —
3          15   122,346       209     18  overview
```

Every rung adds something, so the gate raises no warning.

| tier | intended | actual |
|---|---|---|
| 0 | schema only | ✅ as intended |
| 1 | + data profiling | ✅ 209 profiled columns, +69 KB |
| 2 | + business glossary | ✅ 18 annotated columns / 8 tables (24 with `all_schema_fields`) |
| 3 | + authored guidelines | ⚠️ `overview` aspect on 4 tables; `guidelines` is not available |

### Tier 2: real, but partially truncated by default

**Corrected 2026-09-22.** An earlier version of this note said glossary
enrichment never reached the capsule and that tier 2 was a dead factor level.
That was wrong. Glossary definitions arrive **per-column under a `terms` key**,
carrying the term's display name and description:

```json
"name": "subscriber_type",
"terms": "Subscriber Type; Rider membership category. Distinguishes
          annual/monthly members from single-ride and walk-up casual users."
```

Tier 2 carries **18 annotated columns across 8 tables**; tiers 0 and 1 carry
zero. It is a genuine factor level.

The caveat that remains is smaller: the default capsule truncates each schema to
**25 columns**, so 6 of the 24 term links — 5 on the 153-column `hurricanes`
view, 1 on `air_quality_annual_summary` — fall past the cut.
`all_schema_fields=true` recovers all 24 at ~1.8× the capsule size. See
[[lookup-context-capsule]] for the measurements and the open decision.

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

**This is not fixable with IAM.** Verified 2026-09-22: a near-Owner account gets
the same 403 on `guidelines` while reading `overview` from the same project in
the same breath, and `guidelines` does not appear in
`gcloud dataplex aspect-types list --project=dataplex-types`.

```
$ gcloud dataplex aspect-types describe guidelines --location=global --project=dataplex-types
PERMISSION_DENIED: Permission 'dataplex.aspectTypes.get' denied
$ gcloud dataplex aspect-types describe overview   --location=global --project=dataplex-types
name: projects/dataplex-types/locations/global/aspectTypes/overview
```

So it is an availability restriction on that aspect type — presumably preview or
allowlist-gated — not a permissions gap on our side. `dataplex-types` is
Google-owned, so there is no policy to bind against anyway; the 403 text reads
like a missing role and is not one.

Treat the `overview` fallback as a property of this environment rather than a
misconfiguration. Tier 3 therefore tests an overview blob rather than authored
NL→SQL guidance, which is a caveat on the tier-3 arm, not a defect to repair.
Obtaining real `guidelines` would mean asking Google for allowlist access — an
account conversation, not a command.

## The gate was wrong twice, in opposite directions

First it compared only tier 3 against tier 0, which cannot see a flat rung in
the middle. Then, once it walked every rung, it compared **byte deltas** and
looked only at top-level capsule keys — so it declared a fully-enriched tier 2
dead, because real glossary enrichment is ~1.6 KB and lives per-column.

It now compares an enrichment **signature** — `(aspects, profiled columns,
term-annotated columns)` — and ignores bytes entirely. See `assess_ladder` in
`cli.py`, tested in `tests/test_cli.py`.

## Teardown

`bq-context cleanup` removes everything above. Views cost nothing to store, but
the 45 profile scans are billable BigQuery work each time they run, so leave
them idle rather than rescanning.

Related: [[dataplex-catalog-gotchas]], [[hybrid-vertex-environment]].
