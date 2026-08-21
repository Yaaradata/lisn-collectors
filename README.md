# Collectors (LiSN × Flipkart)

Collectors for the LiSN × Flipkart pilot. LiSN is a separate system that calls us; we build one collector per Flipkart source system. Sentinel is the first.

```
LiSN
  │  POST /v1/collect  (+ identity token when deployed)
  ▼
collector-api  (Cloud Run service, SA: collector-api)
  │  insert collector_request / collector_job
  │  defer fetch_page
  ▼
collector_job  (Cloud SQL `collector` DB)
  │
  ▼
Procrastinate queue  (`sentinel` / `maintenance`)
  │
  ▼
workers  (Cloud Run worker-pools OR jobs, SA: collector-sentinel)
  │  fetch → GCS raw  +  BigQuery append
  ▼
GCS  gs://lisn-raw-zone-…/raw/source=sentinel/…
BigQuery  sentinel_raw.incidents  ──view──►  sentinel_core.incidents_current
```

**Deployment surface for this sprint:** set by `make deploy-preflight` as `DEPLOY_SURFACE` in `.env`. Prefer **worker-pools** when the API is available in `asia-south1` (long-lived pull workers, no task timeout). Fall back to **jobs** when pools are unavailable (24h task ceiling; `CLOUD_RUN_TASK_INDEX` gives stable Procrastinate identity).

## Deployment table

| Component | Cloud Run surface | Instances / tasks | Service account | Command |
|---|---|---|---|---|
| mock-sentinel | Service | min 1 | `mock-sentinel` | `uvicorn mock.sentinel_api:app …` |
| collector-api | Service | min 1 | `collector-api` | `uvicorn collector.api:api …` |
| sentinel workers | worker-pool `wp-col-sentinel` **or** job `col-sentinel` | 3 | `collector-sentinel` | `python -m procrastinate worker -q sentinel -c 1 --delete-jobs never` |
| maintenance worker | worker-pool `wp-col-maintenance` **or** job `col-maintenance` | 1 | `collector-sentinel` | `python -m procrastinate worker -q maintenance -c 1 --delete-jobs never` |

One image (`$IMG`) serves every process — no default `CMD`; each deploy supplies the command.

## Naming convention

| Resource | Pattern | Sentinel | eKart (source #2) |
|---|---|---|---|
| Service account | `collector-<source>` | `collector-sentinel` | `collector-ekart` |
| Queue | `<source>` | `sentinel` | `ekart` |
| Worker pool / job | `wp-col-<source>` / `col-<source>` | `wp-col-sentinel` / `col-sentinel` | `wp-col-ekart` / `col-ekart` |
| Raw dataset | `<source>_raw` | `sentinel_raw` | `ekart_raw` |
| Core dataset | `<source>_core` | `sentinel_core` | `ekart_core` |
| Landing table | `<source>_raw.<entity>` | `sentinel_raw.incidents` | `ekart_raw.<entity>` |
| Current view | `<source>_core.<entity>_current` | `sentinel_core.incidents_current` | `ekart_core.<entity>_current` |
| GCS prefix | `raw/source=<source>/...` | `raw/source=sentinel/...` | `raw/source=ekart/...` |

Shared across sources: one Cloud SQL instance, one `collector` database (jobs carry a `source` column), one GCS bucket partitioned by prefix, one sweeper.

## Running locally

```bash
gcloud auth login
gcloud auth application-default login
# Cloud SQL Auth Proxy on 5432
make mock-run          # :8081
make api               # :8080
make worker            # sentinel -c 1
make sweeper           # maintenance
make e2e               # Sprint 3 gate
make demo              # paced local runbook
make failure-demos     # Sprint 4
```

Local DSNs use `127.0.0.1` via the proxy. Keep `SENTINEL_URL=http://127.0.0.1:8081` for laptop runs (deploy scripts comment a restore line in `.env`).

## Running deployed

```bash
make deploy-preflight  # sets DEPLOY_SURFACE
make grant-invoker     # mock ingress=all + run.invoker for worker SA
make image             # or included in deploy-services
make deploy-services   # mock-sentinel + collector-api
make deploy-workers    # pools or jobs per DEPLOY_SURFACE
make e2e-cloud         # Sprint 5 gate (same pytest files + identity token)
make measure-rate      # ~180 req/min ±20% with 3 workers
make demo-cloud        # paced Cloud Run runbook
make workers-status    # / scale / stop / logs
bash scripts/32_trace_s5.sh
```

Demo API calls use `Authorization: Bearer $(gcloud auth print-identity-token)`. In production LiSN would use its own SA with `roles/run.invoker`.

## Adding a new source

1. Add one file under `collector/sources/` implementing the collector contract (`plan` / `fetch` / `parse`).
2. Register it with one line in `collector/sources/__init__.py` (`REGISTRY`).
3. Create two BigQuery objects: `<source>_raw.<entity>` (landing) and `<source>_core.<entity>_current` (view).
4. Create one service account `collector-<source>` with least-privilege roles (Cloud SQL, GCS prefix, BQ datasets).
5. Deploy one worker surface (`wp-col-<source>` or `col-<source>`) on its own queue with `-c 1`.

## Reliability (Sprint 4)

Two recovery layers: Procrastinate heartbeats/stalled jobs (Layer A) and `collector_job` leases (Layer B). `GET /v1/reconcile` finds the only silent failure (raw in GCS, never loaded). Kill switches: `make pause SOURCE=sentinel` (flag — workers stay up) vs `make workers-stop` (scale to zero — stop paying for idle workers).

## Open Flipkart questions

| # | Question | Notes |
|---|---|---|
| **Q1** | Does Sentinel expose a callable API, or only a console Download button? | **Changes shape** — our `fetch()` is the only method that would switch from HTTP to file-drop. |
| Q2 | Confirm the 50-id-per-call cap for Sentinel (assumed from Multi Track UI). | Drives `batch_cap` / page count. |
| Q3 | Authoritative rate ceiling and burst rules per client. | We quote `workers × 1/min_interval_s`; measure with `make measure-rate`. |
| Q4 | How will LiSN authenticate to our request API in production? | Demo uses a human identity token; production needs LiSN’s SA + `run.invoker`. |
| Q5 | Stable primary key for thread-exploded rows (`id` + `threads.id`)? | Composite key must survive warehouse upserts. |

## Known follow-ups

- **VPC egress** (Direct VPC or connector) is required to reach real Flipkart RFC1918 systems (Sentinel `10.24.1.91`, Multi Track `10.24.2.16`); ID tokens alone are not enough.
- If `DEPLOY_SURFACE=jobs`, schedule **daily re-execution** before the 24h task timeout; in-flight work at kill is recovered by the sweeper.
- Narrow BigQuery roles from **project** scope to **dataset** scope (`sentinel_raw` / `sentinel_core` only).
- Replace streaming inserts with **batch loads** when volume grows.
- No connection pooler yet (Cloud SQL Proxy / connector only).

## Line endings

`.gitattributes` forces LF for shell/Python/SQL/Makefile. CRLF shell scripts fail in Linux containers.

## How to tear down

Destructive and intentional — secrets, SAs, BQ datasets, GCS objects, Artifact Registry, then Cloud SQL. **Warning:** a deleted Cloud SQL instance name cannot be reused for about a week.

## Cost note

**Cloud SQL is the largest line item and is always on.** Set a billing budget alert on the `clariversev1` billing account before leaving the instance running overnight.
