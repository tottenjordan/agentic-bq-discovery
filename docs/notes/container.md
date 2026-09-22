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

Related: [[local-smoke-results]] for the ADK environment variables this image
also sets.
