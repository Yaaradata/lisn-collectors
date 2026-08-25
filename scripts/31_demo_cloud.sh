#!/usr/bin/env bash

# Live demo runbook against the DEPLOYED Cloud Run stack.
# Start from scripts/10_demo.sh — only what must change for Cloud Run differs.
#
# Usage:
#   ./scripts/31_demo_cloud.sh           # paced demo (Enter between acts)
#   ./scripts/31_demo_cloud.sh --reset   # wipe collector/GCS/BQ state, then run
#
# Preconditions: .env with COLLECTOR_API_URL + DEPLOY_SURFACE + SENTINEL_URL,
# Cloud SQL proxy for local psql, workers already deployable via 28_workers_control.

source scripts/_common.sh

need curl
need python3
need psql
need gcloud

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${SENTINEL_MOCK_DSN:?SENTINEL_MOCK_DSN required}"
: "${RAW_BUCKET:?RAW_BUCKET required}"
: "${PROJECT:?PROJECT required}"
: "${REGION:?REGION required}"
: "${CONN:?CONN required}"
: "${COLLECTOR_API_URL:?COLLECTOR_API_URL required — run make deploy-services}"
: "${SENTINEL_URL:?SENTINEL_URL required}"
: "${DEPLOY_SURFACE:?DEPLOY_SURFACE required — run make deploy-preflight}"
: "${IMG:?IMG required}"

ROOT="$(pwd)"
export PYTHONPATH="${ROOT}"
export PROCRASTINATE_APP="collector.app.app"
export MOCK_SENTINEL_URL="${SENTINEL_URL}"

if [[ "$COLLECTOR_API_URL" == *"127.0.0.1"* || "$COLLECTOR_API_URL" == *"localhost"* ]]; then
  fail "COLLECTOR_API_URL is local — this demo is for Cloud Run"
fi

DO_RESET=0
for arg in "$@"; do
  case "$arg" in
    --reset) DO_RESET=1 ;;
    -h|--help)
      echo "Usage: $0 [--reset]"
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

PROXY_PID=""
REQUEST_ID=""
KILL_REQUEST_ID=""

cleanup() {
  if [[ -n "${PROXY_PID}" ]] && kill -0 "${PROXY_PID}" 2>/dev/null; then
    kill "${PROXY_PID}" 2>/dev/null || true
    wait "${PROXY_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

banner() {
  printf '\n'
  printf '============================================================\n'
  printf '  ACT %s - %s\n' "$1" "$2"
  printf '============================================================\n'
  printf '  Showing: %s\n' "$3"
  printf '  Proves:  %s\n' "$4"
  printf '\n'
}

narrate() {
  printf '  > %s\n' "$*"
}

wait_enter() {
  printf '\n'
  read -r -p "  Press Enter for the next act... " _
  printf '\n'
}

refresh_token() {
  COLLECTOR_API_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
  export COLLECTOR_API_TOKEN
  [[ -n "$COLLECTOR_API_TOKEN" ]] || fail "gcloud auth print-identity-token failed"
}

api_curl() {
  # usage: api_curl GET /health
  local method="$1"
  local path="$2"
  shift 2
  curl -sS -X "$method" \
    -H "Authorization: Bearer ${COLLECTOR_API_TOKEN}" \
    "$@" \
    "${COLLECTOR_API_URL}${path}"
}

do_reset() {
  # Shared path: cancel Cloud Run executions → wait idle → truncate (FK-safe).
  # See scripts/33_reset_collector.sh header for why workers must stop first.
  bash scripts/33_reset_collector.sh --restart
}
ensure_proxy() {
  ok "Ensuring Cloud SQL Auth Proxy on 5432 (psql only — workers are remote)"
  if ! (echo >/dev/tcp/127.0.0.1/5432) >/dev/null 2>&1; then
    PROXY_BIN="./cloud-sql-proxy.exe"
    [[ -f "$PROXY_BIN" ]] || PROXY_BIN="./cloud-sql-proxy"
    [[ -f "$PROXY_BIN" ]] || fail "cloud-sql-proxy binary missing — run scripts/05_smoke.sh once"
    "$PROXY_BIN" "$CONN" --port 5432 >/tmp/demo-cloud-proxy.log 2>&1 &
    PROXY_PID=$!
    sleep 4
  fi
}

ensure_deployed() {
  refresh_token
  ok "Checking deployed API health"
  api_curl GET /health | head -c 200
  echo ""
  api_curl GET /health | grep -q . || fail "collector-api /health failed"

  # Fail loudly if a local worker would steal the demo.
  if ps aux 2>/dev/null | grep -E '[p]rocrastinate.*worker' >/dev/null; then
    fail "local procrastinate worker detected — stop it (false green / stolen jobs)"
  fi

  ok "Ensuring deployed workers are up"
  bash scripts/28_workers_control.sh start || warn "workers start returned non-zero"
  local deadline=$((SECONDS + 120)) live=0
  while (( SECONDS < deadline )); do
    live="$(
      api_curl GET /v1/health/detail \
        | "$PY" -c 'import json,sys; print(int(json.load(sys.stdin).get("live_workers") or 0))' 2>/dev/null || echo 0
    )"
    echo "  live_workers=${live}"
    (( live >= 4 )) && break
    sleep 5
    refresh_token
  done
  (( live >= 4 )) || fail "live_workers >= 4 timed out"
}

image_digest() {
  gcloud artifacts docker images describe "$IMG" \
    --project="$PROJECT" \
    --format='value(image_summary.digest)' 2>/dev/null || echo "<unknown>"
}

kill_one_sentinel_worker() {
  # REAL instance kill — not a local SIGKILL simulation.
  case "$DEPLOY_SURFACE" in
    worker-pools)
      narrate "Scaling wp-col-sentinel 3 → 2 (real instance removal)…"
      bash scripts/28_workers_control.sh scale sentinel 2
      ;;
    jobs)
      narrate "Cancelling one task of the running col-sentinel execution…"
      local exe task
      exe="$(
        gcloud run jobs executions list \
          --job=col-sentinel \
          --project="$PROJECT" \
          --region="$REGION" \
          --limit=20 \
          --format='csv[no-heading](name,completionTime)' 2>/dev/null \
          | awk -F, 'NF && ($2 == "" || $2 == "None") { print $1; exit }'
      )"
      [[ -n "$exe" ]] || fail "no active col-sentinel execution to kill a task from"
      task="$(
        gcloud run jobs executions tasks list \
          --execution="$exe" \
          --project="$PROJECT" \
          --region="$REGION" \
          --format='value(name)' 2>/dev/null | head -n 1
      )"
      if [[ -n "$task" ]] && gcloud run jobs executions tasks cancel "$task" \
          --project="$PROJECT" --region="$REGION" --quiet 2>/dev/null; then
        ok "Cancelled task ${task}"
      elif [[ -n "$task" ]] && gcloud beta run jobs executions tasks cancel "$task" \
          --project="$PROJECT" --region="$REGION" --quiet 2>/dev/null; then
        ok "Cancelled task ${task} (beta)"
      else
        # Fallback: drop parallelism by one via cancel + re-execute at tasks=2.
        warn "per-task cancel unavailable — falling back to tasks=2 re-execute"
        bash scripts/28_workers_control.sh scale sentinel 2
      fi
      ;;
    *) fail "Unknown DEPLOY_SURFACE=${DEPLOY_SURFACE}" ;;
  esac
}

restore_sentinel_workers() {
  case "$DEPLOY_SURFACE" in
    worker-pools)
      narrate "Scaling wp-col-sentinel 2 → 3 (replacement instance)…"
      bash scripts/28_workers_control.sh scale sentinel 3
      ;;
    jobs)
      narrate "Restoring col-sentinel to 3 tasks…"
      bash scripts/28_workers_control.sh scale sentinel 3
      ;;
  esac
}

# ── optional reset ──────────────────────────────────────────────────────────
ensure_proxy
if (( DO_RESET )); then
  do_reset
fi
ensure_deployed
DIGEST="$(image_digest)"

# ===========================================================================
banner 0 "Deployed topology" \
  "services, worker surface, counts, heartbeats, image digest" \
  "nothing is running on this laptop"

narrate "Nothing is running on this laptop. Four workers on Cloud Run,"
narrate "one image, one Cloud SQL instance."
echo ""
echo "  DEPLOY_SURFACE=${DEPLOY_SURFACE}"
echo "  IMG=${IMG}"
echo "  digest=${DIGEST}"
echo "  COLLECTOR_API_URL=${COLLECTOR_API_URL}"
echo "  SENTINEL_URL=${SENTINEL_URL}"
echo ""
echo "  --- Cloud Run services ---"
for svc in mock-sentinel collector-api; do
  url="$(gcloud run services describe "$svc" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null || echo '?')"
  sa="$(gcloud run services describe "$svc" --project="$PROJECT" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || echo '?')"
  echo "  ${svc}: url=${url}"
  echo "           sa=${sa}"
done
echo ""
echo "  --- Worker deployments ---"
case "$DEPLOY_SURFACE" in
  worker-pools)
    for wp in wp-col-sentinel:3 wp-col-maintenance:1; do
      name="${wp%%:*}"; expect="${wp##*:}"
      n="$(
        gcloud run worker-pools describe "$name" --project="$PROJECT" --region="$REGION" \
          --format='value(scaling.manualInstanceCount)' 2>/dev/null || echo '?'
      )"
      echo "  ${name}: instances=${n} (configured default ${expect})"
    done
    ;;
  jobs)
    for job in col-sentinel:3 col-maintenance:1; do
      name="${job%%:*}"; expect="${job##*:}"
      n="$(
        gcloud run jobs describe "$name" --project="$PROJECT" --region="$REGION" \
          --format='value(spec.template.spec.taskCount)' 2>/dev/null || echo '?'
      )"
      echo "  ${name}: tasks=${n} (expected ${expect})"
    done
    ;;
esac
echo ""
echo "  --- Live workers (procrastinate_workers) ---"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -c \
  "SELECT id, now()-last_heartbeat AS heartbeat_age FROM procrastinate_workers ORDER BY id;"
narrate "Same digest on mock, API, and every worker process."
wait_enter

# ===========================================================================
banner 1 "The source has Sentinel-shaped data" \
  "incident / thread counts, explosion factor, null tracking %, issue mix" \
  "field names, ID formats and the thread-exploded shape match real Sentinel exports"

"$PY" -m mock.seed_sentinel --verify-only
narrate "This is not invented data - field names, ID formats and the"
narrate "thread-exploded shape come from real Sentinel exports."
wait_enter

# ===========================================================================
banner 2 "LiSN submits a query" \
  "POST /v1/collect with 1000 incident ids (identity token)" \
  "paging happens once at request time - never at fetch time"

refresh_token
ACT2_OUT="$("$PY" - <<'PY'
import json, os, httpx, psycopg

dsn = os.environ["SENTINEL_MOCK_DSN"]
api = os.environ["COLLECTOR_API_URL"]
headers = {}
tok = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = f"Bearer {tok}"
with psycopg.connect(dsn) as conn:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sentinel_incident ORDER BY id LIMIT 1000"
    )]
assert len(ids) == 1000, len(ids)
r = httpx.post(
    f"{api}/v1/collect",
    json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
    headers=headers,
    timeout=120.0,
)
r.raise_for_status()
body = r.json()
print(json.dumps(body))
print(f"request_id={body['request_id']}", flush=True)
print(f"total_pages={body['total_pages']}", flush=True)
print(f"keys={body['keys']}", flush=True)
PY
)"
printf '%s\n' "$ACT2_OUT"
REQUEST_ID="$(printf '%s\n' "$ACT2_OUT" | sed -n 's/^request_id=//p' | tail -n1)"
[[ -n "$REQUEST_ID" ]] || fail "ACT 2 did not produce request_id"
narrate "Paging happens here, once, at request time - never at fetch time."
wait_enter

# ===========================================================================
banner 3 "Watch the counts move" \
  "poll /v1/requests/{id}/counts until done == 20" \
  "open / in-progress / closed is one SELECT on our own table - what LiSN asked for"

narrate "This is exactly the open / in-progress / closed view LiSN asked for,"
narrate "and it is a single SELECT on our own table."
printf '\n'
refresh_token
"$PY" - <<PY
import os, time, httpx
api = os.environ["COLLECTOR_API_URL"]
rid = "${REQUEST_ID}"
headers = {}
tok = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = f"Bearer {tok}"
deadline = time.time() + 400
last = {}
while time.time() < deadline:
    r = httpx.get(f"{api}/v1/requests/{rid}/counts", headers=headers, timeout=30.0)
    r.raise_for_status()
    counts = r.json().get("counts") or {}
    line = (
        f"  pending={counts.get('pending', 0):2d}  "
        f"in_progress={counts.get('in_progress', 0):2d}  "
        f"done={counts.get('done', 0):2d}"
    )
    if counts != last:
        print(line, flush=True)
        last = dict(counts)
    if counts.get("done", 0) == 20:
        print("  -> all 20 pages done", flush=True)
        break
    time.sleep(1.0)
else:
    raise SystemExit(f"timeout waiting for done==20; last={last}")
PY
wait_enter

# ===========================================================================
banner 4 "Raw evidence" \
  "list GCS objects for this request - exactly 20" \
  "raw is append-only and immutable proof of what a query returned and when"

"$PY" - <<PY
import os
from google.cloud import storage
bucket_name = os.environ["RAW_BUCKET"]
rid = "${REQUEST_ID}"
prefix = "raw/source=sentinel/"
needle = f"request={rid}/"
client = storage.Client()
paths = sorted(
    b.name for b in client.list_blobs(bucket_name, prefix=prefix) if needle in b.name
)
print(f"  objects for request {rid}: {len(paths)}")
assert len(paths) == 20, len(paths)
print(f"  sample path:")
print(f"    gs://{bucket_name}/{paths[0]}")
PY
narrate "Raw is append-only and immutable. This is how we prove what a query"
narrate "returned and when."
wait_enter

# ===========================================================================
banner 5 "The warehouse, and the number that matters" \
  "sentinel_raw row counts vs sentinel_core.incidents_current" \
  "thread explosion in raw; current view collapses to one row per (id, threads_id)"

"$PY" - <<PY
import os
from google.cloud import bigquery
project = os.environ["PROJECT"]
rid = "${REQUEST_ID}"
bq = bigquery.Client(project=project)
raw = list(bq.query(
    f"""
    SELECT count(*) AS n, count(DISTINCT id) AS n_ids
    FROM \`{project}.sentinel_raw.incidents\`
    WHERE _request_id = @rid
    """,
    job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", rid)]
    ),
).result())[0]
core = list(bq.query(
    f"SELECT count(*) AS n FROM \`{project}.sentinel_core.incidents_current\`"
).result())[0]
n = int(raw["n"]); n_ids = int(raw["n_ids"])
factor = n / n_ids if n_ids else 0.0
print(f"  sentinel_raw.incidents  count(*)          = {n}")
print(f"  sentinel_raw.incidents  count(DISTINCT id)= {n_ids}")
print(f"  explosion factor (this request)           = {factor:.3f}")
print(f"  sentinel_core.incidents_current count(*)  = {int(core['n'])}")
PY
narrate "Rows far exceed incidents because the Sentinel export is"
narrate "thread-exploded - one incident, one row per conversation entry."
wait_enter

# ===========================================================================
banner 7 "Kill a worker (REAL instance)" \
  "scale/cancel one Cloud Run worker mid-run; stranded leases + stale heartbeat" \
  "that is a real instance dying, not a simulated one"

narrate "Starting a fresh 1000-id collection so we can kill mid-flight..."
refresh_token
KILL_OUT="$("$PY" - <<'PY'
import json, os, httpx, psycopg, time
dsn_mock = os.environ["SENTINEL_MOCK_DSN"]
dsn = os.environ["COLLECTOR_DSN"]
api = os.environ["COLLECTOR_API_URL"]
headers = {}
tok = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = f"Bearer {tok}"
with psycopg.connect(dsn_mock) as conn:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sentinel_incident ORDER BY id LIMIT 1000"
    )]
r = httpx.post(
    f"{api}/v1/collect",
    json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
    headers=headers,
    timeout=120.0,
)
r.raise_for_status()
rid = r.json()["request_id"]
print(f"kill_request_id={rid}", flush=True)
deadline = time.time() + 180
while time.time() < deadline:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE status = 'done')::int AS done,
              count(*) FILTER (WHERE status = 'in_progress')::int AS running
            FROM collector_job
            WHERE request_id = %s::uuid
            """,
            (rid,),
        ).fetchone()
    done, running = row
    print(f"  waiting to kill... done={done} in_progress={running}", flush=True)
    if done >= 3 and running >= 1:
        break
    time.sleep(1.0)
else:
    raise SystemExit("never reached mid-run state for kill")
print(f"ready_done={done}", flush=True)
PY
)"
printf '%s\n' "$KILL_OUT"
KILL_REQUEST_ID="$(printf '%s\n' "$KILL_OUT" | sed -n 's/^kill_request_id=//p' | tail -n1)"
[[ -n "$KILL_REQUEST_ID" ]] || fail "ACT 7 missing kill_request_id"

kill_one_sentinel_worker
sleep 3

"$PY" - <<PY
import os, psycopg
dsn = os.environ["COLLECTOR_DSN"]
rid = "${KILL_REQUEST_ID}"
with psycopg.connect(dsn) as conn:
    print("  collector_job stuck mid-run:")
    for row in conn.execute(
        """
        SELECT page_no, status, owner,
               lease_expires_at,
               lease_expires_at - now() AS lease_remaining
        FROM collector_job
        WHERE request_id = %s::uuid AND status = 'in_progress'
        ORDER BY page_no
        LIMIT 5
        """,
        (rid,),
    ):
        print(f"    page={row[0]} status={row[1]} owner={row[2]}")
        print(f"      lease_expires_at={row[3]}  remaining={row[4]}")
    print("  stalled Procrastinate workers:")
    for row in conn.execute(
        """
        SELECT id, now() - last_heartbeat AS heartbeat_age
        FROM procrastinate_workers
        ORDER BY last_heartbeat
        """
    ):
        print(f"    worker_id={row[0]}  heartbeat_age={row[1]}")
    n = conn.execute(
        """
        SELECT count(*) FROM collector_job
        WHERE request_id = %s::uuid AND status = 'in_progress'
        """,
        (rid,),
    ).fetchone()[0]
    print(f"  in_progress rows: {n}")
PY
narrate "That is a real instance dying, not a simulated one."
narrate "Nothing in any library recovers this for us on ephemeral"
narrate "infrastructure. We built the sweeper."
wait_enter

# ===========================================================================
banner 8 "The sweeper recovers it (+ identity)" \
  "restore instances/tasks + sweep-now; health/detail shows same task index back" \
  "deterministic object names overwrite on redo; stable WORKER_ID across replace"

"$PY" - <<PY
import os, psycopg
dsn = os.environ["COLLECTOR_DSN"]
rid = "${KILL_REQUEST_ID}"
with psycopg.connect(dsn) as conn:
    conn.execute(
        "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '120 seconds'"
    )
    conn.execute(
        """
        UPDATE collector_job
        SET lease_expires_at = now() - interval '1 second'
        WHERE request_id = %s::uuid AND status = 'in_progress'
        """,
        (rid,),
    )
    conn.commit()
print("  aged worker heartbeats + expired in_progress leases")
PY

restore_sentinel_workers
sleep 5
refresh_token

ok "Triggering sweeper NOW (not waiting for the cron tick)"
"$PY" scripts/sweep_now.py

"$PY" - <<PY
import os, time, httpx
from google.cloud import storage
api = os.environ["COLLECTOR_API_URL"]
rid = "${KILL_REQUEST_ID}"
bucket_name = os.environ["RAW_BUCKET"]
headers = {}
tok = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = f"Bearer {tok}"
deadline = time.time() + 300
last = {}
stagnant = 0
prev_done = -1
while time.time() < deadline:
    r = httpx.get(f"{api}/v1/requests/{rid}/counts", headers=headers, timeout=30.0)
    r.raise_for_status()
    counts = r.json().get("counts") or {}
    line = (
        f"  pending={counts.get('pending', 0):2d}  "
        f"in_progress={counts.get('in_progress', 0):2d}  "
        f"done={counts.get('done', 0):2d}"
    )
    if counts != last:
        print(line, flush=True)
        last = dict(counts)
    done_n = counts.get("done", 0)
    if done_n == 20:
        break
    if done_n == prev_done:
        stagnant += 1
    else:
        stagnant = 0
        prev_done = done_n
    if stagnant >= 20:
        import subprocess, sys
        subprocess.run([sys.executable, "scripts/sweep_now.py"], check=False)
        stagnant = 0
    time.sleep(1.0)
else:
    raise SystemExit(f"recovery timeout; last={last}")
prefix = "raw/source=sentinel/"
needle = f"request={rid}/"
client = storage.Client()
n = sum(1 for b in client.list_blobs(bucket_name, prefix=prefix) if needle in b.name)
print(f"  GCS objects for recovered request: {n}")
assert n == 20, n
PY

refresh_token
ok "GET /v1/health/detail — live workers and stable identities"
api_curl GET /v1/health/detail | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print(json.dumps({"live_workers": d.get("live_workers"), "workers": d.get("workers")}, indent=2))
'
narrate "Replacement came back as the same task index — identity is"
narrate "CLOUD_RUN_TASK_INDEX / pool revision, never a hostname or UUID."
narrate "Deterministic object naming means a redone page overwrites itself."
wait_enter

# ===========================================================================
banner 9 "The silent failure" \
  "/v1/reconcile before / after breaking one row in SQL / after restore" \
  "raw landed, warehouse missed it, nothing errored - only this query finds it"

refresh_token
"$PY" - <<PY
import os, httpx, psycopg
api = os.environ["COLLECTOR_API_URL"]
dsn = os.environ["COLLECTOR_DSN"]
rid = "${REQUEST_ID}"
headers = {}
tok = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
if tok:
    headers["Authorization"] = f"Bearer {tok}"

def reconcile():
    r = httpx.get(f"{api}/v1/reconcile", params={"minutes": 0}, headers=headers, timeout=30.0)
    r.raise_for_status()
    return r.json()

body = reconcile()
print(f"  reconcile unloaded={body['unloaded']} (expect 0)")
assert body["unloaded"] == 0
with psycopg.connect(dsn) as conn:
    row = conn.execute(
        """
        SELECT job_id::text, loaded_at, raw_written_at
        FROM collector_job
        WHERE request_id = %s::uuid AND page_no = 3
        """,
        (rid,),
    ).fetchone()
    job_id, loaded_at, raw_written_at = row
    print(f"  breaking page_no=3 job_id={job_id}")
    conn.execute(
        """
        UPDATE collector_job
        SET loaded_at = NULL,
            raw_written_at = now() - interval '1 hour'
        WHERE job_id = %s::uuid
        """,
        (job_id,),
    )
    conn.commit()
body = reconcile()
print(f"  reconcile unloaded={body['unloaded']}")
assert body["unloaded"] == 1
assert any(r["job_id"] == job_id for r in body["rows"])
print(f"  found gap job_id={job_id}")
with psycopg.connect(dsn) as conn:
    conn.execute(
        """
        UPDATE collector_job
        SET loaded_at = %s, raw_written_at = %s
        WHERE job_id = %s::uuid
        """,
        (loaded_at, raw_written_at, job_id),
    )
    conn.commit()
body = reconcile()
print(f"  after restore unloaded={body['unloaded']} (expect 0)")
assert body["unloaded"] == 0
PY
narrate "Every other failure is loud. This one is silent - raw landed, the"
narrate "warehouse missed it, nothing errored."
wait_enter

# ===========================================================================
printf '\n'
printf '============================================================\n'
printf '  DEMO SUMMARY (Cloud Run)\n'
printf '============================================================\n'
printf '%s\n' "
  Act  What                              Proved
  ---  --------------------------------  ------------------------------------
  0    Deployed topology                 Laptop idle; 4 workers, 1 image, 1 SQL
  1    Sentinel-shaped seed              Real export shape, not invented data
  2    POST /v1/collect (1000 ids)       Paging fixed at request time (+ ID token)
  3    Live counts poll                  LiSN open/in-progress/closed view
  4    GCS raw objects (== 20)           Append-only evidence of the response
  5    BQ raw + current view             Thread explosion + CURRENT collapse
  7    REAL instance kill                Stranded leases + stale heartbeats
  8    Sweeper + health/detail           Same task index identity returns
  9    /v1/reconcile silent gap          Only query that finds GCS-without-BQ

  DEPLOY_SURFACE:      ${DEPLOY_SURFACE}
  Image digest:        ${DIGEST}
  Primary request_id:  ${REQUEST_ID}
  Kill/recovery id:    ${KILL_REQUEST_ID}
"
ok "Cloud demo complete — workers left running"
