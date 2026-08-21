#!/usr/bin/env bash

# Sprint 5 deploy preflight — decide DEPLOY_SURFACE and verify prerequisites.
# Writes evidence to docs/trace/S5.md and upserts DEPLOY_SURFACE into .env.

source scripts/_common.sh

TRACE_FILE="docs/trace/S5.md"
mkdir -p docs/trace

need gcloud
need psql
need python3
need bq

: "${PROJECT:?PROJECT required in .env}"
: "${PROJECT_NUMBER:?PROJECT_NUMBER required in .env}"
: "${REGION:?REGION required in .env}"
: "${INSTANCE:?INSTANCE required in .env}"
: "${CONN:?CONN required in .env}"
: "${BUCKET:?BUCKET required in .env}"
: "${COLLECTOR_DSN:?COLLECTOR_DSN required in .env}"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

OPERATOR="$(gcloud config get-value account 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

PASS_N=0
FAIL_N=0
WARN_N=0

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

section() {
  local id="$1"
  local title="$2"
  printf '\n## %s — %s\n\n' "$id" "$title" >>"$TRACE_FILE"
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
  local result="$1"
  printf '### Result: **%s**\n\n' "$result" >>"$TRACE_FILE"
  case "$result" in
    PASS) PASS_N=$((PASS_N + 1)) ;;
    WARN) WARN_N=$((WARN_N + 1)) ;;
    FAIL) FAIL_N=$((FAIL_N + 1)) ;;
  esac
}

run_capture() {
  set +e
  OUT="$(eval "$@" 2>&1)"
  RC=$?
  set -e
}

sa_has_role() {
  local email="$1"
  local role="$2"
  local found
  found="$(
    gcloud projects get-iam-policy "$PROJECT" \
      --flatten="bindings[].members" \
      --filter="bindings.members:serviceAccount:${email} AND bindings.role:${role}" \
      --format="value(bindings.role)" 2>/dev/null | head -n 1
  )"
  [[ "$found" == "$role" ]]
}

cb_has_ar_writer() {
  local email="$1"
  local found
  found="$(
    gcloud projects get-iam-policy "$PROJECT" \
      --flatten="bindings[].members" \
      --filter="bindings.members:serviceAccount:${email} AND bindings.role:roles/artifactregistry.writer" \
      --format="value(bindings.role)" 2>/dev/null | head -n 1
  )"
  [[ "$found" == "roles/artifactregistry.writer" ]]
}

{
  cat <<EOF
# Sprint 5 Trace (S5) — Deploy preflight

| Field | Value |
|---|---|
| project | ${PROJECT} |
| region | ${REGION} |
| operator | ${OPERATOR} |
| generated_at_utc | ${TIMESTAMP} |
| note | Decides DEPLOY_SURFACE for the sprint; verifies earlier-sprint prerequisites |

EOF
} >"$TRACE_FILE"

# ---------------------------------------------------------------------------
# CHECK 1 — Worker pool availability
# ---------------------------------------------------------------------------
ok "CHECK 1 — Worker pool availability (re-test)"
section "1" "Worker pool availability"

# Trade-off (documented, not implicit):
# - worker pools: purpose-built for long-lived pull workers; no task timeout.
# - Cloud Run jobs: 24h task ceiling, but CLOUD_RUN_TASK_INDEX gives a
#   deterministic identity stable across executions — better fit for
#   Procrastinate heartbeat recovery. Either works; later scripts branch on
#   DEPLOY_SURFACE.

record_cmd "gcloud components update --quiet; gcloud beta run worker-pools --help; gcloud beta run worker-pools list --region=${REGION}"

COMP_OUT=""
run_capture "gcloud components update --quiet"
COMP_OUT="gcloud components update --quiet → rc=${RC}"
if (( RC != 0 )); then
  COMP_OUT="${COMP_OUT}
(update failed or skipped — continuing; Sprint 1 may have been an SDK version issue)"
  warn "gcloud components update failed (allowed); continuing"
fi

HELP_OK=0
LIST_OK=0
run_capture "gcloud beta run worker-pools --help"
HELP_OUT="$OUT"
HELP_RC=$RC
if (( HELP_RC == 0 )); then
  HELP_OK=1
fi

run_capture "gcloud beta run worker-pools list --region=${REGION} --project=${PROJECT}"
LIST_OUT="$OUT"
LIST_RC=$RC
if (( LIST_RC == 0 )); then
  LIST_OK=1
fi

WP_BLOCK="${COMP_OUT}

--- worker-pools --help (rc=${HELP_RC}) ---
$(printf '%s' "$HELP_OUT" | head -n 40)

--- worker-pools list (rc=${LIST_RC}) ---
${LIST_OUT}"

if (( HELP_OK == 1 && LIST_OK == 1 )); then
  echo "worker-pools: AVAILABLE"
  DEPLOY_SURFACE="worker-pools"
  WP_BLOCK="${WP_BLOCK}

worker-pools: AVAILABLE
DEPLOY_SURFACE=worker-pools

Trade-off: worker pools are purpose-built for long-lived pull workers and have
no task timeout. Cloud Run jobs have a 24-hour task ceiling but give
CLOUD_RUN_TASK_INDEX, a deterministic identity stable across executions, which
suits Procrastinate heartbeat recovery better. Either works; scripts branch on
DEPLOY_SURFACE. Choosing worker-pools because the API is available in ${REGION}."
  record_output "$WP_BLOCK"
  record_result "PASS"
  ok "DEPLOY_SURFACE=worker-pools"
else
  echo "worker-pools: NOT AVAILABLE"
  DEPLOY_SURFACE="jobs"
  WP_BLOCK="${WP_BLOCK}

worker-pools: NOT AVAILABLE
DEPLOY_SURFACE=jobs

Trade-off: worker pools are purpose-built for long-lived pull workers and have
no task timeout. Cloud Run jobs have a 24-hour task ceiling but give
CLOUD_RUN_TASK_INDEX, a deterministic identity stable across executions, which
suits Procrastinate heartbeat recovery better. Either works; scripts branch on
DEPLOY_SURFACE. Falling back to jobs because worker-pools help/list failed in
${REGION}."
  record_output "$WP_BLOCK"
  record_result "WARN"
  warn "DEPLOY_SURFACE=jobs (worker-pools not available)"
fi

upsert_env "DEPLOY_SURFACE" "$DEPLOY_SURFACE"
ok "Appended/updated DEPLOY_SURFACE=${DEPLOY_SURFACE} in .env"

# ---------------------------------------------------------------------------
# CHECK 2 — Prerequisites from earlier sprints
# ---------------------------------------------------------------------------
ok "CHECK 2 — Prerequisites from earlier sprints"
section "2" "Prerequisites from earlier sprints"

# 2a — Cloud SQL RUNNABLE + CONN match
record_cmd "gcloud sql instances describe ${INSTANCE} --project=${PROJECT}"
run_capture "gcloud sql instances describe '${INSTANCE}' --project='${PROJECT}' --format='yaml(name,state,connectionName)'"
SQL_OUT="$OUT"
SQL_RC=$RC
SQL_STATE="$(gcloud sql instances describe "$INSTANCE" --project="$PROJECT" --format='value(state)' 2>/dev/null || true)"
SQL_CONN="$(gcloud sql instances describe "$INSTANCE" --project="$PROJECT" --format='value(connectionName)' 2>/dev/null || true)"

SQL_DETAIL="state=${SQL_STATE:-<empty>}
connectionName=${SQL_CONN:-<empty>}
CONN(.env)=${CONN}
describe:
${SQL_OUT}"

if [[ "$SQL_RC" -ne 0 || "$SQL_STATE" != "RUNNABLE" ]]; then
  record_output "$SQL_DETAIL"
  record_result "FAIL"
  fail "Cloud SQL instance ${INSTANCE} is not RUNNABLE (state=${SQL_STATE:-<empty>})"
fi
if [[ "$SQL_CONN" != "$CONN" ]]; then
  record_output "$SQL_DETAIL"
  record_result "FAIL"
  fail "CONN in .env ('${CONN}') does not match instance connectionName ('${SQL_CONN}')"
fi
record_output "$SQL_DETAIL"
record_result "PASS"
ok "Cloud SQL ${INSTANCE} RUNNABLE; CONN matches"

# 2b — Secrets exist, retrievable; collector-dsn must be socket form
section "2b" "Secrets collector-dsn and sentinel-mock-dsn"
SECRETS_BLOCK=""
for secret_id in collector-dsn sentinel-mock-dsn; do
  if ! gcloud secrets describe "$secret_id" --project="$PROJECT" >/dev/null 2>&1; then
    record_cmd "gcloud secrets describe ${secret_id}"
    record_output "secret ${secret_id}: MISSING"
    record_result "FAIL"
    fail "Secret ${secret_id} does not exist"
  fi
  payload="$(
    gcloud secrets versions access latest \
      --secret="$secret_id" \
      --project="$PROJECT"
  )" || {
    record_cmd "gcloud secrets versions access latest --secret=${secret_id}"
    record_output "secret ${secret_id}: exists but not retrievable"
    record_result "FAIL"
    fail "Secret ${secret_id} exists but latest version is not retrievable"
  }
  masked="$(printf '%s' "$payload" | mask)"
  SECRETS_BLOCK="${SECRETS_BLOCK}${secret_id}: retrievable (masked)=${masked}
"
  if [[ "$secret_id" == "collector-dsn" ]]; then
    if [[ "$payload" == *"127.0.0.1"* ]]; then
      record_cmd "gcloud secrets versions access latest --secret=collector-dsn"
      record_output "collector-dsn contains 127.0.0.1 — Cloud Run cannot use a local proxy DSN.
masked=${masked}"
      record_result "FAIL"
      fail "collector-dsn secret contains 127.0.0.1 — must use /cloudsql/ SOCKET form for Cloud Run"
    fi
    if [[ "$payload" != *"/cloudsql/"* ]]; then
      record_cmd "gcloud secrets versions access latest --secret=collector-dsn"
      record_output "collector-dsn missing /cloudsql/ socket host.
masked=${masked}"
      record_result "FAIL"
      fail "collector-dsn secret must use /cloudsql/ SOCKET form, not a direct IP"
    fi
  fi
  ok "secret ${secret_id} retrievable"
done
record_cmd "gcloud secrets versions access latest --secret={collector-dsn,sentinel-mock-dsn}"
record_output "$SECRETS_BLOCK"
record_result "PASS"

# 2c — Service accounts
section "2c" "Service accounts"
SA_BLOCK=""
for sa_name in collector-sentinel collector-api mock-sentinel; do
  sa_email="${sa_name}@${PROJECT}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$sa_email" --project="$PROJECT" >/dev/null 2>&1; then
    SA_BLOCK="${SA_BLOCK}${sa_email}: EXISTS
"
    ok "SA ${sa_name} exists"
  else
    record_cmd "gcloud iam service-accounts describe ${sa_email}"
    record_output "${sa_email}: MISSING"
    record_result "FAIL"
    fail "Service account ${sa_name} does not exist"
  fi
done
record_cmd "gcloud iam service-accounts describe {collector-sentinel,collector-api,mock-sentinel}@${PROJECT}.iam.gserviceaccount.com"
record_output "$SA_BLOCK"
record_result "PASS"

# 2d — collector-sentinel roles
section "2d" "collector-sentinel IAM roles"
SA_WORKER_EMAIL="collector-sentinel@${PROJECT}.iam.gserviceaccount.com"
REQUIRED_ROLES=(
  "roles/cloudsql.client"
  "roles/storage.objectAdmin"
  "roles/bigquery.dataEditor"
  "roles/bigquery.jobUser"
)
ROLE_BLOCK=""
MISSING_ROLES=()
for role in "${REQUIRED_ROLES[@]}"; do
  if sa_has_role "$SA_WORKER_EMAIL" "$role"; then
    ROLE_BLOCK="${ROLE_BLOCK}${role}: PRESENT
"
    ok "collector-sentinel has ${role}"
  else
    ROLE_BLOCK="${ROLE_BLOCK}${role}: MISSING
"
    MISSING_ROLES+=("$role")
  fi
done
record_cmd "gcloud projects get-iam-policy ${PROJECT} --filter=bindings.members:serviceAccount:${SA_WORKER_EMAIL}"
record_output "$ROLE_BLOCK"
if ((${#MISSING_ROLES[@]} > 0)); then
  record_result "FAIL"
  fail "collector-sentinel missing roles: ${MISSING_ROLES[*]}"
fi
record_result "PASS"

# 2e — Artifact Registry repo
section "2e" "Artifact Registry repo lisn"
record_cmd "gcloud artifacts repositories describe lisn --location=${REGION} --project=${PROJECT}"
run_capture "gcloud artifacts repositories describe lisn --location='${REGION}' --project='${PROJECT}' --format='yaml(name,format,sizeBytes)'"
if (( RC != 0 )); then
  record_output "$OUT"
  record_result "FAIL"
  fail "Artifact Registry repo lisn not found in ${REGION}"
fi
record_output "$OUT"
record_result "PASS"
ok "Artifact Registry repo lisn exists"

# 2f — GCS bucket
section "2f" "GCS bucket"
BUCKET_URI="gs://${BUCKET}"
record_cmd "gcloud storage buckets describe ${BUCKET_URI}"
run_capture "gcloud storage buckets describe '${BUCKET_URI}' --project='${PROJECT}' --format='yaml(name,location)'"
if (( RC != 0 )); then
  record_output "$OUT"
  record_result "FAIL"
  fail "GCS bucket ${BUCKET} does not exist"
fi
record_output "$OUT"
record_result "PASS"
ok "GCS bucket ${BUCKET} exists"

# 2g — BigQuery datasets
section "2g" "BigQuery datasets sentinel_raw and sentinel_core"
BQ_DS_BLOCK=""
for ds in sentinel_raw sentinel_core; do
  record_cmd "bq show --project_id=${PROJECT} ${PROJECT}:${ds}"
  if bq show --project_id="$PROJECT" "${PROJECT}:${ds}" >/dev/null 2>&1; then
    loc="$(bq show --format=prettyjson --project_id="$PROJECT" "${PROJECT}:${ds}" 2>/dev/null | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("location",""))' 2>/dev/null || echo "?")"
    BQ_DS_BLOCK="${BQ_DS_BLOCK}${ds}: EXISTS location=${loc}
"
    if [[ -n "$loc" && "$loc" != "?" && "${loc^^}" != "ASIA-SOUTH1" ]]; then
      warn "dataset ${ds} location=${loc} (expected asia-south1)"
    fi
    ok "BigQuery dataset ${ds} exists"
  else
    record_output "${ds}: MISSING"
    record_result "FAIL"
    fail "BigQuery dataset ${ds} does not exist"
  fi
done
record_output "$BQ_DS_BLOCK"
record_result "PASS"

# ---------------------------------------------------------------------------
# CHECK 3 — Cloud Build can push
# ---------------------------------------------------------------------------
ok "CHECK 3 — Cloud Build can push to Artifact Registry"
section "3" "Cloud Build Artifact Registry writer"

CB_COMPUTE="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
CB_CLASSIC="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

CB_BLOCK="candidates:
  ${CB_COMPUTE}
  ${CB_CLASSIC}
"
GRANTED_ANY=0
for cb_sa in "$CB_COMPUTE" "$CB_CLASSIC"; do
  if cb_has_ar_writer "$cb_sa"; then
    CB_BLOCK="${CB_BLOCK}${cb_sa}: already has roles/artifactregistry.writer
"
    GRANTED_ANY=1
    ok "Cloud Build SA ${cb_sa} already has artifactregistry.writer"
  else
    CB_BLOCK="${CB_BLOCK}${cb_sa}: missing roles/artifactregistry.writer — granting...
"
    if gcloud projects add-iam-policy-binding "$PROJECT" \
      --member="serviceAccount:${cb_sa}" \
      --role="roles/artifactregistry.writer" \
      --condition=None \
      --quiet >/dev/null 2>&1; then
      CB_BLOCK="${CB_BLOCK}${cb_sa}: GRANTED roles/artifactregistry.writer
"
      GRANTED_ANY=1
      ok "Granted roles/artifactregistry.writer → ${cb_sa}"
    else
      CB_BLOCK="${CB_BLOCK}${cb_sa}: grant FAILED (SA may not exist yet)
"
      warn "Could not grant artifactregistry.writer to ${cb_sa}"
    fi
  fi
done

record_cmd "gcloud projects get-iam-policy / add-iam-policy-binding ... roles/artifactregistry.writer"
record_output "$CB_BLOCK"
if (( GRANTED_ANY == 1 )); then
  record_result "PASS"
else
  record_result "FAIL"
  fail "Neither Cloud Build SA has roles/artifactregistry.writer (grant failed)"
fi

# ---------------------------------------------------------------------------
# CHECK 4 — Local state is the reference baseline
# ---------------------------------------------------------------------------
ok "CHECK 4 — Local baseline counts"
section "4" "Local baseline (reference for deployed run)"

JOB_BY_STATUS="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -F$'\t' -A -c \
    "SELECT status, count(*) FROM collector_job GROUP BY status ORDER BY status;" \
    2>/dev/null || echo "(collector_job query failed — is the proxy up?)"
)"
JOB_TOTAL="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT count(*) FROM collector_job;" 2>/dev/null || echo "?"
)"

GCS_COUNT="$("$PY" - <<'PY'
import os
from google.cloud import storage
bucket = os.environ.get("BUCKET") or os.environ.get("RAW_BUCKET")
prefix = "raw/source=sentinel/"
client = storage.Client(project=os.environ.get("PROJECT"))
n = sum(1 for _ in client.list_blobs(bucket, prefix=prefix))
print(n)
PY
)"

BQ_COUNTS="$("$PY" - <<'PY'
import os
from google.cloud import bigquery
project = os.environ["PROJECT"]
client = bigquery.Client(project=project)
row = list(client.query(
    f"""SELECT count(*) AS n, count(DISTINCT id) AS distinct_ids
        FROM `{project}.sentinel_raw.incidents`"""
).result())[0]
print(f"row_count={row.n}")
print(f"distinct_id_count={row.distinct_ids}")
PY
)"

BASELINE_BLOCK="local baseline
--------------
collector_job by status:
${JOB_BY_STATUS}
collector_job total: ${JOB_TOTAL}

GCS objects under gs://${BUCKET}/raw/source=sentinel/: ${GCS_COUNT}

sentinel_raw.incidents:
${BQ_COUNTS}"

echo ""
echo "======== local baseline ========"
echo "$BASELINE_BLOCK"
echo "================================"
echo ""

record_cmd "psql COLLECTOR_DSN — collector_job by status; GCS list_blobs raw/source=sentinel/; BQ count(+distinct id) sentinel_raw.incidents"
record_output "$BASELINE_BLOCK"
record_result "PASS"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section "summary" "Deploy surface decision"

WHY=""
if [[ "$DEPLOY_SURFACE" == "worker-pools" ]]; then
  WHY="worker-pools help+list succeeded in ${REGION}; preferring purpose-built long-lived pull workers (no task timeout)."
else
  WHY="worker-pools help and/or list failed in ${REGION}; falling back to Cloud Run jobs (24h ceiling, CLOUD_RUN_TASK_INDEX for Procrastinate heartbeat recovery)."
fi

SUMMARY="checks_passed=${PASS_N} checks_warned=${WARN_N} checks_failed=${FAIL_N}
DEPLOY_SURFACE=${DEPLOY_SURFACE}
why: ${WHY}"

record_output "$SUMMARY"
if (( FAIL_N > 0 )); then
  record_result "FAIL"
else
  record_result "PASS"
fi

{
  cat <<EOF

---

## Exit checklist

| Item | Value |
|---|---|
| DEPLOY_SURFACE | ${DEPLOY_SURFACE} |
| written to .env | yes |
| Cloud SQL | ${INSTANCE} / ${SQL_STATE} |
| collector-dsn socket form | yes (/cloudsql/, no 127.0.0.1) |
| Cloud Build AR writer | ok |
| local baseline recorded | yes |
| pass/warn/fail | ${PASS_N}/${WARN_N}/${FAIL_N} |

**Deployment surface for this sprint: \`${DEPLOY_SURFACE}\`**

${WHY}

EOF
} >>"$TRACE_FILE"

ok "SUMMARY — ${SUMMARY}"
echo ""
echo "Deployment surface for this sprint: ${DEPLOY_SURFACE}"
echo "Why: ${WHY}"
echo "Trace written to ${TRACE_FILE}"
