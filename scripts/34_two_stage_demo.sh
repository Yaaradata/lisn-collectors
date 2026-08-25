#!/usr/bin/env bash

# Two-stage collection demo: discovery → bridge SQL → enrichment → proof.
#
# Note: scripts/33_reset_collector.sh already owns the "33" slot; this is 34.
#
# Usage:
#   ./scripts/34_two_stage_demo.sh           # paced (Enter between stages)
#   ./scripts/34_two_stage_demo.sh --reset   # wipe collector output first
#   ./scripts/34_two_stage_demo.sh --auto    # no Enter prompts (verify / CI)
#
# Preconditions: deployed mock + API + workers (incl. col-sentinel-discovery),
# BigQuery discovery table/view, Cloud SQL proxy for local psql/bq bridge.

source scripts/_common.sh

need curl
need python3
need psql
need gcloud
need bq

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${SENTINEL_MOCK_DSN:?SENTINEL_MOCK_DSN required}"
: "${PROJECT:?PROJECT required}"
: "${REGION:?REGION required}"
: "${CONN:?CONN required}"
: "${BUCKET:?BUCKET required}"
: "${RAW_BUCKET:=$BUCKET}"
: "${COLLECTOR_API_URL:?COLLECTOR_API_URL required}"
: "${SENTINEL_URL:?SENTINEL_URL required}"
: "${DEPLOY_SURFACE:?DEPLOY_SURFACE required}"

if [[ "$COLLECTOR_API_URL" == *"127.0.0.1"* || "$COLLECTOR_API_URL" == *"localhost"* ]]; then
  fail "COLLECTOR_API_URL is local — two-stage demo targets the deployed stack"
fi

DO_RESET=0
AUTO=0
for arg in "$@"; do
  case "$arg" in
    --reset) DO_RESET=1 ;;
    --auto) AUTO=1 ;;
    -h|--help)
      echo "Usage: $0 [--reset] [--auto]"
      exit 0
      ;;
  esac
done

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
# Windows Cloud SDK often looks for python3.12 on PATH; pin to the venv.
export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-$PY}"

ROOT="$(pwd -W 2>/dev/null || true)"
if [[ -z "$ROOT" ]]; then
  ROOT="$(pwd)"
fi
# Normalize to forward slashes so both bash redirects and Windows Python agree.
ROOT="${ROOT//\\//}"
BRIDGE_TMP="${ROOT}/.tmp_bridge.sql"
BRIDGE_IDS_FILE="${ROOT}/.tmp_bridge_ids.txt"
ENRICH_JSON_FILE="${ROOT}/.tmp_enrich.json"

PROXY_PID=""
cleanup() {
  if [[ -n "${PROXY_PID}" ]] && kill -0 "${PROXY_PID}" 2>/dev/null; then
    kill "${PROXY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

banner() {
  printf '\n'
  printf '============================================================\n'
  printf '  STAGE %s — %s\n' "$1" "$2"
  printf '============================================================\n'
  printf '  %s\n' "$3"
  printf '\n'
}

narrate() { printf '  > %s\n' "$*"; }

wait_enter() {
  if (( AUTO )); then
    printf '\n'
    return 0
  fi
  printf '\n'
  read -r -p "  Press Enter for the next stage... " _
  printf '\n'
}

ensure_proxy() {
  if (echo >/dev/tcp/127.0.0.1/5432) >/dev/null 2>&1; then
    return 0
  fi
  PROXY_BIN="./cloud-sql-proxy.exe"
  [[ -f "$PROXY_BIN" ]] || PROXY_BIN="./cloud-sql-proxy"
  [[ -f "$PROXY_BIN" ]] || fail "cloud-sql-proxy missing"
  "$PROXY_BIN" "$CONN" --port 5432 >/tmp/two-stage-proxy.log 2>&1 &
  PROXY_PID=$!
  sleep 4
}

api_curl() {
  local method="$1"
  local path="$2"
  shift 2
  local args=(-sS -X "$method")
  if [[ -n "${COLLECTOR_API_TOKEN:-}" ]]; then
    args+=(-H "Authorization: Bearer ${COLLECTOR_API_TOKEN}")
  fi
  curl "${args[@]}" "$@" "${COLLECTOR_API_URL}${path}"
}

ensure_api_auth() {
  COLLECTOR_API_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
  export COLLECTOR_API_TOKEN
  if curl -sf "${COLLECTOR_API_URL}/health" >/dev/null 2>&1; then
    ok "API /health open (or token not required)"
    return 0
  fi
  if [[ -n "$COLLECTOR_API_TOKEN" ]] && api_curl GET /health | grep -q ok; then
    ok "API reachable with identity token"
    return 0
  fi
  warn "No identity token — granting temporary allUsers run.invoker on collector-api"
  gcloud run services add-iam-policy-binding collector-api \
    --region="$REGION" --project="$PROJECT" \
    --member="allUsers" --role="roles/run.invoker" --quiet >/dev/null
  REVOKE_ALLUSERS=1
  sleep 2
  api_curl GET /health | grep -q ok || fail "API /health still failing"
}

revoke_api_auth_if_needed() {
  if [[ "${REVOKE_ALLUSERS:-0}" == "1" ]]; then
    ok "Revoking temporary allUsers on collector-api"
    gcloud run services remove-iam-policy-binding collector-api \
      --region="$REGION" --project="$PROJECT" \
      --member="allUsers" --role="roles/run.invoker" --quiet >/dev/null || true
  fi
}

wait_request_done() {
  local req="$1"
  local label="$2"
  local deadline=$((SECONDS + 300))
  while (( SECONDS < deadline )); do
    local pending
    pending="$(
      psql "$COLLECTOR_DSN" -tAc \
        "SELECT count(*) FROM collector_job
         WHERE request_id='${req}'::uuid AND status NOT IN ('done','failed','dead')"
    )"
    local counts
    counts="$(api_curl GET "/v1/requests/${req}/counts" || true)"
    echo "  ${label}: pending=${pending} counts=${counts}"
    if [[ "$pending" == "0" ]]; then
      return 0
    fi
    sleep 5
  done
  fail "${label}: request ${req} did not finish in time"
}

ISSUE_NAMES='["Delay in Shipping","Delay in Delivery","Wishmaster refused doorstep delivery","FE/Delivery Boy/Person details required","Status check","Request for Reschedule Delivery"]'

# ---------------------------------------------------------------------------
ensure_proxy
ensure_api_auth

if (( DO_RESET )); then
  ok "RESET before two-stage demo"
  bash scripts/33_reset_collector.sh --restart
fi

ok "Ensuring workers (incl. discovery) are up"
bash scripts/28_workers_control.sh start || warn "workers start returned non-zero"

# Fresh seed so a rolling 24h updated window has matches. Stale seed (days old)
# makes discovery succeed with zero ids — looks like a collector bug, is not.
ok "Re-seeding sentinel_mock (updated_on relative to now)"
"$PY" -m mock.seed_sentinel

# ---------------------------------------------------------------------------
banner 1 "Discovery" \
  "POST /v1/collect source=sentinel_discovery — which incidents match a filter"

narrate "This is the entry point. LiSN cannot ask for ids it does not have,"
narrate "so something has to answer 'which incidents are open right now'."

FILTER_JSON="$("$PY" - <<'PY'
import json
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)
start = now - timedelta(hours=24)
print(json.dumps({
    "updated_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "updated_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "issue_names": [
        "Delay in Shipping",
        "Delay in Delivery",
        "Wishmaster refused doorstep delivery",
        "FE/Delivery Boy/Person details required",
        "Status check",
        "Request for Reschedule Delivery",
    ],
    "limit": 1000,
}))
PY
)"

echo "  filter_spec=${FILTER_JSON}"

DISC_RESP="$(
  api_curl POST /v1/collect \
    -H "Content-Type: application/json" \
    -d "{\"source\":\"sentinel_discovery\",\"query_spec\":${FILTER_JSON}}"
)"
echo "  collect response: ${DISC_RESP}"
DISC_REQ="$("$PY" -c "import json,sys; print(json.load(sys.stdin)['request_id'])" <<<"$DISC_RESP")"
DISC_PAGES="$("$PY" -c "import json,sys; print(json.load(sys.stdin)['total_pages'])" <<<"$DISC_RESP")"
[[ "$DISC_PAGES" == "1" ]] || warn "expected total_pages=1 for discovery, got ${DISC_PAGES}"

wait_request_done "$DISC_REQ" "discovery"

DISC_N="$(
  PROJECT="$PROJECT" REGION="$REGION" DISC_REQ="$DISC_REQ" "$PY" - <<'PY'
import os
from google.cloud import bigquery

project = os.environ["PROJECT"]
region = os.environ["REGION"]
rid = os.environ["DISC_REQ"]
client = bigquery.Client(project=project, location=region)
q = f"""
SELECT count(DISTINCT incident_id) AS n
FROM `{project}.sentinel_raw.discovered_ids`
WHERE _request_id = @rid
"""
job = client.query(
    q,
    job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", rid)]
    ),
)
print(list(job.result())[0].n)
PY
)"
echo "  discovered_ids for request: ${DISC_N}"
(( DISC_N > 0 )) || fail "discovery landed 0 ids — check mock window / seed / worker logs"

wait_enter

# ---------------------------------------------------------------------------
banner 2 "The bridge" \
  "sql/008_discovery_to_enrich.sql — discovered but not yet enriched"

narrate "This decides what to collect. It is a business rule, so it lives in SQL"
narrate "where LiSN owns it, not inside the collector."

BRIDGE_TMP="${ROOT}/.tmp_bridge.sql"
BRIDGE_IDS_FILE="${ROOT}/.tmp_bridge_ids.txt"
sed "s/__PROJECT__/${PROJECT}/g" sql/008_discovery_to_enrich.sql >"$BRIDGE_TMP"
ok "Running bridge query"
bq query \
  --project_id="$PROJECT" \
  --location="$REGION" \
  --use_legacy_sql=false \
  --nouse_cache \
  --format=pretty \
  <"$BRIDGE_TMP" | tee "${ROOT}/.tmp_bridge_stage2.txt"

bq query \
  --project_id="$PROJECT" \
  --location="$REGION" \
  --use_legacy_sql=false \
  --nouse_cache \
  --format=csv \
  --max_rows=1000000 \
  <"$BRIDGE_TMP" \
  | tail -n +2 | cut -d, -f1 | grep -v '^$' >"$BRIDGE_IDS_FILE" || true
BRIDGE_N="$(wc -l <"$BRIDGE_IDS_FILE" | tr -d ' ')"
echo "  bridge_id_count=${BRIDGE_N}"
(( BRIDGE_N > 0 )) || fail "bridge returned 0 ids after discovery"
rm -f "$BRIDGE_TMP"

wait_enter

# ---------------------------------------------------------------------------
banner 3 "Enrichment" \
  "POST /v1/collect source=sentinel with the bridged incident_ids"

narrate "Same enrichment path as before — pages, GCS, BigQuery incidents."

ENRICH_JSON_FILE="${ROOT}/.tmp_enrich.json"
"$PY" - <<PY
import json
ids = [line.strip() for line in open(r"${BRIDGE_IDS_FILE}", encoding="utf-8") if line.strip()]
ids = ids[:500]
print(f"using {len(ids)} ids for enrichment", flush=True)
open(r"${ENRICH_JSON_FILE}", "w", encoding="utf-8").write(
    json.dumps({"source": "sentinel", "query_spec": {"incident_ids": ids}})
)
PY

ENRICH_RESP="$(
  api_curl POST /v1/collect \
    -H "Content-Type: application/json" \
    -d @"${ENRICH_JSON_FILE}"
)"
echo "  collect response: ${ENRICH_RESP}"
ENRICH_REQ="$("$PY" -c "import json,sys; print(json.load(sys.stdin)['request_id'])" <<<"$ENRICH_RESP")"
ENRICH_PAGES="$("$PY" -c "import json,sys; print(json.load(sys.stdin)['total_pages'])" <<<"$ENRICH_RESP")"
echo "  enrichment total_pages=${ENRICH_PAGES}"
rm -f "$ENRICH_JSON_FILE"

wait_request_done "$ENRICH_REQ" "enrichment"

GCS_N="$("$PY" - <<PY
from google.cloud import storage
client = storage.Client(project="${PROJECT}")
bucket = client.bucket("${BUCKET}")
n = sum(1 for b in bucket.list_blobs(prefix="raw/") if "${ENRICH_REQ}" in b.name)
print(n)
PY
)"
echo "  GCS objects for enrichment request: ${GCS_N}"

BQ_N="$(
  PROJECT="$PROJECT" REGION="$REGION" ENRICH_REQ="$ENRICH_REQ" "$PY" - <<'PY'
import os
from google.cloud import bigquery

project = os.environ["PROJECT"]
region = os.environ["REGION"]
rid = os.environ["ENRICH_REQ"]
client = bigquery.Client(project=project, location=region)
q = f"SELECT count(*) AS n FROM `{project}.sentinel_raw.incidents` WHERE _request_id=@rid"
job = client.query(
    q,
    job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", rid)]
    ),
)
print(list(job.result())[0].n)
PY
)"
echo "  BigQuery incident rows for enrichment request: ${BQ_N}"
(( GCS_N > 0 )) || fail "no GCS objects for enrichment"
(( BQ_N > 0 )) || fail "no BigQuery rows for enrichment"

wait_enter

# ---------------------------------------------------------------------------
banner 4 "Proof" \
  "Bridge returns zero; /v1/reconcile is clean for what we enriched"

narrate "Everything discovered (that we fed to enrichment) should now be enriched."

# Re-run the same bridge SQL — after enrichment it must return zero rows.
BRIDGE_TMP="${ROOT}/.tmp_bridge.sql"
sed "s/__PROJECT__/${PROJECT}/g" sql/008_discovery_to_enrich.sql >"$BRIDGE_TMP"
ok "Re-running bridge query (expect empty)"
bq query \
  --project_id="$PROJECT" \
  --location="$REGION" \
  --use_legacy_sql=false \
  --nouse_cache \
  --format=pretty \
  <"$BRIDGE_TMP" | tee "${ROOT}/.tmp_bridge_stage4.txt"
PROOF_N="$(
  bq query \
    --project_id="$PROJECT" \
    --location="$REGION" \
    --use_legacy_sql=false \
    --nouse_cache \
    --format=csv \
    --max_rows=1000000 \
    <"$BRIDGE_TMP" \
    | tail -n +2 | grep -c . || true
)"
PROOF_N="${PROOF_N:-0}"
rm -f "$BRIDGE_TMP"
echo "  bridge_id_count_after_enrichment=${PROOF_N}"
[[ "$PROOF_N" == "0" ]] || fail "expected bridge to return 0 after enrichment, got ${PROOF_N}"

RECON="$(api_curl GET "/v1/reconcile?minutes=0")"
echo "  reconcile: ${RECON}"
"$PY" -c "
import json, sys
d = json.loads(sys.argv[1])
unloaded = int(d.get('unloaded') or 0)
print(f'unloaded={unloaded}')
raise SystemExit(0 if unloaded == 0 else 'reconcile unloaded != 0')
" "$RECON"

rm -f "$BRIDGE_IDS_FILE" "${ROOT}/.tmp_bridge_stage2.txt" "${ROOT}/.tmp_bridge_stage4.txt"
revoke_api_auth_if_needed

ok "TWO-STAGE DEMO COMPLETE — discovery → bridge → enrichment → empty bridge"
