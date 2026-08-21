#!/usr/bin/env bash

# Measure the structural rate ceiling against the deployed (or local) mock.
#
# We have been claiming this ceiling without measuring it. Expected:
#   workers × 60s / min_interval_s = 3 × 60 / 1.0 = 180 requests per minute
# PASS if measured is within 20% of expected.
#
# If it comes back near 60 rather than 180, then -c 1 is not doing what we
# think or the sleep is in the wrong place, and the number we would quote
# Flipkart is wrong by 3x. Also note that a rolling deploy briefly runs old and
# new instances together and temporarily doubles this.

source scripts/_common.sh

need gcloud
need curl
need psql
need python3

: "${COLLECTOR_API_URL:?COLLECTOR_API_URL required}"
: "${SENTINEL_URL:?SENTINEL_URL required}"
: "${SENTINEL_MOCK_DSN:?SENTINEL_MOCK_DSN required}"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

WORKERS="${WORKERS:-3}"
WINDOW_S="${WINDOW_S:-60}"
MIN_INTERVAL_S="${MIN_INTERVAL_S:-1.0}"
EXPECTED="$("$PY" -c "print(int(${WORKERS} * ${WINDOW_S} / float('${MIN_INTERVAL_S}')))")"

TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
auth_hdr=()
if [[ -n "$TOKEN" && "$SENTINEL_URL" != *"127.0.0.1"* && "$SENTINEL_URL" != *"localhost"* ]]; then
  auth_hdr=(-H "Authorization: Bearer ${TOKEN}")
fi
api_hdr=()
if [[ -n "$TOKEN" && "$COLLECTOR_API_URL" != *"127.0.0.1"* && "$COLLECTOR_API_URL" != *"localhost"* ]]; then
  api_hdr=(-H "Authorization: Bearer ${TOKEN}")
fi

ok "Expected ceiling: ${WORKERS} workers × ${WINDOW_S}s / ${MIN_INTERVAL_S}s = ${EXPECTED} req"

ok "DELETE ${SENTINEL_URL}/admin/stats"
curl -sS -X DELETE "${auth_hdr[@]}" "${SENTINEL_URL}/admin/stats" \
  || fail "reset /admin/stats failed — redeploy mock with stats endpoints?"

INCIDENT_IDS="$("$PY" - <<'PY'
import os, json, psycopg
dsn = os.environ["SENTINEL_MOCK_DSN"]
with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 1000")
        ids = [r[0] for r in cur.fetchall()]
print(json.dumps(ids))
PY
)"
N_IDS="$("$PY" -c "import json,sys; print(len(json.loads(sys.argv[1])))" "$INCIDENT_IDS")"
[[ "$N_IDS" == "1000" ]] || fail "expected 1000 incident ids, got ${N_IDS}"

ok "POST /v1/collect with 1000 ids"
COLLECT_RESP="$(
  curl -sS "${api_hdr[@]}" \
    -H "Content-Type: application/json" \
    -d "{\"source\":\"sentinel\",\"query_spec\":{\"incident_ids\":${INCIDENT_IDS}}}" \
    "${COLLECTOR_API_URL}/v1/collect"
)"
echo "$COLLECT_RESP" | head -c 200
echo ""
REQ_ID="$("$PY" -c "import json,sys; print(json.load(sys.stdin).get('request_id',''))" <<<"$COLLECT_RESP")"
[[ -n "$REQ_ID" ]] || fail "collect did not return request_id: ${COLLECT_RESP}"

ok "Waiting ${WINDOW_S}s while workers fetch…"
sleep "$WINDOW_S"

STATS="$(curl -sS "${auth_hdr[@]}" "${SENTINEL_URL}/admin/stats")"
echo "stats: ${STATS}"
MEASURED="$("$PY" -c "import json,sys; print(int(json.load(sys.stdin).get('requests') or 0))" <<<"$STATS")"

LO="$("$PY" -c "print(int(${EXPECTED} * 0.8))")"
HI="$("$PY" -c "print(int(${EXPECTED} * 1.2))")"
echo "measured=${MEASURED} expected=${EXPECTED} window=[${LO},${HI}] (±20%)"

if (( MEASURED >= LO && MEASURED <= HI )); then
  ok "RATE CHECK PASS — measured ${MEASURED} within 20% of ${EXPECTED}"
  echo "RATE_GATE: PASSED"
else
  warn "RATE CHECK FAIL — measured ${MEASURED}, expected ~${EXPECTED}"
  echo "RATE_GATE: FAILED — measured ${MEASURED} not in [${LO},${HI}] (expected ${EXPECTED})"
  echo "If near 60 not 180: -c 1 may be wrong or sleep is misplaced (Flipkart quote off by 3x)."
  echo "If near 360: rolling deploy may have briefly doubled instances."
  exit 1
fi
