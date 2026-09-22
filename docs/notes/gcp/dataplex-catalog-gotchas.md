# Dataplex / Knowledge Catalog gotchas

Knowledge Catalog is the product formerly called Dataplex Universal Catalog (renamed April 2026).
The API, SDK, and IAM namespace are all still `dataplex`.

## `lookupContext` returns empty instead of 403 — the worst failure mode here

> "The lookupContext method filters the resources based on your permissions… If you have no
> permissions on the requested resources, the method returns an empty response."

An under-permissioned principal does **not** get a clear error. It gets `{}`. For this
experiment that means tiers 1–3 would score identically to tier 0, every check would pass, the
pipeline would go green, and we would publish a plausible, wrong result — which looks exactly
like upstream's "flat tier response" finding (see [[upstream-experiment]]).

**Therefore:** any preflight must assert *non-empty* context on a known tier-3 table and assert
that tier 3 differs from tier 0, before any measurement runs. Never treat "the run was green" as
evidence that enrichment was real.

Grant both `roles/dataplex.catalogEditor` and `roles/dataplex.catalogViewer` — the latter is the
role the `lookupContext` docs name explicitly, and the overlap is free insurance against a silent
empty response.

## Semantic search: parentheses silently break scoping

The query is built bare, and it has to stay that way:

```python
query = f"{question} system=BIGQUERY parent:datasets/{ds}"
```

Wrapping the free-text question in parentheses makes the query parser **silently drop the
`parent:` predicate**, leaking out-of-scope tables. Upstream verified this live. With 254
datasets in this project (see [[hybrid-vertex-environment]]) that leak would be large and quiet.

Also: `search_entries` is issued against `locations/global`, not a regional endpoint, and
`page_size` is 20 — upstream's probe found raising it to 50/100 adds nothing because search's
internal relevance cutoff, not page size, governs the result count.

## Three different locations for catalog resources

Upstream's code and its own readme disagree here; **the code is authoritative**:

| resource | location |
|---|---|
| BigQuery tier datasets | `US` multi-region |
| DataScans (profiling) | `us-central1` — must be a single region |
| Glossary, terms, entry links | `us` — i.e. `BQ_LOCATION.lower()`, **not** the DataScan region |

Entry links require every referenced entry to live in the link's region (or `global`), so the
glossary, its terms, and the definition links must be co-located with the BigQuery entries.
The readme says these live in `DATAPLEX_LOCATION`; the code puts them in `CATALOG_LOCATION` and
comments explicitly that it is *not* `DATAPLEX_LOCATION`.

## Quotas and limits

- DataScan **runs**: 30/project/user/min (this is the binding one). DataScan **writes**:
  200/project/min. Upstream's hardcoded `time.sleep(5)` between creations yields 12/min — 2.5×
  more conservative than necessary, but creation is not the bottleneck, polling is.
- Do not parallelize scan creation: wall time is dominated by polling, so parallelism converts a
  sleep into a 429 storm and buys nothing.
- `lookupContext` accepts a **maximum of 10 entries per call**. Batch accordingly.
- Entry-link IDs and DataScan IDs cap at **63 characters**; upstream hashes the overflow.
- The `lookupContext` budget option key is unresolved upstream — the REST/proto reference
  documents `context_budget` while a Google Python sample uses `budget`, so it is omitted.

## Entry groups

Creating *entries* in system entry groups like `@bigquery` is not permitted by any role. Creating
**entry links** in `@bigquery` is fine and is what the setup does
(`dataplex.entryLinks.create` + `dataplex.entryGroups.useDefinitionEntryLink`).
