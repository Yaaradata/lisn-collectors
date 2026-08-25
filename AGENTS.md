# AGENTS.md

Collector layer for the LiSN × Flipkart forward-leg pilot. It fetches from Flipkart source systems on behalf of LiSN, lands raw bytes in GCS as evidence, and loads parsed records to BigQuery. Sentinel is the only source implemented.

## Cursor Cloud specific instructions

### What the environment gives you

`.cursor/install.sh` substitutes a local PostgreSQL for Cloud SQL and seeds 1000 mock incidents. `.cursor/environment.json` starts four terminals:

| Terminal | Port | What it is |
|---|---|---|
| `mock-sentinel` | 8081 | Fake Sentinel export. Faults at `POST /admin/fault/{id}`, call counter at `GET /admin/stats` |
| `collector-api` | 8080 | The request API LiSN calls |
| `sentinel-worker` | — | Procrastinate worker, `sentinel` queue, `-c 1` |
| `maintenance-worker` | — | Periodic sweep (cron `*/2`) and `sweep_now` |

`-c 1` on the sentinel worker is load-bearing. One worker equals one connection to the source, which is what the rate arithmetic assumes. Do not raise it to make something finish faster.

### What does not run offline

There are no GCP credentials here. `collector/raw.py` calls `storage.Client()` and `collector/load.py` calls `bigquery.Client()` directly — there is no injection seam — so a page **completes `fetch()` and then fails at the GCS write.** Everything downstream of fetch is unreachable without either real credentials or local fakes.

| Suite | Runs here |
|---|---|
| `tests/test_sentinel_source.py` | yes |
| `tests/test_mock_sentinel.py` | yes |
| `mock/test_sentinel_api.py` | yes |
| `tests/test_raw_determinism.py` | no — needs GCS |
| `tests/test_end_to_end.py` | no — needs GCS and BigQuery |
| `tests/test_failures.py` | no — needs GCS and BigQuery |
| `make e2e`, `make failure-demos`, `make demo` | no |

If a task needs the full pipeline, either set `PROJECT`, `RAW_BUCKET` and Application Default Credentials as secrets (`install.sh` preserves those values in `.env` if already present), or build local fakes. Do not report a task complete on the strength of a fetch that never reached a sink.

### Environment quirks

- Postgres here is 16.x. The developer's machine runs 18. Record the version in any result that could depend on it.
- `.env` is written by `install.sh` and is git-ignored. `PROJECT` and `RAW_BUCKET` are left blank unless supplied as secrets.
- `install.sh` must stay idempotent — it runs on every Build, on prepared disk state.

## Settled architecture — do not reopen

These were decided with the team and are not open questions. An agent that changes one of them has broken something on purpose without knowing why it was there.

- **Procrastinate** is the queue library. Not DBOS, not Conductor, not Celery.
- **`--delete-jobs never`**, lowercase. Procrastinate 3.9 rejects other casing.
- **Mark done only after the BigQuery write commits.** Rows are written before jobs are deferred. Both orderings are deliberate and both are load-bearing for recovery.
- **BigQuery merge key is `(id, threads_id)`.** The Sentinel export is thread-exploded at a measured factor of ~2.48. Partitioning on `id` alone silently discards conversation history.
- **Dataset naming is per-source**: `sentinel_raw`, `sentinel_core`. Not `lisn_raw` / `lisn_core`.
- **Cloud Run Jobs, not worker pools.** Worker pools are unavailable in `asia-south1`. `DEPLOY_SURFACE=jobs` with `--tasks=3` and `CLOUD_RUN_TASK_INDEX` for stable executor identity.
- **Read-only against Flipkart systems.** Nothing in this codebase may write to a source system.
- **Raw bytes are stored exactly as received.** They are the evidence of what the source returned. Do not normalise, reformat, or pretty-print before writing to GCS.

## Audit mode

If the task is an acceptance audit (see `LiSN_Collectors_Audit_Protocol_v1.md`), these rules override normal helpfulness:

1. **Read-only on the code under test.** No edits to `collector/`, `sql/`, `scripts/`, `Makefile`, `Dockerfile`, `requirements.txt`. A test that fails is the deliverable. Fixing the defect and reporting a pass is a false report.
2. **New files only in `tests/audit/`**, plus fault knobs under `/admin/` in `mock/sentinel_api.py`. List every mock change in the report.
3. **PASS, FAIL or BLOCKED.** No "partial". No GCP is BLOCKED, never FAIL, and never a fake result presented as real.
4. **Evidence per test** under `tests/audit/evidence/<TEST-ID>.log`: the command, the raw output, the SQL or HTTP state that proves the assertion.
5. **No fixes, no refactors, no drive-by cleanups.** The developer decides how to fix. Prescribing a fix from outside the context that produced the design is how good architecture gets damaged by a reviewer with less information than the author.
