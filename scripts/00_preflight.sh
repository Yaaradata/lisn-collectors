#!/usr/bin/env bash

source scripts/_common.sh

TRACE_FILE="docs/trace/S1.md"
mkdir -p docs/trace

PASS_COUNT=0
WARN_COUNT=0

trace_header() {
  if [[ ! -f "$TRACE_FILE" ]]; then
    printf '| Result | Check | Details |\n|---|---|---|\n' >"$TRACE_FILE"
  fi
}

trace_line() {
  local result="$1"
  local check="$2"
  local details="$3"
  printf '| %s | %s | %s |\n' "$result" "$check" "$details" >>"$TRACE_FILE"
}

pass_line() {
  local check="$1"
  local details="$2"
  ok "${check}: ${details}"
  trace_line "PASS" "$check" "$details"
  PASS_COUNT=$((PASS_COUNT + 1))
}

warn_line() {
  local check="$1"
  local details="$2"
  warn "${check}: ${details}"
  trace_line "WARN" "$check" "$details"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail_line() {
  local check="$1"
  local details="$2"
  trace_line "FAIL" "$check" "$details"
  fail "${check}: ${details}"
}

# Shared project: collision is WARN-only (AVAILABLE / ALREADY EXISTS).
report_collision() {
  local label="$1"
  local exists="$2"
  if [[ "$exists" == "yes" ]]; then
    warn_line "$label" "ALREADY EXISTS"
  else
    pass_line "$label" "AVAILABLE"
  fi
}

trace_header

PROJECT_EXPECTED="clariversev1"
PROJECT_NUMBER_EXPECTED="153115538723"
REGION_EXPECTED="asia-south1"

echo "## Preflight run $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# CHECK 1 — Tools
# ---------------------------------------------------------------------------
ok "CHECK 1 — Tools"
for tool in gcloud psql python3 docker; do
  need "$tool"
  pass_line "tool:${tool}" "AVAILABLE"
done

# ---------------------------------------------------------------------------
# CHECK 2 — Project and billing
# ---------------------------------------------------------------------------
ok "CHECK 2 — Project and billing"
PROJECT_NUMBER_ACTUAL="$(
  gcloud projects describe "$PROJECT_EXPECTED" \
    --format="value(projectNumber)"
)"
PROJECT_STATE_ACTUAL="$(
  gcloud projects describe "$PROJECT_EXPECTED" \
    --format="value(lifecycleState)"
)"

if [[ "$PROJECT_NUMBER_ACTUAL" == "$PROJECT_NUMBER_EXPECTED" ]]; then
  pass_line "projectNumber" "$PROJECT_NUMBER_ACTUAL"
else
  fail_line "projectNumber" "expected ${PROJECT_NUMBER_EXPECTED}, got ${PROJECT_NUMBER_ACTUAL:-<empty>}"
fi

if [[ "$PROJECT_STATE_ACTUAL" == "ACTIVE" ]]; then
  pass_line "lifecycleState" "$PROJECT_STATE_ACTUAL"
else
  fail_line "lifecycleState" "expected ACTIVE, got ${PROJECT_STATE_ACTUAL:-<empty>}"
fi

BILLING_ENABLED="$(
  gcloud beta billing projects describe "$PROJECT_EXPECTED" \
    --format="value(billingEnabled)"
)"
if [[ "$BILLING_ENABLED" == "True" ]]; then
  pass_line "billingEnabled" "True"
else
  fail_line "billingEnabled" "billingEnabled is not True. Nothing else in this sprint will work."
fi

# ---------------------------------------------------------------------------
# CHECK 3 — Name collisions (shared Clariverse project; WARN never FAIL)
# ---------------------------------------------------------------------------
ok "CHECK 3 — Name collisions (shared project, warn-only)"

if gcloud sql instances describe "lisn-collector-db" --project "$PROJECT_EXPECTED" >/dev/null 2>&1; then
  report_collision "Cloud SQL instance lisn-collector-db" "yes"
else
  report_collision "Cloud SQL instance lisn-collector-db" "no"
fi

if gcloud storage buckets describe "gs://lisn-raw-zone-clariversev1" --project "$PROJECT_EXPECTED" >/dev/null 2>&1; then
  report_collision "GCS bucket lisn-raw-zone-clariversev1" "yes"
else
  report_collision "GCS bucket lisn-raw-zone-clariversev1" "no"
fi

for ds in sentinel_raw sentinel_core; do
  if bq show --project_id="$PROJECT_EXPECTED" "${PROJECT_EXPECTED}:${ds}" >/dev/null 2>&1; then
    report_collision "BigQuery dataset ${ds}" "yes"
  else
    report_collision "BigQuery dataset ${ds}" "no"
  fi
done

for sa_name in collector-sentinel collector-api mock-sentinel; do
  sa_email="${sa_name}@${PROJECT_EXPECTED}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$sa_email" --project "$PROJECT_EXPECTED" >/dev/null 2>&1; then
    report_collision "Service account ${sa_name}" "yes"
  else
    report_collision "Service account ${sa_name}" "no"
  fi
done

if gcloud artifacts repositories describe "lisn" \
  --location="$REGION_EXPECTED" \
  --project="$PROJECT_EXPECTED" >/dev/null 2>&1; then
  report_collision "Artifact Registry lisn" "yes"
else
  report_collision "Artifact Registry lisn" "no"
fi

# ---------------------------------------------------------------------------
# CHECK 4 — Cloud Run worker pools (informational; never fail)
# ---------------------------------------------------------------------------
ok "CHECK 4 — Cloud Run worker pools (informational)"
if gcloud beta run worker-pools --help >/dev/null 2>&1 && \
  gcloud beta run worker-pools list --region="$REGION_EXPECTED" --project="$PROJECT_EXPECTED" >/dev/null 2>&1; then
  echo "worker-pools: AVAILABLE"
  pass_line "worker-pools" "AVAILABLE"
else
  echo "worker-pools: NOT AVAILABLE — fallback is Cloud Run jobs with --tasks=3"
  warn_line "worker-pools" "NOT AVAILABLE — fallback is Cloud Run jobs with --tasks=3"
fi

# ---------------------------------------------------------------------------
# CHECK 5 — Enable APIs (idempotent; Clariverse may already have some on)
# ---------------------------------------------------------------------------
ok "CHECK 5 — Enable APIs"
SERVICES=(
  "sqladmin.googleapis.com"
  "run.googleapis.com"
  "bigquery.googleapis.com"
  "storage.googleapis.com"
  "artifactregistry.googleapis.com"
  "secretmanager.googleapis.com"
  "cloudbuild.googleapis.com"
)

gcloud services enable "${SERVICES[@]}" --project "$PROJECT_EXPECTED" >/dev/null
pass_line "services-enable" "Requested enable for 7 APIs (idempotent)"

for svc in "${SERVICES[@]}"; do
  enabled="$(
    gcloud services list \
      --enabled \
      --project "$PROJECT_EXPECTED" \
      --filter="config.name=${svc}" \
      --format="value(config.name)"
  )"
  if [[ "$enabled" == "$svc" ]]; then
    pass_line "api:${svc}" "ENABLED"
  else
    fail_line "api:${svc}" "NOT ENABLED after enable attempt"
  fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
SAFE_TO_RUN_DATABASE="YES"
if (( WARN_COUNT > 0 )); then
  SAFE_TO_RUN_DATABASE="YES (with warnings)"
fi

summary="checks_passed=${PASS_COUNT}, checks_warned=${WARN_COUNT}, safe_to_run_make_database=${SAFE_TO_RUN_DATABASE}"
ok "SUMMARY — ${summary}"
trace_line "INFO" "summary" "${summary}"
