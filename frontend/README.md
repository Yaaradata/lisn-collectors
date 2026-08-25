# LiSN — Collector Console (demo frontend)

Stand-in UI for **LiSN** (Usha’s system): shows how the collector is called
without typing curl. In production, Usha’s service calls the **same** HTTP
endpoints with its own service account — this page only exists for demos.

## How to run

1. **API auth (deployed):** do **not** put identity tokens in the browser and do
   **not** make `collector-api` publicly invokable. Proxy locally:

   ```bash
   gcloud run services proxy collector-api --region=asia-south1 --port=8080
   ```

   For a laptop API instead: `make api` (leave base URL `http://localhost:8080`).

2. **Frontend:**

   ```bash
   make frontend
   ```

3. Open **http://127.0.0.1:3000/**

No npm install, no build step.

## What each mode demonstrates

| Mode | When to use | What it shows |
|------|-------------|---------------|
| **Collect by ID** | LiSN already has keys | Enrichment only: `source=sentinel`, 50 ids/page, progress + landing proof |
| **Collect by Date Range** | LiSN does not know which incidents exist | Discovery (`sentinel_discovery`) → bridge review → enrichment — two stages on purpose |
| **Results** | After either mode finishes | API-backed tiles: pages, GCS objects, BQ rows/distinct, unloaded + last jobs (with `owner`) |
| **Admin** | Mid-demo reset / worker check | Dry-run then confirmed reset; live workers / heartbeat; zero-worker banner |

## API connection

The page stores the API base URL in `localStorage` (default
`http://localhost:8080`).

### Sample IDs / bridge

- `GET /v1/admin/sample-ids` — ids from BigQuery `sentinel_raw.incidents`
- `GET /v1/discovered/pending` — bridge (`sql/008_discovery_to_enrich.sql`)
- `GET /v1/requests/{id}/results` — concrete landing proof for one request
- `GET /v1/admin/state` — global store counts (reset / ops)

## CORS

The API allows `http://localhost:3000` and `http://127.0.0.1:3000`. Auth for
Cloud Run still goes through the gcloud proxy.
