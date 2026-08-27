# LiSN Collector Deployed Acceptance Report v1

## 1) Run scope

- Environment: deployed GCP stack (`clariversev1`), Cloud Run Jobs + Cloud SQL + GCS + BigQuery.
- This report uses only executed evidence from:
  - Phase 0 artifacts under `/opt/cursor/artifacts/`
  - `tests/deployed/evidence/*.log` for sections `A`, `C`, `D`, `E`, `F`
  - Section `B` measurement artifacts under `/opt/cursor/artifacts/section-b-*.json`
- Postgres version observed in this run context: `PostgreSQL 16.15` (from test evidence).
- No remediation/fix prescriptions are included; this report is descriptive only.

## 2) Executed sections and outcomes

- Phase 0 (`P-1..P-4`): executed (artifacts present).
- Section A: evidence present for `A-1`, `A-2`, `A-3`, `A-4`, `A-5`, `A-7`.
- Section B: `B-1..B-6` measured via deployed measurement artifacts.
- Section C: `C-1..C-4` executed (`4 passed`).
- Section D: executed `D-1`, `D-2`, `D-3`, `D-6`, `D-7`, `D-8`, `D-9`, `D-11`, `D-12` (`9 passed`).
- Section E: executed `E-1..E-6` with `E-6` blocked/skip (`5 passed, 1 skipped`).
- Section F: executed `F-1`, `F-2`, `F-3`, `F-5` (`F-4` excluded).

## 3) Protocol Section-6: eight questions answered in order (number + test ID)

1. **Q1 — Does one-page end-to-end on deployed sinks produce rows?**  
   **Answer:** Yes. `row_count=4`.  
   **Test/ID:** `P-3` (`request_id=458b7f3b-d71b-4500-aa00-2503523f614b`).

2. **Q2 — What is request-accepted to queryable latency on deployed sinks?**  
   **Answer:** `3.6767189502716064s`.  
   **Test/ID:** `P-4` (same `P-3` request path).

3. **Q3 — Is identity-set completeness preserved on a 1000-incident enrichment run?**  
   **Answer:** Yes. `truth_identity_count=2477`, `observed_identity_count=2477`.  
   **Test/ID:** `A-2`.

4. **Q4 — Is discovery→enrichment balance closed (no unaccounted discovered IDs)?**  
   **Answer:** Yes. `discovered=1788`, `enriched=1788`, `pending_of_discovered_after=0`, `balance=0`.  
   **Test/ID:** `A-5`.

5. **Q5 — What is measured throughput at 3 tasks against an instant source?**  
   **Answer:** `48.0 pages/min/task` (`144.0 pages/min total at 3 tasks`).  
   **Test/ID:** `B-2`.

6. **Q6 — Is there a global ceiling in the 1→3 task range?**  
   **Answer:** No ceiling observed in tested range (`ratio_2x=1.977273`, `ratio_3x=2.954541`, `global_ceiling_exists=False`).  
   **Test/ID:** `C-2` (with rates from `C-1`).

7. **Q7 — Do long-run resilience timing constraints hold for D-series long cases?**  
   **Answer:**  
   - `D-11` completed to conclusion within cap: `547.578s` (`<= 1800s`).  
   - `D-12` hold floor met: `242.009s` with 15-second sampling (`sample_count=15`).  
   **Test/ID:** `D-11`, `D-12`.

8. **Q8 — What is the measured 30-minute population ceiling vs DS-2 scale?**  
   **Answer:** `derived_population_ceiling_30m=193484` vs `ds2_population=299190`, margin `-105706`.  
   **Test/ID:** `F-3`.

## 4) Defects (blocking / P1 / P2 / P3)

### Blocking

1. **Nonexistent incident request can complete as success with zero records and no error surface.**  
   Evidence: `E-1` logs `counts={'done': 1}`, `records=0`, explicit observation text: `nonexistent incident returns done:1, records:0 with no error`.

2. **Byte-for-byte fidelity drift between source and warehouse representation for large numeric identities and timestamps.**  
   Evidence: `orderitem-top5-byte-compare.json` shows `byte_for_byte_equal=false` for all top-5 largest `orderItemId` cases, with scientific-notation and timestamp-format drift examples.

### P1

1. **30-minute capacity is below DS-2 population.**  
   Evidence: `F-3` margin `-105706`.

2. **Single-page latency during active sweep can be very high.**  
   Evidence: `F-2` probe latency `371.525s` while sweep is in progress; `D-7` shows `177.119s` probe latency during sweep.

3. **Worker interruption scenarios require intervention to recover flow.**  
   Evidence: `D-8` and `D-9` classified `scenario_status=needed_intervention`.

### P2

1. **Fault-mode payload resilience case is blocked in deployed environment due missing mock admin endpoint.**  
   Evidence: `E-6` blocked with `HTTP 404` for `/admin/payload-fault`; test marked skipped.

2. **Injected source-fault scenario yields terminal dead page(s).**  
   Evidence: `E-4` terminal counts include dead page (`{'dead': 1, 'done': 1}`), `scenario_status=lost_data` under injected persistent source fault.

### P3

1. **No additional P3 defect recorded from executed deployed evidence in this report window.**

## 5) Deployment findings (observed from production logs; not test-produced)

From `docs/deployed/prior_local_findings.md`:

1. **Workers terminate at the 24-hour Cloud Run task ceiling with no restart. (P1)**  
   - Observed executions: `col-sentinel-j4wkc`, `col-sentinel-discovery-z82fd` hitting timeout ceiling.  
   - No automatic restart behavior documented in worker deployment path.

2. **Deployed workers do not survive database unavailability. (P1)**  
   - `col-maintenance-qmd84` failed during Cloud SQL invalid state / pool initialization failure sequence.  
   - Failed task did not auto-restart.

3. **Periodic sweep is deferred by every worker, not only maintenance worker. (P2)**  
   - Sweep defer behavior observed in both sentinel and discovery worker logs.

## 6) NOT RUN tests (ID + reason)

1. **D-4 — NOT RUN**  
   Reason: explicitly excluded by instruction for this run.

2. **D-5 — NOT RUN**  
   Reason: explicitly excluded by instruction for this run (and Cloud SQL admin capability intentionally constrained in this environment).

3. **D-10 — NOT RUN**  
   Reason: explicitly excluded by instruction for this run.

4. **E-6 (full payload-fault execution path) — NOT RUN / BLOCKED**  
   Reason: deployed mock does not expose `/admin/payload-fault` endpoints (`HTTP 404`), so required per-mode fault injection could not be executed on deployed.

5. **F-4 — NOT RUN**  
   Reason: explicitly excluded by instruction for this run (`do not call admin delete endpoint`).

6. **A-6 — NOT RUN in this report window**  
   Reason: no executed deployed evidence artifact was produced for `A-6` in this reporting set.

## 7) Notes

- This report intentionally does **not** prescribe fixes.
- Results are bounded to executed and evidenced tests/artifacts listed above.
