# Pipeline runs — milestone 1 exit

2026-09-22. Both exit criteria met: the pilot is green cold, and green again as
a resume after a deliberate mid-flight cancellation.

| run | profile | cells | result |
|---|---|---|---|
| `smoke-01` | smoke | — | FAILED at `validate-config` — self-impersonation |
| `smoke-02` | smoke | — | FAILED at `validate-config` — `"default"` alias |
| `smoke-03` | smoke | 18/18 | **SUCCEEDED**, 20/20 tasks |
| `pilot-01` | pilot | 120/120 | **SUCCEEDED** cold, 24/24 shards |
| `pilot-02` | pilot | 112/120 | cancelled mid-flight on purpose |
| `pilot-02` (resubmit) | pilot | 120/120 | **SUCCEEDED**, 20 tasks cache-skipped |

Both failures were caught by `validate-config`, the fail-fast gate, with every
downstream task correctly cancelled — nothing was provisioned or measured under
a broken identity.

## Two ways to get identity wrong in a pipeline

**A task cannot impersonate itself.** The components originally passed
`--impersonate <pipeline SA>`, but a pipeline task *already runs as* that
account, so it was asking to impersonate itself:

```
Permission 'iam.serviceAccounts.getAccessToken' denied
```

Impersonation is a **local** tool for checking as the SA from a developer
account. Inside the pipeline the useful question is the opposite: assert we
*are* the expected principal. That also catches Vertex silently falling back to
the Compute Engine default SA when `service_account=` is omitted.

**`"default"` is an alias, not an identity.** With `--expect-identity` in place
the next run still failed:

```
FAIL  running as default, expected bq-context-pipeline@...
```

The pipeline was correct — `PipelineJob.service_account` confirmed
`bq-context-pipeline@...`. GCE-family credentials report
`service_account_email` as the literal string `"default"`, and it stays
`"default"` after a refresh. Because that is truthy, the gate compared it
against the real address and rejected a correctly-configured run.

`_effective_identity` now treats the alias as unresolved and asks the metadata
server (`/instance/service-accounts/default/email`) for the real address —
gated on the credentials actually being the VM's, because under a *user* ADC the
metadata server still answers, with the VM's account, which is not who the calls
are made as. Locally the command now reports "(ADC, principal not resolvable)",
which is the honest answer.

## Resume, as demonstrated

`pilot-02` was cancelled ~4 minutes in: 17 tasks cancelled, 112 of 120 cells
already durable across 23 shards. Resubmitting with the **same
`experiment_id`** produced:

```
JOB: PIPELINE_STATE_SUCCEEDED
tasks: {'SKIPPED': 20, 'SUCCEEDED': 36}
expected=120 present=120 missing=0
120 cells, 120 unique
24 shard files, 120 records, none holding other than exactly 5
```

The 20 skipped tasks are **KFP cache hits** — shards that finished before the
cancellation were skipped without starting a VM. That is the shard-level
mechanism, and it is only safe because `code_version` is an explicit shard input
so a changed commit invalidates it.

Worth being precise: this run exercised the **shard-level** resume. The
**cell-level** JSONL resume was proven separately under a hard `SIGKILL` in
[[local-smoke-results]]. Note also that a shard which finds nothing to do takes
the early-return path and writes a `_SUCCESS` marker but no new attempt file —
which is why all 24 shards here still have only `attempt-0001.jsonl`.

## Profiles need a question limit

Without one, "smoke" inherits all 25 questions and becomes 150 cells rather than
18. `--limit` takes a deterministic prefix, not a sample: resume can only
recognise prior work if the same cells are targeted every time. `merge` takes
the same flag or it reports phantom missing cells.

## Cost

Six live pipeline runs (two failed fast, one smoke, three pilot) plus the
rebuilds came to roughly 280 measured cells. Each pilot is ~9 minutes wall clock
at `parallelism=8`.

Related: [[kfp-pipeline]], [[pipeline-service-account]], [[local-smoke-results]].
