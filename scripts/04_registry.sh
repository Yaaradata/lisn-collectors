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

need gcloud

: "${PROJECT:?PROJECT is required in .env}"
: "${REGION:?REGION is required in .env}"

REPO_NAME="lisn"
IMG_VALUE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO_NAME}/collector:v1"
EXPECTED_FULL_PATH="projects/${PROJECT}/locations/${REGION}/repositories/${REPO_NAME}"

echo "## Registry setup $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# STEP 1 — Artifact Registry Docker repository
# ---------------------------------------------------------------------------
ok "STEP 1 — Artifact Registry repository ${REPO_NAME}"
# One repository serves every collector. They all ship from the same image —
# only the queue argument and the environment differ per source.

if gcloud artifacts repositories describe "$REPO_NAME" \
  --location="$REGION" \
  --project="$PROJECT" >/dev/null 2>&1; then
  warn "Artifact Registry repository ${REPO_NAME} already exists; skipping create"
  trace_line "WARN" "repo:${REPO_NAME}" "ALREADY EXISTS"
else
  gcloud artifacts repositories create "$REPO_NAME" \
    --repository-format=docker \
    --location="$REGION" \
    --project="$PROJECT" \
    --description="LiSN collector images"
  ok "Created Artifact Registry repository ${REPO_NAME}"
  trace_line "PASS" "repo:${REPO_NAME}" "CREATED"
fi

# ---------------------------------------------------------------------------
# STEP 2 — Append IMG to .env
# ---------------------------------------------------------------------------
ok "STEP 2 — Write IMG to .env"
upsert_env "IMG" "$IMG_VALUE"
reload_env
ok "IMG=${IMG}"
trace_line "PASS" "IMG" "$IMG"

# ---------------------------------------------------------------------------
# STEP 3 — Configure Docker auth for Artifact Registry
# ---------------------------------------------------------------------------
ok "STEP 3 — Configure Docker auth"
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
ok "Docker configured for ${REGION}-docker.pkg.dev"
trace_line "PASS" "docker-auth" "${REGION}-docker.pkg.dev"

# ---------------------------------------------------------------------------
# VERIFY — do NOT build or push (Sprint 2)
# ---------------------------------------------------------------------------
ok "VERIFY — repository metadata (no image build/push)"

repo_format="$(
  gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" \
    --project="$PROJECT" \
    --format='value(format)'
)"
verify "repository format" "DOCKER" "$(printf '%s' "$repo_format" | tr '[:lower:]' '[:upper:]')"

repo_full_name="$(
  gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" \
    --project="$PROJECT" \
    --format='value(name)'
)"
verify "repository full path" "$EXPECTED_FULL_PATH" "$repo_full_name"

echo "resolved IMG=${IMG}"
trace_line "PASS" "resolved-IMG" "$IMG"
verify "IMG in .env" "$IMG_VALUE" "$IMG"

ok "Registry scaffolding complete (no image built or pushed)."
