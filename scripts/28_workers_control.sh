#!/usr/bin/env bash

# Worker fleet control — start / stop / scale / restart / wait-idle / status / logs.
# Branches on DEPLOY_SURFACE (worker-pools | jobs) from .env.
#
# Two kill switches exist and they do different things:
#
#   - collector_control flag (Sprint 4 / make pause SOURCE=sentinel):
#     Workers stay running but stop calling the source. Work returns to pending
#     and resumes when cleared. Instant, no deploy.
#
#   - scale to zero (this script / make workers-stop):
#     The workers themselves stop. Anything in flight is recovered by the
#     sweeper.
#
# Use the flag to pause traffic; use scaling to stop paying for idle workers.
#
# Usage: ./scripts/28_workers_control.sh <start|stop|scale|restart|wait-idle|status|logs> [source] [n]
#
#   start [source]         resume configured instance/task counts
#   stop  [source]         instances=0 / cancel running execution
#   scale <source> <n>     set concurrency (also the rate lever:
#                          n workers × min_interval_s=1.0 ⇒ ceiling of n req/s
#                          to Sentinel — changing n changes the number we quote
#                          Flipkart, so record it)
#   restart [source]       cancel any active execution, wait until idle, execute
#   wait-idle [seconds]    block until no active tasks (default 120); fail on timeout
#   status                 configured + running + heartbeats + job counts
#   logs  <source> [n]     tail last n log entries (default 50)

source scripts/_common.sh

need gcloud

: "${PROJECT:?PROJECT required in .env}"
: "${REGION:?REGION required in .env}"
: "${DEPLOY_SURFACE:?DEPLOY_SURFACE required — run make deploy-preflight first}"

ACTION="${1:-}"
ARG2="${2:-}"
ARG3="${3:-}"

DEFAULT_SENTINEL_N=3
DEFAULT_DISCOVERY_N=1
DEFAULT_MAINT_N=1

usage() {
  cat <<'EOF'
Usage: ./scripts/28_workers_control.sh <start|stop|scale|restart|wait-idle|status|logs> [source] [n]

  start [source]       worker-pools: restore configured --instances
                       jobs:         gcloud run jobs execute col-<source>
                       (skips only when an execution is genuinely active)
  stop  [source]       worker-pools: --instances=0
                       jobs:         cancel running execution(s)
  scale <source> <n>   worker-pools: --instances=n
                       jobs:         update --tasks=n and re-execute
                       (rate lever: n × 1.0s interval ⇒ n req/s ceiling)
  restart [source]     cancel active execution(s), wait until idle, execute fresh
  wait-idle [seconds]  wait until no active running tasks (default 120); FAIL on timeout
  status               configured / running / heartbeats / collector_job counts
  logs  <source> [n]   last n log lines (default 50)

  source: sentinel | sentinel_discovery | maintenance
          (omit on start/stop/restart to apply to all three)
EOF
}

[[ -n "$ACTION" ]] || { usage; exit 1; }

resource_name() {
  local source="$1"
  # sentinel_discovery → col-sentinel-discovery (hyphenated Cloud Run name)
  local slug="${source//_/-}"
  case "$DEPLOY_SURFACE" in
    worker-pools) printf 'wp-col-%s' "$slug" ;;
    jobs)         printf 'col-%s' "$slug" ;;
    *) fail "Unknown DEPLOY_SURFACE='${DEPLOY_SURFACE}'" ;;
  esac
}

default_n() {
  case "$1" in
    sentinel)            printf '%s' "$DEFAULT_SENTINEL_N" ;;
    sentinel_discovery)  printf '%s' "$DEFAULT_DISCOVERY_N" ;;
    maintenance)         printf '%s' "$DEFAULT_MAINT_N" ;;
    *) fail "unknown source '$1' (sentinel|sentinel_discovery|maintenance)" ;;
  esac
}

resolve_sources() {
  local source="${1:-}"
  if [[ -z "$source" ]]; then
    printf '%s\n' sentinel sentinel_discovery maintenance
  else
    case "$source" in
      sentinel|sentinel_discovery|maintenance) printf '%s\n' "$source" ;;
      *) fail "unknown source '$source' (sentinel|sentinel_discovery|maintenance)" ;;
    esac
  fi
}

wp_bin() {
  if gcloud run worker-pools update --help >/dev/null 2>&1; then
    printf '%s' "gcloud run worker-pools"
  elif gcloud beta run worker-pools update --help >/dev/null 2>&1; then
    printf '%s' "gcloud beta run worker-pools"
  else
    fail "gcloud run worker-pools not available"
  fi
}

wp_set_instances() {
  local name="$1"
  local n="$2"
  local bin
  bin="$(wp_bin)"
  # shellcheck disable=SC2086
  ${bin} update "$name" \
    --project="$PROJECT" \
    --region="$REGION" \
    --instances="$n" \
    --quiet
  ok "${name} --instances=${n}"
}

wp_get_instances() {
  local name="$1"
  local n
  n="$(
    gcloud run worker-pools describe "$name" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format='value(scaling.manualInstanceCount)' 2>/dev/null \
    || gcloud beta run worker-pools describe "$name" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format='value(scaling.manualInstanceCount)' 2>/dev/null \
    || true
  )"
  if [[ -z "$n" ]]; then
    n="$(
      gcloud run worker-pools describe "$name" \
        --project="$PROJECT" \
        --region="$REGION" \
        --format=yaml 2>/dev/null \
        | awk '/manualInstanceCount:/{print $2; exit}' \
      || true
    )"
  fi
  printf '%s' "${n:-?}"
}

# One CSV row per recent execution (no message field — it is multi-line and
# breaks parsers). Verified against our gcloud SDK:
#   name, runningCount, conditions[0].type, conditions[0].status,
#   conditions[0].reason, startTime
#
# Note: conditions[0].type is the K8s-style "Completed" condition name and stays
# "Completed" even while tasks are still running (status=False). Terminal vs
# live is NOT readable from the type string alone.
job_execution_rows() {
  local job="$1"
  local limit="${2:-20}"
  gcloud run jobs executions list \
    --job="$job" \
    --project="$PROJECT" \
    --region="$REGION" \
    --limit="$limit" \
    --format='csv[no-heading](name,status.runningCount,status.conditions[0].type,status.conditions[0].status,status.conditions[0].reason,status.startTime)' \
    2>/dev/null || true
}

# Active = currently has running tasks.
# Cancelled / completed / failed executions report runningCount=0 (do not infer
# activity from "an execution record exists", and do not use completionTime —
# it can be empty or misleading in list views). conditions[0].type is not a
# reliable terminal signal on this SDK (see comment on job_execution_rows).
job_row_is_active() {
  local running="${1:-0}"
  [[ "$running" =~ ^[0-9]+$ ]] || running=0
  (( running > 0 ))
}

job_active_executions() {
  local job="$1"
  local name running ctype cstatus reason start
  while IFS=',' read -r name running ctype cstatus reason start; do
    [[ -z "$name" ]] && continue
    if job_row_is_active "$running"; then
      printf '%s\n' "$name"
    fi
  done < <(job_execution_rows "$job")
}

job_latest_execution_row() {
  local job="$1"
  job_execution_rows "$job" 1 | head -n1
}

job_running_task_count() {
  local job="$1"
  local name running ctype cstatus reason start
  local total=0
  while IFS=',' read -r name running ctype cstatus reason start; do
    [[ -z "$name" ]] && continue
    [[ "$running" =~ ^[0-9]+$ ]] || continue
    if job_row_is_active "$running"; then
      total=$((total + running))
    fi
  done < <(job_execution_rows "$job")
  printf '%s' "$total"
}

job_age_seconds() {
  local start="$1"
  if [[ -z "$start" || "$start" == "None" ]]; then
    printf '?'
    return
  fi
  python - "$start" <<'PY' 2>/dev/null || printf '?'
import sys
from datetime import datetime, timezone
raw = sys.argv[1].strip()
if raw.endswith("Z"):
    raw = raw[:-1] + "+00:00"
try:
    t = datetime.fromisoformat(raw)
except ValueError:
    print("?", end="")
    raise SystemExit
if t.tzinfo is None:
    t = t.replace(tzinfo=timezone.utc)
print(int((datetime.now(timezone.utc) - t).total_seconds()), end="")
PY
}

job_cancel_active() {
  local job="$1"
  local exe
  local found=0
  while IFS= read -r exe; do
    [[ -z "$exe" ]] && continue
    found=1
    ok "Cancelling ${exe}"
    gcloud run jobs executions cancel "$exe" \
      --project="$PROJECT" \
      --region="$REGION" \
      --quiet || warn "cancel failed for ${exe}"
  done < <(job_active_executions "$job")
  if (( found == 0 )); then
    warn "${job}: no active execution to cancel"
  fi
}

# Wait until no execution for this job reports running tasks.
# Returns 0 when idle; 1 on timeout (caller decides warn vs fail).
job_wait_idle() {
  local job="$1"
  local timeout_s="${2:-120}"
  local elapsed=0
  local running
  while (( elapsed < timeout_s )); do
    running="$(job_running_task_count "$job")"
    [[ "$running" =~ ^[0-9]+$ ]] || running=0
    if (( running == 0 )); then
      ok "${job}: idle (running tasks=0)"
      return 0
    fi
    echo "  waiting for ${job} to idle (running=${running}, ${elapsed}s/${timeout_s}s)..."
    sleep 3
    elapsed=$((elapsed + 3))
  done
  warn "${job}: still running after ${timeout_s}s"
  return 1
}

job_execute() {
  local job="$1"
  local exe
  exe="$(
    gcloud run jobs execute "$job" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format='value(metadata.name)' \
      --quiet
  )"
  ok "Executed ${job} → ${exe}"
}

job_get_tasks() {
  local job="$1"
  local n
  n="$(
    gcloud run jobs describe "$job" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format='value(spec.template.spec.taskCount)' 2>/dev/null || true
  )"
  if [[ -z "$n" ]]; then
    n="$(
      gcloud run jobs describe "$job" \
        --project="$PROJECT" \
        --region="$REGION" \
        --format=yaml 2>/dev/null \
        | awk '/taskCount:/{print $2; exit}'
    )"
  fi
  printf '%s' "${n:-?}"
}

cmd_start() {
  local source
  while IFS= read -r source; do
    local name n
    name="$(resource_name "$source")"
    n="$(default_n "$source")"
    case "$DEPLOY_SURFACE" in
      worker-pools)
        wp_set_instances "$name" "$n"
        ;;
      jobs)
        local active
        active="$(job_active_executions "$name" | head -n1 || true)"
        if [[ -n "$active" ]]; then
          warn "${name}: already running (${active}), leaving it"
        else
          job_execute "$name"
        fi
        ;;
    esac
  done < <(resolve_sources "$ARG2")
}

cmd_stop() {
  local source
  while IFS= read -r source; do
    local name
    name="$(resource_name "$source")"
    case "$DEPLOY_SURFACE" in
      worker-pools)
        wp_set_instances "$name" 0
        ;;
      jobs)
        job_cancel_active "$name"
        ;;
    esac
  done < <(resolve_sources "$ARG2")
}

cmd_restart() {
  local source
  while IFS= read -r source; do
    local name n
    name="$(resource_name "$source")"
    n="$(default_n "$source")"
    case "$DEPLOY_SURFACE" in
      worker-pools)
        wp_set_instances "$name" 0
        sleep 2
        wp_set_instances "$name" "$n"
        ;;
      jobs)
        ok "Restarting ${name}"
        job_cancel_active "$name"
        job_wait_idle "$name" 120 || fail "${name}: did not go idle within 120s — refusing to re-execute"
        job_execute "$name"
        ;;
    esac
  done < <(resolve_sources "$ARG2")
}

cmd_wait_idle() {
  local timeout_s="${ARG2:-120}"
  [[ "$timeout_s" =~ ^[0-9]+$ ]] || fail "wait-idle seconds must be an integer, got '$timeout_s'"
  local source name
  while IFS= read -r source; do
    name="$(resource_name "$source")"
    case "$DEPLOY_SURFACE" in
      worker-pools)
        local elapsed=0 instances
        while (( elapsed < timeout_s )); do
          instances="$(wp_get_instances "$name")"
          if [[ "$instances" == "0" ]]; then
            ok "${name}: idle (instances=0)"
            break
          fi
          echo "  waiting for ${name} instances→0 (now=${instances}, ${elapsed}s/${timeout_s}s)..."
          sleep 3
          elapsed=$((elapsed + 3))
          if (( elapsed >= timeout_s )); then
            fail "${name}: instances still ${instances} after ${timeout_s}s"
          fi
        done
        ;;
      jobs)
        job_wait_idle "$name" "$timeout_s" \
          || fail "${name}: still has running tasks after ${timeout_s}s — refusing to proceed"
        ;;
    esac
  done < <(resolve_sources "")
}

cmd_scale() {
  local source="$ARG2"
  local n="$ARG3"
  [[ -n "$source" && -n "$n" ]] || { usage; fail "scale requires <source> <n>"; }
  [[ "$n" =~ ^[0-9]+$ ]] || fail "n must be a non-negative integer, got '$n'"
  case "$source" in
    sentinel|sentinel_discovery|maintenance) ;;
    *) fail "unknown source '$source' (sentinel|sentinel_discovery|maintenance)" ;;
  esac

  local name
  name="$(resource_name "$source")"

  # This is also the rate lever. n workers at min_interval_s=1.0 gives a ceiling
  # of n requests/second to Sentinel. Changing n changes the number we quote
  # Flipkart, so record it.
  echo "RATE_LEVER source=${source} n=${n} ceiling≈${n} req/s (min_interval_s=1.0)"

  case "$DEPLOY_SURFACE" in
    worker-pools)
      wp_set_instances "$name" "$n"
      ;;
    jobs)
      ok "Updating ${name} --tasks=${n} --parallelism=${n}"
      gcloud run jobs update "$name" \
        --project="$PROJECT" \
        --region="$REGION" \
        --tasks="$n" \
        --parallelism="$n" \
        --quiet
      # Re-execute so the new task count is live. Cancel first to avoid two
      # executions claiming the same CLOUD_RUN_TASK_INDEX identities.
      job_cancel_active "$name"
      job_wait_idle "$name" 120 \
        || fail "${name}: still has running tasks after cancel — refusing to re-execute"
      if (( n > 0 )); then
        ok "Re-executing ${name} with tasks=${n}"
        job_execute "$name"
      else
        ok "${name} scaled to 0 — left stopped after cancel"
      fi
      ;;
  esac
}

cmd_status() {
  echo "DEPLOY_SURFACE=${DEPLOY_SURFACE}  region=${REGION}"
  echo ""

  case "$DEPLOY_SURFACE" in
    worker-pools)
      printf '%-14s %-28s %-12s %-12s\n' "SOURCE" "RESOURCE" "CONFIGURED" "RUNNING"
      printf '%-14s %-28s %-12s %-12s\n' "------" "--------" "----------" "-------"
      local source
      for source in sentinel sentinel_discovery maintenance; do
        local name configured running
        name="$(resource_name "$source")"
        configured="$(default_n "$source")"
        running="$(wp_get_instances "$name")"
        printf '%-14s %-28s %-12s %-12s\n' \
          "$source" "$name" "${configured} (default)" "${running}"
      done
      ;;
    jobs)
      printf '%-14s %-18s %-28s %-8s %-16s %-10s %-8s\n' \
        "SOURCE" "JOB" "LATEST_EXEC" "RUNNING" "CONDITION" "AGE" "UP?"
      printf '%-14s %-18s %-28s %-8s %-16s %-10s %-8s\n' \
        "------" "---" "-----------" "-------" "---------" "---" "---"
      local source
      for source in sentinel sentinel_discovery maintenance; do
        local name row exe_name running ctype cstatus reason start age up cond_disp
        name="$(resource_name "$source")"
        row="$(job_latest_execution_row "$name")"
        if [[ -z "$row" ]]; then
          printf '%-14s %-18s %-28s %-8s %-16s %-10s %-8s\n' \
            "$source" "$name" "(none)" "0" "-" "-" "no"
          continue
        fi
        IFS=',' read -r exe_name running ctype cstatus reason start <<<"$row"
        [[ "$running" =~ ^[0-9]+$ ]] || running=0
        if [[ -n "$reason" && "$reason" != "None" ]]; then
          cond_disp="$reason"
        elif [[ -n "$ctype" ]]; then
          cond_disp="${ctype}:${cstatus:-?}"
        else
          cond_disp="-"
        fi
        age="$(job_age_seconds "$start")"
        if [[ "$age" != "?" ]]; then
          age="${age}s"
        fi
        if job_row_is_active "$running"; then
          up="YES"
        else
          up="no"
        fi
        printf '%-14s %-18s %-28s %-8s %-16s %-10s %-8s\n' \
          "$source" "$name" "$exe_name" "$running" "$cond_disp" "$age" "$up"
      done
      ;;
  esac

  echo ""
  if ! command -v psql >/dev/null 2>&1; then
    warn "psql not on PATH — skipping DB heartbeat / job counts"
    return 0
  fi
  if [[ -z "${COLLECTOR_DSN:-}" ]]; then
    warn "COLLECTOR_DSN unset — skipping DB heartbeat / job counts"
    return 0
  fi

  echo "======== procrastinate_workers ========"
  if ! psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
SELECT id,
       now() - last_heartbeat AS heartbeat_age,
       EXTRACT(EPOCH FROM (now() - last_heartbeat))::int AS age_seconds
FROM procrastinate_workers
ORDER BY id;
SQL
  then
    warn "DB query failed (is the Cloud SQL proxy up?) — fleet table above is still valid"
    return 0
  fi

  echo ""
  echo "======== collector_job by source, status ========"
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
SELECT source, status, count(*) AS n
FROM collector_job
GROUP BY source, status
ORDER BY source, status;
SQL
}

cmd_logs() {
  local source="$ARG2"
  local n="${ARG3:-50}"
  [[ -n "$source" ]] || { usage; fail "logs requires <source>"; }
  [[ "$n" =~ ^[0-9]+$ ]] || fail "n must be a positive integer"
  case "$source" in
    sentinel|sentinel_discovery|maintenance) ;;
    *) fail "unknown source '$source' (sentinel|sentinel_discovery|maintenance)" ;;
  esac

  local name filter
  name="$(resource_name "$source")"

  case "$DEPLOY_SURFACE" in
    worker-pools)
      filter="resource.type=\"cloud_run_worker_pool\" AND resource.labels.worker_pool_name=\"${name}\""
      ;;
    jobs)
      filter="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${name}\""
      ;;
  esac

  ok "Last ${n} log entries for ${name}"
  gcloud logging read "$filter" \
    --project="$PROJECT" \
    --limit="$n" \
    --freshness=7d \
    --format='value(timestamp,severityPayload,textPayload)' \
    --order=desc
}

case "$ACTION" in
  start)     cmd_start ;;
  stop)      cmd_stop ;;
  scale)     cmd_scale ;;
  restart)   cmd_restart ;;
  wait-idle) cmd_wait_idle ;;
  status)    cmd_status ;;
  logs)      cmd_logs ;;
  -h|--help|help) usage ;;
  *) usage; fail "unknown action '$ACTION'" ;;
esac
