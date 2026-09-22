# Gemini endpoints and quota in `hybrid-vertex`

*Verified 2026-09-22 against project `hybrid-vertex`.*

## The experiment's models are `global`-endpoint only

`gemini-3.6-flash` (agent model) and `gemini-3.5-flash-lite` (reranker model) **do not exist
in `us-central1`**. They resolve only at the `global` endpoint:

| model | `us-central1` | `global` |
|---|---|---|
| `gemini-3.6-flash` | 404 | 200 |
| `gemini-3.5-flash-lite` | 404 | 200 |

So `GOOGLE_CLOUD_LOCATION=global` for the genai/ADK client, while pipeline *compute* stays in
`us-central1`. These are two different "locations" and conflating them is the most likely
day-one failure. Upstream's `.env` already sets `AGENT_MODEL_LOCATION=global` and
`TOOL_MODEL_LOCATION=global` — that is why, not a stylistic choice.

Note `config.py` mutates `os.environ["GOOGLE_CLOUD_LOCATION"]` at import time when
`AGENT_MODEL_LOCATION` is set, because ADK reads model endpoints from that env var.

### Probe it correctly

`GET` on the publisher-model resource returns **404 even for models that exist** — it is not a
valid availability probe. Use `POST :countTokens`:

```bash
TOK=$(gcloud auth print-access-token)
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}' \
  "https://aiplatform.googleapis.com/v1/projects/hybrid-vertex/locations/global/publishers/google/models/gemini-3.6-flash:countTokens"
```

## These models are on Dynamic Shared Quota — there is no headroom to check

No `generate_content_requests_per_minute_per_project_per_base_model` bucket exists for
`gemini-3.5-flash`, `gemini-3.6-flash`, or `gemini-3.5-flash-lite`. That is the DSQ signature:
no per-project limit, nothing to raise, and **a 429 means transient shared-pool contention**.

Consequences for the experiment:

- Do not plan around "check quota headroom, then set concurrency." There is no number to read.
- The mitigation is client-side backoff plus an empirical concurrency ramp, not a quota request.
- `hybrid-vertex` is a shared sandbox, so the risk is other tenants, not our own load. At the
  planned 8-way shard concurrency we are ~2% of a comparable published bucket.
- The `global` endpoint routes to whichever region has capacity, which is itself the documented
  first-line 429 mitigation.

See [[hybrid-vertex-environment]] for the rest of the project's state and
[[upstream-experiment]] for the measured token volumes this has to absorb.
