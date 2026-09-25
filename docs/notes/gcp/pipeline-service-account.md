# The pipeline service account

`bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com`, created 2026-09-22.

Deliberately **not** the Compute Engine default SA. Omitting `service_account=`
on a `PipelineJob` silently falls back to it, and in a long-lived sandbox that
account often carries Editor — so the pipeline works here and breaks in any
project that enforces least privilege.

## Grants

Project-level on `hybrid-vertex`:

| role | for |
|---|---|
| `roles/aiplatform.user` | Gemini calls, running the pipeline |
| `roles/bigquery.dataEditor` | create the 4 tier datasets and 60 views |
| `roles/bigquery.jobUser` | run queries and profile scans |
| `roles/dataplex.dataScanEditor` | create / run the 45 profile scans |
| `roles/dataplex.catalogEditor` | glossary, terms, entry links, aspects |
| `roles/dataplex.catalogViewer` | the role `lookupContext` docs name explicitly |
| `roles/browser` | `resourcemanager.projects.get`, cheapest precise grant |
| `roles/serviceusage.serviceUsageConsumer` | `serviceusage.services.use` |
| `roles/logging.logWriter` | component logs |

Scoped, not project-wide:

- `roles/storage.objectAdmin` on **`gs://hybrid-vertex-bq-context` only**. The
  project has ~310 buckets; a project-level grant would reach all of them.

On the SA itself, for the human who submits:

- `roles/iam.serviceAccountUser` — without it submission fails with a confusing
  `PERMISSION_DENIED` on `actAs`.
- `roles/iam.serviceAccountTokenCreator` — needed to impersonate for the checks
  below.

Not granted, and not needed: `roles/artifactregistry.reader`. The image and the
pipeline live in the same project, so the Vertex service agent can pull without
it. It becomes necessary the day the image moves.

`roles/dataplex.admin` is deliberately avoided. It is the tempting unblock when
setup fails at minute 35, and it makes the IAM story untestable.

## Verify as the SA, never as yourself

```bash
SA=bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com
bq-context validate-config --impersonate "$SA"
bq-context preflight --tier 3 --impersonate "$SA"
```

A developer ADC account here is near-Owner, so **checking as yourself proves
nothing** — everything passes locally and the pipeline fails on a fresh
principal. Both gates therefore take `--impersonate`, and both thread those
credentials all the way down to the BigQuery, Dataplex, and Storage clients.

That threading is not incidental. The first version of `validate-config`
reported `storage writable` from a client built on ambient ADC while claiming to
check the SA — a false pass in the one command whose entire job is honesty. Any
new check added here must take `credentials` explicitly.

## Result: the silent-empty hazard is not present

Running `preflight` as the SA returned a context ladder **byte-for-byte
identical** to the ADC run:

```
tier   tables     bytes  profiled  aspects
0          15    49,089         0  —
1          15   118,275        15  —
2          15   119,882        15  —
3          15   122,346        15  overview
```

So `lookupContext` is not silently empty for this principal, and the
`dataplex.entries.get` / `catalogViewer` grants are doing their job. That was
the main risk this task existed to retire. (The tier-2 flatness is a separate,
unrelated problem — see [[corpus-provisioning]].)

**This covers `lookupContext`, not search.** Semantic `search_entries` does
return different tables to this SA than to ADC, even with identical grants. It
depends on the age of the principal, not its roles. Until 2026-09-25,
`--impersonate` never reached the search, so this check could not see it. See
[Search depends on identity](../search-depends-on-identity.md).

## The 16 checked permissions

`validate-config` calls `testIamPermissions` for these, grouped by purpose so a
failure names the action that would break rather than a bare permission string:

- **create the tier datasets and views** — `bigquery.datasets.create|get`,
  `bigquery.tables.create|get|list|update`
- **run queries and profile scans** — `bigquery.jobs.create`,
  `dataplex.datascans.create|run`
- **write catalog enrichment** — `dataplex.entries.update`,
  `dataplex.entryLinks.create`, `dataplex.glossaries.create`
- **read catalog context** — `dataplex.entries.get`, `dataplex.entryGroups.get`
- **call Gemini** — `aiplatform.endpoints.predict`
- **resolve the project** — `resourcemanager.projects.get`

`dataplex.entries.get` is the one that matters most, for the reason above.

## The 17th permission is checked somewhere else, deliberately

`secretmanager.versions.access` — on the secret holding the Gemini Developer API
key that PaperBanana needs — is **not** in that table, and a test enforces its
absence. Two live behaviours are the reason, both verified against the API on
2026-09-22.

**A resource-level binding is invisible to a project-level check.** The table
above is resolved with one `testIamPermissions` against `projects/hybrid-vertex`,
which only sees policy attached at the project. `roles/secretmanager.secretAccessor`
is bound directly on `bq-context-secret` here, so listing the permission in that
table would report a grant as missing while it is working — and the obvious fix
for that false alarm is to bind the role across the whole project. The check has
to name the secret resource:

```python
client.test_iam_permissions(
    request={
        "resource": f"projects/{project}/secrets/{secret}",
        "permissions": ["secretmanager.versions.access"],
    }
)
```

**A secret that does not exist returns an empty permission list, not `NotFound`
— the same answer as one you may not read.** This is the `lookupContext` trap in
a new costume (see [[dataplex-catalog-gotchas]]): the API answers "nothing" where
it could answer "denied". Measured:

| secret | `test_iam_permissions` | `get_secret` |
|---|---|---|
| `bq-context-secret` | `['secretmanager.versions.access']` | exists |
| `definitely-not-a-real-secret` | `[]` | `NotFound` |

So an empty list alone cannot pick a remedy, and the two remedies are opposite —
create the secret, versus bind a role on one that already exists. `validate-config`
probes with `get_secret` before advising. That probe needs `secretmanager.secrets.get`,
which a least-privilege principal may lack, so anything other than `NotFound` is
treated as "exists": guessing "absent" would tell someone to create a secret they
already have.

**Why the check exists at all.** `figures.api_key` deliberately swallows a denied
or absent secret and returns `None`, because `finalize` is the exit task and may
only turn a run red for missing cells. Without a gate at submit time, a missing
grant is a green pipeline with no diagrams, ninety minutes in, and nothing in the
logs naming IAM.

An unset `SECRET_ID` is reported but is **not** a failure — figures are opt-in
and off by default, so requiring a secret would fail the runs that never wanted
one. `validate-config --require-secret` makes it fatal, for the figures path.

Related: [[dataplex-catalog-gotchas]], [[hybrid-vertex-environment]], [[kfp-pipeline]].
