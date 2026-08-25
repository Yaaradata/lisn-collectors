#!/usr/bin/env bash

# Grant the minimum IAM for POST /v1/admin/reset on collector-api.
#
# WHY THIS IS NEEDED: collector-api holds only roles/cloudsql.client. The reset
# endpoint needs GCS delete (under raw/) and BigQuery TRUNCATE on landing
# tables, so it needs more. Grant the minimum, scoped to specific resources.
#
# DO NOT grant storage.objectAdmin or bigquery.dataEditor at project level.
# That would let this API touch every bucket and dataset in clariversev1,
# including Clariverse's. Sprint 1 already flagged narrowing those as a TODO;
# do it properly here rather than widening.
#
# Usage:
#   ./scripts/35_grant_admin_reset.sh
#   make grant-admin-reset
#
# Idempotent.

source scripts/_common.sh

need gcloud
need bq
need python3

: "${PROJECT:?PROJECT required in .env}"
: "${REGION:?REGION required in .env}"
: "${BUCKET:?BUCKET required in .env}"
: "${SA_API:?SA_API required in .env}"

API_SERVICE="collector-api"
DATASET_RAW="sentinel_raw"
MEMBER="serviceAccount:${SA_API}"

if [[ -x .venv/Scripts/python.exe ]]; then
  export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-.venv/Scripts/python.exe}"
elif [[ -x .venv/bin/python ]]; then
  export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-.venv/bin/python}"
fi

ok "Grant admin-reset IAM for ${SA_API} (scoped, not project-wide data roles)"

# ---------------------------------------------------------------------------
# 1. GCS — bucket-scoped objectAdmin (delete under raw/ only enforced in code)
# ---------------------------------------------------------------------------
ok "STEP 1 — GCS bucket IAM: roles/storage.objectAdmin on gs://${BUCKET}"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="${MEMBER}" \
  --role="roles/storage.objectAdmin" \
  --project="$PROJECT" \
  --quiet >/dev/null

ok "Granted roles/storage.objectAdmin on gs://${BUCKET} → ${SA_API}"

# ---------------------------------------------------------------------------
# 2. BigQuery — dataset ACLs (legacy access list; IAM bindings often blocked)
#    - WRITER on sentinel_raw  (truncate / load / bridge source tables)
#    - READER on sentinel_core (views used by GET /v1/discovered/pending)
# ---------------------------------------------------------------------------
grant_dataset_role() {
  local dataset="$1" role="$2"
  ok "STEP 2 — BigQuery dataset access: ${role} on ${dataset} for ${SA_API}"
  local tmp_json tmp_update
  tmp_json="$(mktemp)"
  tmp_update="$(mktemp)"
  bq show --project_id="$PROJECT" --format=prettyjson "${PROJECT}:${dataset}" >"$tmp_json"
  python3 - "$tmp_json" "$tmp_update" "$SA_API" "$role" <<'PY'
import json, sys
src, dst, email, role = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
doc = json.load(open(src, encoding="utf-8"))
access = list(doc.get("access") or [])
if not any(a.get("userByEmail") == email for a in access):
    access.append({"role": role, "userByEmail": email})
json.dump({"access": access}, open(dst, "w", encoding="utf-8"), indent=2)
print(f"access_entries={len(access)} includes={email} role={role}")
PY
  bq update --project_id="$PROJECT" --source "$tmp_update" "${PROJECT}:${dataset}" >/dev/null
  rm -f "$tmp_json" "$tmp_update"
  ok "Granted dataset ${role} on ${PROJECT}:${dataset} → ${SA_API}"
}

grant_dataset_role "${DATASET_RAW}" "WRITER"
grant_dataset_role "${DATASET_CORE:-sentinel_core}" "READER"

ok "sentinel_raw / sentinel_core access (verify print):"
for ds in "${DATASET_RAW}" "${DATASET_CORE:-sentinel_core}"; do
  bq show --format=prettyjson --project_id="$PROJECT" "${PROJECT}:${ds}" \
    | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(json.dumps({"id": d.get("id"), "access": [a for a in d.get("access", []) if "collector-api" in json.dumps(a)]}, indent=2))
'
done

# ---------------------------------------------------------------------------
# 3. BigQuery jobUser — project-level by nature
#
# jobUser alone permits running queries/jobs but not reading or writing table
# data; data access is still controlled by the dataset grant above. Without it,
# TRUNCATE TABLE fails even with dataEditor on the dataset.
# ---------------------------------------------------------------------------
ok "STEP 3 — roles/bigquery.jobUser on project ${PROJECT} (job execution only)"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="${MEMBER}" \
  --role="roles/bigquery.jobUser" \
  --quiet >/dev/null

ok "Granted roles/bigquery.jobUser → ${SA_API}"

# ---------------------------------------------------------------------------
# 4. Enable the kill-switch env var on collector-api
# ---------------------------------------------------------------------------
ok "STEP 4 — Set ALLOW_ADMIN_RESET=1 on Cloud Run service ${API_SERVICE}"
gcloud run services update "$API_SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --update-env-vars="ALLOW_ADMIN_RESET=1" \
  --quiet

ok "ALLOW_ADMIN_RESET=1 set on ${API_SERVICE}"
echo "  NOTE: remove ALLOW_ADMIN_RESET (or set to 0) to disable POST /v1/admin/reset"
echo "        without redeploying code — gcloud run services update --update-env-vars=ALLOW_ADMIN_RESET=0"

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
ok "VERIFY — bucket IAM shows ${SA_API} with objectAdmin"
BUCKET_POLICY="$(
  gcloud storage buckets get-iam-policy "gs://${BUCKET}" \
    --project="$PROJECT" \
    --format=json
)"
echo "$BUCKET_POLICY" | python3 -c '
import json, sys
sa = sys.argv[1]
policy = json.load(sys.stdin)
hits = []
for b in policy.get("bindings", []):
    if b.get("role") == "roles/storage.objectAdmin" and sa in (b.get("members") or []):
        hits.append(b["role"])
print(json.dumps({"objectAdmin_for_sa": bool(hits), "matching_roles": hits}, indent=2))
raise SystemExit(0 if hits else 1)
' "serviceAccount:${SA_API}" \
  || fail "gs://${BUCKET} IAM missing objectAdmin for ${SA_API}"
ok "bucket IAM: objectAdmin present for ${SA_API}"

ok "VERIFY — sentinel_raw dataset access lists ${SA_API}"
DS_SHOW="$(
  bq show --format=prettyjson --project_id="$PROJECT" "${PROJECT}:${DATASET_RAW}"
)"
echo "$DS_SHOW" | python3 -c '
import json, sys
sa = sys.argv[1]
email = sa.split(":", 1)[-1] if ":" in sa else sa
doc = json.load(sys.stdin)
found = False
for a in doc.get("access", []) or []:
    if a.get("userByEmail") == email and str(a.get("role", "")).upper() in (
        "WRITER", "OWNER", "roles/bigquery.dataEditor".upper()
    ):
        found = True
    role = str(a.get("role") or "")
    if a.get("userByEmail") == email and (
        role.upper() == "WRITER" or role.upper() == "OWNER" or "dataEditor" in role
    ):
        found = True
print(json.dumps({"access": doc.get("access", [])}, indent=2)[:4000])
raise SystemExit(0 if found else 1)
' "serviceAccount:${SA_API}" \
  || fail "${DATASET_RAW} access list missing ${SA_API}"
ok "sentinel_raw lists ${SA_API}"

ok "VERIFY — sentinel_core dataset access lists ${SA_API} as READER"
bq show --format=prettyjson --project_id="$PROJECT" "${PROJECT}:${DATASET_CORE:-sentinel_core}" \
  | python3 -c '
import json, sys
sa = sys.argv[1]
email = sa.split(":", 1)[-1] if ":" in sa else sa
doc = json.load(sys.stdin)
found = any(
    a.get("userByEmail") == email
    and str(a.get("role", "")).upper() in ("READER", "WRITER", "OWNER")
    for a in (doc.get("access") or [])
)
print(json.dumps({"id": doc.get("id"), "access_for_sa": [a for a in doc.get("access", []) if a.get("userByEmail") == email]}, indent=2))
raise SystemExit(0 if found else 1)
' "serviceAccount:${SA_API}" \
  || fail "${DATASET_CORE:-sentinel_core} access list missing ${SA_API}"
ok "sentinel_core lists ${SA_API}"

ok "VERIFY — ${SA_API} has NO project-level storage.objectAdmin or bigquery.dataEditor"
PROJECT_WIDE="$(
  gcloud projects get-iam-policy "$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.members:${MEMBER} AND (bindings.role:roles/storage.objectAdmin OR bindings.role:roles/bigquery.dataEditor)" \
    --format="csv[no-heading](bindings.role,bindings.members)" 2>/dev/null || true
)"
if [[ -n "$(echo "$PROJECT_WIDE" | tr -d '[:space:]')" ]]; then
  echo "$PROJECT_WIDE"
  fail "${SA_API} still has project-level storage.objectAdmin or bigquery.dataEditor — narrow/remove those bindings"
fi
ok "no project-level objectAdmin/dataEditor for ${SA_API}"

ok "VERIFY — ${SA_API} has no accessor on secret sentinel-mock-dsn"
MOCK_SECRET_HITS="$(
  gcloud secrets get-iam-policy "sentinel-mock-dsn" \
    --project="$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.role:roles/secretmanager.secretAccessor AND bindings.members:${MEMBER}" \
    --format="value(bindings.members)" 2>/dev/null || true
)"
if [[ -n "$(echo "$MOCK_SECRET_HITS" | tr -d '[:space:]')" ]]; then
  echo "$MOCK_SECRET_HITS"
  fail "${SA_API} unexpectedly has secretAccessor on sentinel-mock-dsn"
fi
ok "sentinel-mock-dsn: ${SA_API} has no access (correct)"

ok "VERIFY — ALLOW_ADMIN_RESET on ${API_SERVICE}"
RESET_ENV="$(
  gcloud run services describe "$API_SERVICE" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(spec.template.spec.containers[0].env)' 2>/dev/null \
  | tr ',' '\n' | grep -E "ALLOW_ADMIN_RESET" || true
)"
# Also try yaml extract
if [[ -z "$RESET_ENV" ]]; then
  RESET_ENV="$(
    gcloud run services describe "$API_SERVICE" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format=yaml \
    | awk '/name: ALLOW_ADMIN_RESET/{getline; print}' || true
  )"
fi
echo "ALLOW_ADMIN_RESET env: ${RESET_ENV:-<check describe>}"
ENV_VAL="$(
  gcloud run services describe "$API_SERVICE" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format=json \
  | python3 -c '
import json, sys
svc = json.load(sys.stdin)
envs = svc["spec"]["template"]["spec"]["containers"][0].get("env") or []
for e in envs:
    if e.get("name") == "ALLOW_ADMIN_RESET":
        print(e.get("value", ""))
        break
'
)"
[[ "$ENV_VAL" == "1" ]] || fail "ALLOW_ADMIN_RESET is '${ENV_VAL}' (want 1)"
ok "ALLOW_ADMIN_RESET=1 on ${API_SERVICE}"

ok "grant-admin-reset complete"
echo ""
echo "  Summary:"
echo "    gs://${BUCKET}          → roles/storage.objectAdmin (${SA_API})"
echo "    ${PROJECT}:${DATASET_RAW} → dataset WRITER / dataEditor (${SA_API})"
echo "    ${PROJECT}:${DATASET_CORE:-sentinel_core} → dataset READER (${SA_API})"
echo "    project ${PROJECT}      → roles/bigquery.jobUser (${SA_API})  # jobs only"
echo "    ${API_SERVICE}          → ALLOW_ADMIN_RESET=1"
echo "  Disable endpoint: update env ALLOW_ADMIN_RESET=0 (no code redeploy)."
