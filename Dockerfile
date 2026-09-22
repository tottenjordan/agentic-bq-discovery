# Runner image for the Vertex AI Pipelines components.
#
# Layout follows /home/user/novastorm/bq_insights_agent/Dockerfile, whose header
# records the cold-start measurements that motivate it: container start to first
# app log was ~3 min there, dominated by image pull and a cold Python import
# graph, and `uv run` re-resolved the environment on every container start.
# Both are addressed here — bytecode is precompiled at build time, the uv cache
# is kept out of the image, and the venv is on PATH so nothing re-resolves.
#
# Build with Cloud Build, not locally: the image is ~2 GB, Cloud Build runs
# in-region next to Artifact Registry, and the workstation uplink is not worth
# spending on it.
#
#   gcloud builds submit --region=us-central1 --config cloudbuild.yaml \
#     --substitutions=_TAG=$(git rev-parse --short HEAD)

# Match .python-version (3.13). A mismatch here makes uv download a managed
# interpreter at build time, which is slow and silently diverges prod from dev.
FROM python:3.13-slim

# uv from its own distroless image — no `pip install uv` bootstrap layer.
# Must be 0.11.x: pyproject pins `uv_build>=0.11.28,<0.12.0` as the build
# backend, so an older uv cannot build this project at all.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PYTHONUNBUFFERED=1

# Layer 1: dependencies only. Invalidated solely by pyproject.toml / uv.lock,
# so editing source does not re-resolve ~180 packages.
# --frozen pins to uv.lock; --no-dev drops ruff/pytest/ty from the runtime image.
# kfp is a runtime dependency precisely so it survives --no-dev, which is the
# precondition for install_kfp_package=False on the components.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: source, then install the project itself. README.md is required
# because pyproject declares `readme = "README.md"`.
#
# experiments/ carries questions.json, which run-shard reads at runtime. Baking
# it in rather than fetching it from GCS means the question set is versioned
# with the code, so the git SHA in `code_version` describes the questions too —
# which matters because that SHA is part of the KFP cache key.
COPY README.md ./
COPY experiments/ ./experiments/
COPY src/ ./src/
RUN uv sync --frozen --no-dev \
    && python -m compileall -q /app/src

# No ENTRYPOINT or CMD. KFP overwrites the container command with its own
# `sh -c ... python3 -m kfp.dsl.executor_main` shim, so anything set here is
# ignored for pipeline use.
#
# PATH is the load-bearing line: it is what makes KFP's injected `python3`
# resolve to the venv interpreter. Omit it and every component dies with a bare
# ModuleNotFoundError that says nothing about PATH.
#
# The GOOGLE_* variables are what ADK builds its own genai client from. The CLI
# also sets them via ExperimentConfig.configure_adk_env(), so this is belt and
# braces — but it is the difference between a container that works and a local
# run that does not, so both paths set them deliberately.
# GOOGLE_CLOUD_LOCATION is the *model endpoint*: gemini-3.x flash models return
# 404 in us-central1 and 200 at global.
ENV PATH="/app/.venv/bin:$PATH" \
    BQ_CONTEXT_LOG_FORMAT=json \
    GOOGLE_GENAI_USE_VERTEXAI=true \
    GOOGLE_CLOUD_PROJECT=hybrid-vertex \
    GOOGLE_CLOUD_LOCATION=global
