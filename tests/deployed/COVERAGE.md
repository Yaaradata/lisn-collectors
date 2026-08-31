# Protocol coverage map

Source of truth for which LiSN Collector Deployed Acceptance v3 protocol IDs
have a matching test. Status values:

- **IMPLEMENTED** — test asserts the protocol scenario
- **NOT IMPLEMENTED** — no protocol-matching test (do not invent a green stub)
- **IMPLEMENTED AS SOMETHING ELSE** — a test exists under a descriptive name; it does **not** measure the protocol scenario

Renamed former `test_d*` / `test_f*` IDs so a green run cannot be read as protocol coverage.

---

## A · Retrieval

| ID | Status | Test |
|---|---|---|
| A-1 | IMPLEMENTED | `test_a1_single_incident_row_and_fields` |
| A-2 | IMPLEMENTED | `test_a2_thousand_incidents_identity_equality` |
| A-3 | IMPLEMENTED (sampled) | `test_a3_population_scale_identity_count` — sample of population, not full equality |
| A-4 | IMPLEMENTED | `test_a4_discovery_window_identity_equality` |
| A-5 | IMPLEMENTED | `test_a5_discovery_to_enrichment_balance` |
| A-6 | IMPLEMENTED | `test_a6_null_thread_incident_survives` |
| A-7 | IMPLEMENTED | `test_a7_order_item_ids_retrieval` |

## B · Speed

| ID | Status | Test / note |
|---|---|---|
| B-1 | IMPLEMENTED AS SOMETHING ELSE | measured via `docs/deployed/artifacts/` one-offs, not `tests/deployed/test_b.py` |
| B-2 | IMPLEMENTED AS SOMETHING ELSE | same (artifact / derived from B-1) |
| B-3 | IMPLEMENTED AS SOMETHING ELSE | same; also related capacity figure in `test_run_to_conclusion_and_30m_capacity` |
| B-4 | IMPLEMENTED AS SOMETHING ELSE | related: `test_single_page_latency_during_backlog` / `test_single_request_latency_while_sweeping` (not full-pop sweep) |
| B-5 | IMPLEMENTED AS SOMETHING ELSE | artifact only |
| B-6 | NOT IMPLEMENTED | no cold-start-to-first-call test |

## C · Rate limit

| ID | Status | Test |
|---|---|---|
| C-1 | IMPLEMENTED | `test_c1_calls_per_second_floor` |
| C-2 | IMPLEMENTED | `test_c2_global_ceiling` |
| C-3 | IMPLEMENTED | `test_c3_rolling_redeploy_peak` |
| C-4 | IMPLEMENTED | `test_c4_discovery_and_enrichment_independent_rates` |

## D · Survives production

| ID | Protocol scenario | Status | Test |
|---|---|---|---|
| D-1 | Cancel one sentinel task mid-fetch; recovery / duplication | NOT IMPLEMENTED | was falsely `test_d1_*`; now `test_single_page_happy_path` (happy path only) |
| D-2 | Cancel all three tasks mid-sweep; auto-recovery | NOT IMPLEMENTED | was falsely `test_d2_*`; now `test_multi_page_happy_path` |
| D-3 | 24h task-ceiling stop duration | NOT IMPLEMENTED | was falsely `test_d3_*`; now `test_bulk_enrichment_completion`. Ceiling evidence is observational in deployment findings, not a controlled test |
| D-4 | Cloud SQL stopped 60s | NOT IMPLEMENTED | destructive — excluded |
| D-5 | Cloud SQL restarted mid-sweep | NOT IMPLEMENTED | destructive — excluded |
| D-6 | Source down (mock scaled to zero 60s) | IMPLEMENTED | `test_d6_source_down_60s` |
| D-7 | Source slow (20s/call) against lease | NOT IMPLEMENTED | needs mock delay fault + double-fetch assertion; was falsely `test_d7_*` → `test_single_request_latency_while_sweeping` |
| D-8 | Source garbage payload modes | IMPLEMENTED AS SOMETHING ELSE | closest: `test_e6_payload_fault_modes` (E module). Former `test_d8_*` is now `test_sentinel_worker_cancel_and_manual_restart` |
| D-9 | Source returns unrequested records | IMPLEMENTED | `test_d9_unrequested_records` (Pass 4 / `missing_keys.unexpected`) |
| D-10 | GCS write denied 60s | NOT IMPLEMENTED | destructive — excluded |
| D-11 | Permanent page failure; time-to-dead-letter | NOT IMPLEMENTED | needs controlled permanent fault + timing; was falsely `test_d11_*` → `test_large_run_to_conclusion_30m_cap`. Partial signal in `test_e4_source_fault_dead_letters` |
| D-12 | Killswitch pause 240s under load; zero source calls | IMPLEMENTED | `test_d12_killswitch_pause_240s` |

### Renamed non-protocol D tests (do not map to IDs above)

| Test | What it actually measures |
|---|---|
| `test_single_page_happy_path` | Small enrichment completes; identity equality |
| `test_multi_page_happy_path` | Medium enrichment completes; identity equality |
| `test_bulk_enrichment_completion` | Large enrichment completes; delay classification |
| `test_discovery_to_enrichment_bridge` | Discovery → enrichment balance for one window |
| `test_single_request_latency_while_sweeping` | Probe latency under concurrent backlog |
| `test_sentinel_worker_cancel_and_manual_restart` | Cancel + **manual** restart mid-run |
| `test_discovery_worker_cancel_and_manual_restart` | Discovery cancel + **manual** restart |
| `test_large_run_to_conclusion_30m_cap` | Large run finishes under 30 minutes |
| `test_hold_240s_sampling_15s` | 240s observation under load **without** killswitch |

## E · Operator visibility

| ID | Status | Test |
|---|---|---|
| E-1 | IMPLEMENTED AS SOMETHING ELSE | `test_e1_nonexistent_incident_observation` — not-found / counts surface, not request `closed_at` lifecycle |
| E-2 | IMPLEMENTED AS SOMETHING ELSE | `test_e2_kill_and_resume_enrichment` — kill/resume, not stalled-page health surface |
| E-3 | IMPLEMENTED AS SOMETHING ELSE | `test_e3_kill_after_progress_and_resume` — not reconcile-after-cancel |
| E-4 | IMPLEMENTED AS SOMETHING ELSE | skipped-window loss evidenced in `Q3-gap.log`; `test_e4_source_fault_dead_letters` is permanent source fault |
| E-5 | IMPLEMENTED AS SOMETHING ELSE | `test_e5_dead_letter_auth_surface` — auth only; not forced DSN-in-error |
| E-6 | NOT IMPLEMENTED | data-lag metric absent; `test_e6_payload_fault_modes` is payload garbage (closer to D-8) |

## F · Deployment holds

| ID | Protocol scenario | Status | Test |
|---|---|---|---|
| F-1 | Workers restart after termination / scheduler | NOT IMPLEMENTED | no Cloud Scheduler / unattended re-execute. Was falsely `test_f1_*` → `test_throughput_floor_60s`. Reason: Pass 12 scheduler not built |
| F-2 | Periodic sweep isolated to maintenance queue | NOT IMPLEMENTED | observational in deployment findings; was falsely `test_f2_*` → `test_single_page_latency_during_backlog` |
| F-3 | `CLOUD_RUN_TASK_INDEX` identity stability | IMPLEMENTED | `test_f3_worker_identity_stability` (restart is test-driven; see note in test) |
| F-4 | Destructive admin unauthenticated | NOT IMPLEMENTED | excluded (`do not call admin delete`) |
| F-5 | Ten consecutive cycles drift/health | NOT IMPLEMENTED | was falsely `test_f5_*` → `test_discovery_and_enrichment_parallel_rate` |

### Renamed non-protocol F tests

| Test | What it actually measures |
|---|---|
| `test_throughput_floor_60s` | Mock call rate over ≥60s with 3 tasks |
| `test_single_page_latency_during_backlog` | Probe latency during medium backlog |
| `test_run_to_conclusion_and_30m_capacity` | Full-sweep sustained throughput / 30m ceiling |
| `test_discovery_and_enrichment_parallel_rate` | Rate independence under concurrency |

---

## Recovery claim (honest)

Induced-failure tests that resume work (`test_sentinel_worker_cancel_and_manual_restart`, `test_discovery_worker_cancel_and_manual_restart`, `test_e2_*`, `test_e3_*`, `test_f3_worker_identity_stability`) **perform their own restart**. Unattended recovery after worker death is **UNMEASURED** until Pass 12's scheduler exists. Do not report “most failures recover on their own” from this suite.
