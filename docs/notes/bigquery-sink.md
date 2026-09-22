# The BigQuery query sink

`hybrid-vertex.bigquery_context_results.cells` — created 2026-09-22, loaded with
full-01's 3,000 cells.

**A sink, not the system of record.** `gs://…/experiments/{id}/merged/results.jsonl`
is authoritative. This table is a convenience for slicing results with SQL, so a
load failure is a warning rather than a failed sweep — re-running `merge`
reloads it.

## Shape

| property | value |
|---|---|
| partitioning | `DAY` on `written_at` |
| clustering | `(approach, tier)` — almost every query filters on both |
| payload | one `JSON` column holding `relevance`, `nominated`, `ranked_tables`, `search_stats` |
| scope | every experiment; always filter `WHERE experiment_id = …` |

Scalar fields that queries filter or aggregate on are real columns; the
variable-width nested fields go in `payload` so a new per-approach statistic is
not a schema migration.

## The bug worth remembering

Handing a BigQuery `JSON` column a **`str`** (i.e. `json.dumps(...)`) stores it
as a JSON *string scalar*, not an object. `JSON_TYPE(payload)` returns `'string'`
and every `JSON_VALUE(payload.x)` returns `NULL` — the column is write-only.

**The load job succeeds.** Nothing warns. It was only caught by querying the
table instead of trusting the load, and it is pinned now by
`test_payload_is_an_object_not_a_string`. Pass the dict; let the client
serialise it.

```sql
-- the check, if a payload path ever returns NULL
SELECT JSON_TYPE(payload), COUNT(*) FROM …cells GROUP BY 1   -- must be 'object'
```

## Loading

`bq-context merge` loads automatically when `--out` is a `gs://` URI;
`--no-bigquery` opts out, and a local `--out` never loads. Delete-then-append
scoped to `experiment_id`, so re-running merge does not double rows — verified
by loading full-01 three times and still seeing 3,000 rows / 3,000 distinct
`cell_key`.

## It reproduces the warm-up confound in SQL

```sql
SELECT tier,
       COUNTIF(CAST(JSON_VALUE(payload.search_stats.raw_search_count) AS INT64) = 0) AS zero_hit,
       ROUND(AVG(CAST(JSON_VALUE(payload.search_stats.raw_search_count) AS INT64)), 2) AS mean_hits
FROM `hybrid-vertex.bigquery_context_results.cells`
WHERE experiment_id = "full-01" AND approach = "search_direct"
GROUP BY tier ORDER BY tier
```

```
tier  zero_hit  mean_hits
0     10        2.88
1     5         3.92
2     0         4.64
3     0         4.96
```

Matching [[full-run-results]] exactly — a third independent view of the same
index warm-up confound, now reachable without re-deriving it from JSONL.

Related: [[full-run-results]], [[corpus-provisioning]].
