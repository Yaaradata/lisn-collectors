# LiSN Collector Acceptance Report v1 (Regenerated)

## 1) Evidence basis and run state

- Sources: pytest outputs plus `tests/acceptance/evidence/*.log`.
- Environment: local Postgres 16.x; local mock Sentinel + collector API + workers.
- This report is descriptive only and does not prescribe fixes.

## 2) Latest section runs

### Section D

```text
3 passed in 241.34s (0:04:01)
```

- D-1 floor assertion passed (`elapsed_s=60.000075`).
- Measured calls/second:
  - 1 worker: `0.21666639740450302`
  - 3 workers: `0.6999988789867717`
  - 6 workers: `1.4666648703933212`

### Section E

```text
Correction rerun (E-6 only): FAILED
FAILED tests/acceptance/test_e.py::test_e6_garbage_payloads
1 failed in 823.25s (0:13:43)
```

- E-6 rewritten with four garbage-payload modes driven through terminal pipeline states:
  - `truncated_json`: `raw_landed=true`, `loud_failure=false`, `attempts_before_dead_letter=null`, `rest_completed=true`, `page0_final_status=pending`
  - `html_error_page` (`500 text/html`): `raw_landed=false`, `loud_failure=true`, `attempts_before_dead_letter=5`, `rest_completed=true`
  - `empty_body_200`: `raw_landed=true`, `loud_failure=true`, `attempts_before_dead_letter=5`, `rest_completed=true`
  - `incidents_string`: `raw_landed=true`, `loud_failure=false`, `attempts_before_dead_letter=null`, `rest_completed=true`
  - `E-6 elapsed_s=822.795789` (self-asserted `>10s`)
  - Loudness assertion restored and now failing: `AssertionError: E-6 mode=truncated_json failure not loud`
- E-9 executed with sink outage via temporary bad bucket override:
  - outage mode: `RAW_BUCKET=bucket-does-not-exist-e9-outage` during outage window
  - 60-second hold applied mid-run
  - recovery after restore: terminal `done=2 failed=0 dead=0`, `reconcile.unloaded=0`
  - retry evidence on affected page: `attempts=3`
- E-11:
  - 240-second hold with 15-second sampling loop
  - `recovery_latency_s=15.043799979997857`
  - `elapsed_s=240.853` (self-asserted floor)
- E-10 rewritten and re-run:
  - `time_to_dead_letter_s=95.99067213400122`
  - `calls_burned=4`
  - `elapsed_s=95.992` (self-asserted > 60s, cap 30m)
- Per-test call durations from latest Section E run:
  - `E-1` 8.43s
  - `E-2` 4.38s
  - `E-3` 10.41s
  - `E-4` 34.49s
  - `E-5` 20.45s
  - `E-6` 631.36s
  - `E-7` 8.63s
  - `E-8` 0.00s (skipped)
  - `E-9` 64.95s
  - `E-10` 96.30s
  - `E-11` 241.16s
  - `E-12` 6.40s
- E-1 duration changed from a prior `54.86s` to `8.43s` after restoring a healthy dedicated `sentinel-worker` session with trimmed `PROJECT/RAW_BUCKET` env values. The prior longer timing included worker/session state drift and is not directly comparable as a recovery-latency benchmark.

### Focused C/A/F run (requested previously)

```text
FAILED tests/acceptance/test_c.py::test_c4_hostile_query_shapes_rejected_without_5xx
1 failed, 7 passed in 430.66s (0:07:10)
```

### A/B/C full run (earlier)

```text
FAILED tests/acceptance/test_b.py::test_b1_ds4_null_thread_identities_preserved
1 failed, 8 passed in 490.74s (0:08:10)
```

### G run

```text
FAILED tests/acceptance/test_g.py::test_g7_admin_reset_in_progress_guard_then_forced_delete
1 failed, 2 passed in 106.65s (0:01:46)
```

### H run

```text
FAILED tests/acceptance/test_h.py::test_h14_order_item_ids_enrichment_works_discovery_ignores
1 failed, 14 passed in 1376.98s (0:22:56)
```

## 3) Coverage gap (protocol tests vs implemented)

Status legend: `PASS`, `FAIL`, `BLOCKED`, `NOT RUN`.

| Section | Tests in protocol | Tests implemented | Run status | NOT RUN IDs |
|---|---:|---:|---|---|
| A | 5 | 3 (`A-1..A-3`) | Partial | `A-4`, `A-5` |
| B | 5 | 3 (`B-1..B-3`) | Partial | `B-4`, `B-5` |
| C | 5 | 5 (`C-1..C-5`) | Complete | none |
| D | 4 | 3 (`D-1..D-3`) | Partial | `D-4` |
| E | 12 | 12 (`E-1..E-12`) | Partial (11 run, 1 NOT RUN) | `E-8` |
| F | 3 | 3 (`F-1..F-3`) | Complete | none |
| G | 8 | 3 (`G-5..G-7`) | Partial | `G-1`, `G-2`, `G-3`, `G-4`, `G-8` |
| H | 15 (`H-0..H-14`) | 15 | Complete | none |

### Section E NOT RUN tests (coverage gap)

- `E-8` (`database down`) — **NOT RUN**: missing precondition was isolated ability to take database down without impacting shared active environment.

## 4) Protocol section-6 questions and answers (ordered; with producing test ID)

> Note: the v2 protocol document (`LiSN_Collector_Production_Acceptance_v2_bf50.md`) is not present in this workspace snapshot, so the exact question sentences are not reproducible verbatim from repository files. Ordered answers below map to the eight section-6 slots used in this run history.

1. **Q1** — DS-3 heavy-incident identity cardinality: **500**. Test: `A-2`.
2. **Q2** — DS-3 zero-thread incident identity cardinality: **1**. Test: `A-2`.
3. **Q3** — DS-4 null-thread identity loss: **150 missing null-thread identities**. Test: `B-1`.
4. **Q4** — H-3 missing identity count across five-window union: **60 missing identities**. Test: `H-3`.
5. **Q5** — H-3 health-surface visibility for that gap: **none** (`reconcile.unloaded=0`, `dead_letter.dead=0`, `health.dead=0`, `health.unloaded=0`, failed/dead discovery jobs=`0`). Test: `H-3`.
6. **Q6** — G-7 residual raw objects after forced reset: **1521**. Test: `G-7`.
7. **Q7** — Measured throughput pages/minute/worker: **12.178848**. Test: `F-2`.
8. **Q8** — Derived max population for 30-minute window: **18268 incidents**. Test: `F-2`.

## 5) C-4 per-case status lines (requested)

From `tests/acceptance/evidence/C-4.log`:

- `case=1 status=400 payload={'detail': 'incident_ids, order_item_ids or order_ids required — no generic queries'}`
- `case=2 status=400 payload={'detail': 'incident_ids, order_item_ids or order_ids required — no generic queries'}`
- `case=3 status=400 payload={'detail': 'incident_ids, order_item_ids or order_ids required — no generic queries'}`
- `case=4 status=200 payload={'request_id': 'c2281bb3-92a9-4097-90a9-8ddaabdf101f', 'total_pages': 1, 'keys': 6}`
- `case=5 status=200 payload={'request_id': 'aeb281a7-a331-49b2-808b-75f683bbafd7', 'total_pages': 1, 'keys': 3}`
- `case=6 status=400 payload={'detail': 'incident_ids, order_item_ids or order_ids required — no generic queries'}`

Cases returning 200: **case 4 and case 5**.

- Case 4 type-confusion detail: input `{"incident_ids": "IN2608"}` returned `200` with `keys: 6`, meaning the planner iterated the string character-by-character and planned six incident IDs.
- Case 5 type-confusion detail: input `{"incident_ids": [None, 1, {}]}` returned `200` and planned those non-string values directly as IDs.

## 6) Defect list by priority

### Blocking

1. Garbage truncated JSON accepted without loud terminal surfacing (`E-6`): `truncated_json` page-0 final status is `pending` (not `dead`), loudness assertion fails, while sibling page completed.
2. Dead-letter surface exposes credential-bearing DSN text (`G-5`): forced DSN appears in `/v1/dead-letter` while endpoint is unauthenticated.
3. Null-thread identity loss in enrichment output (`B-1`): truth null identities `150`, observed `0`.
4. Silent discovery gap not surfaced (`H-3`): missing identities `60` while health/reconcile/dead-letter remain zero.
5. Unsupported discovery key silently accepted (`H-14`): status `200`, no caller-visible dropped-key signal.
6. Forced destructive reset leaves non-zero GCS raw objects (`G-7`): `gcs_objects_raw=1521`.
7. Planner type-confusion in `incident_ids` handling (`C-4`):
   - case 4 accepted string scalar and iterated it as six IDs (`keys=6`);
   - case 5 accepted `None`, `1`, and `{}` as planned IDs.

### P1

1. 30-minute capacity below DS-2 population (`F-3`): margin `-281286`.

### P2

1. Forced reset returns warnings with partial GCS delete behavior while SQL/BQ clear (`G-7` warning payload).

### P3

1. No additional P3 defect recorded from executed evidence set.

## 7) Deployment findings (GCP, clariversev1)

**Source note:** The findings in this section are **observed from production logs** (Cloud Run / Cloud SQL activity) and are **not test-produced** by the local acceptance suite.

1. **Workers terminate at the 24-hour Cloud Run task ceiling with no restart. (P1)**
   - **Evidence:** Executions `col-sentinel-j4wkc` and `col-sentinel-discovery-z82fd` both logged:
     - `Terminating task because it has reached the maximum timeout of 86400 seconds`
     - `Stop requested`
     - `Stopped worker on queues ...`
   - **Evidence:** Both executions showed clean operation over the preceding 24 hours (including 2-minute sweep logs).
   - **Evidence:** `scripts/27_deploy_workers.sh` does not include logic to restart a completed or failed execution.
   - **Operational consequence:** With a 30-minute hot-tier cycle, collection can stop daily; visible signal is a `Failed` row in Cloud Run console.

2. **Deployed workers do not survive database unavailability. (P1)**
   - **Evidence:** `col-maintenance` execution `col-maintenance-qmd84` started at `2026-08-25T16:03:55Z`, exited at `16:04:38Z`, failed with `exit code: 1`.
   - **Evidence sequence:** Cloud SQL proxy emitted `Error 409: The instance or operation is not in an appropriate state to handle the request.` with `invalidState`, followed by repeated `psycopg.pool: error connecting in 'pool-1'`, then `pool initialization incomplete after 30.0 sec`, then `Container called exit(1)`.
   - **Evidence:** Instance `lisn-collector-db` was stopped during incident window and started at `16:06:05Z` via `SqlInstancesPatchRequest body={'settings': {'activationPolicy': 'ALWAYS'}}`.
   - **Evidence:** Cloud Run Jobs did not restart failed task; worker remained down until human action.
   - **Coverage note:** This is the exact scenario targeted by `E-8` (database down), which is **NOT RUN** in the local suite.

3. **Periodic sweep is deferred by every worker, not only maintenance worker. (P2)**
   - **Evidence:** `col-sentinel` and `col-sentinel-discovery` logs both include `INFO:procrastinate.periodic:Periodic job sweep[...]` with interleaved sequence IDs (e.g., `1633`, `1634` on sentinel; `1636`, `1637` on discovery).
   - **Evidence basis:** `@app.periodic` registration occurs at import time on the shared app object, so each worker process defers sweep independent of consumed queue.
