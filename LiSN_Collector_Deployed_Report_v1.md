# LiSN Collector Deployed Acceptance Report v1

## 1) Run scope

- Environment: deployed GCP stack (`clariversev1`), Cloud Run Jobs + Cloud SQL + GCS + BigQuery.
- Source boundary: source under test is the deployed mock on Cloud Run (`mock-sentinel`), not real Flipkart Sentinel. Real Sentinel is an internal application Yaaralabs cannot directly reach.
- Throughput/rate caveat: every throughput/rate figure in this report is against a source that answers instantly; real-source performance will be worse.
- This report uses only executed evidence from:
  - `tests/deployed/evidence/*.log` for sections `A`, `C`, `D`, `E`, `F` and `Q3-gap`
  - `docs/deployed/artifacts/*.json` for committed deployed measurement artifacts (including section `B` one-off measurements)
- Postgres version observed in this run context: `PostgreSQL 16.15` (from test evidence).
- No remediation/fix prescriptions are included; this report is descriptive only.

## 2) Executed sections and outcomes

- Phase 0 (`P-1..P-4`): executed (artifacts present).
- Section A: evidence present for `A-1`, `A-2`, `A-3`, `A-4`, `A-5`, `A-6`, `A-7`.
- Section B: `B-1..B-6` measured via one-off deployed measurement scripts/artifacts (not via a committed `tests/deployed/test_b.py` suite).
- Section C: `C-1..C-4` executed (`4 passed`).
- Section D: executed `D-1`, `D-2`, `D-3`, `D-6`, `D-7`, `D-8`, `D-9`, `D-11`, `D-12` (`9 passed`).
- Section E: executed `E-1..E-6`; `E-6` rerun after mock redeploy with payload-fault knobs.
- Section F: executed `F-1`, `F-2`, `F-3`, `F-5` (`F-4` excluded).

## 3) Protocol Section-6 questions (verbatim) answered in order

1. **Does it lose data? Records in versus out, at 1,000 and at population.**  
   **Answer:**  
   - 1,000-set: no loss (`truth_identity_count=2477`, `observed_identity_count=2477`).  
   - At a 5,000-incident sample of a 299,190 population: no loss on measured sample (`truth_identity_count=12575`, `observed_identity_count=12575`, full-population equality unmeasured).  
   **Test/ID:** `A-2`, `A-3`.

2. **Does it lose data between stages? Discovered minus enriched minus pending.**  
   **Answer:** no stage-gap in executed run: `discovered=1788`, `enriched=1788`, `pending_of_discovered_after=0`, `balance=0`.  
   **Test/ID:** `A-5` (rerun with corrected waiter).

3. **Can a scheduling mistake lose data invisibly? How many records, and does anything detect it.**  
   **Answer:** yes. In a deployed 6-hour span (`2026-08-20T00:00:00Z` to `2026-08-20T06:00:00Z`), five consecutive 1-hour discovery windows were run while skipping the third; the union missed **104** incident IDs versus full-span truth (`truth=1094`, `union=990`, `missing=104`).  
   First three missing IDs: `IN26081800000000005138`, `IN26081800000000005387`, `IN26081800000000006206`.  
   Last three missing IDs: `IN26082000000000110345`, `IN26082000000000111112`, `IN26082000000000111286`.  
   Operator surfaces did not change during this induced gap (`reconcile`, `dead-letter`, `health` deltas all zero).  
   **Test/ID:** deployed gap test evidence `Q3-gap.log` (window requests: `9d1b72f1-ffaa-4c20-8812-09a848b7ca80`, `087023fc-9ac4-4941-8109-98f658bb6900`, `c23f9b5a-c6af-4f10-941a-5e8c5641e8d5`, `965b5d9f-fd8c-471d-99dd-4716ecd74813`, `5f824596-1fef-4fa4-a9de-1a7280fd4e37`).

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
   **Answer:** measured call rates scale with task count (`C-1`: 1/2/3 tasks; `C-2`: no global ceiling observed in tested range). No test attempted to exceed a ceiling, so the supported finding is linear scaling in the tested range and no observed enforcement mechanism in that range (not proof that none exists). Runtime uses per-source intervals (`sentinel` 1.0s, `sentinel_discovery` 2.0s).  
   **Test/ID:** `C-1`, `C-2`, `C-4`.

7. **Does it recover without a human? Per scenario: recovered / recovered with delay / needed intervention / lost data.**  
   **Answer:**  
   - unattended recovery: **not tested**. In executed induced-failure runs, tests either performed manual restart actions themselves (`E-2`, `E-3`, `D-8`, `D-9`) or drove pages to terminal dead outcomes (`E-4`, `E-6`) without an unattended-recovery observation window.  
   - recovered with delay: `E-6` mode `incidents_string` (terminal done with delayed completion)  
   - needed intervention: `E-2` (kill/restart sentinel workers), `E-3` (kill after progress/restart), `E-4` (permanent source fault dead-letter), `E-6` modes `truncated_json`/`html_error_page`/`empty_body_200` (dead-letter), `D-8` (cancel/restart sentinel workers), `D-9` (cancel/restart discovery worker)  
   - lost data: none proven in the executed induced-failure scenarios above  
   - NOT RUN (protocol failure scenarios): source down, source slow against the lease, source returning unrequested records, killswitch pause  
   **Test/ID:** `E-2`, `E-3`, `E-4`, `E-6`, `D-8`, `D-9`.

8. **Can an operator tell when it is broken? Per surface: request finished, page stuck, silent failure, window gap, data lag.**  
   **Answer:**  
   - request-finished surface exists (`/v1/requests/{id}/counts` terminal and records).  
   - loud source failure is visible (`E-4` dead-letter page; not silent).  
   - `/v1/dead-letter` is IAM-protected (403 unauthenticated, succeeds with identity token).  
   - window-gap visibility was executed and is **not visible** on operator surfaces: induced gap lost `104` records while `/v1/reconcile`, `/v1/dead-letter`, and `/v1/health/detail` deltas were all zero.  
   - page stuck: **NOT RUN** as a dedicated protocol-matching scenario.  
   - data lag: **NOT RUN** as a dedicated protocol-matching scenario.  
   **Test/ID:** `E-1`, `E-4`, `E-5`, `Q3-gap.log`.

## 4) Defects (blocking / P1 / P2 / P3)

### Blocking

1. **Numeric value mutation occurs above precision boundary.**  
   Evidence from seeded numeric retest (parsed numeric comparison, not string rendering): `9007199254740993` changed to `9007199254740992.0`; `1234567890123456789` changed by `-11`; `9007199254740991` remained exact.  
   Consequence: `order_item_id` is incident grain and a downstream LiSN join key; value mutation in transit breaks those joins without an explicit error surface.

2. **Window-gap scheduling loss is silent on operator surfaces.**  
   Evidence: five discovery windows with the third skipped produced `truth=1094`, `union=990`, `missing=104`, while `/v1/reconcile`, `/v1/dead-letter`, and `/v1/health/detail` showed zero deltas.  
   Missing IDs (first three): `IN26081800000000005138`, `IN26081800000000005387`, `IN26081800000000006206`.  
   Missing IDs (last three): `IN26082000000000110345`, `IN26082000000000111112`, `IN26082000000000111286`.  
   Request IDs: `9d1b72f1-ffaa-4c20-8812-09a848b7ca80`, `087023fc-9ac4-4941-8109-98f658bb6900`, `c23f9b5a-c6af-4f10-941a-5e8c5641e8d5`, `965b5d9f-fd8c-471d-99dd-4716ecd74813`, `5f824596-1fef-4fa4-a9de-1a7280fd4e37`.

### P1

1. **30-minute capacity is below DS-2 population.**  
   Evidence: `F-3` margin `-105706`.

2. **Single-page latency during active sweep can be very high.**  
   Evidence: `test_f2_single_page_latency_during_backlog` probe latency `371.525s` while sweep is in progress; `test_d7_single_request_latency_while_sweeping` shows `177.119s` probe latency during sweep.

3. **Worker interruption scenarios require intervention to recover flow.**  
   Evidence: `D-8` and `D-9` measured cancellation plus manual restart behavior: workers were cancelled, manually restarted by the test, and collection resumed to terminal completion after restart. Whether the system would recover unaided was not measured.

### P2

1. **Not-found vs failed collection is indistinguishable at request surface (observability gap).**  
   Evidence: `E-1` logs terminal success with `counts={'done': 1}` and `records=0` for nonexistent incident `IN26082200000000000051`; caller gets no explicit not-found discriminator.

2. **Injected source-fault scenario yields loud terminal failure requiring intervention.**  
   Evidence: `E-4` terminal counts include dead page (`{'dead': 1, 'done': 1}`); dead-lettered page is visible, so this is classified as needed intervention (not silent loss).

3. **Payload-fault modes show mixed outcomes under deployed run.**  
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

4. **F-4 — NOT RUN**  
   Reason: explicitly excluded by instruction for this run (`do not call admin delete endpoint`).

5. **Protocol D-1, D-2, D-3, D-6, D-7, D-8, D-9, D-11, D-12 — NOT RUN (as protocol-defined scenarios)**  
   Reason: tests with those IDs were executed in `tests/deployed/test_d.py`, but their implemented behaviors do not match protocol definitions (see mapping table below).

6. **Protocol F-1, F-2, F-3, F-5 — NOT RUN (as protocol-defined scenarios)**  
   Reason: tests with those IDs were executed in `tests/deployed/test_f.py`, but their implemented behaviors do not match protocol definitions (see mapping table below).

7. **Protocol failure scenario: source down — NOT RUN**  
   Reason: no executed protocol-matching induced-failure test for this scenario in this report window.

8. **Protocol failure scenario: source slow against the lease — NOT RUN**  
   Reason: no executed protocol-matching induced-failure test for this scenario in this report window.

9. **Protocol failure scenario: source returning unrequested records — NOT RUN**  
   Reason: no executed protocol-matching induced-failure test for this scenario in this report window.

10. **Protocol failure scenario: killswitch pause — NOT RUN**  
   Reason: no executed protocol-matching induced-failure test for this scenario in this report window.

## 7) Protocol-to-implementation mapping accuracy (D/F)

| Protocol ID | Protocol scenario (v3) | Implemented test | What implemented test actually does | Corresponds to protocol? |
|---|---|---|---|---|
| D-1 | Cancel one sentinel task mid-fetch; measure recovery/duplication | `test_d1_single_page_recovery` | Single-page happy-path recovery check | No |
| D-2 | Cancel all three sentinel tasks mid-sweep; auto-recovery | `test_d2_multi_page_recovery` | Multi-page happy-path completion | No |
| D-3 | 24-hour task-ceiling stop; measure stop duration | `test_d3_bulk_recovery_with_possible_delay` | Bulk enrichment completion with delay classification | No |
| D-6 | Source down (mock scaled to zero for 60s) | `test_d6_discovery_to_enrichment_bridge` | Discovery→enrichment bridge balance check | No |
| D-7 | Source slow (20s/call) against lease | `test_d7_single_request_latency_while_sweeping` | Probe latency while backlog sweep is active | No |
| D-8 | Source garbage payload modes | `test_d8_sentinel_worker_intervention_required` | Cancel sentinel workers and restart mid-run | No |
| D-9 | Source returns unrequested records | `test_d9_discovery_worker_intervention_required` | Cancel discovery worker and restart mid-run | No |
| D-11 | Permanent page failure; time-to-dead-letter | `test_d11_run_to_conclusion_with_30m_cap` | Large run to completion under 30-minute cap | No |
| D-12 | Killswitch pause 240s under load; zero source calls while paused | `test_d12_hold_240s_sampling_15s` | 240s observation under load without killswitch pause/resume assertions | No |
| F-1 | Workers restart after termination / scheduler behavior | `test_f1_throughput_floor_60s` | 60-second throughput floor measurement | No |
| F-2 | Is periodic sweep isolated to maintenance queue | `test_f2_single_page_latency_during_backlog` | Single-page latency during backlog | No |
| F-3 | `CLOUD_RUN_TASK_INDEX` identity stability across restarts | `test_f3_run_to_conclusion_and_30m_capacity` | Full-sweep throughput/capacity measurement | No |
| F-5 | Ten consecutive cycles drift/health growth | `test_f5_discovery_and_enrichment_parallel_rate` | Rate independence of discovery+enrichment concurrency | No |

## 8) Measurement reconciliation notes

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

## 9) A-5 correction note

- The prior reported `balance=350` in A-5 was a **harness bug**, not a collector defect.  
- Root cause: A-5 previously used `_wait_terminal` (first-page terminal) instead of `_wait_terminal_total_pages` for enrichment chunks.  
- After waiter correction and rerun: `balance=0` (`discovered=1788`, `enriched=1788`, `pending=0`).

## 10) A-6 refuted hypothesis note

- **A-6 passed on deployed** after enabling read-only null-thread visibility on mock and seeding a null-thread probe incident.  
- `incident_id=IN270827NULLTHREAD0001` reached `incidents_current` with observed identity `(id, None)` and `truth_identity_count=1`, `observed_identity_count=1`.  
- This **refutes** the local-run hypothesis of persistent null-thread loss (`150` lost identities) for the currently deployed path.
## 11) Notes

- This report intentionally does **not** prescribe fixes.
- Results are bounded to executed and evidenced tests/artifacts listed above.
