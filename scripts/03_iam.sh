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

reload_env() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}

ensure_sa() {
  local name="$1"
  local display="$2"
  local email="${name}@${PROJECT}.iam.gserviceaccount.com"

  if gcloud iam service-accounts describe "$email" --project="$PROJECT" >/dev/null 2>&1; then
    warn "Service account ${name} already exists; skipping create" >&2
    trace_line "WARN" "sa:${name}" "ALREADY EXISTS"
  else
    gcloud iam service-accounts create "$name" \
      --project="$PROJECT" \
      --display-name="$display"
    ok "Created service account ${email}" >&2
    trace_line "PASS" "sa:${name}" "CREATED"
  fi
  printf '%s\n' "$email"
}

bind_project_role() {
  local member="$1"
  local role="$2"
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${member}" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
  ok "Bound ${role} → ${member}"
  trace_line "PASS" "role:${role}" "${member}"
}

ensure_secret_with_value() {
  local secret_id="$1"
  local value="$2"

  if gcloud secrets describe "$secret_id" --project="$PROJECT" >/dev/null 2>&1; then
    warn "Secret ${secret_id} already exists; adding new version"
    printf '%s' "$value" | gcloud secrets versions add "$secret_id" \
      --project="$PROJECT" \
      --data-file=- >/dev/null
    trace_line "WARN" "secret:${secret_id}" "NEW VERSION ADDED"
  else
    gcloud secrets create "$secret_id" \
      --project="$PROJECT" \
      --replication-policy=automatic >/dev/null
    printf '%s' "$value" | gcloud secrets versions add "$secret_id" \
      --project="$PROJECT" \
      --data-file=- >/dev/null
    ok "Created secret ${secret_id}"
    trace_line "PASS" "secret:${secret_id}" "CREATED"
  fi
}

grant_secret_accessor() {
  local secret_id="$1"
  local sa_email="$2"
  gcloud secrets add-iam-policy-binding "$secret_id" \
    --project="$PROJECT" \
    --member="serviceAccount:${sa_email}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
  ok "secretAccessor on ${secret_id} → ${sa_email}"
  trace_line "PASS" "secret-iam:${secret_id}" "${sa_email}"
}

need gcloud

: "${PROJECT:?PROJECT is required in .env}"

if [[ -z "${COLLECTOR_DSN_SOCKET:-}" || -z "${SENTINEL_MOCK_DSN_SOCKET:-}" ]]; then
  fail "COLLECTOR_DSN_SOCKET and SENTINEL_MOCK_DSN_SOCKET must be set in .env (run scripts/01_database.sh first)."
fi

echo "## IAM setup $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# STEP 1 — Three service accounts
# ---------------------------------------------------------------------------
ok "STEP 1 — Service accounts"
# One service account PER SOURCE is deliberate. eKart will get collector-ekart,
# FDP will get collector-fdp, and each will be able to reach only its own source
# credential and its own BigQuery datasets. This is the read-only posture
# expressed in IAM rather than in a document.

SA_WORKER_EMAIL="$(ensure_sa "collector-sentinel" "Sentinel collector worker")"
SA_API_EMAIL="$(ensure_sa "collector-api" "Request API that LiSN calls")"
SA_MOCK_EMAIL="$(ensure_sa "mock-sentinel" "Fake Sentinel service")"

upsert_env "SA_WORKER" "$SA_WORKER_EMAIL"
upsert_env "SA_API" "$SA_API_EMAIL"
upsert_env "SA_MOCK" "$SA_MOCK_EMAIL"
reload_env

ok "SA_WORKER=${SA_WORKER}"
ok "SA_API=${SA_API}"
ok "SA_MOCK=${SA_MOCK}"
trace_line "PASS" "SA_WORKER" "$SA_WORKER"
trace_line "PASS" "SA_API" "$SA_API"
trace_line "PASS" "SA_MOCK" "$SA_MOCK"

# ---------------------------------------------------------------------------
# STEP 2 — Roles, least privilege
# ---------------------------------------------------------------------------
ok "STEP 2 — Project roles (least privilege)"
# bigquery.dataEditor writes the rows, bigquery.jobUser runs the load job.
# Both are required, and missing the second produces a confusing permission
# error at the very last step of the pipeline.
#
# TODO(later-sprint): narrow BigQuery roles from project level to dataset
# level, so collector-sentinel can write sentinel_raw but not ekart_raw.

WORKER_ROLES=(
  "roles/cloudsql.client"
  "roles/storage.objectAdmin"
  "roles/bigquery.dataEditor"
  "roles/bigquery.jobUser"
)

for role in "${WORKER_ROLES[@]}"; do
  bind_project_role "$SA_WORKER" "$role"
done

bind_project_role "$SA_API" "roles/cloudsql.client"
bind_project_role "$SA_MOCK" "roles/cloudsql.client"

# ---------------------------------------------------------------------------
# STEP 3 — Secrets from socket DSNs (new version if secret exists)
# ---------------------------------------------------------------------------
ok "STEP 3 — Secrets"
ensure_secret_with_value "collector-dsn" "$COLLECTOR_DSN_SOCKET"
ensure_secret_with_value "sentinel-mock-dsn" "$SENTINEL_MOCK_DSN_SOCKET"

# ---------------------------------------------------------------------------
# STEP 4 — Narrow secret access
# ---------------------------------------------------------------------------
ok "STEP 4 — Narrow secret access"
# mock-sentinel must NOT be able to read collector-dsn, and the collector
# accounts must NOT be able to read sentinel-mock-dsn. The mock stands in for a
# Flipkart system, so it is walled off exactly as the real one will be.

grant_secret_accessor "collector-dsn" "$SA_WORKER"
grant_secret_accessor "collector-dsn" "$SA_API"
grant_secret_accessor "sentinel-mock-dsn" "$SA_MOCK"

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
ok "VERIFY — roles, secrets, and narrow IAM"

expected_roles="$(printf '%s\n' "${WORKER_ROLES[@]}" | sort | paste -sd, -)"
actual_roles="$(
  gcloud projects get-iam-policy "$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${SA_WORKER}" \
    --format="value(bindings.role)" \
    | grep -E '^(roles/cloudsql\.client|roles/storage\.objectAdmin|roles/bigquery\.dataEditor|roles/bigquery\.jobUser)$' \
    | sort -u \
    | paste -sd, -
)"
verify "collector-sentinel roles (sorted)" "$expected_roles" "$actual_roles"

# Both secrets exist and latest version is retrievable.
for secret_id in collector-dsn sentinel-mock-dsn; do
  if gcloud secrets describe "$secret_id" --project="$PROJECT" >/dev/null 2>&1; then
    latest="$(
      gcloud secrets versions list "$secret_id" \
        --project="$PROJECT" \
        --filter="state:ENABLED" \
        --sort-by="~createTime" \
        --limit=1 \
        --format="value(name)"
    )"
    if [[ -n "$latest" ]]; then
      payload="$(
        gcloud secrets versions access latest \
          --secret="$secret_id" \
          --project="$PROJECT"
      )"
      masked="$(printf '%s' "$payload" | mask)"
      ok "secret ${secret_id} latest retrievable: ${masked}"
      if [[ "$secret_id" == "collector-dsn" ]]; then
        # Spec: print collector-dsn through mask()
        echo "collector-dsn (masked): ${masked}"
        trace_line "PASS" "collector-dsn" "$masked"
      else
        trace_line "PASS" "sentinel-mock-dsn" "latest version retrievable (value not printed)"
      fi
      verify "secret ${secret_id} exists+retrievable" "yes" "yes"
    else
      verify "secret ${secret_id} exists+retrievable" "yes" "no-enabled-version"
    fi
  else
    verify "secret ${secret_id} exists+retrievable" "yes" "missing"
  fi
done

# sentinel-mock-dsn IAM: among serviceAccount secretAccessors, only mock-sentinel.
mock_secret_sa_accessors="$(
  gcloud secrets get-iam-policy "sentinel-mock-dsn" \
    --project="$PROJECT" \
    --flatten="bindings[].members" \
    --filter="bindings.role:roles/secretmanager.secretAccessor AND bindings.members:serviceAccount:*" \
    --format="value(bindings.members)" \
    | sed 's#serviceAccount:##' \
    | sort -u \
    | paste -sd, -
)"
verify "sentinel-mock-dsn IAM SA accessors" "$SA_MOCK" "$mock_secret_sa_accessors"

ok "IAM scaffolding complete."
