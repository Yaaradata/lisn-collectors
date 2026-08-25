# LiSN Collector Acceptance Report v1

## 1) Run scope

- Source of truth for this report:
  - Pytest outputs from executed acceptance sections/tests.
  - Evidence logs under `tests/acceptance/evidence/`.
- Code under test: current `cursor/discovery-worker-gap-2390` branch head.
- Environment note: local Postgres in this cloud environment is 16.x (per workspace operating guidance).

## 2) Executed sections and pytest outcomes

### Section A/B/C (full file-set run)

Command:

```bash
.venv/bin/pytest -q tests/acceptance/test_a.py tests/acceptance/test_b.py tests/acceptance/test_c.py
```

Outcome:

```text
FAILED tests/acceptance/test_b.py::test_b1_ds4_null_thread_identities_preserved
1 failed, 8 passed in 490.74s (0:08:10)
```

### Section H (full file run)

Command:

```bash
.venv/bin/pytest -q tests/acceptance/test_h.py
```

Outcome:

```text
FAILED tests/acceptance/test_h.py::test_h14_order_item_ids_enrichment_works_discovery_ignores
1 failed, 14 passed in 1376.98s (0:22:56)
```

### Section G (full file run)

Command:

```bash
.venv/bin/pytest -q tests/acceptance/test_g.py
```

Outcome:

```text
FAILED tests/acceptance/test_g.py::test_g7_admin_reset_in_progress_guard_then_forced_delete
1 failed, 2 passed in 106.65s (0:01:46)
```

### Requested focused run (C-2, C-3, C-4, C-5, A-2, Section F)

Command:

```bash
.venv/bin/pytest -q \
  tests/acceptance/test_c.py::test_c2_split_requests_union_identity_complete \
  tests/acceptance/test_c.py::test_c3_multi_key_request_rejected_with_no_silent_fallback \
  tests/acceptance/test_c.py::test_c4_hostile_query_shapes_rejected_without_5xx \
  tests/acceptance/test_c.py::test_c5_order_item_ids_identity_complete \
  tests/acceptance/test_a.py::test_a2_ds3_skewed_threads_identity_complete \
  tests/acceptance/test_f.py
```

Outcome:

```text
FAILED tests/acceptance/test_c.py::test_c4_hostile_query_shapes_rejected_without_5xx
1 failed, 7 passed in 430.66s (0:07:10)
```

## 3) Evidence inventory used

- A: `A-1.log`, `A-2.log`, `A-3.log`
- B: `B-1.log`, `B-2.log`, `B-3.log`
- C: `C-1.log`, `C-2.log`, `C-3.log`, `C-5.log`  
  (`C-4.log` absent because assertion fails before evidence write)
- F: `F-1.log`, `F-2.log`, `F-3.log`
- G: `G-5.log`, `G-6.log`, `G-7.log`
- H: `H-0.log` … `H-14.log`

## 4) Identity-set correctness highlights

- A-2 (DS-3 skew) preserves identity-set behavior at extremes:
  - Heavy incident identities: `500` truth / `500` observed.
  - Zero-thread incident identities: `1` truth / `1` observed.
- B-2 (DS-6, `order_item_ids`) identity-set match:
  - Truth identities: `14`
  - Observed identities: `14`
  - Missing: `0`, Extra: `0`
- B-1 (DS-4 null-thread) identity-set mismatch:
  - Truth null-thread identities: `150`
  - Observed null-thread identities: `0`

## 5) Section results summary

| Section | Status | Evidence basis |
|---|---|---|
| A | PASS in combined A/B/C run | `A-*.log`, pytest A/B/C output |
| B | FAIL | `B-1.log`, pytest A/B/C output |
| C | FAIL | pytest focused run (`C-4` failure), `C-2.log`, `C-3.log`, `C-5.log` |
| F | PASS in focused run | `F-1.log`, `F-2.log`, `F-3.log` |
| G | FAIL | `G-5.log`, `G-6.log`, `G-7.log`, pytest G output |
| H | FAIL | `H-3.log`, `H-14.log`, pytest H output |

## 6) Protocol Section-6 questions — numeric answers with producing tests

| # | Numeric answer | Produced by test |
|---|---:|---|
| Q1 | DS-3 heavy incident identity cardinality = `500` | `A-2` (`tests/acceptance/test_a.py::test_a2...`) |
| Q2 | DS-3 zero-thread incident identity cardinality = `1` | `A-2` |
| Q3 | DS-4 missing null-thread identities = `150` (`truth=150`, `observed=0`) | `B-1` |
| Q4 | Gap-window silent-loss missing identities = `60` | `H-3` |
| Q5 | Silent-loss detection surfaces reported: `reconcile.unloaded=0`, `dead_letter.dead=0`, `health.dead=0`, `health.unloaded=0`, failed/dead discovery jobs=`0` | `H-3` |
| Q6 | Forced reset residual GCS raw objects after run = `1521` | `G-7` |
| Q7 | Measured throughput = `12.178848` pages/min/worker | `F-2` |
| Q8 | Derived max population in 30-minute window = `18268` incidents (also `17904` in second measurement) | `F-2` (`F-3` corroborates second measurement) |

## 7) Blocking defects and priority list

### Blocking defects

1. **Null-thread identity loss in enrichment output.**  
   Evidence: `B-1` reports `truth_null_identity_count=150`, `observed_null_identity_count=0`; failing assertion with `missing=150`.

2. **Silent discovery gap not surfaced by health mechanisms.**  
   Evidence: `H-3` reports `missing_count=60` while `reconcile.unloaded=0`, `dead_letter.dead=0`, `health.dead=0`, `health.unloaded=0`.

3. **Unsupported `order_item_ids` key on discovery is accepted without caller-visible signal.**  
   Evidence: `H-14` shows `unsupported_collect_status=200`, payload contains request id, `dropped_key_visible=False`, and output cardinality unchanged (`baseline_count=1000`, `unsupported_count=1000`).

4. **Destructive reset leaves non-zero raw GCS footprint after forced run.**  
   Evidence: `G-7` forced reset returns `200`; SQL/BQ cleared to zero, but `/v1/admin/state` shows `gcs_objects_raw=1521`.

5. **Hostile query-shape case accepted with HTTP 200 in C-4 matrix.**  
   Evidence: `test_c4_hostile_query_shapes_rejected_without_5xx` fails on `assert status == 400` due to observed `200`.

### P1

1. **DSN appears in dead-letter surface via `last_error`.**  
   Evidence: `G-5` includes `forced DSN exception: postgresql://...` and `/v1/dead-letter` echoes that value in `last_error`.

2. **30-minute capacity materially below DS-2 population.**  
   Evidence: `F-3` reports `ds2_incident_population=299190`, derived 30-minute maximum `17904`, margin `-281286`.

### P2

1. **Forced reset run returned `success=false` with explicit GCS delete warning while SQL/BQ paths reported clear.**  
   Evidence: `G-7` `forced_payload.warnings` includes object-delete 404; `collector_*` and BQ tables are zero after run.

### P3

1. **No additional P3-grade defect recorded from executed evidence set.**

## 8) Notes

- This report is descriptive only and intentionally does not prescribe fixes.
- Results reflect executed tests/evidence only; sections not executed in this run are not inferred.
