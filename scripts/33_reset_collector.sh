#!/usr/bin/env bash

# Reset collector *output* only — never source data (sentinel_mock) or infra.
#
# WHY WORKERS MUST STOP BEFORE TRUNCATE
# -------------------------------------
# procrastinate_jobs.worker_id REFERENCES procrastinate_workers(id).
# A live worker has a row in procrastinate_workers (e.g. id=4). Its next
# fetch_job writes worker_id=4 into procrastinate_jobs. If we TRUNCATE
# procrastinate_workers while that process is still running, the FK fires:
#
#   insert or update on table "procrastinate_jobs" violates foreign key
#   constraint "procrastinate_jobs_worker_id_fkey"
#   DETAIL: Key (worker_id)=(4) is not present in table "procrastinate_workers".
#
# The worker then exits(1) and the Cloud Run execution goes Failed — correctly
# refusing a deleted identity. The reset was wrong, not the worker.
#
# Order is therefore mandatory, not arbitrary:
#   1. Detect active workers (deployed executions OR local processes)
#   2. Stop them and WAIT until idle (120s timeout → hard fail)
#   3. Truncate procrastinate_* (periodic_defers → events → jobs → workers)
#   4. Truncate collector tables (raw_manifest, collector_job, …)
#   5. Clear GCS raw/ + TRUNCATE sentinel_raw.incidents
#   6. Print (or run) the commands to bring workers back
#
# Usage:
#   ./scripts/33_reset_collector.sh              # stop, wipe, print restart cmds
#   ./scripts/33_reset_collector.sh --restart    # also start workers again
#
# Active-execution detection is delegated to scripts/28_workers_control.sh
# (do not reimplement runningCount checks here).

source scripts/_common.sh

need psql
need python3

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${PROJECT:?PROJECT required}"
: "${RAW_BUCKET:=${BUCKET:-}}"
: "${RAW_BUCKET:?RAW_BUCKET or BUCKET required}"

DO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --restart) DO_RESTART=1 ;;
    -h|--help)
      echo "Usage: $0 [--restart]"
      exit 0
      ;;
    *)
      fail "unknown arg '$arg' (want --restart?)"
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

is_deployed_surface() {
  case "${DEPLOY_SURFACE:-}" in
    jobs|worker-pools) return 0 ;;
    *) return 1 ;;
  esac
}

# Kill local `procrastinate worker` processes (laptop demo / stray processes).
stop_local_workers() {
  local found=0
  local pids=""

  if command -v pgrep >/dev/null 2>&1; then
    pids="$(pgrep -f 'procrastinate.*worker' 2>/dev/null || true)"
  fi

  if [[ -z "$pids" ]] && command -v wmic >/dev/null 2>&1; then
    pids="$(
      wmic process where "name='python.exe' or name='python3.exe'" get ProcessId,CommandLine 2>/dev/null \
        | grep -i 'procrastinate' | grep -i 'worker' | grep -v -i 'wmic' \
        | awk '{print $NF}' || true
    )"
  fi

  if [[ -z "$(echo "$pids" | tr -d '[:space:]')" ]]; then
    # Also match common make worker / python -m forms via ps.
    pids="$(
      ps aux 2>/dev/null \
        | grep -E '[p]ython.*-m procrastinate worker|[p]rocrastinate worker' \
        | awk '{print $2}' || true
    )"
  fi

  if [[ -z "$(echo "$pids" | tr -d '[:space:]')" ]]; then
    ok "No local procrastinate worker processes"
    return 0
  fi

  found=1
  echo "$pids" | while read -r pid; do
    [[ -z "$pid" ]] && continue
    ok "Killing local procrastinate worker pid=${pid}"
    if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* || "$(uname -s)" == CYGWIN* ]]; then
      taskkill //PID "${pid}" //F >/dev/null 2>&1 || kill -9 "${pid}" 2>/dev/null || true
    else
      kill -TERM "${pid}" 2>/dev/null || true
      sleep 1
      kill -9 "${pid}" 2>/dev/null || true
    fi
  done

  # Brief settle so sockets / heartbeats drop.
  sleep 2
  if (( found )); then
    ok "Local workers stopped"
  fi
}

print_restart_commands() {
  echo ""
  echo "======== workers are down — bring them back ========"
  if is_deployed_surface; then
    cat <<EOF
  make workers-start
  # or:
  bash scripts/28_workers_control.sh start
  # or per job:
  gcloud run jobs execute col-sentinel --region=${REGION:-asia-south1} --project=${PROJECT}
  gcloud run jobs execute col-maintenance --region=${REGION:-asia-south1} --project=${PROJECT}
EOF
  else
    cat <<EOF
  # local demo stack (from repo root, venv active):
  python -m procrastinate worker -q sentinel -c 1 --delete-jobs never
  python -m procrastinate worker -q maintenance -c 1 --delete-jobs never
  # or re-run: make demo / ./scripts/10_demo.sh  (starts its own worker)
EOF
  fi
  echo "===================================================="
  echo ""
}

# ---------------------------------------------------------------------------
# 1–2. Stop workers and wait until idle
# ---------------------------------------------------------------------------
ok "RESET — stop workers before truncating Procrastinate (FK: jobs.worker_id → workers.id)"

if is_deployed_surface; then
  need gcloud
  : "${REGION:?REGION required for deployed reset}"
  ok "DEPLOY_SURFACE=${DEPLOY_SURFACE} — cancelling active Cloud Run executions via 28_workers_control"
  bash scripts/28_workers_control.sh stop
  bash scripts/28_workers_control.sh wait-idle 120
  # Stray laptop workers would also break the FK story.
  stop_local_workers
else
  ok "Local surface — killing local procrastinate worker processes"
  stop_local_workers
fi

# ---------------------------------------------------------------------------
# 3. Procrastinate tables — order respects jobs.worker_id → workers.id
# ---------------------------------------------------------------------------
ok "Truncating procrastinate_* (periodic_defers → events → jobs → workers)"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
TRUNCATE procrastinate_periodic_defers RESTART IDENTITY CASCADE;
TRUNCATE procrastinate_events RESTART IDENTITY CASCADE;
TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE;
TRUNCATE procrastinate_workers RESTART IDENTITY CASCADE;
SQL

# ---------------------------------------------------------------------------
# 4. Collector output tables
# ---------------------------------------------------------------------------
ok "Truncating collector output tables"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
TRUNCATE raw_manifest, collector_job, collector_request RESTART IDENTITY CASCADE;
DO $$
BEGIN
  IF to_regclass('public.collector_control') IS NOT NULL THEN
    EXECUTE 'TRUNCATE collector_control RESTART IDENTITY CASCADE';
  END IF;
END $$;
SQL

# ---------------------------------------------------------------------------
# 5. GCS raw/ + BigQuery landing table
# ---------------------------------------------------------------------------
ok "Clearing gs://${RAW_BUCKET}/raw/"
"$PY" - <<'PY'
import os
from google.cloud import storage

bucket_name = os.environ.get("RAW_BUCKET") or os.environ["BUCKET"]
client = storage.Client(project=os.environ.get("PROJECT"))
n = 0
for blob in client.list_blobs(bucket_name, prefix="raw/"):
    blob.delete()
    n += 1
print(f"deleted_gcs_objects={n}")
PY

ok "Truncating ${PROJECT}.sentinel_raw.incidents (+ discovered_ids if present)"
"$PY" - <<'PY'
import os
from google.cloud import bigquery

project = os.environ["PROJECT"]
client = bigquery.Client(project=project)
for table in (
    f"{project}.sentinel_raw.incidents",
    f"{project}.sentinel_raw.discovered_ids",
):
    try:
        client.query(f"TRUNCATE TABLE `{table}`").result()
        print(f"truncate {table} ok")
    except Exception as exc:  # noqa: BLE001
        # discovered_ids may not exist yet on older envs
        print(f"skip {table}: {exc}")
PY

ok "RESET data plane empty"

# ---------------------------------------------------------------------------
# 6. Restart workers or tell the operator how
# ---------------------------------------------------------------------------
if (( DO_RESTART )); then
  if is_deployed_surface; then
    ok "Restarting deployed workers (--restart)"
    bash scripts/28_workers_control.sh start
  else
    warn "--restart on local surface: start workers via the demo script (printed below)"
    print_restart_commands
  fi
else
  print_restart_commands
  warn "Workers are NOT running. Pass --restart to start them, or run the commands above."
fi

ok "RESET complete"
