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
- Section A: evidence present for `A-1`, `A-2`, `A-3`, `A-4`, `A-5`, `A-6`, `A-7`.
- Section B: `B-1..B-6` measured via deployed measurement artifacts.
- Section C: `C-1..C-4` executed (`4 passed`).
- Section D: executed `D-1`, `D-2`, `D-3`, `D-6`, `D-7`, `D-8`, `D-9`, `D-11`, `D-12` (`9 passed`).
- Section E: executed `E-1..E-6`; `E-6` rerun after mock redeploy with payload-fault knobs.
- Section F: executed `F-1`, `F-2`, `F-3`, `F-5` (`F-4` excluded).

## 3) Protocol Section-6 questions (verbatim) answered in order

1. **Does it lose data? Records in versus out, at 1,000 and at population.**  
   **Answer:**  
   - 1,000-set: no loss (`truth_identity_count=2477`, `observed_identity_count=2477`).  
   - Population sample (A-3 cap): no loss on measured sample (`truth_identity_count=12575`, `observed_identity_count=12575`, sample size `5000`, full-population equality unmeasured).  
   **Test/ID:** `A-2`, `A-3`.

2. **Does it lose data between stages? Discovered minus enriched minus pending.**  
   **Answer:** no stage-gap in executed run: `discovered=1788`, `enriched=1788`, `pending_of_discovered_after=0`, `balance=0`.  
   **Test/ID:** `A-5` (rerun with corrected waiter).

3. **Can a scheduling mistake lose data invisibly? How many records, and does anything detect it.**  
   **Answer:** not directly measured in deployed run sequence (gap-invisibility scenario from local `H-3` was not rerun on deployed in this pass).  
   **Test/ID:** `NOT RUN on deployed` (no deployed `H-3` evidence in this report window).

4. **Does it change data? Fields compared, fields mismatched.**  
   **Answer:** yes, for high-magnitude numeric IDs above precision boundary. Seeded values show mutation in warehouse numeric value:  
   - `9007199254740991` preserved;  
   - `9007199254740993` became `9007199254740992.0`;  
   - `1234567890123456789` became `1.2345678901234568E+18` (delta `-11`).  
   **Test/ID:** precision retest artifact (`orderitem-precision-value-compare`, request `96af1585-1eef-46a0-a9f2-74ca6739586d`).

5. **Is it fast enough for a 30-minute cycle, and up to what population?**  
   **Answer:** sustained full-sweep measurement indicates `derived_population_ceiling_30m=193484` (< DS-2 `299190`, margin `-105706`), so not sufficient for DS-2 scale in 30 minutes.  
   **Test/ID:** `F-3`.

6. **Does it stay inside a rate limit, and is anything enforcing one?**  
   **Answer:** measured call rates scale with task count (`C-1`: 1/2/3 tasks; `C-2`: no global ceiling observed in tested range). Runtime uses per-source intervals (`sentinel` 1.0s, `sentinel_discovery` 2.0s), but no external hard cap surface was evidenced in deployed run.  
   **Test/ID:** `C-1`, `C-2`, `C-4`.

7. **Does it recover without a human? Per scenario: recovered / recovered with delay / needed intervention / lost data.**  
   **Answer:**  
   - recovered: `D-1`, `D-2`, `D-3`, `D-6`, `D-11`  
   - recovered with delay: `D-7`, `D-12`  
   - needed intervention: `D-8`, `D-9`  
   - lost data: none in executed D-scope  
   **Test/ID:** `D-1`, `D-2`, `D-3`, `D-6`, `D-7`, `D-8`, `D-9`, `D-11`, `D-12`.

8. **Can an operator tell when it is broken? Per surface: request finished, page stuck, silent failure, window gap, data lag.**  
   **Answer:**  
   - request-finished surface exists (`/v1/requests/{id}/counts` terminal and records).  
   - loud source failure is visible (`E-4` dead-letter page; not silent).  
   - `/v1/dead-letter` is IAM-protected (403 unauthenticated, succeeds with identity token).  
   - window-gap visibility and formal data-lag metric were not executed as dedicated deployed scenarios in this report window.  
   **Test/ID:** `E-1`, `E-4`, `E-5`; plus `NOT RUN on deployed` for gap-visibility/data-lag dedicated scenarios.

## 4) Defects (blocking / P1 / P2 / P3)

### Blocking

1. **Nonexistent incident request can complete as success with zero records and no error surface.**  
   Evidence: `E-1` logs `counts={'done': 1}`, `records=0`, explicit observation text: `nonexistent incident returns done:1, records:0 with no error`.

2. **Numeric value mutation occurs above precision boundary.**  
   Evidence from seeded numeric retest (parsed numeric comparison, not string rendering): `9007199254740993` changed to `9007199254740992.0`; `1234567890123456789` changed by `-11`; `9007199254740991` remained exact.

### P1

1. **30-minute capacity is below DS-2 population.**  
   Evidence: `F-3` margin `-105706`.

2. **Single-page latency during active sweep can be very high.**  
   Evidence: `F-2` probe latency `371.525s` while sweep is in progress; `D-7` shows `177.119s` probe latency during sweep.

3. **Worker interruption scenarios require intervention to recover flow.**  
   Evidence: `D-8` and `D-9` classified `scenario_status=needed_intervention`.

### P2

1. **Injected source-fault scenario yields loud terminal failure requiring intervention.**  
   Evidence: `E-4` terminal counts include dead page (`{'dead': 1, 'done': 1}`); dead-lettered page is visible, so this is classified as needed intervention (not silent loss).

2. **Payload-fault modes show mixed outcomes under deployed run.**  
   Evidence `E-6`: `truncated_json`, `html_error_page`, `empty_body_200` each terminal `dead=1`; `incidents_string` terminal `done=1` with `records=7` after delay.

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

## 7) Measurement reconciliation notes

1. **Latency during sweep (B-5 vs F-2 vs D-7):**
   - `B-5 = 2740.669s` measured during a full-population sweep (`4929` pages / `246412` IDs), with probe submitted while that large sweep was already active.
   - `F-2 = 371.525s` measured during a medium backlog (`600` pages / `30000` IDs).
   - `D-7 = 177.119s` measured during a smaller backlog (`400` pages / `20000` IDs).
   - These are not contradictory: they are queue-delay measurements under different concurrent backlog sizes and queue positions.  
   - **Primary sustained operational reference in this report:** `F-2` (explicit section-F backlog interference scenario); `B-5` is worst-case full-pop overlap evidence.

2. **30-minute ceiling (B-3 vs F-3):**
   - `B-3 = 215999` is derived from short-window rate (`B-2`: `48.0` pages/min/task over a 60s sample).
   - `F-3 = 193484` is derived from full-sweep sustained throughput (`42.996502` pages/min/worker over `4929` pages).
   - Difference is expected burst-vs-sustained behavior.  
   - **Primary cycle-capacity figure for planning:** `F-3` sustained value (`193484`).

## 8) A-5 correction note

- The prior reported `balance=350` in A-5 was a **harness bug**, not a collector defect.  
- Root cause: A-5 previously used `_wait_terminal` (first-page terminal) instead of `_wait_terminal_total_pages` for enrichment chunks.  
- After waiter correction and rerun: `balance=0` (`discovered=1788`, `enriched=1788`, `pending=0`).
## 9) Notes

- This report intentionally does **not** prescribe fixes.
- Results are bounded to executed and evidenced tests/artifacts listed above.
