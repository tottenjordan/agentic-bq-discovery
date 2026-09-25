# The GCS layout, and why it is shaped this way

One bucket holds three different kinds of thing with three different lifetimes,
and the rules below are what keeps them from destroying each other. The tree is
the easy part; the reasoning is what someone reproducing this on their own data
actually needs.

```
gs://{bucket}/
├── corpus/
│   ├── provisioned/{resource_prefix}.json   what `ensure-infra` built, and when
│   └── {fingerprint}/
│       ├── manifest.json                    what the corpus *is*
│       └── ladder.json                      what `preflight` measured about it
│
├── experiments/{experiment_id}/
│   ├── experiment.json        the corpus + questions this id was FIRST run against
│   ├── questions.json         the question set, snapshotted at submission
│   ├── shards/{tier}__{approach}/           STABLE — resume and merge read this
│   │   ├── attempt-NNNN.jsonl
│   │   ├── summary-NNNN.json
│   │   └── _SUCCESS | _FAILED           exactly one; see below
│   ├── merged/                              STABLE — deterministic, regenerated
│   │   ├── results.jsonl
│   │   └── missing.json
│   └── runs/{run_id}/                       one execution, never overwritten
│       ├── manifest.json      commit, corpus, config, completeness counts
│       ├── preflight.json     what preflight measured on this run
│       ├── scoring/report.md
│       ├── scoring/executive.html
│       └── plots/*.png
│
├── pipeline_root/             KFP's; do not touch
└── _scratch/validate_config   the writability probe
```

## The four rules

**A path resume depends on may not be versioned.** `resume.shard_prefix` and
`merge` both build the shard path from `experiment_id` alone. Version it — by
run, by date, by anything — and a resumed run cannot find the previous one's
work, which is the whole reliability story here. `merged/` is stable for a
different reason: it is a pure function of the shards, so a second copy is
4.8 MB that can only ever disagree with the first.

**A path nothing depends on should be versioned.** Report, executive HTML,
figures and the manifest are ~420 KB per execution and are what a human reads.
Keying them on `experiment_id` meant every execution overwrote the last.
`hard-full-01` ran three times — the original, a cache-hit no-op, and the
`--no-cache` recovery — and kept one report. The failed run's report was the
evidence for the shard exit-code bug, and it is gone.

**`run_id` is minted once, at submission.** It is a pipeline parameter for the
same reason `experiment_id` is. Generated inside the pipeline instead, each task
would mint its own and a single run's artifacts would land in as many folders as
there are tasks that write one. Format is `{UTC timestamp}-{short SHA}`:
timestamp first so a bucket listing is chronological, SHA second so a folder says
what produced it without opening anything. Not the Vertex job id — a local run
has none — but the job id is in the manifest.

**The corpus is keyed by fingerprint, not by resource prefix.** A prefix is
reused as enrichment changes, so a record under it is overwritten by the next
provisioning; fingerprints accumulate. It is also the identifier already on every
cell and in the BigQuery sink, so `corpus/{fingerprint}/` is where a reader lands
after a `GROUP BY corpus_fingerprint`.

## The shard markers are exclusive, and were not

`_SUCCESS` and `_FAILED` describe a shard's terminal state, so exactly one
should exist. Until this was fixed, `_write_marker` wrote one and left the
other — and **every** `_FAILED` in the bucket, all six across two experiments,
had a newer `_SUCCESS` beside it. The mechanism is ordinary: a shard fails and
writes `_FAILED`, KFP retries it, the retry resumes into the same directory and
writes `_SUCCESS`, and nothing removes the first.

It survived because nothing in the code reads these files. The consumer is a
person opening the bucket to find what broke, and for them the signal was wrong
in exactly the situation they opened it for — a stale `_FAILED` is
indistinguishable from a real one. The reverse case is worse: a shard that
passed and later started failing kept advertising success, which is the marker
someone trusts to mean the data is complete.

`ArtifactStore.delete` exists for this one caller. It takes a single path, with
no prefix or recursive form, on purpose: this store holds twelve hours of
irreplaceable agent output and the only object the code ever removes is a
seventeen-byte marker it wrote itself. Resist widening it.

## Why the question set is snapshotted, not referenced

`submit-pipeline --questions` could have passed a URI through and let each shard
read it. Copying it into the experiment prefix instead buys two things a
reference cannot.

It **cannot change under a running sweep.** A full factorial is 24 shards over
~2 hours; the source file is a mutable object for all of it. The snapshot is
written once, before the job is created.

It **archives the question set with the results it produced.** The same move
`corpus/{fingerprint}/` makes: a year later, "what was byoq-01 actually asked?"
is answerable from the bucket rather than from someone's laptop.

The snapshot is the one write in this layout that is *not* best effort. The
corpus and provisioning records are diagnostics — losing one costs provenance.
This is an input every shard reads, so failing to write it stops the submission
rather than producing 24 shards that cannot find their questions.

## Two things that follow from the exit task's guarantee

`finalize` is an `ExitHandler` exit task: its value is that it runs when
something upstream died. Two consequences show up in this layout.

It **cannot take preflight's fingerprint as a task output** — that would make it
depend on a task allowed to fail. So `preflight` writes
`runs/{run_id}/preflight.json` and `finalize` reads it back, degrading to an
empty record if it is absent. Same bargain `merge_report` already makes for
`missing.json`.

It **writes the manifest before the completeness check**, so a red run gets one.
A failed run is exactly when someone needs to know what it was running.

## What is deliberately not here

- **No migration.** Experiments from before this change keep their old shape:
  `full-01` and `hard-full-01` have `scoring/` and `plots/` directly under the
  experiment prefix. `merge` and `score` still read them, because nothing about
  the stable paths moved.
- **`pipeline_root/` stays where it is.** It is KFP's, `submit-pipeline` passes
  it, and moving it would orphan every existing job's artifacts.
- **No lifecycle policy on `runs/`.** 420 KB per execution is not yet a problem.
  Worth revisiting if someone runs the factorial daily.

## Related

- [The Vertex AI Pipeline](kfp-pipeline.md) — the topology these paths serve.
- [Provisioning the four-tier corpus](gcp/corpus-provisioning.md) — what
  `ensure-infra` creates, which is what `corpus/` now records.
