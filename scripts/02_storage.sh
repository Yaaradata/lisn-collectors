#!/usr/bin/env bash

source scripts/_common.sh

TRACE_FILE="docs/trace/S1.md"
mkdir -p docs/trace

trace_line() {
  local result="$1"
  local check="$2"
  local details="$3"
  if [[ ! -f "$TRACE_FILE" ]]; then
    printf '| Result | Check | Details |\n|---|---|---|\n' >"$TRACE_FILE"
  fi
  printf '| %s | %s | %s |\n' "$result" "$check" "$details" >>"$TRACE_FILE"
}

need gcloud
need bq

: "${PROJECT:?PROJECT is required in .env}"
: "${REGION:?REGION is required in .env}"
: "${BUCKET:?BUCKET is required in .env}"
: "${DEMO_SOURCE:?DEMO_SOURCE is required in .env}"

echo "## Storage setup $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# STEP 1 — GCS bucket
# ---------------------------------------------------------------------------
ok "STEP 1 — GCS bucket"
# public-access-prevention: this bucket will hold Flipkart customer data and it
# should be structurally impossible to make public.
#
# Layout: ONE bucket serves every collector, partitioned by prefix
# raw/source=<source>/dt=<date>/request=<id>/page=<n>.json. Six buckets would
# mean six lifecycle rules to keep in sync for no benefit.
BUCKET_URI="gs://${BUCKET}"

if gcloud storage buckets describe "$BUCKET_URI" --project="$PROJECT" >/dev/null 2>&1; then
  warn "Bucket ${BUCKET_URI} already exists; skipping create"
  trace_line "WARN" "bucket-create" "${BUCKET} ALREADY EXISTS"
else
  set +e
  create_out="$(
    gcloud storage buckets create "$BUCKET_URI" \
      --project="$PROJECT" \
      --location="$REGION" \
      --uniform-bucket-level-access \
      --public-access-prevention 2>&1
  )"
  create_rc=$?
  set -e

  if (( create_rc == 0 )); then
    ok "Created bucket ${BUCKET_URI}"
    trace_line "PASS" "bucket-create" "${BUCKET} CREATED"
  elif printf '%s' "$create_out" | grep -Eqi '409|AlreadyExists|Conflict|already exists|you already own'; then
    # Idempotent race: if we can describe it in this project, continue.
    # Otherwise the global name is taken — change BUCKET in .env and rerun.
    if gcloud storage buckets describe "$BUCKET_URI" --project="$PROJECT" >/dev/null 2>&1; then
      warn "Bucket ${BUCKET_URI} already exists in this project; continuing"
      trace_line "WARN" "bucket-create" "${BUCKET} ALREADY EXISTS"
    else
      fail "Bucket name '${BUCKET}' is taken globally (409). Change BUCKET in .env and rerun."
    fi
  else
    fail "Failed to create bucket ${BUCKET_URI}: ${create_out}"
  fi
fi

# ---------------------------------------------------------------------------
# STEP 2 — Lifecycle (90-day raw retention for the pilot)
# ---------------------------------------------------------------------------
ok "STEP 2 — Lifecycle (delete objects older than 90 days)"
lifecycle_file="$(mktemp)"
cat >"$lifecycle_file" <<'EOF'
{
  "rule": [
    {
      "action": { "type": "Delete" },
      "condition": { "age": 90 }
    }
  ]
}
EOF

gcloud storage buckets update "$BUCKET_URI" \
  --project="$PROJECT" \
  --lifecycle-file="$lifecycle_file" >/dev/null
rm -f "$lifecycle_file"
ok "Lifecycle set: delete objects older than 90 days"
trace_line "PASS" "lifecycle" "age=90 delete"

# ---------------------------------------------------------------------------
# STEP 3 — BigQuery datasets per source (empty only; no tables/views)
# ---------------------------------------------------------------------------
ok "STEP 3 — BigQuery datasets for DEMO_SOURCE=${DEMO_SOURCE}"
# Design: raw is append-only evidence — every row we ever fetched, proving what
# a query returned and when. Core is the current picture, a VIEW over raw, not a
# second copy. They are separate datasets so LiSN can be granted read on core
# without also getting every historical fetch, and so recreating the view can
# never touch the raw table.
#
# Both MUST be in asia-south1. BigQuery cannot query across locations, and a
# dataset accidentally created in US will fail against asia-south1 data later
# in a confusing way.
#
# Parameterised by source so adding eKart later is one variable change
# (DEMO_SOURCE=ekart), not a rewrite.

create_source_datasets() {
  local source="$1"
  local source_title
  source_title="$(printf '%s' "$source" | awk '{print toupper(substr($0,1,1)) substr($0,2)}')"
  local raw_ds="${source}_raw"
  local core_ds="${source}_core"
  local raw_desc="${source_title} raw landing — append only, never updated, never deleted"
  local core_desc="${source_title} current view — one row per entity, latest wins; this is what LiSN reads"

  if bq show --project_id="$PROJECT" "${PROJECT}:${raw_ds}" >/dev/null 2>&1; then
    warn "Dataset ${raw_ds} already exists; skipping create"
    trace_line "WARN" "dataset:${raw_ds}" "ALREADY EXISTS"
  else
    bq --location="$REGION" mk \
      --dataset \
      --description="${raw_desc}" \
      "${PROJECT}:${raw_ds}"
    ok "Created dataset ${raw_ds} in ${REGION}"
    trace_line "PASS" "dataset:${raw_ds}" "CREATED in ${REGION}"
  fi

  if bq show --project_id="$PROJECT" "${PROJECT}:${core_ds}" >/dev/null 2>&1; then
    warn "Dataset ${core_ds} already exists; skipping create"
    trace_line "WARN" "dataset:${core_ds}" "ALREADY EXISTS"
  else
    bq --location="$REGION" mk \
      --dataset \
      --description="${core_desc}" \
      "${PROJECT}:${core_ds}"
    ok "Created dataset ${core_ds} in ${REGION}"
    trace_line "PASS" "dataset:${core_ds}" "CREATED in ${REGION}"
  fi
}

create_source_datasets "$DEMO_SOURCE"

# ---------------------------------------------------------------------------
# VERIFY — empty datasets only; no tables and no views
# ---------------------------------------------------------------------------
ok "VERIFY — bucket and datasets (no tables/views created)"

bucket_location="$(
  gcloud storage buckets describe "$BUCKET_URI" \
    --project="$PROJECT" \
    --format='value(location)'
)"
# Normalize for verify: API often returns ASIA-SOUTH1.
verify "bucket location" "ASIA-SOUTH1" "$(printf '%s' "$bucket_location" | tr '[:lower:]' '[:upper:]')"

uniform_access="$(
  gcloud storage buckets describe "$BUCKET_URI" \
    --project="$PROJECT" \
    --format='value(uniform_bucket_level_access)'
)"
# Some gcloud versions return True/true or nested; normalize to True/False-ish.
if [[ "$uniform_access" == "True" || "$uniform_access" == "true" ]]; then
  verify "uniform access" "True" "True"
else
  # Fallback field path used by older describe output.
  uniform_access_alt="$(
    gcloud storage buckets describe "$BUCKET_URI" \
      --project="$PROJECT" \
      --format='value(iamConfiguration.uniformBucketLevelAccess.enabled)' 2>/dev/null || true
  )"
  if [[ "$uniform_access_alt" == "True" || "$uniform_access_alt" == "true" ]]; then
    verify "uniform access" "True" "True"
  else
    verify "uniform access" "True" "${uniform_access:-${uniform_access_alt:-}}"
  fi
fi

pap="$(
  gcloud storage buckets describe "$BUCKET_URI" \
    --project="$PROJECT" \
    --format='value(public_access_prevention)'
)"
if [[ -z "$pap" ]]; then
  pap="$(
    gcloud storage buckets describe "$BUCKET_URI" \
      --project="$PROJECT" \
      --format='value(iamConfiguration.publicAccessPrevention)' 2>/dev/null || true
  )"
fi
verify "public access prevention" "enforced" "$(printf '%s' "$pap" | tr '[:upper:]' '[:lower:]')"

lifecycle_age="$(
  gcloud storage buckets describe "$BUCKET_URI" \
    --project="$PROJECT" \
    --format='value(lifecycle_config.rule.condition.age)' 2>/dev/null || true
)"
if [[ -z "$lifecycle_age" ]]; then
  lifecycle_age="$(
    gcloud storage buckets describe "$BUCKET_URI" \
      --project="$PROJECT" \
      --format='json(lifecycle_config)' \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); rules=(d.get("lifecycle_config") or {}).get("rule") or []; print(next((r.get("condition",{}).get("age","") for r in rules if (r.get("action") or {}).get("type")=="Delete"), ""))' 2>/dev/null || true
  )"
fi
verify "lifecycle age" "90" "${lifecycle_age}"

raw_ds="${DEMO_SOURCE}_raw"
core_ds="${DEMO_SOURCE}_core"

raw_location="$(
  bq show --format=prettyjson "${PROJECT}:${raw_ds}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("location",""))'
)"
core_location="$(
  bq show --format=prettyjson "${PROJECT}:${core_ds}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("location",""))'
)"

verify "${raw_ds} location" "asia-south1" "$(printf '%s' "$raw_location" | tr '[:upper:]' '[:lower:]')"
verify "${core_ds} location" "asia-south1" "$(printf '%s' "$core_location" | tr '[:upper:]' '[:lower:]')"

ok "Storage scaffolding complete (empty datasets only; no tables/views)."
