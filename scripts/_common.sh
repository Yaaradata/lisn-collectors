#!/usr/bin/env bash

set -euo pipefail

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

TRACE_FILE="docs/trace/S1.md"
mkdir -p "$(dirname "$TRACE_FILE")"

ok() {
  printf '\033[0;32m[OK]\033[0m %s\n' "$*"
}

warn() {
  printf '\033[0;33m[WARN]\033[0m %s\n' "$*"
}

fail() {
  printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"
  exit 1
}

need() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    fail "Missing required command on PATH: ${cmd}"
  fi
}

verify() {
  local description="$1"
  local expected="$2"
  local actual="$3"
  local result

  if [[ "$expected" == "$actual" ]]; then
    result="PASS"
    ok "${description}: expected='${expected}' actual='${actual}'"
  else
    result="FAIL"
    warn "${description}: expected='${expected}' actual='${actual}'"
  fi

  printf '| %s | %s | %s | %s |\n' \
    "$result" "$description" "$expected" "$actual" >>"$TRACE_FILE"
}

mask() {
  sed 's/:[^:@]*@/:***@/'
}

# ---------------------------------------------------------------------------
# OpenTelemetry / SigNoz (deployed stack)
# ---------------------------------------------------------------------------

# Never put the ingestion key in .env, Dockerfile, or git. Cloud Run mounts it
# from Secret Manager: SIGNOZ_INGESTION_KEY=signoz-ingestion-key:latest
ensure_signoz_secret() {
  local secret_id="signoz-ingestion-key"
  if gcloud secrets describe "$secret_id" --project="$PROJECT" >/dev/null 2>&1; then
    ok "secret ${secret_id} exists"
    return 0
  fi
  # Create only from a file or stdin fd — never from an argv literal that could
  # land in shell history / CI logs.
  if [[ -n "${SIGNOZ_INGESTION_KEY_FILE:-}" && -f "${SIGNOZ_INGESTION_KEY_FILE}" ]]; then
    gcloud secrets create "$secret_id" \
      --project="$PROJECT" \
      --replication-policy=automatic \
      --data-file="${SIGNOZ_INGESTION_KEY_FILE}"
    ok "created secret ${secret_id} from SIGNOZ_INGESTION_KEY_FILE"
    return 0
  fi
  fail "Secret ${secret_id} missing. Rotate the key in SigNoz (chat-pasted keys are burned), then:

  printf '%s' \"\$NEW_KEY\" > /tmp/signoz-key && chmod 600 /tmp/signoz-key
  export SIGNOZ_INGESTION_KEY_FILE=/tmp/signoz-key
  # re-run this deploy script (or):
  gcloud secrets create ${secret_id} --project=${PROJECT} \\
    --replication-policy=automatic --data-file=/tmp/signoz-key
  shred -u /tmp/signoz-key 2>/dev/null || rm -f /tmp/signoz-key

Never put the key in .env, Dockerfile, or git."
}

grant_signoz_secret_accessors() {
  local secret_id="signoz-ingestion-key"
  local sa
  for sa in "$SA_API" "$SA_WORKER" "$SA_MOCK"; do
    gcloud secrets add-iam-policy-binding "$secret_id" \
      --project="$PROJECT" \
      --member="serviceAccount:${sa}" \
      --role="roles/secretmanager.secretAccessor" \
      --quiet >/dev/null
    ok "secretAccessor ${secret_id} → ${sa}"
  done
}

# Common OTel env for every component. SERVICE_VERSION should be the image digest.
otel_env_vars() {
  local service_name="$1"
  local version="${2:-unknown}"
  printf '%s' "OTEL_ENABLED=1,OTEL_EXPORTER_OTLP_ENDPOINT=ingest.us2.signoz.cloud:443,OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=https://ingest.us2.signoz.cloud/v1/logs,OTEL_LOGS_EXPORT_SCHEDULE_MS=2000,DEPLOYMENT_ENV=pilot,LOG_LEVEL=INFO,SERVICE_VERSION=${version},OTEL_SERVICE_NAME=${service_name}"
}

image_digest() {
  local digest
  digest="$(
    gcloud artifacts docker images describe "$IMG" \
      --project="$PROJECT" \
      --format='value(image_summary.digest)' 2>/dev/null || true
  )"
  if [[ -z "$digest" ]]; then
    digest="$(
      gcloud builds list --project="$PROJECT" --limit=1 \
        --format='value(results.images[0].digest)' 2>/dev/null || true
    )"
  fi
  printf '%s' "${digest:-unknown}"
}
