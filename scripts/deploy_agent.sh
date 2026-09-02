#!/usr/bin/env bash

# Idempotent deploy of the LiSN collector diagnostic agent (Cloud Run SERVICE).
#
# Unlike collector-api (min-instances=1, always warm for LiSN), this is
# request-driven ops tooling — min-instances=0, scale to zero is fine.
#
# Identity is collector-agent@… — a NEW read-only SA. Do NOT reuse
# collector-api (cloudsql.client only by design) or collector-sentinel
# (holds storage + BQ write). Widening those would increase blast radius.

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
: "${CONN:?CONN required in .env}"

AGENT_SERVICE="collector-agent"
SA_AGENT_EMAIL="${SA_AGENT:-collector-agent@${PROJECT}.iam.gserviceaccount.com}"
IMG_AGENT_DEFAULT="${REGION}-docker.pkg.dev/${PROJECT}/lisn/collector-agent:v1"
IMG_AGENT="${IMG_AGENT:-$IMG_AGENT_DEFAULT}"

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
# STEP 0 — Preconditions: SA + secrets exist
# ---------------------------------------------------------------------------
ok "STEP 0 — Preconditions"

if ! gcloud iam service-accounts describe "$SA_AGENT_EMAIL" \
  --project="$PROJECT" >/dev/null 2>&1; then
  fail "Missing SA ${SA_AGENT_EMAIL}. Create it and grant the read-only roles first
  (see agent/backend/README.md — Service account)."
fi

for secret_id in collector-dsn-readonly sentinel-mock-dsn-readonly agent-dsn; do
  if ! gcloud secrets describe "$secret_id" --project="$PROJECT" >/dev/null 2>&1; then
    fail "Missing secret ${secret_id}. Create socket-form DSNs for lisn_agent_ro /
  lisn_agent_session (see agent/backend/README.md — Database role)."
  fi
done

upsert_env "SA_AGENT" "$SA_AGENT_EMAIL"
upsert_env "IMG_AGENT" "$IMG_AGENT"
reload_env

# ---------------------------------------------------------------------------
# STEP 1 — Build and push agent image (separate from the collector image)
# ---------------------------------------------------------------------------
ok "STEP 1 — Build and push ${IMG_AGENT}"
# Context is agent/backend — its own Dockerfile / requirements, not the collector.
gcloud builds submit agent/backend --tag "$IMG_AGENT" --project="$PROJECT"

DIGEST="$(
  gcloud artifacts docker images describe "$IMG_AGENT" \
    --project="$PROJECT" \
    --format='value(image_summary.digest)' 2>/dev/null || true
)"
ok "Pushed ${IMG_AGENT}"
echo "digest=${DIGEST:-<unknown>}"

# ---------------------------------------------------------------------------
# STEP 2 — Deploy Cloud Run service
# ---------------------------------------------------------------------------
ok "STEP 2 — Deploy ${AGENT_SERVICE}"

# Auth required: do NOT pass --allow-unauthenticated. Never allUsers.
# Secrets via --set-secrets only — never DSN / API key env literals.
#
# MODEL_PROVIDER=vertex is the safer default (prompts stay in clariversev1).
# That is UNRESOLVED pending customer data-governance confirmation — see README.

ENV_VARS="GCP_PROJECT=${PROJECT}"
ENV_VARS+=",GOOGLE_CLOUD_PROJECT=${PROJECT}"
ENV_VARS+=",GCP_REGION=${REGION}"
ENV_VARS+=",BQ_RAW_DATASET=sentinel_raw"
ENV_VARS+=",BQ_CORE_DATASET=sentinel_core"
ENV_VARS+=",BQ_LANDING_TABLE=incidents_v2"
ENV_VARS+=",BQ_MAX_BYTES_BILLED=1000000000"
ENV_VARS+=",MODEL_PROVIDER=${MODEL_PROVIDER:-vertex}"
ENV_VARS+=",VERTEX_MODEL=${VERTEX_MODEL:-gemini-2.5-flash}"
ENV_VARS+=",VERTEX_LOCATION=${VERTEX_LOCATION:-us-central1}"
ENV_VARS+=",AGENT_PORT=8090"
# Optional SigNoz product URL (not a secret). Key stays in Secret Manager when set.
if [[ -n "${SIGNOZ_BASE_URL:-}" ]]; then
  ENV_VARS+=",SIGNOZ_BASE_URL=${SIGNOZ_BASE_URL}"
fi

SECRETS="COLLECTOR_DSN_READONLY=collector-dsn-readonly:latest"
SECRETS+=",SENTINEL_MOCK_DSN_READONLY=sentinel-mock-dsn-readonly:latest"
SECRETS+=",AGENT_DSN=agent-dsn:latest"
if gcloud secrets describe "signoz-api-key" --project="$PROJECT" >/dev/null 2>&1; then
  SECRETS+=",SIGNOZ_API_KEY=signoz-api-key:latest"
  ok "mounting optional secret signoz-api-key"
else
  warn "signoz-api-key secret absent — SigNoz source will report unavailable"
fi

gcloud run deploy "$AGENT_SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMG_AGENT" \
  --service-account="$SA_AGENT_EMAIL" \
  --add-cloudsql-instances="$CONN" \
  --ingress=all \
  --min-instances=0 \
  --max-instances=3 \
  --cpu=1 \
  --memory=1Gi \
  --timeout=300 \
  --port=8090 \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="$SECRETS" \
  --quiet

for public_member in "allUsers" "allAuthenticatedUsers"; do
  gcloud run services remove-iam-policy-binding "$AGENT_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="${public_member}" --role="roles/run.invoker" \
    --quiet >/dev/null 2>&1 || true
done

# Grant the deploying operator invoker so VERIFY can call with an identity token.
OPERATOR="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -n "$OPERATOR" ]]; then
  gcloud run services add-iam-policy-binding "$AGENT_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --member="user:${OPERATOR}" \
    --role="roles/run.invoker" \
    --quiet >/dev/null
  ok "run.invoker → user:${OPERATOR}"
fi

AGENT_URL="$(service_url "$AGENT_SERVICE")"
upsert_env "COLLECTOR_AGENT_URL" "$AGENT_URL"
reload_env
ok "COLLECTOR_AGENT_URL=${COLLECTOR_AGENT_URL}"

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
ok "VERIFY — Ready + SA + no allUsers + health + diagnose + chat"

READY="$(
  gcloud run services describe "$AGENT_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --format='value(status.conditions[0].status)'
)"
[[ "$READY" == "True" ]] || fail "${AGENT_SERVICE} Ready=${READY:-<empty>}"
ok "${AGENT_SERVICE} Ready=True"

ACTUAL_SA="$(
  gcloud run services describe "$AGENT_SERVICE" \
    --project="$PROJECT" --region="$REGION" \
    --format='value(spec.template.spec.serviceAccountName)'
)"
if [[ "$ACTUAL_SA" != "$SA_AGENT_EMAIL" && "$ACTUAL_SA" != *collector-agent* ]]; then
  fail "SA expected ${SA_AGENT_EMAIL}, got ${ACTUAL_SA}"
fi
ok "SA=${ACTUAL_SA}"

POLICY="$(
  gcloud run services get-iam-policy "$AGENT_SERVICE" \
    --project="$PROJECT" --region="$REGION"
)"
if printf '%s' "$POLICY" | grep -q "allUsers"; then
  fail "${AGENT_SERVICE} IAM includes allUsers — must NOT be publicly invokable"
fi
ok "no allUsers on ${AGENT_SERVICE}"

UNAUTH_CODE="$(
  curl -s -o /dev/null -w "%{http_code}" "${COLLECTOR_AGENT_URL}/health" || true
)"
echo "unauthenticated GET /health → HTTP ${UNAUTH_CODE}"
if [[ "$UNAUTH_CODE" != "401" && "$UNAUTH_CODE" != "403" ]]; then
  fail "expected 401/403 without token, got ${UNAUTH_CODE}"
fi
ok "rejects unauthenticated callers (${UNAUTH_CODE})"

TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
[[ -n "$TOKEN" ]] || fail "gcloud auth print-identity-token failed — re-run gcloud auth login"

ok "GET /health/sources"
SOURCES_JSON="$(mktemp)"
SOURCES_RESP="$(
  curl -sS -H "Authorization: Bearer ${TOKEN}" \
    "${COLLECTOR_AGENT_URL}/health/sources"
)"
echo "${SOURCES_RESP}"
printf '%s' "$SOURCES_RESP" >"$SOURCES_JSON"
SOURCES_JSON="$SOURCES_JSON" "$PY" - <<'PY' || fail "/health/sources missing a required source"
import json, os, sys
body = json.load(open(os.environ["SOURCES_JSON"], encoding="utf-8"))
rows = body.get("sources") or []
names = {str(r.get("name", "")).lower() for r in rows}
aliases = {
    "sql": {"sql", "cloud_sql", "collector_sql", "postgres"},
    "bq": {"bq", "bigquery"},
    "signoz": {"signoz"},
    "gcp": {"gcp", "cloud_run", "cloudrun", "gcp_run"},
}
missing = []
for logical, opts in aliases.items():
    if not any(n in opts or n.startswith(logical) for n in names):
        missing.append(logical)
if missing:
    print("missing sources:", missing, "got", sorted(names))
    sys.exit(1)
print("sources reporting:", sorted(names))
for r in rows:
    print(f"  - {r.get('name')}: {r.get('status')} ({r.get('message')})")
PY
rm -f "$SOURCES_JSON"

ok "GET /v1/diagnose/incident/IN270827PRECISION01"
DIAG_JSON="$(mktemp)"
DIAG_RESP="$(
  curl -sS -H "Authorization: Bearer ${TOKEN}" \
    "${COLLECTOR_AGENT_URL}/v1/diagnose/incident/IN270827PRECISION01"
)"
echo "${DIAG_RESP}" | head -c 800
echo
printf '%s' "$DIAG_RESP" >"$DIAG_JSON"
DIAG_JSON="$DIAG_JSON" "$PY" - <<'PY' || fail "diagnose response had no recognisable verdict"
import json, os, sys
body = json.load(open(os.environ["DIAG_JSON"], encoding="utf-8"))
verdict = str(body.get("verdict") or body.get("status") or "")
if not verdict:
    print("no verdict field:", list(body.keys())[:20])
    sys.exit(1)
print("verdict=", verdict)
PY
rm -f "$DIAG_JSON"
ok "diagnose returned a verdict"

ok "POST /v1/chat — was incident fetched?"
CHAT_JSON="$(mktemp)"
CHAT_SESSION="deploy-verify-$(date +%s)"
CHAT_RESP="$(
  curl -sS -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "content-type: application/json" \
    "${COLLECTOR_AGENT_URL}/v1/chat" \
    -d "{\"session_id\":\"${CHAT_SESSION}\",\"message\":\"Was incident IN270827PRECISION01 fetched?\"}"
)"
echo "${CHAT_RESP}" | head -c 1200
echo
printf '%s' "$CHAT_RESP" >"$CHAT_JSON"
CHAT_JSON="$CHAT_JSON" "$PY" - <<'PY' || fail "chat response missing required tool_calls"
import json, os, sys
body = json.load(open(os.environ["CHAT_JSON"], encoding="utf-8"))
calls = body.get("tool_calls") or []
names = [c.get("name") for c in calls]
print("tool_calls=", names)
print("reply_prefix=", (body.get("reply") or "")[:240])
if not calls:
    sys.exit(1)
if not any(n in {"diagnose_incident", "check_incident_collected"} for n in names):
    print("expected diagnose_incident or check_incident_collected")
    sys.exit(1)
PY
rm -f "$CHAT_JSON"
ok "chat answered with tool_calls listed"

ok "Service account role list (must contain no write roles)"
echo "----- project-level roles for ${SA_AGENT_EMAIL} -----"
gcloud projects get-iam-policy "$PROJECT" \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${SA_AGENT_EMAIL}" \
  --format="table(bindings.role)"
echo "----- dataset ACL (sentinel_raw / sentinel_core) -----"
"$PY" - <<'PY'
from google.cloud import bigquery
client = bigquery.Client(project="clariversev1")
sa = "collector-agent@clariversev1.iam.gserviceaccount.com"
for ds_id in ("sentinel_raw", "sentinel_core"):
    ds = client.get_dataset(ds_id)
    hits = [
        f"role={e.role} type={e.entity_type} id={e.entity_id}"
        for e in ds.access_entries
        if sa in str(getattr(e, "entity_id", ""))
    ]
    print(f"{ds_id}:")
    for h in hits or ["(no entry — unexpected)"]:
        print(" ", h)
PY
echo "----- forbidden roles check -----"
FORBIDDEN="roles/storage.objectAdmin roles/bigquery.dataEditor roles/run.admin roles/secretmanager.admin"
FOUND_BAD=""
ACTUAL="$(
  gcloud projects get-iam-policy "$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${SA_AGENT_EMAIL}" \
    --format="value(bindings.role)"
)"
for bad in $FORBIDDEN; do
  if printf '%s\n' "$ACTUAL" | grep -qx "$bad"; then
    FOUND_BAD="${FOUND_BAD} ${bad}"
  fi
done
if [[ -n "$FOUND_BAD" ]]; then
  fail "SA holds forbidden write/admin roles:${FOUND_BAD}"
fi
ok "no forbidden write/admin roles on ${SA_AGENT_EMAIL}"

ok "deploy-agent complete (digest=${DIGEST:-<unknown>})"
echo "COLLECTOR_AGENT_URL=${COLLECTOR_AGENT_URL}"
