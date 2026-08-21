#!/usr/bin/env bash

# Live demo runbook - performed in front of an audience.
# Pacing and narration matter as much as correctness.
#
# Usage:
#   ./scripts/10_demo.sh           # run the demo (starts local stack if needed)
#   ./scripts/10_demo.sh --reset   # wipe collector/GCS/BQ state, then run
#
# Preconditions: .env loaded, Cloud SQL reachable (proxy), seed data present.

source scripts/_common.sh

need curl
need python3
need psql
need bq

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${SENTINEL_MOCK_DSN:?SENTINEL_MOCK_DSN required}"
: "${RAW_BUCKET:?RAW_BUCKET required}"
: "${PROJECT:?PROJECT required}"
: "${CONN:?CONN required}"

ROOT="$(pwd)"
export PYTHONPATH="${ROOT}"
export SENTINEL_URL="http://127.0.0.1:8081"
export COLLECTOR_API_URL="http://127.0.0.1:8080"
export MOCK_SENTINEL_URL="http://127.0.0.1:8081"
export PROCRASTINATE_APP="collector.app.app"

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

MOCK_PID=""
API_PID=""
WORKER_PID=""
PROXY_PID=""
REQUEST_ID=""
KILL_REQUEST_ID=""

cleanup() {
  for pid in "$WORKER_PID" "$API_PID" "$MOCK_PID" "$PROXY_PID"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
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

hard_kill_pid() {
  local pid="$1"
  if [[ -z "${pid}" ]]; then
    return 0
  fi
  if ! kill -0 "${pid}" 2>/dev/null; then
    return 0
  fi
  if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* || "$(uname -s)" == CYGWIN* ]]; then
    taskkill //PID "${pid}" //F >/dev/null 2>&1 || kill -9 "${pid}" 2>/dev/null || true
  else
    kill -9 "${pid}" 2>/dev/null || true
  fi
  wait "${pid}" 2>/dev/null || true
}

do_reset() {
  ok "RESET - truncating collector state, GCS sentinel raw prefix, BQ landing table"

  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
TRUNCATE TABLE raw_manifest, collector_job, collector_request RESTART IDENTITY CASCADE;
DO $$
BEGIN
  IF to_regclass('public.collector_control') IS NOT NULL THEN
    TRUNCATE TABLE collector_control RESTART IDENTITY CASCADE;
  END IF;
END $$;
DELETE FROM procrastinate_periodic_defers;
DELETE FROM procrastinate_events;
DELETE FROM procrastinate_jobs;
DELETE FROM procrastinate_workers;
SQL

  "$PY" - <<'PY'
import os
from google.cloud import storage

bucket_name = os.environ["RAW_BUCKET"]
prefix = "raw/source=sentinel/"
client = storage.Client()
bucket = client.bucket(bucket_name)
n = 0
for blob in client.list_blobs(bucket_name, prefix=prefix):
    blob.delete()
    n += 1
print(f"deleted {n} GCS objects under gs://{bucket_name}/{prefix}")
PY

  bq query \
    --project_id="$PROJECT" \
    --use_legacy_sql=false \
    --nouse_cache \
    "TRUNCATE TABLE \`${PROJECT}.sentinel_raw.incidents\`"
  ok "RESET complete"
}

ensure_stack() {
  ok "Ensuring Cloud SQL Auth Proxy on 5432"
  if ! (echo >/dev/tcp/127.0.0.1/5432) >/dev/null 2>&1; then
    PROXY_BIN="./cloud-sql-proxy.exe"
    [[ -f "$PROXY_BIN" ]] || PROXY_BIN="./cloud-sql-proxy"
    [[ -f "$PROXY_BIN" ]] || fail "cloud-sql-proxy binary missing - run scripts/05_smoke.sh once"
    "$PROXY_BIN" "$CONN" --port 5432 >/tmp/demo-proxy.log 2>&1 &
    PROXY_PID=$!
    sleep 4
  fi

  if ! curl -sf "${MOCK_SENTINEL_URL}/health" >/dev/null 2>&1; then
    ok "Starting mock Sentinel on :8081"
    "$PY" -m uvicorn mock.sentinel_api:app --host 127.0.0.1 --port 8081 >/tmp/demo-mock.log 2>&1 &
    MOCK_PID=$!
  else
    ok "Mock already healthy on :8081"
  fi

  if ! curl -sf "${COLLECTOR_API_URL}/health" >/dev/null 2>&1; then
    ok "Starting request API on :8080"
    "$PY" -m uvicorn collector.api:api --host 127.0.0.1 --port 8080 >/tmp/demo-api.log 2>&1 &
    API_PID=$!
  else
    ok "API already healthy on :8080"
  fi

  deadline=$((SECONDS + 60))
  while (( SECONDS < deadline )); do
    if curl -sf "${MOCK_SENTINEL_URL}/health" >/dev/null \
      && curl -sf "${COLLECTOR_API_URL}/health" >/dev/null; then
      break
    fi
    sleep 1
  done
  curl -sf "${MOCK_SENTINEL_URL}/health" >/dev/null || fail "mock health"
  curl -sf "${COLLECTOR_API_URL}/health" >/dev/null || fail "api health"

  ok "Starting sentinel worker (-c 1)"
  "$PY" -m procrastinate worker -q sentinel -c 1 --delete-jobs never >/tmp/demo-worker.log 2>&1 &
  WORKER_PID=$!
  sleep 2
  kill -0 "$WORKER_PID" 2>/dev/null || fail "worker died - see /tmp/demo-worker.log"
}

# ── optional reset ──────────────────────────────────────────────────────────
if (( DO_RESET )); then
  do_reset
fi

ensure_stack

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
  "POST /v1/collect with 1000 incident ids" \
  "paging happens once at request time - never at fetch time"

ACT2_OUT="$("$PY" - <<'PY'
import json, os, httpx, psycopg

dsn = os.environ["SENTINEL_MOCK_DSN"]
api = os.environ["COLLECTOR_API_URL"]
with psycopg.connect(dsn) as conn:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sentinel_incident ORDER BY id LIMIT 1000"
    )]
assert len(ids) == 1000, len(ids)
r = httpx.post(
    f"{api}/v1/collect",
    json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
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

"$PY" - <<PY
import os, time, httpx

api = os.environ["COLLECTOR_API_URL"]
rid = "${REQUEST_ID}"
deadline = time.time() + 400
last = {}
while time.time() < deadline:
    r = httpx.get(f"{api}/v1/requests/{rid}/counts", timeout=30.0)
    r.raise_for_status()
    counts = r.json().get("counts") or {}
    pending = counts.get("pending", 0)
    running = counts.get("in_progress", 0)
    done = counts.get("done", 0)
    line = f"  pending={pending:2d}  in_progress={running:2d}  done={done:2d}"
    if counts != last:
        print(line, flush=True)
        last = dict(counts)
    if done == 20:
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

n = int(raw["n"])
n_ids = int(raw["n_ids"])
factor = n / n_ids if n_ids else 0.0
print(f"  sentinel_raw.incidents  count(*)          = {n}")
print(f"  sentinel_raw.incidents  count(DISTINCT id)= {n_ids}")
print(f"  explosion factor (this request)           = {factor:.3f}")
print(f"  sentinel_core.incidents_current count(*)  = {int(core['n'])}")
PY

narrate "Rows far exceed incidents because the Sentinel export is"
narrate "thread-exploded - one incident, one row per conversation entry."
narrate "Raw accumulates every fetch; the current view collapses to one row"
narrate "per (id, threads_id)."
wait_enter

# ===========================================================================
banner 6 "Kill a worker" \
  "SIGKILL mid-run; show in_progress leases and stalled heartbeat age" \
  "ephemeral infra does not recover this for us - that is why we built the sweeper"

narrate "Starting a fresh 1000-id collection so we can kill mid-flight..."

KILL_OUT="$("$PY" - <<'PY'
import json, os, httpx, psycopg, time

dsn_mock = os.environ["SENTINEL_MOCK_DSN"]
dsn = os.environ["COLLECTOR_DSN"]
api = os.environ["COLLECTOR_API_URL"]
with psycopg.connect(dsn_mock) as conn:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM sentinel_incident ORDER BY id LIMIT 1000"
    )]
r = httpx.post(
    f"{api}/v1/collect",
    json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
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
[[ -n "$KILL_REQUEST_ID" ]] || fail "ACT 6 missing kill_request_id"

ok "SIGKILL worker pid=${WORKER_PID}"
hard_kill_pid "$WORKER_PID"
WORKER_PID=""

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
    assert n >= 1, n
    print(f"  in_progress rows: {n}")
PY

narrate "Nothing in any library recovers this for us on ephemeral"
narrate "infrastructure. We built the sweeper."
wait_enter

# ===========================================================================
banner 7 "The sweeper recovers it" \
  "new worker + manual sweep-now; counts -> done; GCS still exactly 20" \
  "deterministic object names overwrite on redo - no duplicates, no orphans"

# Age heartbeats/leases so the sweeper can act without waiting production timers.
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

ok "Starting replacement sentinel worker"
"$PY" -m procrastinate worker -q sentinel -c 1 --delete-jobs never >/tmp/demo-worker.log 2>&1 &
WORKER_PID=$!
sleep 2
kill -0 "$WORKER_PID" 2>/dev/null || fail "replacement worker died"

ok "Triggering sweeper NOW (not waiting for the cron tick)"
"$PY" scripts/sweep_now.py

"$PY" - <<PY
import os, time
import httpx
from google.cloud import storage

api = os.environ["COLLECTOR_API_URL"]
rid = "${KILL_REQUEST_ID}"
bucket_name = os.environ["RAW_BUCKET"]

deadline = time.time() + 240
last = {}
stagnant = 0
prev_done = -1
while time.time() < deadline:
    r = httpx.get(f"{api}/v1/requests/{rid}/counts", timeout=30.0)
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
    # One extra sweep only if progress stalls — do not open a pool every second.
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

narrate "Deterministic object naming means a redone page overwrites itself."
narrate "No duplicates, no orphans."
wait_enter

# ===========================================================================
banner 8 "The silent failure" \
  "/v1/reconcile before / after breaking one row in SQL / after restore" \
  "raw landed, warehouse missed it, nothing errored - only this query finds it"

"$PY" - <<PY
import os, httpx, psycopg

api = os.environ["COLLECTOR_API_URL"]
dsn = os.environ["COLLECTOR_DSN"]
rid = "${REQUEST_ID}"

def reconcile():
    r = httpx.get(f"{api}/v1/reconcile", params={"minutes": 0}, timeout=30.0)
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
narrate "warehouse missed it, nothing errored. This query is the only thing"
narrate "that finds it, which is why it was called non-negotiable."
wait_enter

# ===========================================================================
printf '\n'
printf '============================================================\n'
printf '  DEMO SUMMARY\n'
printf '============================================================\n'
printf '%s\n' "
  Act  What                              Proved
  ---  --------------------------------  ------------------------------------
  1    Sentinel-shaped seed              Real export shape, not invented data
  2    POST /v1/collect (1000 ids)       Paging fixed at request time
  3    Live counts poll                  LiSN open/in-progress/closed view
  4    GCS raw objects (== 20)           Append-only evidence of the response
  5    BQ raw + current view             Thread explosion + CURRENT collapse
  6    SIGKILL mid-run                   Leases + stalled heartbeats visible
  7    Manual sweeper recovery           Redo overwrites; still exactly 20
  8    /v1/reconcile silent gap          Only query that finds GCS-without-BQ

  Primary request_id:  ${REQUEST_ID}
  Kill/recovery id:    ${KILL_REQUEST_ID}
"
ok "Demo complete"
