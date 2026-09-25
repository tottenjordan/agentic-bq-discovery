#!/usr/bin/env bash
# Daily check: does the pipeline SA still see a different semantic search index
# than a long-standing principal? See docs/notes/search-depends-on-identity.md.
#
# A Cloud Run job runs `bq-context preflight --impersonate <pipeline SA>` once a
# day from Cloud Scheduler. Preflight searches as the SA and then as the job's
# own identity, and warns per (tier, question) pair where the two differ. The
# job is a thin wrapper over the CLI, like every pipeline component.
#
# THE JOB'S OWN IDENTITY IS THE BASELINE, so it must be a principal that already
# sees the complete index. A freshly created SA would share the pipeline SA's
# degraded view, the two would agree, and the check would report a heal that
# never happened. The default compute SA matched the developer 12/12.
#
# usage: scripts/identity-watch.sh deploy|run|logs|down
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?GOOGLE_CLOUD_PROJECT is unset. Export it or set it in .env.}"
REGION="${REGION:-us-central1}"
JOB="${JOB:-bq-context-identity-watch}"
SCHEDULE="${SCHEDULE:-0 14 * * *}"
PIPELINE_SA="${PIPELINE_SA:-bq-context-pipeline@${PROJECT}.iam.gserviceaccount.com}"
QUESTIONS="${QUESTIONS:-experiments/questions-enrichment.json}"
IMAGE="${IMAGE:-${REGION}-docker.pkg.dev/${PROJECT}/bq-context/runner:$(git rev-parse --short HEAD)}"

project_number() {
  gcloud projects describe "$PROJECT" --format='value(projectNumber)'
}
WATCH_SA="${WATCH_SA:-$(project_number)-compute@developer.gserviceaccount.com}"

# Parsed by tests/test_identity_watch.py: every flag must be a real preflight option.
JOB_ARGS="preflight,--tier,3,--impersonate,${PIPELINE_SA},--questions,${QUESTIONS}"

# The config the pipeline forwards (CONFIG_ENV_KEYS), read from .env when present.
env_vars() {
  local vars="GOOGLE_CLOUD_PROJECT=${PROJECT}" key value
  for key in AGENT_MODEL BQ_LOCATION CORPUS_PROFILE DATAPLEX_LOCATION RESOURCE_PREFIX TOOL_MODEL TOP_K; do
    value="${!key:-$(sed -n "s/^${key}=//p" .env 2>/dev/null | head -1)}"
    [ -n "$value" ] && vars="${vars},${key}=${value}"
  done
  echo "$vars"
}

deploy() {
  gcloud artifacts docker images describe "$IMAGE" --project "$PROJECT" >/dev/null 2>&1 \
    || { echo "No image at ${IMAGE}. Run 'make image', or set IMAGE to one that exists." >&2; exit 1; }

  echo "baseline  ${WATCH_SA}"
  echo "checks    ${PIPELINE_SA}"
  echo "image     ${IMAGE}"

  # The one extra grant: the baseline may mint tokens for the pipeline SA only.
  gcloud iam service-accounts add-iam-policy-binding "$PIPELINE_SA" --project "$PROJECT" \
    --member "serviceAccount:${WATCH_SA}" --role roles/iam.serviceAccountTokenCreator \
    --condition None --quiet >/dev/null

  gcloud run jobs deploy "$JOB" --project "$PROJECT" --region "$REGION" \
    --image "$IMAGE" --service-account "$WATCH_SA" \
    --command bq-context --args "$JOB_ARGS" \
    --set-env-vars "$(env_vars)" \
    --max-retries 0 --task-timeout 15m --quiet

  gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJECT" --region "$REGION" \
    --member "serviceAccount:${WATCH_SA}" --role roles/run.invoker --quiet >/dev/null

  local uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB}:run"
  local verb=create
  gcloud scheduler jobs describe "$JOB" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 \
    && verb=update
  gcloud scheduler jobs "$verb" http "$JOB" --project "$PROJECT" --location "$REGION" \
    --schedule "$SCHEDULE" --time-zone UTC --uri "$uri" --http-method POST \
    --oauth-service-account-email "$WATCH_SA" --quiet >/dev/null

  echo "scheduled '${SCHEDULE}' UTC. Run now with: scripts/identity-watch.sh run"
}

run() {
  gcloud run jobs execute "$JOB" --project "$PROJECT" --region "$REGION" --wait
}

# One line per probe and per warning, oldest first. A shrinking "N of 48" is a heal.
logs() {
  gcloud logging read --project "$PROJECT" --freshness "${FRESHNESS:-10d}" --order asc \
    --format 'value(timestamp.date("%Y-%m-%d %H:%M"),textPayload)' \
    "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${JOB}\" AND
     (textPayload:\"search found\" OR textPayload:\"semantic search returns\")"
}

down() {
  gcloud scheduler jobs delete "$JOB" --project "$PROJECT" --location "$REGION" --quiet || true
  gcloud run jobs delete "$JOB" --project "$PROJECT" --region "$REGION" --quiet || true
  gcloud iam service-accounts remove-iam-policy-binding "$PIPELINE_SA" --project "$PROJECT" \
    --member "serviceAccount:${WATCH_SA}" --role roles/iam.serviceAccountTokenCreator \
    --condition None --quiet >/dev/null || true
  echo "removed ${JOB}, its schedule, and the token-creator grant"
}

case "${1:-}" in
  deploy | run | logs | down) "$1" ;;
  *) echo "usage: $0 deploy|run|logs|down" >&2; exit 2 ;;
esac
