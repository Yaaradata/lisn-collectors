#!/usr/bin/env bash

source scripts/_common.sh

TRACE_FILE="docs/trace/S1.md"
mkdir -p docs/trace

trace_line() {
  local result="$1"
  local check="$2"
  local details="$3"
  if [[ ! -f "$TRACE_FILE" ]]; then
    printf '| Result | Check | Details |\n|---|---|---|\n' >"$TRACE_FILE"
  fi
  printf '| %s | %s | %s |\n' "$result" "$check" "$details" >>"$TRACE_FILE"
}

upsert_env() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  if [[ -f .env ]] && grep -q "^${key}=" .env; then
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      index($0, k "=") == 1 {
        print k "=" v
        done=1
        next
      }
      { print }
      END { if (!done) print k "=" v }
    ' .env >"$tmp" && mv "$tmp" .env
  else
    printf '%s=%s\n' "$key" "$value" >>.env
  fi
}

reload_env() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}

need gcloud

: "${PROJECT:?PROJECT is required in .env}"
: "${REGION:?REGION is required in .env}"
: "${IMG:?IMG is required in .env}"
: "${SA_MOCK:?SA_MOCK is required in .env}"
: "${CONN:?CONN is required in .env}"

SERVICE_NAME="mock-sentinel"

echo "## Deploy mock $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# STEP 1 — Build and push image
# ---------------------------------------------------------------------------
ok "STEP 1 — Build and push ${IMG}"
# First image in the lisn Artifact Registry repository created in Sprint 1.
gcloud builds submit --tag "$IMG" --project "$PROJECT"
ok "Image built and pushed: ${IMG}"
trace_line "PASS" "image" "$IMG"

# ---------------------------------------------------------------------------
# STEP 2 — Deploy mock-sentinel to Cloud Run
# ---------------------------------------------------------------------------
ok "STEP 2 — Deploy ${SERVICE_NAME}"
# --ingress=internal: the mock stands in for a Flipkart internal system on a
# private address, so it must not be reachable from the public internet.
#
# --set-secrets: uses the SOCKET-form DSN (sentinel-mock-dsn), which is why we
# kept it separate from the direct-IP DSN used locally.
gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMG" \
  --service-account="$SA_MOCK" \
  --add-cloudsql-instances="$CONN" \
  --ingress=internal \
  --min-instances=1 \
  --port=8080 \
  --set-secrets="SENTINEL_MOCK_DSN=sentinel-mock-dsn:latest" \
  --set-env-vars="PYTHONPATH=/app" \
  --command=uvicorn \
  --args="mock.sentinel_api:app,--host,0.0.0.0,--port,8080" \
  --quiet

ok "Deployed ${SERVICE_NAME}"
trace_line "PASS" "deploy" "$SERVICE_NAME"

# ---------------------------------------------------------------------------
# STEP 3 — Capture service URL into .env
# ---------------------------------------------------------------------------
ok "STEP 3 — Capture SENTINEL_URL"
SENTINEL_URL_VALUE="$(
  gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(status.url)'
)"
upsert_env "SENTINEL_URL" "$SENTINEL_URL_VALUE"
reload_env
ok "SENTINEL_URL=${SENTINEL_URL}"
trace_line "PASS" "SENTINEL_URL" "$SENTINEL_URL"

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
ok "VERIFY — mock-sentinel service"
ready="$(
  gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(status.conditions[0].status)'
)"
verify "service condition Ready" "True" "$ready"

sa_deployed="$(
  gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(spec.template.spec.serviceAccountName)'
)"
# Accept full email or short name ending with mock-sentinel.
if [[ "$sa_deployed" == "$SA_MOCK" || "$sa_deployed" == *mock-sentinel* ]]; then
  verify "deployed service account is mock-sentinel" "mock-sentinel" "mock-sentinel"
else
  verify "deployed service account is mock-sentinel" "$SA_MOCK" "$sa_deployed"
fi

echo "SENTINEL_URL=${SENTINEL_URL}"
echo "Note: /health cannot be curled from a laptop because ingress is internal — this is expected and correct."
trace_line "PASS" "ingress-note" "internal; laptop curl of /health not expected"

ok "Mock deploy complete (request API and workers are NOT deployed — Sprint 3 / 5)."
