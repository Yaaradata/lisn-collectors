# LiSN Collector Diagnostic Agent

Read-only ops assistant for the collector. It answers “was this collected?”,
“why is there a gap?”, and “what did the workers do?” by querying the four
consoles an operator currently checks by hand. It **deliberately cannot**:

- write collector / warehouse / mock-source data
- trigger a collection, reset, restart, or pause
- touch GCS objects
- widen its own IAM

Chat sessions are the only writes, and they go to `agent_session` /
`agent_message` via a separate narrow role (`lisn_agent_session`). Diagnostic
reads use `lisn_agent_ro` (SELECT only).

## Five system facts

Anyone extending the tools needs these — getting them wrong produces wrong
answers even when the SQL is valid:

1. Landing table is `sentinel_raw.incidents_v2`; current view is
   `sentinel_core.incidents_current`.
2. The Sentinel export is thread-exploded (~2.5 rows per incident).
   `count(*)` is **never** an incident count — always `COUNT(DISTINCT id)`.
3. Raw is append-only. The same incident collected five times is five rows and
   that is correct, not a duplicate defect. A copies ratio above 1 is expected.
4. A discovery window that hits its `id_count` cap is `status='partial'` and
   covers only part of its range even when calendar boundaries look continuous.
5. `sentinel_mock` is the source; the collector database is operational state
   (requests, jobs, windows); BigQuery is the warehouse.

## Tool inventory

| Tool | One line |
|---|---|
| `diagnose_incident` | Deterministic “was X collected / why not?” chain — prefer this first |
| `diagnose_time_range` | Coverage + missing counts for a time window |
| `explain_gap` | Classify a known gap (never scheduled / truncated / failed / …) |
| `check_incident_collected` | Warehouse presence for one id |
| `get_collection_stats` | Distinct-incident counts / copies ratio for a range |
| `compare_source_to_warehouse` | Source vs warehouse shortfall for a range |
| `get_discovery_windows` | Rows from `discovery_window` |
| `get_failed_jobs` | Dead / failed `collector_job` rows |
| `get_request_status` | One `collector_request` + its pages |
| `get_worker_history` | Recent worker / job attempt history |
| `search_logs` | SigNoz log search (`POST /api/v5/query_range`) |
| `get_traces_for_request` | SigNoz traces for a request id |
| `get_metric` | SigNoz metric timeseries |
| `get_job_executions` | Cloud Run Job execution history (read-only) |

## Diagnostic HTTP endpoints

Useful **independently of chat** — same chains the agent tools call:

| Method | Path |
|---|---|
| `GET` | `/v1/diagnose/incident/{id}` |
| `GET` | `/v1/diagnose/range?from=…&to=…` |
| `GET` | `/v1/diagnose/gap?from=…&to=…` |
| `GET` | `/health` / `/health/sources` |
| `POST` | `/v1/chat` |
| `GET`/`DELETE` | `/v1/chat/{session_id}` |

## MODEL_PROVIDER — UNRESOLVED

`MODEL_PROVIDER` defaults to **`vertex`**. That is a **default, not a
decision**.

| Provider | Where prompts/results go |
|---|---|
| `vertex` | Stay inside `clariversev1` (Gemini via Vertex AI) |
| `anthropic` | Third-party API |

Collector payloads include Flipkart incident ids, order ids, and agent names.
**Confirm with the customer before anything but local testing uses a non-Vertex
provider.** Marked **UNRESOLVED** pending that data-governance confirmation.

Current Vertex defaults: `VERTEX_MODEL=gemini-2.5-flash`,
`VERTEX_LOCATION=us-central1` (Gemini publisher models are often unavailable in
`asia-south1`; BQ and Cloud Run stay on `GCP_REGION=asia-south1`).

## Service account (read-only)

**Do not reuse `collector-api`.** That identity deliberately holds only
`roles/cloudsql.client`; widening it increases the blast radius of the service
LiSN depends on.

```bash
gcloud iam service-accounts create collector-agent \
  --display-name="LiSN collector ops agent"
```

Grant, and nothing more:

| Role | Scope |
|---|---|
| `roles/cloudsql.client` | project |
| `roles/bigquery.jobUser` | project (required to run any query) |
| `roles/bigquery.dataViewer` | **dataset** `sentinel_raw` + `sentinel_core` only — not project |
| `roles/run.viewer` | project (job execution history) |
| `roles/aiplatform.user` | project — **only because** `MODEL_PROVIDER=vertex` |

Also: `roles/secretmanager.secretAccessor` on the three agent DSN secrets
(and optional `signoz-api-key`) — scoped to those secrets, **not**
`secretmanager.admin`.

Explicitly **do not** grant: `storage.objectAdmin`, `bigquery.dataEditor`,
`run.admin`, `secretmanager.admin`.

Dataset binding is applied as BigQuery dataset ACL `READER` for the agent SA
(equivalent to dataViewer at dataset scope; the `gcloud bq datasets
add-iam-policy-binding` verb is not available on this CLI track).

## Database role — structural read-only

`lisn_agent_ro` is a Postgres role with **SELECT only** on `collector` and
`sentinel_mock`. `COLLECTOR_DSN_READONLY` / `SENTINEL_MOCK_DSN_READONLY` point
at it. **This is the structural guarantee that a bug in the agent cannot write
to collector state.**

`lisn_agent_session` may `INSERT/UPDATE/DELETE` only on `agent_session` /
`agent_message` (for chat memory). It cannot touch collector tables.

Secrets (socket form for Cloud Run):

| Secret | Env var |
|---|---|
| `collector-dsn-readonly` | `COLLECTOR_DSN_READONLY` |
| `sentinel-mock-dsn-readonly` | `SENTINEL_MOCK_DSN_READONLY` |
| `agent-dsn` | `AGENT_DSN` |

Apply session DDL once:

```bash
psql "$COLLECTOR_DSN" -f sql/agent_schema.sql
```

## Run locally

```bash
cd agent/backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv/Scripts/activate
pip install -r requirements.txt

# from repo root .env — RO DSNs, not the postgres superuser
export COLLECTOR_DSN_READONLY=…
export SENTINEL_MOCK_DSN_READONLY=…
export AGENT_DSN=…          # lisn_agent_session
export GCP_PROJECT=clariversev1
export GCP_REGION=asia-south1
export MODEL_PROVIDER=vertex
export VERTEX_MODEL=gemini-2.5-flash
export VERTEX_LOCATION=us-central1

uvicorn app.main:app --host 0.0.0.0 --port 8090
```

```bash
curl -s localhost:8090/health/sources | jq .
curl -s localhost:8090/v1/diagnose/incident/IN270827PRECISION01 | jq .verdict
curl -s localhost:8090/v1/chat -H 'content-type: application/json' \
  -d '{"session_id":"demo-1","message":"Was incident IN270827PRECISION01 fetched?"}'
```

Tests (live Vertex + real BQ/SQL):

```bash
cd agent/backend && pytest tests/test_agent.py -v -s
```

## Deploy

```bash
# from repo root, .env loaded
make deploy-agent
# or: bash scripts/deploy_agent.sh
```

Deploys Cloud Run service **`collector-agent`**:

- `--min-instances=0` (request-driven; scale to zero)
- authentication **required** — never `allUsers`
- secrets via `--set-secrets` only
- image: `$REGION-docker.pkg.dev/$PROJECT/lisn/collector-agent:v1`

```bash
TOKEN="$(gcloud auth print-identity-token)"
curl -sH "Authorization: Bearer $TOKEN" "$COLLECTOR_AGENT_URL/health/sources"
```

## Cost notes

From the live E2E suite (`tests/test_agent.py`, `gemini-2.5-flash`):

| | Average | Worst case |
|---|---|---|
| Tokens / question | ~8.2k | ~35k (gap explanation) |
| BigQuery bytes scanned / question | ~800 B | ~6 KB (8-round tool loop) |

Write-refusal turns are cheapest (~3k tokens, 0 BQ). Every BQ query is capped
by `BQ_MAX_BYTES_BILLED` (default 1 GB). Chat responses can surface
`usage` / meter fields from the agent runtime; operators should treat the
table above as the known running cost, not a surprise.

## Safety rails in code

- SQL client rejects non-SELECT statements (word-boundary keyword check).
- BigQuery client refuses to run without `maximum_bytes_billed`.
- GCP client only `get_job` / `list_executions`.
- Tool exceptions are returned as `{error: …}` to the model — the graph does
  not crash into a plausible guess.
- Graph caps tool rounds at 8, then stops with an explicit note.
