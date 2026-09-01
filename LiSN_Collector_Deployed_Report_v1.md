# LiSN Collector Deployed Acceptance Report v1 (Updated after Batch 1 + Batch 2 reruns)

## 1) Run scope

- Environment: deployed GCP stack (`clariversev1`), Cloud Run Jobs + Cloud SQL + GCS + BigQuery.
- Source boundary: source under test is the deployed mock (`mock-sentinel`), not production Flipkart Sentinel.
- Throughput/rate caveat: all measured rates are against an instant mock source.
- Evidence sources:
  - `tests/deployed/evidence/*.log` (protocol sections A/C/D/E/F and prior Q3 gap evidence)
  - `docs/deployed/artifacts/*.json` (committed one-off measurement artifacts for prior B/Q3 runs)
- Postgres version captured in current run context: `PostgreSQL 16.15`.

## 2) Batch execution summary

### Batch 1 (fix retest) — status

- Numeric precision retest (seeded `9007199254740991`, `9007199254740993`, `1234567890123456789`): confirmed fixed in deployed path; values preserved in `sentinel_raw.incidents_v2`.
- Window contiguity guard retest: non-contiguous submit without `allow_gap` was refused (`409`), and explicit gap submission (`allow_gap=true`) was surfaced by both `/v1/discovery/gaps` and `/v1/health/detail`.
- Malformed payload retest (all four modes): `truncated_json`, `html_error_page`, `empty_body_200`, `incidents_string` all terminated as dead-letter outcomes, with no successful record ingestion for malformed payloads.
- Results endpoint retest (`/v1/requests/{id}/results`): confirmed fixed to `incidents_v2` path; request counts and `bigquery_rows` matched in one-page proof.

### Batch 2 (regression rerun) — status

- Section A rerun: `7 passed`.
- Section C rerun: `4 passed`.
- Section A harness was aligned to current serving table (`sentinel_raw.incidents_v2`) and current key-type contract for `order_item_ids` (string identifiers).

## 3) Protocol section-6 questions (current status after Batch 1 + Batch 2)

1. **Does it lose data? Records in versus out, at 1,000 and at population.**  
   **Answer:** In rerun evidence, no loss at 1,000 (`2477 == 2477`) and no loss in the 5,000 sample (`12575 == 12575`) from a discovered population of `246412`; full-population equality remains unmeasured.  
   **Evidence:** `A-2.log`, `A-3.log`.

2. **Does it lose data between stages? Discovered minus enriched minus pending.**  
   **Answer:** No stage-gap observed in rerun (`discovered=1788`, `enriched=1788`, `pending=0`, `balance=0`).  
   **Evidence:** `A-5.log`.

3. **Can a scheduling mistake lose data invisibly? How many records, and does anything detect it.**  
   **Answer:** Prior pre-fix deployed run proved silent gap loss (`missing=104`, no signal on operator surfaces). Batch 1 retest shows current deployment now rejects non-contiguous discovery windows unless explicitly allowed, and explicit gaps are surfaced via `/v1/discovery/gaps` and `/v1/health/detail`.  
   **Evidence:** prior committed artifact `docs/deployed/artifacts/deployed-gap-test-q3.json` + Batch 1 rerun outcomes.

4. **Does it change data? Fields compared, fields mismatched.**  
   **Answer:** Pre-fix deployed behavior mutated high-magnitude numeric IDs. Batch 1 retest confirms this is fixed in current deployed path for seeded probe values (`9007199254740991`, `9007199254740993`, `1234567890123456789`) in `incidents_v2`.  
   **Evidence:** prior committed artifact `docs/deployed/artifacts/orderitem-precision-value-compare.json` + Batch 1 rerun outcomes.

5. **Is it fast enough for a 30-minute cycle, and up to what population?**  
   **Answer:** No new Batch 1/2 rerun replaced prior sustained-capacity conclusion; sustained 30-minute ceiling remains below DS-2 population in previously committed B/F evidence.  
   **Evidence:** `section-b-throughput-v2.json`, historical `F-3.log`.

6. **Does it stay inside a rate limit, and is anything enforcing one?**  
   **Answer:** Batch 2 C rerun reproduces linear scaling in tested range (1/2/3 tasks), with no observed global ceiling in that range; this is not proof that no ceiling exists outside tested load.  
   **Evidence:** `C-1.log`, `C-2.log`, `C-4.log`.

7. **Does it recover without a human? Per scenario: recovered / recovered with delay / needed intervention / lost data.**  
   **Answer:** Unattended recovery remains unmeasured. Executed induced-failure runs either performed manual restarts (worker cancellation tests) or drove pages to terminal dead states.  
   - recovered with delay: observed in prior `E-6` mixed-mode history.  
   - needed intervention: worker cancellation/restart and permanent fault scenarios.  
   - lost data: not newly proven in Batch 1/2 reruns.  
   **Evidence:** `E-2.log`, `E-3.log`, `E-4.log`, `E-6.log`, `D-8.log`, `D-9.log`.

8. **Can an operator tell when it is broken? Per surface: request finished, page stuck, silent failure, window gap, data lag.**  
   **Answer:**  
   - request-finished surface works (`/counts`, `/results`) in current deployment.  
   - dead-letter/auth surface exists and is IAM-protected.  
   - window-gap visibility is now surfaced in current deployment (Batch 1 retest), whereas prior run showed invisibility under pre-fix behavior.  
   - dedicated page-stuck and data-lag protocol scenarios remain not rerun in Batch 2.  
   **Evidence:** `E-1.log`, `E-5.log`, prior `Q3-gap.log`, Batch 1 rerun outcomes.

## 4) Batch 1 + Batch 2 finding status

### Resolved in current deployed retest window

1. Numeric precision mutation for high-magnitude IDs (resolved in `incidents_v2` path).
2. Discovery-window gap guard/visibility defect (guard + surfaced gaps now present).
3. Malformed `incidents_string` accepted-as-success behavior (now terminal dead-letter path).
4. `/v1/requests/{id}/results` wrong-table lookup P1 (now aligned with `incidents_v2` in one-page proof).

### Still open / not replaced by Batch 2 rerun

1. Sustained 30-minute capacity shortfall vs DS-2 scale (no new sustained-capacity rerun in Batch 2).
2. Elevated single-request latency under concurrent backlog pressure (no new D/F latency rerun in Batch 2).
3. Unattended autonomous recovery remains unmeasured (manual intervention present in existing induced-failure tests).

## 5) Batch 2 detailed regression evidence

### Section A

- `A-1`: single-incident equality passed (`4 == 4`).  
- `A-2`: 1,000-input equality passed (`2477 == 2477`).  
- `A-3`: sample-population equality passed (`12575 == 12575`, sample size 5000).  
- `A-4`: discovery-window ID equality passed (`1784 == 1784`).  
- `A-5`: discovery-to-enrichment balance passed (`balance=0`).  
- `A-6`: null-thread incident passed (`observed=(id, None)`).  
- `A-7`: `order_item_ids` retrieval passed (`4 == 4`).  

Evidence: `tests/deployed/evidence/A-1.log` … `A-7.log`.

### Section C

- `C-1` (60s floor):  
  - tasks=1: `0.683332` calls/sec  
  - tasks=2: `1.366664` calls/sec  
  - tasks=3: `2.066663` calls/sec
- `C-2`: `ratio_2x=2.000000`, `ratio_3x=3.024390`, `global_ceiling_exists=False` (tested range).
- `C-3`: rolling-redeploy analogue peak `4.306387` calls/sec (`2.083739x` baseline).
- `C-4`: concurrent independence held (`concurrent_over_sum_ratio=1.014706`).

Evidence: `tests/deployed/evidence/C-1.log` … `C-4.log`.

## 6) Notes

- This report is descriptive only; no prescriptive fix plan is included.
- Where this file references Batch 1 rerun outcomes not yet mirrored by a committed JSON artifact in `docs/deployed/artifacts/`, those outcomes are intentionally marked as current deployed retest results from the same frozen test window.
