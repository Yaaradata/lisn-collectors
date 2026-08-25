#!/usr/bin/env bash

# Idempotent deploy of the shared collector image as mock-sentinel + collector-api.
# Workers are a later step; this script only ships the two Cloud Run SERVICES.

source scripts/_common.sh

need gcloud
need curl

: "${PROJECT:?PROJECT required in .env}"
: "${REGION:?REGION required in .env}"
: "${IMG:?IMG required in .env}"
: "${SA_MOCK:?SA_MOCK required in .env}"
: "${SA_API:?SA_API required in .env}"
: "${CONN:?CONN required in .env}"
: "${BUCKET:?BUCKET required in .env}"

MOCK_SERVICE="mock-sentinel"
API_SERVICE="collector-api"

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

# Overwrite SENTINEL_URL with the deployed mock URL, but keep any previous
# localhost value as a commented line so laptop runs can restore it.
upsert_sentinel_url() {
  local new_url="$1"
  local old_url="${SENTINEL_URL:-}"
  local tmp
  tmp="$(mktemp)"

  if [[ -n "$old_url" && "$old_url" != "$new_url" ]]; then
    if [[ "$old_url" == *"127.0.0.1"* || "$old_url" == *"localhost"* ]]; then
      # Drop any prior restore comment for SENTINEL_URL, then rewrite .env.
      awk -v old="$old_url" -v neu="$new_url" '
        BEGIN { commented=0; done=0 }
        /^# SENTINEL_URL=/ { next }
        /^SENTINEL_URL=/ {
          if (!commented) {
            print "# SENTINEL_URL=" old "  # local — restore for laptop runs"
            commented=1
          }
          print "SENTINEL_URL=" neu
          done=1
          next
        }
        { print }
        END {
          if (!done) {
            if (!commented) {
              print "# SENTINEL_URL=" old "  # local — restore for laptop runs"
            }
            print "SENTINEL_URL=" neu
          }
        }
      ' .env >"$tmp" && mv "$tmp" .env
      return
    fi
  fi

  # Ensure a local restore hint exists even when overwriting a Cloud Run URL.
  if ! grep -q '^# SENTINEL_URL=.*127\.0\.0\.1' .env 2>/dev/null; then
    printf '%s\n' \
      "# SENTINEL_URL=http://127.0.0.1:8081  # local — restore for laptop runs" >>.env
  fi
  upsert_env "SENTINEL_URL" "$new_url"
}

reload_env() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}

service_ready() {
  local name="$1"
  gcloud run services describe "$name" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(status.conditions[0].status)'
}

service_sa() {
  local name="$1"
  gcloud run services describe "$name" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(spec.template.spec.serviceAccountName)'
}

service_url() {
  local name="$1"
  gcloud run services describe "$name" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(status.url)'
}

service_ingress() {
  local name="$1"
  local ingress
  ingress="$(
    gcloud run services describe "$name" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format='value(metadata.annotations[run.googleapis.com/ingress])' 2>/dev/null || true
  )"
  if [[ -z "$ingress" ]]; then
    ingress="$(
      gcloud run services describe "$name" \
        --project="$PROJECT" \
        --region="$REGION" \
        --format=yaml 2>/dev/null | awk '/^  ingress:/{print $2; exit}'
    )"
  fi
  printf '%s' "$ingress"
}

# ---------------------------------------------------------------------------
# STEP 1 — Build and push
# ---------------------------------------------------------------------------
ok "STEP 1 — Build and push ${IMG}"
# One image serves every process. It has no default CMD; the command is
# supplied per deployment. The mock, the API and the workers all run this image.
gcloud builds submit --tag "$IMG" --project="$PROJECT"

DIGEST="$(
  gcloud artifacts docker images describe "$IMG" \
    --project="$PROJECT" \
    --format='value(image_summary.digest)' 2>/dev/null || true
)"
if [[ -z "$DIGEST" ]]; then
  DIGEST="$(
    gcloud builds list --project="$PROJECT" --limit=1 \
      --format='value(results.images[0].digest)' 2>/dev/null || true
  )"
fi
ok "Pushed ${IMG}"
echo "digest=${DIGEST:-<unknown>}"

# ---------------------------------------------------------------------------
# STEP 2 — Deploy mock-sentinel (Cloud Run SERVICE)
# ---------------------------------------------------------------------------
ok "STEP 2 — Deploy ${MOCK_SERVICE}"
# Auth required: do NOT pass --allow-unauthenticated. Callers need run.invoker
# plus an ID token (see scripts/24_grant_invoker.sh for the worker SA).
gcloud run deploy "$MOCK_SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMG" \
  --service-account="$SA_MOCK" \
  --add-cloudsql-instances="$CONN" \
  --ingress=all \
  --min-instances=1 \
  --port=8080 \
  --set-secrets="SENTINEL_MOCK_DSN=sentinel-mock-dsn:latest" \
  --command=uvicorn \
  --args="mock.sentinel_api:app,--host,0.0.0.0,--port,8080" \
  --quiet

# Strip public invokers if any (some gcloud builds lack --no-allow-unauthenticated).
for public_member in "allUsers" "allAuthenticatedUsers"; do
  gcloud run services remove-iam-policy-binding "$MOCK_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="${public_member}" --role="roles/run.invoker" \
    --quiet >/dev/null 2>&1 || true
done

SENTINEL_URL_VALUE="$(service_url "$MOCK_SERVICE")"
upsert_sentinel_url "$SENTINEL_URL_VALUE"
reload_env
ok "SENTINEL_URL=${SENTINEL_URL}"

# ---------------------------------------------------------------------------
# STEP 3 — Deploy collector-api (Cloud Run SERVICE)
# ---------------------------------------------------------------------------
ok "STEP 3 — Deploy ${API_SERVICE}"
# SA_API holds only cloudsql.client. The API writes rows and defers jobs; it
# never touches GCS or BigQuery. Do not widen it.
#
# Auth required. Demo:
#   curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" ...
# In production LiSN would use its own service account with run.invoker rather
# than a human identity.
gcloud run deploy "$API_SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMG" \
  --service-account="$SA_API" \
  --add-cloudsql-instances="$CONN" \
  --min-instances=1 \
  --port=8080 \
  --set-env-vars="SENTINEL_URL=${SENTINEL_URL},RAW_BUCKET=${BUCKET},PROJECT=${PROJECT},GOOGLE_CLOUD_PROJECT=${PROJECT},REGION=${REGION},ALLOW_ADMIN_RESET=${ALLOW_ADMIN_RESET:-1},USE_ID_TOKEN=1" \
  --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
  --command=uvicorn \
  --args="collector.api:api,--host,0.0.0.0,--port,8080" \
  --quiet

for public_member in "allUsers" "allAuthenticatedUsers"; do
  gcloud run services remove-iam-policy-binding "$API_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="${public_member}" --role="roles/run.invoker" \
    --quiet >/dev/null 2>&1 || true
done

COLLECTOR_API_URL_VALUE="$(service_url "$API_SERVICE")"
upsert_env "COLLECTOR_API_URL" "$COLLECTOR_API_URL_VALUE"
reload_env
ok "COLLECTOR_API_URL=${COLLECTOR_API_URL}"

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
ok "VERIFY — Ready + service accounts + ingress + /health"

MOCK_READY="$(service_ready "$MOCK_SERVICE")"
API_READY="$(service_ready "$API_SERVICE")"
[[ "$MOCK_READY" == "True" ]] || fail "${MOCK_SERVICE} Ready=${MOCK_READY:-<empty>}"
[[ "$API_READY" == "True" ]] || fail "${API_SERVICE} Ready=${API_READY:-<empty>}"
ok "${MOCK_SERVICE} Ready=True"
ok "${API_SERVICE} Ready=True"

MOCK_SA="$(service_sa "$MOCK_SERVICE")"
API_SA="$(service_sa "$API_SERVICE")"
if [[ "$MOCK_SA" != "$SA_MOCK" && "$MOCK_SA" != *mock-sentinel* ]]; then
  fail "${MOCK_SERVICE} SA expected ${SA_MOCK}, got ${MOCK_SA}"
fi
if [[ "$API_SA" != "$SA_API" && "$API_SA" != *collector-api* ]]; then
  fail "${API_SERVICE} SA expected ${SA_API}, got ${API_SA}"
fi
ok "${MOCK_SERVICE} SA=${MOCK_SA}"
ok "${API_SERVICE} SA=${API_SA}"

INGRESS="$(service_ingress "$MOCK_SERVICE")"
echo "mock-sentinel ingress=${INGRESS}"
[[ "$INGRESS" == "all" ]] || fail "expected mock-sentinel ingress=all, got '${INGRESS:-<empty>}'"

POLICY="$(
  gcloud run services get-iam-policy "$MOCK_SERVICE" \
    --project="$PROJECT" \
    --region="$REGION"
)"
if printf '%s' "$POLICY" | grep -q "allUsers"; then
  fail "${MOCK_SERVICE} IAM includes allUsers — must NOT be publicly invokable"
fi
ok "${MOCK_SERVICE} is NOT publicly invokable (no allUsers)"

UNAUTH_CODE="$(
  curl -s -o /dev/null -w "%{http_code}" "${SENTINEL_URL}/health" || true
)"
echo "unauthenticated GET ${SENTINEL_URL}/health → HTTP ${UNAUTH_CODE}"
# 403 (forbidden) or 401 (unauthorized) both mean auth is required.
if [[ "$UNAUTH_CODE" != "401" && "$UNAUTH_CODE" != "403" ]]; then
  fail "expected 401/403 without token for mock /health, got ${UNAUTH_CODE}"
fi
ok "mock /health rejects unauthenticated callers (${UNAUTH_CODE})"

echo ""
echo "SENTINEL_URL=${SENTINEL_URL}"
echo "COLLECTOR_API_URL=${COLLECTOR_API_URL}"
echo ""

ok "GET collector-api /health with identity token"
TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  fail "gcloud auth print-identity-token failed — re-run gcloud auth login"
fi
HEALTH_RESP="$(
  curl -sS -H "Authorization: Bearer ${TOKEN}" \
    "${COLLECTOR_API_URL}/health"
)"
echo "collector-api /health → ${HEALTH_RESP}"
[[ -n "$HEALTH_RESP" ]] || fail "empty /health response from collector-api"

ok "deploy-services complete (digest=${DIGEST:-<unknown>})"
