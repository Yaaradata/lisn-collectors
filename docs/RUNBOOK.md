# Collectors runbook

## Seeding `sentinel_mock`

Single path: `python -m mock.seed_sentinel` / `make seed`.

**Precedence**

1. `N_INCIDENTS` set → exactly that many incidents (quick demo override),
   spread across `SEED_START_DATE`..`SEED_END_DATE`.
2. Otherwise date-range mode (production-like defaults):
   - `SEED_START_DATE` / `SEED_END_DATE` (default 2026-08-18 .. 2026-08-25)
   - `SEED_MIN_PER_DAY` / `SEED_MAX_PER_DAY` (default 35000 .. 40000)

Uses day-at-a-time `COPY`, drops/rebuilds `idx_sentinel_thread_incident_id`,
and `ANALYZE`s both tables at the end.

## Two-stage collection

The collector supports two query shapes on the same `plan` / `fetch` / `parse`
contract. Use them together when LiSN does not already hold incident keys.

### Shape A — Discovery (`source=sentinel_discovery`)

**When:** You need to answer *which* incidents match a console-style filter
(Create/Update window, status, issue type) before you can ask for bodies.

**Request:** `POST /v1/collect` with a filter spec (`updated_from`/`updated_to`
and/or `created_*`, optional `statuses` / `issue_names`, `limit`). Windows are
capped at 15 days (real Sentinel console rule).

**Behaviour:** `plan()` emits **one** page — discovery is sequential because
page N+1’s cursor comes from page N. The worker (`col-sentinel-discovery`,
`--tasks=1`) follows cursors inside `fetch()` (cap 10 pages / job). Landing
table: `sentinel_raw.discovered_ids` → view `sentinel_core.discovered_ids_latest`.

**Do not** scale discovery tasks in parallel; that duplicates cursor walks.

### Shape B — Enrichment (`source=sentinel`)

**When:** You already have keys (`incident_ids`, `order_item_ids`, or `order_ids`).

**Request:** `POST /v1/collect` with exactly one key list. Pages fan out
(`batch_cap=50`) across `col-sentinel` (`--tasks=3`). Landing table:
`sentinel_raw.incidents` → `sentinel_core.incidents_current`.

### The bridge (LiSN-owned SQL)

`sql/008_discovery_to_enrich.sql` returns ids in `discovered_ids_latest` that
are not yet in `incidents_current`. That decision is a **business** rule
(staleness, issue-type policy) and lives in SQL for LiSN — the collector stays
dumb and only collects what it is told.

### Why the raw zone exists (used in anger)

Landing tables can be wrong. Ours were: `orderItemId` / `orderItemUnitId` /
`threads_communicationId` were mapped through `float` / `FLOAT64` and values
above 2^53 silently mutated. We did **not** re-query Sentinel. We re-parsed
every object under `gs://$BUCKET/raw/source=sentinel/` with the corrected
`parse()` and batch-loaded `sentinel_raw.incidents_v2`, preserving
`_request_id`, `_page_no`, `_raw_uri`, and original `_ingested_at` from
`raw_manifest.written_at`. That recovery is `scripts/36_backfill_ids.py`.

The append-only raw zone is the evidence store. This is the strongest argument
for the design — a field-mapping mistake is recoverable without asking the
source again. Keep `sentinel_raw.incidents_pre_id_fix` after the swap as
evidence of what the defect produced; drop it after the pilot.

If BigQuery refuses `ALTER TABLE … RENAME` because of a streaming buffer,
`scripts/36_backfill_ids.py --swap` falls back to
`CREATE TABLE incidents_pre_id_fix AS SELECT * FROM incidents` and serves
from `incidents_v2` until the buffer drains. The collector’s `bq_table` then
targets `incidents_v2`. After the buffer clears, rename
`incidents` → `incidents_zombie`, `incidents_v2` → `incidents`, and restore
`bq_table` to `sentinel_raw.incidents`.

```bash
python scripts/36_backfill_ids.py           # backfill + reconcile
python scripts/36_backfill_ids.py --swap    # rename v2 → incidents (keeps old)
python scripts/36_backfill_ids.py --verify  # probes + fresh collect + reconcile
```

### Demo

```bash
# Apply discovery BQ objects (once)
make bigquery SOURCE=sentinel_discovery

# Deploy workers including col-sentinel-discovery
make image && make deploy-services && make deploy-workers

# Paced two-stage demo (Enter between stages); --auto for CI
bash scripts/34_two_stage_demo.sh --reset
bash scripts/34_two_stage_demo.sh --auto --reset
```

(`scripts/33_reset_collector.sh` is the shared reset path; the two-stage demo is
`34_two_stage_demo.sh`.)

## Reset the collector data

Destructive clear of **collector output only** via
`DELETE /v1/admin/collector-data` (Cloud SQL allowlist, GCS `raw/`, BQ landing
tables). Does not touch `sentinel_mock`, does not truncate
`procrastinate_workers`, and does not touch anything outside the allowlist.

`POST /v1/admin/reset` is a **deprecated alias** with the same behaviour
(parameters in the JSON body). Prefer DELETE.

### Curl (identity token) — dry run first

```bash
# Preview — dry_run defaults to true; reports before/would_delete, changes nothing
curl -sS -X DELETE \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$COLLECTOR_API_URL/v1/admin/collector-data?confirm=reset-collector-data&dry_run=true" \
  | python -m json.tool

# Real delete
curl -sS -X DELETE \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$COLLECTOR_API_URL/v1/admin/collector-data?confirm=reset-collector-data&dry_run=false" \
  | python -m json.tool

# Deprecated POST alias (still works)
curl -sS -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H 'Content-Type: application/json' \
  -d '{"confirm":"reset-collector-data","dry_run":true}' \
  "$COLLECTOR_API_URL/v1/admin/reset" | python -m json.tool
```

Or: `make reset-api` (dry run) / `make reset-api-force` (real) — both use DELETE.

Note: DELETE puts `confirm` in the query string, so it appears in Cloud Run
request logs. That token is a typo guard, not a secret; Cloud Run auth plus
`ALLOW_ADMIN_RESET` are the real controls.

### The `preserved` block

The response includes live counts when `SENTINEL_MOCK_DSN` is set on the API
process (typical for local uvicorn). Example:

```json
"preserved": {
  "sentinel_incident": 298412,
  "sentinel_thread": 741055,
  "procrastinate_workers": 5
}
```

- `sentinel_incident` / `sentinel_thread` are read-only live counts from
  `sentinel_mock`. The endpoint never writes to that database. It warns only
  if those counts *change* across the reset (not if they differ from any
  historical seed size). On Cloud Run the API usually has no mock DSN —
  those fields are then `null` and worker survival is judged from
  `procrastinate_workers` alone.
- `procrastinate_workers` is counted live from the collector DB. If it drops to
  zero, workers were harmed; do not continue demos until workers are healthy
  again (`make workers-status` / `make workers-start`).

### Safe with workers running

The endpoint deletes only Procrastinate jobs with `status <> 'doing'` and never
truncates `procrastinate_workers`. It is safe to call while Cloud Run worker
jobs are live. (Truncating workers under a live process caused
`procrastinate_jobs_worker_id_fkey` failures and `exited(1)` — that path is
deliberately absent here.)

### Mid-run refusal

If any `collector_job` is `in_progress`, the endpoint returns **409** and
deletes nothing, unless the body/query includes `force=true`. Prefer waiting for
idle (`/v1/requests/{id}/counts`) over forcing.

### Disable without a code redeploy

```bash
gcloud run services update collector-api --region=$REGION \
  --update-env-vars=ALLOW_ADMIN_RESET=0
```

Or remove the env var. The handler returns 403 when `ALLOW_ADMIN_RESET` is not
exactly `1`. Scoped IAM: `make grant-admin-reset` (bucket/dataset grants only —
not project-level `objectAdmin` / `dataEditor`).

### vs `scripts/33_reset_collector.sh`

| | `DELETE /v1/admin/collector-data` | `scripts/33_reset_collector.sh` |
|--|-----------------------------------|----------------------------------|
| Workers | Safe while live | Stops workers, waits idle, then wipes |
| `procrastinate_workers` | Preserved | Truncated |
| Use when | Demo re-run / API-driven clean slate | Full local/deployed teardown before restart |

The script is the fuller wipe. The endpoint is the safe subset for a live stack.
(`POST /v1/admin/reset` is the deprecated alias of the DELETE route.)
