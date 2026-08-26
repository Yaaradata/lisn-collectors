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
10 passed, 2 skipped in 482.99s (0:08:02)
```

- E-10 rewritten and re-run:
  - `time_to_dead_letter_s=101.02768666500197`
  - `calls_burned=5`
  - `elapsed_s=101.029048` (self-asserted > 60s, cap 30m)
- E-11:
  - 240-second hold with 15-second sampling loop
  - `recovery_latency_s=15.043125436000992`
  - `elapsed_s=240.849785` (self-asserted floor)
- Per-test call durations from latest Section E run:
  - `E-1` 54.86s
  - `E-2` 6.45s
  - `E-3` 10.43s
  - `E-4` 32.48s
  - `E-5` 20.47s
  - `E-6` 0.13s
  - `E-7` 8.85s
  - `E-8` 0.00s (skipped)
  - `E-9` 0.00s (skipped)
  - `E-10` 101.36s
  - `E-11` 241.18s
  - `E-12` 6.43s

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
| A/B/C (combined) | 15 | 9 (per requested accounting baseline) | Partial | `A-* / B-* / C-*` IDs beyond implemented set (protocol-owned list), explicitly not run |
| D | 4 | 3 (`D-1..D-3`) | Partial | `D-4` |
| E | 12 | 12 (`E-1..E-12`) | Partial (10 run, 2 NOT RUN) | `E-8`, `E-9` |
| F | 3 | 3 (`F-1..F-3`) | Complete | none |
| G | 8 | 3 (`G-5..G-7`) | Partial | `G-1`, `G-2`, `G-3`, `G-4`, `G-8` |
| H | 15 (`H-0..H-14`) | 15 | Complete | none |

### Section E NOT RUN tests (coverage gap)

- `E-8` (`database down`) — **NOT RUN**: missing precondition was isolated ability to take database down without impacting shared active environment.
- `E-9` (`sink down`) — **NOT RUN**: missing precondition was isolated sink-outage harness for GCS/BigQuery under shared live credentials.

## 4) Protocol section-6 numeric answers (with producing tests)

| # | Numeric answer | Producing test |
|---|---:|---|
| Q1 | DS-3 heavy incident identity count = `500` | `A-2` |
| Q2 | DS-3 zero-thread incident identity count = `1` | `A-2` |
| Q3 | DS-4 missing null-thread identities = `150` | `B-1` |
| Q4 | Silent gap missing identities = `60` | `H-3` |
| Q5 | Silent gap health surfaces: `reconcile.unloaded=0`, `dead_letter.dead=0`, `health.dead=0`, `health.unloaded=0`, failed/dead discovery jobs=`0` | `H-3` |
| Q6 | Forced reset residual GCS objects = `1521` | `G-7` |
| Q7 | Throughput pages/min/worker = `12.178848` | `F-2` |
| Q8 | Derived 30-minute max population = `18268` incidents | `F-2` |

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

1. Null-thread identity loss in enrichment output (`B-1`): truth null identities `150`, observed `0`.
2. Silent discovery gap not surfaced (`H-3`): missing identities `60` while health/reconcile/dead-letter remain zero.
3. Unsupported discovery key silently accepted (`H-14`): status `200`, no caller-visible dropped-key signal.
4. Forced destructive reset leaves non-zero GCS raw objects (`G-7`): `gcs_objects_raw=1521`.
5. Planner type-confusion in `incident_ids` handling (`C-4`):
   - case 4 accepted string scalar and iterated it as six IDs (`keys=6`);
   - case 5 accepted `None`, `1`, and `{}` as planned IDs.

### P1

1. Dead-letter surface includes DSN-bearing forced error text (`G-5`).
2. 30-minute capacity below DS-2 population (`F-3`): margin `-281286`.

### P2

1. Forced reset returns warnings with partial GCS delete behavior while SQL/BQ clear (`G-7` warning payload).

### P3

1. No additional P3 defect recorded from executed evidence set.
