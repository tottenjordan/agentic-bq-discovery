# The runner container

Built 2026-09-22. Image `us-central1-docker.pkg.dev/hybrid-vertex/bq-context/runner`,
**1.44 GB**, Cloud Build **1m33s** (local `docker build` 83s).

## `ENV PATH="/app/.venv/bin:$PATH"` is the whole integration

KFP overwrites the container command with its own
`sh -c ... python3 -m kfp.dsl.executor_main` shim, so `ENTRYPOINT` and `CMD` are
ignored for pipeline use. `PATH` is what makes that injected bare `python3`
resolve to the venv interpreter.

Confirmed empirically rather than assumed — running the built image with `PATH`
reset to the system default:

```
$ docker run --rm -e PATH=/usr/local/bin:/usr/bin:/bin $IMG python3 -c "import bq_context"
ModuleNotFoundError: No module named 'bq_context'
```

No mention of PATH, no hint at the cause. In a pipeline that surfaces ten
minutes and a container pull into a run. `cloudbuild.yaml` therefore asserts
`sys.executable.startswith("/app/.venv/")` at build time, which turns a
ten-minute mystery into a two-second build failure.

## Two corrections to the plan

**uv must be 0.11, not 0.9.** `pyproject.toml` pins `uv_build>=0.11.28,<0.12.0`
as the build backend, so `ghcr.io/astral-sh/uv:0.9` cannot build the project at
all. The plan's version predates the pin.

**`experiments/` has to be in the image.** `run-shard` reads
`experiments/questions.json` at runtime and the plan's Dockerfile only copied
`src/` and `README.md`. Baking the questions in rather than fetching from GCS
also means the git SHA in `code_version` describes the question set, which
matters because that SHA is part of the KFP cache key.

## Cloud Build substitutions do not nest

This fails to parse:

```yaml
substitutions:
  _REPO: us-central1-docker.pkg.dev/${PROJECT_ID}/bq-context/runner   # NO
steps:
  - name: ${_REPO}:${_TAG}
```

> `invalid build step name "...${PROJECT_ID}...": could not parse reference`

`$PROJECT_ID` is expanded in step `name`/`args`, but *not* inside another
substitution's default value. Write the full path inline in every step.

## Layer split, and why

`pyproject.toml` + `uv.lock` are copied and synced before the source, so editing
code does not re-resolve ~180 packages. `UV_COMPILE_BYTECODE=1` plus
`compileall` precompiles 10,264 files at build time so cold imports do not pay
`.pyc` compilation — the novastorm image measured ~70s of cold import graph
without it (see [[prior-art-novastorm-kfp]]).

`kfp` is a **runtime** dependency, not dev, specifically so `uv sync --no-dev`
keeps it in the image. That is the precondition for `install_kfp_package=False`
on the components.

## Tagging

Always reference the **SHA tag**, never `:latest`. The KFP execution cache key
includes the image reference, so an immutable tag makes the cache correct while
a floating tag makes it lie — you would get a cached green result from code you
changed three commits ago.

`make image` refuses to build from a dirty working tree, since the SHA tag would
not describe the image contents. `make image-ref` prints the current reference.

A `:cache` tag is pushed alongside for `--cache-from` on subsequent builds.

Current: `:1baa86f`, digest
`sha256:76ac686c3b70af1bc1251f8dafc42c63fdd91f91d13f8b92195247873dd7caf8`.

## Where each environment variable actually comes from

Audited end to end on 2026-09-22, because the answer was not what the code
suggested. There are four sources, and knowing which one wins is the whole game:

| variable | image | forwarded to tasks | notes |
|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | yes | — | every component body also sets it from the `project` pipeline parameter |
| `GOOGLE_CLOUD_LOCATION` | `global` | **never** | derived; `configure_adk_env` overwrites it in-process |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` | **never** | derived, same reason |
| `BQ_CONTEXT_LOG_FORMAT` | `json` | — | structured logging for Cloud Logging |
| `AGENT_MODEL`, `TOOL_MODEL` | no | **yes** | the models under test |
| `BQ_LOCATION`, `DATAPLEX_LOCATION` | no | **yes** | |
| `RESOURCE_PREFIX` | no | **yes** | which corpus is measured |
| `TOP_K` | no | **yes** | retrieval depth; changes the metrics |
| `SECRET_ID` | no | `finalize` only | the only task that generates figures |

**The six "forwarded" rows were not forwarded at all until this audit.** Nothing
set them in the container, so `ExperimentConfig.from_env` fell through to its
defaults on every pipeline run regardless of `.env`. They *looked* fine only
because each `.env` value happened to equal the corresponding default — including
`RESOURCE_PREFIX`, which had genuinely diverged earlier the same day.

The second consequence was worse than the divergence. Shards key their cache on
`code_version` and `corpus_fingerprint`; neither moves when `AGENT_MODEL` changes,
so switching models and resubmitting at the same commit returned cells scored
with the **previous** model, green. Forwarding the values puts them in the
executor's container spec, which is part of what Vertex hashes.

`GOOGLE_CLOUD_LOCATION` is excluded deliberately and a test enforces it. It is an
*output* of `configure_adk_env`, not user configuration, and this repo's own
`.env` carries `us-central1` — forwarding that would send every Gemini call to an
endpoint where these models 404 (see [[gemini-endpoints-and-quota]]). It is inert
today only because the reranker passes `location=` explicitly and ADK is
configured before any agent is built.

Two things the audit found that are *not* problems: `GOOGLE_GENAI_USE_VERTEXAI`
may be `TRUE` or `true` — google-genai lowercases, verified for
`true/TRUE/True/1`. And `PROJECT_NUM` in `.env` is dead: `corpus/setup.py`
resolves the project number through the API and never reads it.

Related: [[local-smoke-results]] for the ADK environment variables this image
also sets, [[kfp-pipeline]] for why these are compile-time constants rather than
pipeline parameters.
