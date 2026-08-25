#!/usr/bin/env bash

# Idempotent worker deploy. Branches on DEPLOY_SURFACE from .env (Pass 1).
#
# FLAGS THAT MATTER:
#   -c 1
#     One job at a time per worker. Three workers therefore means at most three
#     concurrent calls to Sentinel. This is what makes the rate ceiling
#     structural rather than configured.
#   --delete-jobs never
#     Keep completed rows so job history is visible in the demo.
#     Note: Procrastinate 3.9 requires lowercase (never, successful, always).
#   shutdown grace period (worker pools: --grace-period)
#     Longer than the slowest page so a redeploy lets the worker finish its
#     current job rather than stranding it. (lease_seconds=300; pages are far
#     shorter — 120s is generous.)

source scripts/_common.sh

need gcloud
need curl
need psql
need python3

: "${PROJECT:?PROJECT required in .env}"
: "${REGION:?REGION required in .env}"
: "${IMG:?IMG required in .env}"
: "${SA_WORKER:?SA_WORKER required in .env}"
: "${CONN:?CONN required in .env}"
: "${BUCKET:?BUCKET required in .env}"
: "${SENTINEL_URL:?SENTINEL_URL required — run make deploy-services first}"
: "${COLLECTOR_API_URL:?COLLECTOR_API_URL required — run make deploy-services first}"
: "${COLLECTOR_DSN:?COLLECTOR_DSN required for verify queries}"
: "${DEPLOY_SURFACE:?DEPLOY_SURFACE required — run make deploy-preflight first}"

if [[ "$SENTINEL_URL" == *"127.0.0.1"* || "$SENTINEL_URL" == *"localhost"* ]]; then
  fail "SENTINEL_URL is still local (${SENTINEL_URL}) — deploy mock first (make deploy-services)"
fi

GRACE_PERIOD="120s"
SENTINEL_ARGS="-m,procrastinate,worker,-q,sentinel,-c,1,--delete-jobs,never"
# Discovery is sequential: each page's cursor comes from the previous response.
# Enrichment fans out because pages are independent; discovery cannot, so --tasks=1.
DISCOVERY_ARGS="-m,procrastinate,worker,-q,sentinel_discovery,-c,1,--delete-jobs,never"
MAINT_ARGS="-m,procrastinate,worker,-q,maintenance,-c,1,--delete-jobs,never"

worker_env() {
  local collector_source="$1"
  printf '%s' "PROCRASTINATE_APP=collector.app.app,SENTINEL_URL=${SENTINEL_URL},RAW_BUCKET=${BUCKET},GOOGLE_CLOUD_PROJECT=${PROJECT},USE_ID_TOKEN=1,COLLECTOR_SOURCE=${collector_source}"
}

wp_deploy_cmd() {
  # Prefer GA; fall back to beta if needed.
  if gcloud run worker-pools deploy --help >/dev/null 2>&1; then
    printf '%s' "gcloud run worker-pools deploy"
  elif gcloud beta run worker-pools deploy --help >/dev/null 2>&1; then
    printf '%s' "gcloud beta run worker-pools deploy"
  else
    fail "gcloud run worker-pools deploy not available — set DEPLOY_SURFACE=jobs or update the SDK"
  fi
}

job_has_active_execution() {
  local job="$1"
  local row
  row="$(
    gcloud run jobs executions list \
      --job="$job" \
      --project="$PROJECT" \
      --region="$REGION" \
      --limit=20 \
      --format='csv[no-heading](name,completionTime)' 2>/dev/null \
      | awk -F, 'NF && ($2 == "" || $2 == "None") { print $1; exit }'
  )"
  [[ -n "$row" ]]
}

api_health_detail() {
  local token
  token="$(gcloud auth print-identity-token 2>/dev/null || true)"
  if [[ -z "$token" ]]; then
    fail "gcloud auth print-identity-token failed — re-run gcloud auth login"
  fi
  curl -sS -H "Authorization: Bearer ${token}" \
    "${COLLECTOR_API_URL}/v1/health/detail"
}

ok "DEPLOY_SURFACE=${DEPLOY_SURFACE}"

case "$DEPLOY_SURFACE" in
  worker-pools)
    # -----------------------------------------------------------------
    # PATH A — worker pools
    # Worker pools have no task timeout, which suits a worker designed to
    # run indefinitely. Kill switch is --instances=0.
    # -----------------------------------------------------------------
    WP_CMD="$(wp_deploy_cmd)"
    ok "PATH A — worker pools via: ${WP_CMD}"

    ok "Deploy wp-col-sentinel (instances=3)"
    # shellcheck disable=SC2086
    ${WP_CMD} wp-col-sentinel \
      --project="$PROJECT" \
      --region="$REGION" \
      --image="$IMG" \
      --service-account="$SA_WORKER" \
      --add-cloudsql-instances="$CONN" \
      --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
      --set-env-vars="$(worker_env sentinel)" \
      --instances=3 \
      --grace-period="$GRACE_PERIOD" \
      --command=python \
      --args="$SENTINEL_ARGS" \
      --quiet


    # instances=1: discovery cannot fan out; each cursor page depends on the prior response.
    ok "Deploy wp-col-sentinel-discovery (instances=1)"
    # shellcheck disable=SC2086
    ${WP_CMD} wp-col-sentinel-discovery \
      --project="$PROJECT" \
      --region="$REGION" \
      --image="$IMG" \
      --service-account="$SA_WORKER" \
      --add-cloudsql-instances="$CONN" \
      --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
      --set-env-vars="$(worker_env sentinel_discovery)" \
      --instances=1 \
      --grace-period="$GRACE_PERIOD" \
      --command=python \
      --args="$DISCOVERY_ARGS" \
      --quiet

    ok "Deploy wp-col-maintenance (instances=1)"
    # shellcheck disable=SC2086
    ${WP_CMD} wp-col-maintenance \
      --project="$PROJECT" \
      --region="$REGION" \
      --image="$IMG" \
      --service-account="$SA_WORKER" \
      --add-cloudsql-instances="$CONN" \
      --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
      --set-env-vars="$(worker_env maintenance)" \
      --instances=1 \
      --grace-period="$GRACE_PERIOD" \
      --command=python \
      --args="$MAINT_ARGS" \
      --quiet

    ok "VERIFY — instance counts"
    for wp in wp-col-sentinel:3 wp-col-sentinel-discovery:1 wp-col-maintenance:1; do
      name="${wp%%:*}"
      expect="${wp##*:}"
      actual="$(
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
      if [[ -z "$actual" ]]; then
        actual="$(
          gcloud run worker-pools describe "$name" \
            --project="$PROJECT" \
            --region="$REGION" \
            --format=yaml 2>/dev/null \
            | awk '/manualInstanceCount:/{print $2; exit}' \
          || true
        )"
      fi
      echo "${name} instances=${actual:-<empty>} (expected ${expect})"
      [[ "$actual" == "$expect" ]] || fail "${name}: expected ${expect} instances, got '${actual:-<empty>}'"
    done
    ;;

  jobs)
    # -----------------------------------------------------------------
    # PATH B — Cloud Run jobs
    # --max-retries=0: Procrastinate owns retries. If Cloud Run retried a
    # whole task it would start a second worker with the SAME task index and
    # therefore the same executor identity, and two workers would claim one
    # identity.
    #
    # --task-timeout=86400s: the 24-hour ceiling. A worker running past it is
    # killed. Re-execute daily; anything in flight at that moment is recovered
    # by the sweeper on the next run. Known operational task, not a defect.
    #
    # Cloud Run jobs do not expose --grace-period like worker pools; in-flight
    # work at hard kill / timeout is recovered by the sweeper.
    # -----------------------------------------------------------------
    ok "PATH B — Cloud Run jobs"

    ok "Deploy col-sentinel (tasks=3 parallelism=3)"
    gcloud run jobs deploy col-sentinel \
      --project="$PROJECT" \
      --region="$REGION" \
      --image="$IMG" \
      --service-account="$SA_WORKER" \
      --set-cloudsql-instances="$CONN" \
      --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
      --set-env-vars="$(worker_env sentinel)" \
      --tasks=3 \
      --parallelism=3 \
      --task-timeout=86400s \
      --max-retries=0 \
      --command=python \
      --args="$SENTINEL_ARGS" \
      --quiet


    # --tasks=1: enrichment fans out because pages are independent. Discovery
    # cannot, because each page's cursor comes from the previous response.
    # Parallel discovery tasks would duplicate cursor walks.
    ok "Deploy col-sentinel-discovery (tasks=1 parallelism=1)"
    gcloud run jobs deploy col-sentinel-discovery \
      --project="$PROJECT" \
      --region="$REGION" \
      --image="$IMG" \
      --service-account="$SA_WORKER" \
      --set-cloudsql-instances="$CONN" \
      --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
      --set-env-vars="$(worker_env sentinel_discovery)" \
      --tasks=1 \
      --parallelism=1 \
      --task-timeout=86400s \
      --max-retries=0 \
      --command=python \
      --args="$DISCOVERY_ARGS" \
      --quiet

    ok "Deploy col-maintenance (tasks=1 parallelism=1)"
    gcloud run jobs deploy col-maintenance \
      --project="$PROJECT" \
      --region="$REGION" \
      --image="$IMG" \
      --service-account="$SA_WORKER" \
      --set-cloudsql-instances="$CONN" \
      --set-secrets="COLLECTOR_DSN=collector-dsn:latest" \
      --set-env-vars="$(worker_env maintenance)" \
      --tasks=1 \
      --parallelism=1 \
      --task-timeout=86400s \
      --max-retries=0 \
      --command=python \
      --args="$MAINT_ARGS" \
      --quiet

    ok "VERIFY — task counts"
    for job_spec in col-sentinel:3 col-sentinel-discovery:1 col-maintenance:1; do
      name="${job_spec%%:*}"
      expect="${job_spec##*:}"
      tasks="$(
        gcloud run jobs describe "$name" \
          --project="$PROJECT" \
          --region="$REGION" \
          --format='value(spec.template.spec.taskCount)' 2>/dev/null || true
      )"
      if [[ -z "$tasks" ]]; then
        tasks="$(
          gcloud run jobs describe "$name" \
            --project="$PROJECT" \
            --region="$REGION" \
            --format=yaml \
            | awk '/taskCount:/{print $2; exit}'
        )"
      fi
      echo "${name} tasks=${tasks:-<empty>} (expected ${expect})"
      [[ "$tasks" == "$expect" ]] || fail "${name}: expected ${expect} tasks, got '${tasks:-<empty>}'"
    done

    ok "Start executions if none are already running (idempotent)"
    for job in col-sentinel col-sentinel-discovery col-maintenance; do
      if job_has_active_execution "$job"; then
        warn "${job}: active execution already running — not starting a duplicate"
      else
        ok "Executing ${job}"
        # Do NOT pass --wait: these workers run up to 24h.
        gcloud run jobs execute "$job" \
          --project="$PROJECT" \
          --region="$REGION" \
          --quiet
      fi
    done
    ;;

  *)
    fail "Unknown DEPLOY_SURFACE='${DEPLOY_SURFACE}' (expected worker-pools or jobs)"
    ;;
esac

# ---------------------------------------------------------------------------
# VERIFY — live workers via API + DB
# ---------------------------------------------------------------------------
ok "Poll GET /v1/health/detail until live_workers >= 5 (timeout 120s)"
deadline=$((SECONDS + 120))
DETAIL=""
LIVE=0
while (( SECONDS < deadline )); do
  DETAIL="$(api_health_detail || true)"
  LIVE="$(
    printf '%s' "$DETAIL" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
    print(int(d.get("live_workers") or 0))
except Exception:
    print(0)
' 2>/dev/null || echo 0
  )"
  echo "  live_workers=${LIVE}"
  if (( LIVE >= 4 )); then
    break
  fi
  sleep 5
done

if (( LIVE < 5 )); then
  echo "Last /v1/health/detail:"
  echo "$DETAIL"
  fail "timed out waiting for live_workers >= 5 (got ${LIVE})"
fi

ok "live_workers=${LIVE}"
echo ""
echo "======== /v1/health/detail workers ========"
printf '%s' "$DETAIL" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(json.dumps({"live_workers": d.get("live_workers"), "workers": d.get("workers")}, indent=2))
'
echo "==========================================="
echo ""

ok "procrastinate_workers (direct)"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
SELECT id,
       now() - last_heartbeat AS heartbeat_age,
       EXTRACT(EPOCH FROM (now() - last_heartbeat))::int AS age_seconds
FROM procrastinate_workers
ORDER BY id;
SQL

ROW_N="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT count(*) FROM procrastinate_workers WHERE now() - last_heartbeat < interval '60 seconds';"
)"
echo "heartbeating_workers(<60s)=${ROW_N}"
if (( ROW_N < 5 )); then
  fail "expected >= 5 heartbeating procrastinate_workers rows, got ${ROW_N}"
fi

ok "deploy-workers complete (DEPLOY_SURFACE=${DEPLOY_SURFACE})"
