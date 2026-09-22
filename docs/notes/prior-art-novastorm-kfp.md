# Reusable KFP prior art at `/home/user/novastorm`

`/home/user/novastorm/bq_insights_agent` is a substantial, working Vertex AI Pipelines codebase
in **this same GCP project**. It is outside this repo, so nothing here records it — but it has
already paid for lessons we would otherwise repeat. Verified present 2026-09-22.

## `src/pipelines/compilation.py` — never commit pipeline YAML

Adopt this rule verbatim. Their writeup documents the incident behind it:

> Two copies of the compiled YAML ended up tracked in git and both went stale — one six months
> out of date pinning another project's container image, the other four months behind `dag.py`,
> missing seventeen pipeline parameters.

The dangerous part is *why it stayed invisible*: **a stale template still submits successfully.**
Every parameter the callers passed existed in both the old and new spec, so Vertex accepted the
job and ran an obsolete pipeline that returned plausible-looking results. Nothing errored.

So: compile at the point of use, never read a YAML somebody else wrote. Compiling is cheap —
they measured 0.3s for a 332KB file, so there is no reason to cache it. They also note importing
the DAG costs ~0.7s and drags the whole component graph, hence the deferred `from kfp import
compiler` inside the function.

## `Dockerfile` — uv-in-Docker, with measured numbers

Their header comments carry real measurements from a daily production run:

- Container start → first app log was **~3 min**, dominated by image pull (~128s) and the cold
  Python import graph (~70s for vertexai/aiplatform/adk).
- The jobs originally used `uv run <cli>`, which **re-resolves the environment and rebuilds the
  editable project on every container start**.

Both fixes are worth copying: precompile bytecode at build time (`UV_COMPILE_BYTECODE=1` for
deps plus `compileall` for source) so cold imports don't pay `.pyc` compilation, keep the uv
cache out of the image so the pull is smaller, and **invoke the venv binary directly rather than
`uv run`** so there is no environment re-resolve at start.

Their base-image note is the inverse of ours and worth understanding rather than copying: they
moved *from* `python:3.13-slim` *to* `python:3.11-slim` because their pinned interpreter is 3.11
and the mismatch forced uv to download a managed Python at build. The principle — match the base
image to `.python-version` — is what transfers. For us that means `python:3.13-slim`.

## Other files worth reading before writing pipeline code

- `src/pipelines/dag.py`, `components.py` — working KFP v2 topology in this project.
- `src/pipelines/deploy.py` (841 lines) — includes a `package_and_upload_code` tarball-to-GCS
  plus `sys.path` hack for getting a non-PyPI source tree into a component. It works, but it is
  the workaround a custom container makes unnecessary. Know it exists; don't reach for it.

Related: [[hybrid-vertex-environment]].
