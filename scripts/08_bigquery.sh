#!/usr/bin/env bash

source scripts/_common.sh

need bq

: "${PROJECT:?PROJECT is required in .env}"
: "${REGION:?REGION is required in .env}"

SQL_SRC="sql/003_bigquery.sql"
TMP_SQL="$(mktemp)"
sed "s/__PROJECT__/${PROJECT}/g" "$SQL_SRC" >"$TMP_SQL"

ok "Applying BigQuery SQL for project=${PROJECT} region=${REGION}"
bq query \
  --project_id="$PROJECT" \
  --location="$REGION" \
  --use_legacy_sql=false \
  --nouse_cache \
  <"$TMP_SQL"
rm -f "$TMP_SQL"
ok "Applied ${SQL_SRC}"

ok "VERIFY — table schema / partitioning"
table_json="$(bq show --format=prettyjson "${PROJECT}:sentinel_raw.incidents")"
printf '%s\n' "$table_json" | python3 -c '
import json, sys
t = json.load(sys.stdin)
fields = {f["name"]: f for f in t.get("schema", {}).get("fields", [])}
ing = fields.get("_ingested_at") or {}
default = (ing.get("defaultValueExpression") or "")
print("_ingested_at default:", default or "<missing>")
part = t.get("timePartitioning") or {}
print("timePartitioning:", part)
cluster = t.get("clustering") or {}
print("clustering:", cluster)
assert default, "_ingested_at must have a default expression"
assert (part.get("field") == "_ingested_at") or (part.get("type") == "DAY" and part.get("field") == "_ingested_at"), part
'

ok "VERIFY — view exists and is empty before first collection"
view_json="$(bq show --format=prettyjson "${PROJECT}:sentinel_core.incidents_current")"
printf '%s\n' "$view_json" | python3 -c '
import json, sys
v = json.load(sys.stdin)
assert v.get("type") == "VIEW", v.get("type")
print("view type: VIEW")
print("view id:", v.get("id"))
'
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

ok "VERIFY — both objects in ${REGION}"
table_loc="$(printf '%s' "$table_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("location","").lower())')"
view_loc="$(printf '%s' "$view_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("location","").lower())')"
verify "sentinel_raw.incidents location" "asia-south1" "$table_loc"
verify "sentinel_core.incidents_current location" "asia-south1" "$view_loc"

ok "BigQuery landing table and current view ready."
