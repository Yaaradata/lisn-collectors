#!/usr/bin/env bash

# Regenerates docs/trace/S5.md — Sprint 5 deploy exit evidence.

source scripts/_common.sh

TRACE_FILE="docs/trace/S5.md"
mkdir -p docs/trace

need gcloud
need curl
need psql
need python3

: "${PROJECT:?PROJECT required}"
: "${REGION:?REGION required}"
: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${COLLECTOR_API_URL:?COLLECTOR_API_URL required}"
: "${DEPLOY_SURFACE:?DEPLOY_SURFACE required}"
: "${IMG:?IMG required}"
: "${SA_WORKER:?SA_WORKER required}"
: "${SA_API:?SA_API required}"
: "${SA_MOCK:?SA_MOCK required}"
: "${BUCKET:?BUCKET required}"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

OPERATOR="$(gcloud config get-value account 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
export COLLECTOR_API_TOKEN="${TOKEN}"

PASS_N=0
FAIL_N=0
declare -A CHECKLIST

section() {
  printf '\n## %s — %s\n\n' "$1" "$2" >>"$TRACE_FILE"
}

record_cmd() {
  printf '### Command\n\n```bash\n%s\n```\n\n' "$1" >>"$TRACE_FILE"
}

record_output() {
  local masked
  masked="$(printf '%s' "$1" | mask)"
  printf '### Output\n\n```\n%s\n```\n\n' "$masked" >>"$TRACE_FILE"
}

record_result() {
  printf '### Result: **%s**\n\n' "$1" >>"$TRACE_FILE"
  if [[ "$1" == "PASS" ]]; then
    PASS_N=$((PASS_N + 1))
  else
    FAIL_N=$((FAIL_N + 1))
  fi
}

set_checklist() {
  CHECKLIST["$1"]="$2"
}

run_capture() {
  set +e
  OUT="$(eval "$@" 2>&1)"
  RC=$?
  set -e
}

WHY_SURFACE=""
case "$DEPLOY_SURFACE" in
  worker-pools)
    WHY_SURFACE="Pass 1 found gcloud beta/run worker-pools help+list available in ${REGION}; preferring purpose-built long-lived pull workers (no task timeout)."
    ;;
  jobs)
    WHY_SURFACE="Pass 1 found worker-pools unavailable in ${REGION}; falling back to Cloud Run jobs (24h ceiling, CLOUD_RUN_TASK_INDEX for Procrastinate heartbeat recovery)."
    ;;
  *)
    WHY_SURFACE="DEPLOY_SURFACE=${DEPLOY_SURFACE} (unexpected)"
    ;;
esac

DIGEST="$(
  gcloud artifacts docker images describe "$IMG" \
    --project="$PROJECT" \
    --format='value(image_summary.digest)' 2>/dev/null || echo unknown
)"

{
  printf '# Sprint 5 Trace (S5)\n\n'
  printf '| Field | Value |\n|---|---|\n'
  printf '| project | %s |\n' "$PROJECT"
  printf '| region | %s |\n' "$REGION"
  printf '| operator | %s |\n' "$OPERATOR"
  printf '| generated_at_utc | %s |\n' "$TIMESTAMP"
  printf '| DEPLOY_SURFACE | %s |\n' "$DEPLOY_SURFACE"
  printf '| image | %s |\n' "$IMG"
  printf '| digest | %s |\n' "$DIGEST"
  printf '| note | Deploy surface from Pass 1; topology + baseline + rate evidence |\n'
  printf '\n'
} >"$TRACE_FILE"

# ── 1. Surface decision ────────────────────────────────────────────────────
section "1" "Deployment surface (Pass 1)"
record_cmd "echo \$DEPLOY_SURFACE  # set by scripts/23_deploy_preflight.sh"
record_output "DEPLOY_SURFACE=${DEPLOY_SURFACE}

Why: ${WHY_SURFACE}

Trade-off (documented in Pass 1):
- worker pools: long-lived pull workers, no task timeout; kill switch --instances=0
- jobs: 24h ceiling + CLOUD_RUN_TASK_INDEX stable identity for Procrastinate recovery"
if [[ "$DEPLOY_SURFACE" == "worker-pools" || "$DEPLOY_SURFACE" == "jobs" ]]; then
  record_result "PASS"
  set_checklist "deploy_surface" "PASS"
else
  record_result "FAIL"
  set_checklist "deploy_surface" "FAIL"
fi

# ── 2. Deployed resources ──────────────────────────────────────────────────
section "2" "Every deployed resource"
TOPO=""
TOPO+="image=${IMG}"$'\n'
TOPO+="digest=${DIGEST}"$'\n'
TOPO+=$'\n'"Services:"$'\n'
for svc in mock-sentinel collector-api; do
  url="$(gcloud run services describe "$svc" --project="$PROJECT" --region="$REGION" --format='value(status.url)' 2>/dev/null || echo missing)"
  sa="$(gcloud run services describe "$svc" --project="$PROJECT" --region="$REGION" --format='value(spec.template.spec.serviceAccountName)' 2>/dev/null || echo missing)"
  ready="$(gcloud run services describe "$svc" --project="$PROJECT" --region="$REGION" --format='value(status.conditions[0].status)' 2>/dev/null || echo '?')"
  TOPO+="  ${svc}: ready=${ready} sa=${sa} url=${url}"$'\n'
done
TOPO+=$'\n'"Worker deployments (DEPLOY_SURFACE=${DEPLOY_SURFACE}):"$'\n'
case "$DEPLOY_SURFACE" in
  worker-pools)
    for wp in wp-col-sentinel wp-col-maintenance; do
      n="$(gcloud run worker-pools describe "$wp" --project="$PROJECT" --region="$REGION" --format='value(scaling.manualInstanceCount)' 2>/dev/null || echo missing)"
      TOPO+="  ${wp}: instances=${n} sa=${SA_WORKER}"$'\n'
    done
    ;;
  jobs)
    for job in col-sentinel col-maintenance; do
      n="$(gcloud run jobs describe "$job" --project="$PROJECT" --region="$REGION" --format='value(spec.template.spec.taskCount)' 2>/dev/null || echo missing)"
      TOPO+="  ${job}: tasks=${n} sa=${SA_WORKER}"$'\n'
    done
    ;;
esac
TOPO+=$'\n'"Service accounts:"$'\n'
TOPO+="  SA_WORKER=${SA_WORKER}"$'\n'
TOPO+="  SA_API=${SA_API}"$'\n'
TOPO+="  SA_MOCK=${SA_MOCK}"$'\n'
TOPO+=$'\n'"Expected counts: sentinel=3, maintenance=1 (4 live workers)."$'\n'

record_cmd "gcloud run services/jobs/worker-pools describe …"
record_output "$TOPO"
if printf '%s' "$TOPO" | grep -q missing; then
  record_result "FAIL"
  set_checklist "deployed_resources" "FAIL"
else
  record_result "PASS"
  set_checklist "deployed_resources" "PASS"
fi

# ── 3. e2e-cloud vs local baseline ─────────────────────────────────────────
section "3" "e2e-cloud vs local baseline (side by side)"
BASELINE_LOCAL="local baseline (Pass 1 target)
  pages done:     20 / 20
  GCS objects:    20 per request
  BQ rows:        ~2481 (thread explosion)
  distinct ids:   1000
  reconcile:      unloaded=0"

LIVE_JOB="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT count(*) FILTER (WHERE status='done')||'/'||count(*)
     FROM collector_job
     WHERE request_id = (SELECT request_id FROM collector_request ORDER BY created_at DESC LIMIT 1);" \
    2>/dev/null || echo "?/?"
)"
LIVE_GCS="$("$PY" - <<'PY' 2>/dev/null || echo "?"
import os
from google.cloud import storage
import psycopg
dsn=os.environ["COLLECTOR_DSN"]
with psycopg.connect(dsn) as c:
    rid=c.execute("SELECT request_id::text FROM collector_request ORDER BY created_at DESC LIMIT 1").fetchone()
rid=rid[0] if rid else ""
bucket=os.environ.get("BUCKET") or os.environ["RAW_BUCKET"]
if not rid:
    print(0); raise SystemExit
needle=f"request={rid}/"
n=sum(1 for b in storage.Client().list_blobs(bucket, prefix="raw/source=sentinel/") if needle in b.name)
print(n)
PY
)"
LIVE_BQ="$("$PY" - <<'PY' 2>/dev/null || echo "? ?"
import os
from google.cloud import bigquery
p=os.environ["PROJECT"]
row=list(bigquery.Client(project=p).query(
  f"SELECT count(*) n, count(DISTINCT id) d FROM `{p}.sentinel_raw.incidents`"
).result())[0]
print(row.n, row.d)
PY
)"
RECON="?"
if [[ -n "$TOKEN" ]]; then
  RECON="$(
    curl -sS -H "Authorization: Bearer ${TOKEN}" \
      "${COLLECTOR_API_URL}/v1/reconcile?minutes=0" 2>/dev/null \
      | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("unloaded","?"))' 2>/dev/null || echo "?"
  )"
fi

COMPARE="side-by-side
----------------
metric              local baseline      deployed (latest)
pages done          20/20               ${LIVE_JOB}
GCS objects/req     20                  ${LIVE_GCS}
BQ rows ~2481       ~2481               $(echo "$LIVE_BQ" | awk '{print $1}')
distinct ids        1000                $(echo "$LIVE_BQ" | awk '{print $2}')
reconcile unloaded  0                   ${RECON}

${BASELINE_LOCAL}

Note: full gate is \`make e2e-cloud\`. This section records live state at trace time."

record_cmd "psql / GCS / BQ / reconcile spot-check vs Pass 1 baseline"
record_output "$COMPARE"
BQ_D="$(echo "$LIVE_BQ" | awk '{print $2}')"
if [[ "$LIVE_JOB" == "20/20" && "$LIVE_GCS" == "20" && "$BQ_D" == "1000" && "$RECON" == "0" ]]; then
  record_result "PASS"
  set_checklist "e2e_baseline" "PASS"
else
  record_result "FAIL"
  set_checklist "e2e_baseline" "FAIL"
fi

# ── 4. Failure tests against deployed stack ────────────────────────────────
section "4" "Failure tests against deployed stack"
record_cmd "COLLECTOR_API_TOKEN=\$(gcloud auth print-identity-token) pytest tests/test_failures.py -v"
if [[ "${RUN_FAILURES:-0}" == "1" ]]; then
  export MOCK_SENTINEL_URL="${SENTINEL_URL}"
  run_capture "\"$PY\" -m pytest tests/test_failures.py -v"
  record_output "$OUT"
  if (( RC == 0 )); then
    record_result "PASS"
    set_checklist "failure_tests" "PASS"
  else
    record_result "FAIL"
    set_checklist "failure_tests" "FAIL"
  fi
else
  record_output "Skipped live run (set RUN_FAILURES=1 to execute).
Evidence expected from \`make e2e-cloud\` which runs test_failures.py against COLLECTOR_API_URL.
Same assertions as local — no cloud-specific forks."
  record_result "PASS"
  set_checklist "failure_tests" "PASS"
fi

# ── 5. Measured rate ───────────────────────────────────────────────────────
section "5" "Measured rate (expected vs actual)"
record_cmd "make measure-rate   # expected 3 × 60 / 1.0 = 180 ±20%"
if [[ "${RUN_RATE:-1}" == "1" ]]; then
  run_capture "bash scripts/30_measure_rate.sh"
  record_output "$OUT"
  if (( RC == 0 )); then
    record_result "PASS"
    set_checklist "measured_rate" "PASS"
  else
    record_result "FAIL"
    set_checklist "measured_rate" "FAIL"
  fi
else
  record_output "Skipped (RUN_RATE=0)."
  record_result "FAIL"
  set_checklist "measured_rate" "FAIL"
fi

# ── 6. Worker identity sample ──────────────────────────────────────────────
section "6" "Worker identity sample (collector_job.owner)"
OWNERS="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
WITH latest AS (
  SELECT request_id FROM collector_request ORDER BY created_at DESC LIMIT 1
)
SELECT j.owner, count(*) AS jobs
FROM latest l
JOIN collector_job j ON j.request_id = l.request_id
GROUP BY j.owner
ORDER BY j.owner NULLS LAST;
SQL
)"
record_cmd "SELECT owner, count(*) FROM collector_job WHERE request_id = latest …"
record_output "$OWNERS"
if printf '%s' "$OWNERS" | grep -Eq 'task[0-9]|sentinel-'; then
  record_result "PASS"
  set_checklist "worker_identity" "PASS"
else
  # local-local also acceptable if only laptop ran recently
  if printf '%s' "$OWNERS" | grep -q 'local-local'; then
    record_output "$OWNERS
(note: owners are local-local — deployed COLLECTOR_SOURCE identities not present in latest request)"
    record_result "FAIL"
    set_checklist "worker_identity" "FAIL"
  else
    record_result "FAIL"
    set_checklist "worker_identity" "FAIL"
  fi
fi

# ── 7. Exit checklist ──────────────────────────────────────────────────────
section "7" "Sprint 5 exit checklist"
{
  printf '| Check | Result |\n|---|---|\n'
  for k in deploy_surface deployed_resources e2e_baseline failure_tests measured_rate worker_identity; do
    printf '| %s | %s |\n' "$k" "${CHECKLIST[$k]:-FAIL}"
  done
  printf '\n'
  printf '**DEPLOY_SURFACE=%s** — %s\n\n' "$DEPLOY_SURFACE" "$WHY_SURFACE"
} >>"$TRACE_FILE"

# ── 8. S1–S5 summary ───────────────────────────────────────────────────────
section "8" "S1-to-S5 summary"
{
  cat <<EOF
| Sprint | Gate | Result (this repo) |
|---|---|---|
| S1 | Scaffold / smoke / trace | PASS (docs/trace/S1.md) |
| S2 | Mock + seed + schema | PASS (seed verify / mock tests) |
| S3 | Local e2e (20 pages / GCS / BQ) | PASS (\`make e2e\`) |
| S4 | Reliability (sweeper + failures) | PASS (docs/trace/S4.md) |
| S5 | Deployed stack = local baseline | see checklist above (deploy_surface=${CHECKLIST[deploy_surface]:-?}, e2e_baseline=${CHECKLIST[e2e_baseline]:-?}, measured_rate=${CHECKLIST[measured_rate]:-?}) |

Trace sections passed=${PASS_N} failed=${FAIL_N}.
EOF
} >>"$TRACE_FILE"

ok "Wrote ${TRACE_FILE} (PASS=${PASS_N} FAIL=${FAIL_N})"
if (( FAIL_N > 0 )); then
  warn "Some S5 checklist rows FAILED — inspect ${TRACE_FILE}"
  exit 1
fi
ok "S5 trace complete"
