# Collectors (LiSN × Flipkart)

Collectors for the LiSN × Flipkart pilot. LiSN is a separate system that calls us; we build one collector per Flipkart source system. Sentinel is the first.

## Naming convention

| Resource | Pattern | Sentinel | eKart (source #2) |
|---|---|---|---|
| Service account | `collector-<source>` | `collector-sentinel` | `collector-ekart` |
| Queue | `<source>` | `sentinel` | `ekart` |
| Worker pool | `wp-col-<source>` | `wp-col-sentinel` | `wp-col-ekart` |
| Raw dataset | `<source>_raw` | `sentinel_raw` | `ekart_raw` |
| Core dataset | `<source>_core` | `sentinel_core` | `ekart_core` |
| Landing table | `<source>_raw.<entity>` | `sentinel_raw.incidents` | `ekart_raw.<entity>` |
| Current view | `<source>_core.<entity>_current` | `sentinel_core.incidents_current` | `ekart_core.<entity>_current` |
| GCS prefix | `raw/source=<source>/...` | `raw/source=sentinel/...` | `raw/source=ekart/...` |

Shared across sources (not per-source): one Cloud SQL instance, one `collector` database (jobs carry a `source` column), one GCS bucket partitioned by prefix, one sweeper.

## What Sprint 1 creates

| Resource | Name | State after Sprint 1 |
|---|---|---|
| Cloud SQL instance | `lisn-collector-db` (POSTGRES_16, `db-g1-small`) | **running** (empty of app tables) |
| Database | `collector` | **empty** |
| Database | `sentinel_mock` | **empty** |
| GCS bucket | `lisn-raw-zone-clariversev1` | **empty** (90-day lifecycle) |
| BigQuery dataset | `sentinel_raw` | **empty** (no tables) |
| BigQuery dataset | `sentinel_core` | **empty** (no views) |
| Service account | `collector-sentinel` | **exists** |
| Service account | `collector-api` | **exists** |
| Service account | `mock-sentinel` | **exists** |
| Secret | `collector-dsn` | **exists** |
| Secret | `sentinel-mock-dsn` | **exists** |
| Artifact Registry | `lisn` (Docker) | **empty** (no image pushed) |

Project: `clariversev1` · region: `asia-south1`. This is a shared GCP project with Clariverse — every name is namespaced; scripts are idempotent.

## Raw vs core

**Raw** is append-only evidence: every page we ever fetched, proving what a query returned and when.  
**Core** is the current picture LiSN reads — one row per entity, latest wins — implemented as a view over raw, not a second copy.

They are **separate datasets** so LiSN can be granted read on `*_core` without also getting every historical fetch, and so recreating a view can never touch the raw table.

## How to run

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project clariversev1

# optional but recommended
python -m venv .venv
source .venv/Scripts/activate   # Git Bash on Windows
pip install -r requirements.txt

cp .env.example .env   # if needed; repo may already have a local .env
make all               # preflight → database → storage → iam → registry → smoke
```

Individual steps: `make preflight`, `make database`, `make storage`, `make iam`, `make registry`, `make smoke`.

## How to verify

```bash
make smoke             # Sprint 1 exit gate (shell + Python reachability)
make trace             # regenerate docs/trace/S1.md (read-only checks)
```

Gate banners:

- `SPRINT 1 GATE: PASSED — proceed to Sprint 2`
- `SPRINT 1 GATE: FAILED — <which check> — do not proceed`

## How to reset between demo runs

Safe reset (keeps infrastructure, clears demo artefacts):

- Delete any leftover smoke objects under `gs://$BUCKET/raw/source=sentinel/`
- Drop any leftover `_smoke` tables in BigQuery / Postgres if a smoke run was interrupted
- Re-run `make smoke` and `make trace`

Do **not** delete the Cloud SQL instance between demos unless you intend a full teardown.

## How to tear down

Tear down is intentional and destructive. Approximate reverse order:

```bash
# Secrets
gcloud secrets delete collector-dsn --quiet
gcloud secrets delete sentinel-mock-dsn --quiet

# Service accounts (remove IAM bindings first if required by policy)
gcloud iam service-accounts delete collector-sentinel@$PROJECT.iam.gserviceaccount.com --quiet
gcloud iam service-accounts delete collector-api@$PROJECT.iam.gserviceaccount.com --quiet
gcloud iam service-accounts delete mock-sentinel@$PROJECT.iam.gserviceaccount.com --quiet

# BigQuery datasets (empty in Sprint 1)
bq rm -r -f -d $PROJECT:sentinel_raw
bq rm -r -f -d $PROJECT:sentinel_core

# GCS bucket (empty objects first)
gcloud storage rm -r gs://$BUCKET/**

# Artifact Registry
gcloud artifacts repositories delete lisn --location=$REGION --quiet

# Cloud SQL — WARNING below
gcloud sql instances delete $INSTANCE --quiet
```

**Warning:** a deleted Cloud SQL instance **name cannot be reused for about a week**. If you delete `lisn-collector-db`, you will need a temporary new `INSTANCE` value in `.env` until the name is released.

## Not in this sprint

- No tables / views / Procrastinate schema  
- No seed or Flipkart data  
- No collector application code beyond scaffolding  
- No image build/push, no Cloud Run worker pool / job deploy  

Those land in later sprints.

## Cost note

**Cloud SQL is the largest line item and is always on** while the instance exists. Set a **billing budget alert** on the `clariversev1` billing account before leaving the instance running overnight or across demos. Artifact Registry and empty BigQuery datasets are cheap; GCS is cheap until raw volume grows; Secret Manager is negligible at this scale.
