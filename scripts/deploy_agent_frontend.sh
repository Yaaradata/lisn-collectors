#!/usr/bin/env bash

# Idempotent deploy of the LiSN collector ops console (Cloud Run SERVICE).
#
# AUTH: The agent backend (collector-agent) requires Cloud Run authentication.
# Browsers cannot mint identity tokens, so the UI proxies API calls through
# Next.js route handlers (/api/agent/*) using the UI service account.
#
# BUILD: NEXT_PUBLIC_AGENT_API_URL is baked at BUILD time — not runtime.
# The Cloud Build step MUST pass NEXT_PUBLIC_AGENT_API_URL=/api/agent.
# Building without it (or with localhost) produces a UI that silently calls
# the wrong host in production.

source scripts/_common.sh

need gcloud
need curl

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/Scripts/python ]]; then
  PY=".venv/Scripts/python"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

: "${PROJECT:?PROJECT required in .env}"
: "${REGION:?REGION required in .env}"

UI_SERVICE="collector-agent-ui"
AGENT_SERVICE="collector-agent"
SA_UI_EMAIL="${SA_AGENT_UI:-collector-agent-ui@${PROJECT}.iam.gserviceaccount.com}"
IMG_UI_DEFAULT="${REGION}-docker.pkg.dev/${PROJECT}/lisn/collector-agent-ui:v1"
IMG_UI="${IMG_AGENT_UI:-$IMG_UI_DEFAULT}"

# Baked into the browser bundle at docker build — NOT overridden at Cloud Run deploy.
NEXT_PUBLIC_AGENT_API_URL="${NEXT_PUBLIC_AGENT_API_URL:-/api/agent}"

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

service_url() {
  gcloud run services describe "$1" \
    --project="$PROJECT" --region="$REGION" \
    --format='value(status.url)'
}

# ---------------------------------------------------------------------------
# STEP 0 — Preconditions
# ---------------------------------------------------------------------------
ok "STEP 0 — Preconditions"

if [[ "$NEXT_PUBLIC_AGENT_API_URL" == *localhost* ]] || [[ "$NEXT_PUBLIC_AGENT_API_URL" == *127.0.0.1* ]]; then
  fail "NEXT_PUBLIC_AGENT_API_URL must not be localhost (got ${NEXT_PUBLIC_AGENT_API_URL}). It is baked at BUILD time."
fi

if ! gcloud run services describe "$AGENT_SERVICE" \
  --project="$PROJECT" --region="$REGION" >/dev/null 2>&1; then
  fail "Backend ${AGENT_SERVICE} not deployed. Run scripts/deploy_agent.sh first."
fi

COLLECTOR_AGENT_URL="${COLLECTOR_AGENT_URL:-$(service_url "$AGENT_SERVICE")}"
[[ -n "$COLLECTOR_AGENT_URL" ]] || fail "Could not resolve ${AGENT_SERVICE} URL"
upsert_env "COLLECTOR_AGENT_URL" "$COLLECTOR_AGENT_URL"
reload_env
ok "COLLECTOR_AGENT_URL=${COLLECTOR_AGENT_URL}"

if ! gcloud iam service-accounts describe "$SA_UI_EMAIL" \
  --project="$PROJECT" >/dev/null 2>&1; then
  ok "Creating SA ${SA_UI_EMAIL}"
  gcloud iam service-accounts create collector-agent-ui \
    --project="$PROJECT" \
    --display-name="Collector agent ops console (UI proxy only)"
fi

gcloud run services add-iam-policy-binding "$AGENT_SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --member="serviceAccount:${SA_UI_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null
ok "roles/run.invoker on ${AGENT_SERVICE} → ${SA_UI_EMAIL}"

upsert_env "SA_AGENT_UI" "$SA_UI_EMAIL"
upsert_env "IMG_AGENT_UI" "$IMG_UI"
reload_env

# ---------------------------------------------------------------------------
# STEP 1 — Build and push UI image
# ---------------------------------------------------------------------------
ok "STEP 1 — Build and push ${IMG_UI}"
warn "NEXT_PUBLIC_AGENT_API_URL=${NEXT_PUBLIC_AGENT_API_URL} is baked into the image at BUILD time"
if [[ "$NEXT_PUBLIC_AGENT_API_URL" != "/api/agent" ]]; then
  warn "Expected /api/agent for production — browser calls must hit the server proxy"
fi

gcloud builds submit agent/frontend \
  --project="$PROJECT" \
  --config=agent/frontend/cloudbuild.yaml \
  --substitutions="_IMAGE=${IMG_UI},_NEXT_PUBLIC_AGENT_API_URL=${NEXT_PUBLIC_AGENT_API_URL}"

DIGEST="$(
  gcloud artifacts docker images describe "$IMG_UI" \
    --project="$PROJECT" \
    --format='value(image_summary.digest)' 2>/dev/null || true
)"
ok "Pushed ${IMG_UI}"
echo "digest=${DIGEST:-<unknown>}"

# ---------------------------------------------------------------------------
# STEP 2 — Deploy Cloud Run UI service
# ---------------------------------------------------------------------------
ok "STEP 2 — Deploy ${UI_SERVICE}"

# Auth required on the UI — never --allow-unauthenticated / allUsers.
# AGENT_BACKEND_URL is RUNTIME (server proxy only) — not NEXT_PUBLIC_*.
ENV_VARS="AGENT_BACKEND_URL=${COLLECTOR_AGENT_URL}"
ENV_VARS+=",NODE_ENV=production"

gcloud run deploy "$UI_SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMG_UI" \
  --service-account="$SA_UI_EMAIL" \
  --ingress=all \
  --min-instances=0 \
  --max-instances=3 \
  --cpu=1 \
  --memory=512Mi \
  --timeout=300 \
  --port=3000 \
  --set-env-vars="$ENV_VARS" \
  --quiet

for public_member in "allUsers" "allAuthenticatedUsers"; do
  gcloud run services remove-iam-policy-binding "$UI_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="${public_member}" --role="roles/run.invoker" \
    --quiet >/dev/null 2>&1 || true
done

OPERATOR="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -n "$OPERATOR" ]]; then
  gcloud run services add-iam-policy-binding "$UI_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="user:${OPERATOR}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null
  ok "run.invoker on ${UI_SERVICE} → user:${OPERATOR}"
fi

UI_URL="$(service_url "$UI_SERVICE")"
upsert_env "COLLECTOR_AGENT_UI_URL" "$UI_URL"
reload_env
ok "COLLECTOR_AGENT_UI_URL=${COLLECTOR_AGENT_UI_URL}"

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
ok "VERIFY — UI proxy, sources, diagnose, chat; backend still private"

TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
[[ -n "$TOKEN" ]] || fail "gcloud auth print-identity-token failed — re-run gcloud auth login"

READY="$(
  gcloud run services describe "$UI_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --format='value(status.conditions[0].status)'
)"
[[ "$READY" == "True" ]] || fail "${UI_SERVICE} Ready=${READY:-<empty>}"
ok "${UI_SERVICE} Ready=True"

ACTUAL_SA="$(
  gcloud run services describe "$UI_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --format='value(spec.template.spec.serviceAccountName)'
)"
[[ "$ACTUAL_SA" == "$SA_UI_EMAIL" ]] || fail "UI SA expected ${SA_UI_EMAIL}, got ${ACTUAL_SA}"
ok "UI SA=${ACTUAL_SA}"

UI_POLICY="$(
  gcloud run services get-iam-policy "$UI_SERVICE" \
    --project="$PROJECT" --region="$REGION"
)"
if printf '%s' "$UI_POLICY" | grep -q "allUsers"; then
  fail "${UI_SERVICE} IAM includes allUsers"
fi
ok "no allUsers on ${UI_SERVICE}"

AGENT_POLICY="$(
  gcloud run services get-iam-policy "$AGENT_SERVICE" \
    --project="$PROJECT" --region="$REGION"
)"
echo "----- ${AGENT_SERVICE} IAM (must NOT include allUsers) -----"
echo "$AGENT_POLICY"
if printf '%s' "$AGENT_POLICY" | grep -q "allUsers"; then
  fail "${AGENT_SERVICE} IAM includes allUsers — backend must stay private"
fi
ok "no allUsers on ${AGENT_SERVICE}"

UNAUTH_AGENT="$(
  curl -s -o /dev/null -w "%{http_code}" "${COLLECTOR_AGENT_URL}/health" || true
)"
echo "unauthenticated GET backend /health → HTTP ${UNAUTH_AGENT}"
[[ "$UNAUTH_AGENT" == "401" || "$UNAUTH_AGENT" == "403" ]] \
  || fail "backend must reject unauthenticated callers, got ${UNAUTH_AGENT}"
ok "backend rejects unauthenticated callers (${UNAUTH_AGENT})"

ok "GET ${UI_URL}/api/agent/health/sources (via UI proxy)"
SOURCES_JSON="$(mktemp)"
SOURCES_RESP="$(
  curl -sS -H "Authorization: Bearer ${TOKEN}" \
    "${COLLECTOR_AGENT_UI_URL}/api/agent/health/sources"
)"
echo "${SOURCES_RESP}" | head -c 1200
echo
printf '%s' "$SOURCES_RESP" >"$SOURCES_JSON"
SOURCES_JSON="$SOURCES_JSON" "$PY" - <<'PY' || fail "/health/sources missing a required source"
import json, os, sys
body = json.load(open(os.environ["SOURCES_JSON"], encoding="utf-8"))
rows = body.get("sources") or []
names = {str(r.get("name", "")).lower() for r in rows}
required = {"sql", "bigquery", "signoz", "gcp"}
missing = [s for s in required if s not in names]
if missing:
    print("missing:", missing, "got", sorted(names))
    sys.exit(1)
for r in rows:
    if r.get("name") in required:
        print(f"  {r['name']}: {r.get('status')}")
print("all four status-bar sources present")
PY
rm -f "$SOURCES_JSON"

ok "GET /api/agent/v1/diagnose/incident/IN270827PRECISION01"
DIAG_JSON="$(mktemp)"
DIAG_RESP="$(
  curl -sS -H "Authorization: Bearer ${TOKEN}" \
    "${COLLECTOR_AGENT_UI_URL}/api/agent/v1/diagnose/incident/IN270827PRECISION01"
)"
echo "${DIAG_RESP}" | head -c 600
echo
printf '%s' "$DIAG_RESP" >"$DIAG_JSON"
DIAG_JSON="$DIAG_JSON" "$PY" - <<'PY' || fail "diagnose returned no verdict"
import json, os, sys
body = json.load(open(os.environ["DIAG_JSON"], encoding="utf-8"))
verdict = body.get("verdict")
if not verdict:
    sys.exit(1)
print("verdict=", verdict)
PY
rm -f "$DIAG_JSON"

ok "POST /api/agent/v1/chat"
CHAT_JSON="$(mktemp)"
CHAT_SESSION="ui-deploy-verify-$(date +%s)"
CHAT_RESP="$(
  curl -sS -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "content-type: application/json" \
    "${COLLECTOR_AGENT_UI_URL}/api/agent/v1/chat" \
    -d "{\"session_id\":\"${CHAT_SESSION}\",\"message\":\"Was incident IN270827PRECISION01 collected?\"}"
)"
echo "${CHAT_RESP}" | head -c 1200
echo
printf '%s' "$CHAT_RESP" >"$CHAT_JSON"
CHAT_JSON="$CHAT_JSON" "$PY" - <<'PY' || fail "chat missing tool_calls"
import json, os, sys
body = json.load(open(os.environ["CHAT_JSON"], encoding="utf-8"))
calls = body.get("tool_calls") or []
names = [c.get("name") for c in calls]
print("tool_calls=", names)
print("reply_prefix=", (body.get("reply") or "")[:200])
if not calls:
    sys.exit(1)
PY
rm -f "$CHAT_JSON"

UI_ROLES="$(
  gcloud projects get-iam-policy "$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${SA_UI_EMAIL}" \
    --format="value(bindings.role)"
)"
echo "----- project roles for ${SA_UI_EMAIL} -----"
printf '%s\n' "$UI_ROLES"
if printf '%s\n' "$UI_ROLES" | grep -qvE '^roles/run\.invoker$' \
  && [[ -n "$(printf '%s\n' "$UI_ROLES" | grep -v '^roles/run\.invoker$' | grep -v '^$' || true)" ]]; then
  warn "UI SA has project-level roles beyond run.invoker — review if unexpected"
fi

ok "deploy-agent-frontend complete"
echo "COLLECTOR_AGENT_UI_URL=${COLLECTOR_AGENT_UI_URL}"
echo "COLLECTOR_AGENT_URL=${COLLECTOR_AGENT_URL} (private backend)"
