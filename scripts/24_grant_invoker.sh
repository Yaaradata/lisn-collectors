#!/usr/bin/env bash

# Patch mock-sentinel for service-to-service auth (Sprint 5).
#
# PROBLEM: --ingress=internal accepts VPC traffic but NOT another Cloud Run
# service on default internet egress. The worker fails with a connection error,
# not a permission error — easy to misdiagnose.
#
# SOLUTION this sprint: --ingress=all + authentication required + ID tokens.
# Grant roles/run.invoker on mock-sentinel to the worker SA.
#
# PRODUCTION path (known future task, not a gap): real Flipkart systems live on
# RFC1918 (Sentinel 10.24.1.91, Multi Track 10.24.2.16) and are unreachable from
# default Cloud Run egress regardless of auth. Real systems will need Direct VPC
# egress or a connector.

source scripts/_common.sh

need gcloud

: "${PROJECT:?PROJECT required in .env}"
: "${REGION:?REGION required in .env}"
: "${SA_WORKER:?SA_WORKER required in .env}"

SERVICE_NAME="mock-sentinel"

ok "STEP 1 — Patch ${SERVICE_NAME} to --ingress=all (auth required)"
# Do NOT pass --allow-unauthenticated. Authentication stays required; only the
# worker SA (roles/run.invoker) plus an ID token may call the service.
# Note: some gcloud builds reject --no-allow-unauthenticated on `services update`;
# we set ingress here and strip public invokers via IAM below.
gcloud run services update "$SERVICE_NAME" \
  --project="$PROJECT" \
  --region="$REGION" \
  --ingress=all \
  --quiet

ok "Updated ingress=all"

# Ensure the service is NOT publicly invokable.
for public_member in "allUsers" "allAuthenticatedUsers"; do
  if gcloud run services get-iam-policy "$SERVICE_NAME" \
      --project="$PROJECT" --region="$REGION" \
      --flatten="bindings[].members" \
      --filter="bindings.role:roles/run.invoker AND bindings.members:${public_member}" \
      --format="value(bindings.members)" 2>/dev/null | grep -q "${public_member}"; then
    warn "Removing ${public_member} run.invoker from ${SERVICE_NAME}"
    gcloud run services remove-iam-policy-binding "$SERVICE_NAME" \
      --project="$PROJECT" \
      --region="$REGION" \
      --member="${public_member}" \
      --role="roles/run.invoker" \
      --quiet >/dev/null || true
  fi
done
ok "Unauthenticated public invokers removed (if any)"

ok "STEP 2 — Grant roles/run.invoker → ${SA_WORKER}"
gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --project="$PROJECT" \
  --region="$REGION" \
  --member="serviceAccount:${SA_WORKER}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

ok "Granted run.invoker on ${SERVICE_NAME} to ${SA_WORKER}"

ok "VERIFY — IAM policy"
POLICY="$(
  gcloud run services get-iam-policy "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$REGION"
)"
echo "$POLICY"
if ! printf '%s' "$POLICY" | grep -q "roles/run.invoker"; then
  fail "roles/run.invoker missing from ${SERVICE_NAME} IAM policy"
fi
if ! printf '%s' "$POLICY" | grep -q "${SA_WORKER}"; then
  fail "${SA_WORKER} missing from ${SERVICE_NAME} IAM policy"
fi
ok "IAM policy includes run.invoker for ${SA_WORKER}"

ok "VERIFY — ingress setting"
INGRESS="$(
  gcloud run services describe "$SERVICE_NAME" \
    --project="$PROJECT" \
    --region="$REGION" \
    --format='value(metadata.annotations[run.googleapis.com/ingress])'
)"
# Newer gcloud may surface ingress as a top-level field.
if [[ -z "$INGRESS" ]]; then
  INGRESS="$(
    gcloud run services describe "$SERVICE_NAME" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format='value(spec.template.metadata.annotations[run.googleapis.com/ingress])' 2>/dev/null || true
  )"
fi
if [[ -z "$INGRESS" ]]; then
  INGRESS="$(
    gcloud run services describe "$SERVICE_NAME" \
      --project="$PROJECT" \
      --region="$REGION" \
      --format=yaml 2>/dev/null | awk '/^  ingress:/{print $2; exit}'
  )"
fi
echo "ingress=${INGRESS}"
if [[ "$INGRESS" != "all" ]]; then
  fail "expected ingress=all, got '${INGRESS:-<empty>}'"
fi
ok "ingress=all confirmed"

ok "grant-invoker complete — worker must set USE_ID_TOKEN=1 on Cloud Run"
