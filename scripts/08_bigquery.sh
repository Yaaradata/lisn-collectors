#!/usr/bin/env bash

# Apply BigQuery landing SQL for a source.
#
# Usage:
#   ./scripts/08_bigquery.sh                     # sentinel enrichment (default)
#   ./scripts/08_bigquery.sh sentinel
#   ./scripts/08_bigquery.sh sentinel_discovery   # discovery table + view + bridge
#
# SOURCE may also be passed as SOURCE=sentinel_discovery.

source scripts/_common.sh

need bq
need python3

: "${PROJECT:?PROJECT is required in .env}"
: "${REGION:?REGION is required in .env}"

SOURCE_ARG="${1:-${SOURCE:-sentinel}}"

case "$SOURCE_ARG" in
  sentinel)
    SQL_SRC="sql/003_bigquery.sql"
    TABLE_ID="${PROJECT}:sentinel_raw.incidents_v2"
    VIEW_ID="${PROJECT}:sentinel_core.incidents_current"
    CLUSTER_EXPECT="id"
    ;;
  sentinel_discovery|discovery)
    SOURCE_ARG="sentinel_discovery"
    SQL_SRC="sql/007_bigquery_discovery.sql"
    BRIDGE_SQL="sql/008_discovery_to_enrich.sql"
    TABLE_ID="${PROJECT}:sentinel_raw.discovered_ids"
    VIEW_ID="${PROJECT}:sentinel_core.discovered_ids_latest"
    CLUSTER_EXPECT="incident_id"
    ;;
  *)
    fail "unknown source '${SOURCE_ARG}' (sentinel | sentinel_discovery)"
    ;;
esac

apply_sql() {
  local src="$1"
  local tmp
  tmp="$(mktemp)"
  sed "s/__PROJECT__/${PROJECT}/g" "$src" >"$tmp"
  ok "Applying ${src} for project=${PROJECT} region=${REGION}"
  bq query \
    --project_id="$PROJECT" \
    --location="$REGION" \
    --use_legacy_sql=false \
    --nouse_cache \
    <"$tmp"
  rm -f "$tmp"
  ok "Applied ${src}"
}

apply_sql "$SQL_SRC"

ok "VERIFY — table schema / partitioning (${TABLE_ID})"
table_json="$(bq show --format=prettyjson "${TABLE_ID}")"
printf '%s\n' "$table_json" | python3 -c '
import json, sys
expect_cluster = sys.argv[1]
t = json.load(sys.stdin)
fields = {f["name"]: f for f in t.get("schema", {}).get("fields", [])}
ing = fields.get("_ingested_at") or {}
default = (ing.get("defaultValueExpression") or "")
print("_ingested_at default:", default or "<none>")
part = t.get("timePartitioning") or {}
print("timePartitioning:", part)
cluster = t.get("clustering") or {}
print("clustering:", cluster)
assert part.get("field") == "_ingested_at", part
fields_cluster = cluster.get("fields") or []
assert expect_cluster in fields_cluster, (expect_cluster, fields_cluster)
' "$CLUSTER_EXPECT"

ok "VERIFY — view exists (${VIEW_ID})"
view_json="$(bq show --format=prettyjson "${VIEW_ID}")"
printf '%s\n' "$view_json" | python3 -c '
import json, sys
v = json.load(sys.stdin)
assert v.get("type") == "VIEW", v.get("type")
print("view type: VIEW")
print("view id:", v.get("id"))
'

ok "VERIFY — both objects in ${REGION}"
table_loc="$(printf '%s' "$table_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("location","").lower())')"
view_loc="$(printf '%s' "$view_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("location","").lower())')"
verify "${TABLE_ID} location" "asia-south1" "$table_loc"
verify "${VIEW_ID} location" "asia-south1" "$view_loc"

if [[ "$SOURCE_ARG" == "sentinel_discovery" ]]; then
  ok "VERIFY — bridge query (008) against empty discovery returns 0 rows"
  bridge_tmp="$(mktemp)"
  sed "s/__PROJECT__/${PROJECT}/g" "$BRIDGE_SQL" >"$bridge_tmp"
  bridge_n="$(
    bq query \
      --project_id="$PROJECT" \
      --location="$REGION" \
      --use_legacy_sql=false \
      --nouse_cache \
      --format=csv \
      --max_rows=1000000 \
      <"$bridge_tmp" \
      | tail -n +2 | grep -c . || true
  )"
  rm -f "$bridge_tmp"
  # csv with only header → 0 data lines; empty result may print nothing after header
  bridge_n="${bridge_n:-0}"
  verify "discovery_to_enrich row count (empty)" "0" "$bridge_n"
else
  ok "VERIFY — view is empty before first collection"
  row_count="$(
    bq query \
      --project_id="$PROJECT" \
      --location="$REGION" \
      --use_legacy_sql=false \
      --format=csv \
      --max_rows=1 \
      "SELECT count(*) AS n FROM \`${PROJECT}.sentinel_core.incidents_current\`" \
      | tail -n 1 | tr -d '[:space:]'
  )"
  verify "incidents_current row count before collection" "0" "$row_count"
fi

ok "BigQuery ready for source=${SOURCE_ARG}."
