#!/usr/bin/env bash

# Worker fleet control — start / stop / scale / status / logs.
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
# Usage: ./scripts/28_workers_control.sh <start|stop|scale|status|logs> [source] [n]
#
#   start [source]         resume configured instance/task counts
#   stop  [source]         instances=0 / cancel running execution
#   scale <source> <n>     set concurrency (also the rate lever:
#                          n workers × min_interval_s=1.0 ⇒ ceiling of n req/s
#                          to Sentinel — changing n changes the number we quote
#                          Flipkart, so record it)
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
DEFAULT_MAINT_N=1

usage() {
  cat <<'EOF'
Usage: ./scripts/28_workers_control.sh <start|stop|scale|status|logs> [source] [n]

  start [source]       worker-pools: restore configured --instances
                       jobs:         gcloud run jobs execute col-<source>
  stop  [source]       worker-pools: --instances=0
                       jobs:         cancel running execution(s)
  scale <source> <n>   worker-pools: --instances=n
                       jobs:         update --tasks=n and re-execute
                       (rate lever: n × 1.0s interval ⇒ n req/s ceiling)
  status               configured / running / heartbeats / collector_job counts
  logs  <source> [n]   last n log lines (default 50)

  source: sentinel | maintenance   (omit on start/stop to apply to both)
EOF
}

[[ -n "$ACTION" ]] || { usage; exit 1; }

resource_name() {
  local source="$1"
  case "$DEPLOY_SURFACE" in
    worker-pools) printf 'wp-col-%s' "$source" ;;
    jobs)         printf 'col-%s' "$source" ;;
    *) fail "Unknown DEPLOY_SURFACE='${DEPLOY_SURFACE}'" ;;
  esac
}

default_n() {
  case "$1" in
    sentinel)     printf '%s' "$DEFAULT_SENTINEL_N" ;;
    maintenance)  printf '%s' "$DEFAULT_MAINT_N" ;;
    *) fail "unknown source '$1' (sentinel|maintenance)" ;;
  esac
}

resolve_sources() {
  local source="${1:-}"
  if [[ -z "$source" ]]; then
    printf '%s\n' sentinel maintenance
  else
    case "$source" in
      sentinel|maintenance) printf '%s\n' "$source" ;;
      *) fail "unknown source '$source' (sentinel|maintenance)" ;;
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

job_active_executions() {
  local job="$1"
  gcloud run jobs executions list \
    --job="$job" \
    --project="$PROJECT" \
    --region="$REGION" \
    --limit=20 \
    --format='csv[no-heading](name,completionTime)' 2>/dev/null \
    | awk -F, 'NF && ($2 == "" || $2 == "None") { print $1 }'
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

job_running_count() {
  local job="$1"
  local n=0
  while IFS= read -r _; do
    [[ -z "$_" ]] && continue
    n=$((n + 1))
  done < <(job_active_executions "$job")
  # Each active execution runs `tasks` parallel task containers; report taskCount
  # when an execution is active, else 0.
  if (( n > 0 )); then
    job_get_tasks "$job"
  else
    printf '0'
  fi
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
        if [[ -n "$(job_active_executions "$name")" ]]; then
          warn "${name}: already has an active execution — leave it running"
        else
          ok "Executing ${name}"
          gcloud run jobs execute "$name" \
            --project="$PROJECT" \
            --region="$REGION" \
            --quiet
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

cmd_scale() {
  local source="$ARG2"
  local n="$ARG3"
  [[ -n "$source" && -n "$n" ]] || { usage; fail "scale requires <source> <n>"; }
  [[ "$n" =~ ^[0-9]+$ ]] || fail "n must be a non-negative integer, got '$n'"
  case "$source" in
    sentinel|maintenance) ;;
    *) fail "unknown source '$source'" ;;
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
      # Brief pause so cancel settles before execute.
      sleep 3
      if (( n > 0 )); then
        ok "Re-executing ${name} with tasks=${n}"
        gcloud run jobs execute "$name" \
          --project="$PROJECT" \
          --region="$REGION" \
          --quiet
      else
        ok "${name} scaled to 0 — left stopped after cancel"
      fi
      ;;
  esac
}

cmd_status() {
  need psql
  : "${COLLECTOR_DSN:?COLLECTOR_DSN required for status queries}"

  echo "DEPLOY_SURFACE=${DEPLOY_SURFACE}  region=${REGION}"
  echo ""
  printf '%-14s %-28s %-12s %-12s\n' "SOURCE" "RESOURCE" "CONFIGURED" "RUNNING"
  printf '%-14s %-28s %-12s %-12s\n' "------" "--------" "----------" "-------"

  local source
  for source in sentinel maintenance; do
    local name configured running
    name="$(resource_name "$source")"
    configured="$(default_n "$source")"
    case "$DEPLOY_SURFACE" in
      worker-pools)
        running="$(wp_get_instances "$name")"
        # For pools, "configured" default vs current instances: show both.
        # Current instances is the live dial; default is the start target.
        printf '%-14s %-28s %-12s %-12s\n' \
          "$source" "$name" "${configured} (default)" "${running}"
        ;;
      jobs)
        local tasks
        tasks="$(job_get_tasks "$name")"
        running="$(job_running_count "$name")"
        printf '%-14s %-28s %-12s %-12s\n' \
          "$source" "$name" "tasks=${tasks}" "${running}"
        ;;
    esac
  done

  echo ""
  echo "======== procrastinate_workers ========"
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
SELECT id,
       now() - last_heartbeat AS heartbeat_age,
       EXTRACT(EPOCH FROM (now() - last_heartbeat))::int AS age_seconds
FROM procrastinate_workers
ORDER BY id;
SQL

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
    sentinel|maintenance) ;;
    *) fail "unknown source '$source'" ;;
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
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  scale)  cmd_scale ;;
  status) cmd_status ;;
  logs)   cmd_logs ;;
  -h|--help|help) usage ;;
  *) usage; fail "unknown action '$ACTION'" ;;
esac
