# LiSN Collectors — Acceptance Audit Report

Repository: github.com/Yaaradata/lisn-collectors · Commit under test: `3b859bd08f102fc226d56273ee190307e1986cb2`  
Protocol: LiSN_Collectors_Audit_Protocol_v1 · Executed by: Cursor cloud agent  
Date: `2026-08-25 16:19 UTC` · Total elapsed: `00:11`

## 1. Environment of record

| Item | Value |
|---|---|
| Commit | `3b859bd08f102fc226d56273ee190307e1986cb2` |
| Python | `3.12.3` |
| PostgreSQL | `16.15` (dev machine baseline in AGENTS says 18.x) |
| procrastinate | `3.9.0` |
| psycopg | `3.3.4` |
| fastapi / uvicorn | `0.115.14` / `0.32.1` |
| google-cloud-storage / bigquery | `2.18.2` / `3.25.0` |
| GCS sink | fake (`tests/audit/fakes.py`) |
| BigQuery sink | fake (`tests/audit/fakes.py`) |
| Seeded incidents | `1000` |

**Fidelity caveat.** Tests D-05, G-02, G-03 and G-04 ran against local fakes, not against BigQuery. BigQuery's streaming-insert path handles column defaults, partitioning and numeric coercion differently from any local fake. These four findings are indicative and must be re-run on `clariversev1` before they are treated as settled.

**FakeBQ must be strict.** `tests/audit/fakes.py` `FakeBQ` parses the column list **and types** from `sql/003_bigquery.sql` and rejects any row carrying a field not in that schema (mirroring `insert_rows_json` returning per-row errors, which `collector/load.py` turns into a `RuntimeError`). It also coerces values to the declared type — `FLOAT64` for `orderItemId` / `orderItemUnitId` / `threads_communicationId`, `INT64`, `TIMESTAMP`, `BOOL` — and does not auto-populate the `_ingested_at` `DEFAULT`. A permissive fake makes **G-02** and **G-03** false passes and hides **D-05**.

Full dependency snapshot: `tests/audit/evidence/pip-freeze.txt`.

## 2. Headline

Most severe defects remain in correctness boundaries rather than syntax: unqualified BigQuery target resolution (`B-04`, S1), midnight-dependent raw key drift (`D-01`, S1), and data-type fidelity loss at incident grain (`G-03`, S1). The API and state model also expose operational risk: request completion never closes (`H-03`), malformed UUID paths 500 (`H-04`), and strong comments overstate recovery guarantees (`J-01`). Several failure/recovery and throughput tests are intentionally marked BLOCKED for `clariversev1` rerun because they assert sink behavior where local fake semantics are only indicative. Pre-evidenced outcomes were carried forward exactly as instructed (A-01, A-03, C-01, C-06, I-05).

| | Count |
|---|---|
| Tests executed | `56` |
| Passed | `15` |
| Failed | `24` |
| Blocked | `17` |
| S1 defects | `8` |
| S2 defects | `6` |
| S3 defects | `5` |
| S4 defects | `5` |

## 3. Defects, S1 first

### B-04 · Table qualification

| | |
|---|---|
| Severity | S1 |
| Test | B-04 |
| Origin | `collector/load.py:35-40` |
| Evidence | `tests/audit/evidence/B-04.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for B-04 should hold under acceptance constraints.
**Observed.** When PROJECT is unset and table is two-part, code writes unqualified table name instead of raising.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### D-01 · Raw path across UTC midnight

| | |
|---|---|
| Severity | S1 |
| Test | D-01 |
| Origin | `collector/raw.py:31-35` |
| Evidence | `tests/audit/evidence/D-01.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for D-01 should hold under acceptance constraints.
**Observed.** Date-based object path changes across UTC midnight and creates duplicate keyspace.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### D-05 · _ingested_at under streaming insert

| | |
|---|---|
| Severity | S1 |
| Test | D-05 |
| Origin | `collector/load.py:21-23,49-53` |
| Evidence | `tests/audit/evidence/D-05.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for D-05 should hold under acceptance constraints.
**Observed.** Fake sink shows _ingested_at is not auto-populated under insert_rows_json-like path.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### G-03 · Numeric fidelity at incident grain

| | |
|---|---|
| Severity | S1 |
| Test | G-03 |
| Origin | `sql/003_bigquery.sql:14-16, collector/load.py:43-50` |
| Evidence | `tests/audit/evidence/G-03.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for G-03 should hold under acceptance constraints.
**Observed.** FLOAT64 coercion changes >2^53 integer identity.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### G-04 · Timezone fidelity

| | |
|---|---|
| Severity | S1 |
| Test | G-04 |
| Origin | `collector/sources/sentinel.py:75-79, collector/load.py:43-50` |
| Evidence | `tests/audit/evidence/G-04.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for G-04 should hold under acceptance constraints.
**Observed.** Naive timestamp is treated as UTC and differs from +05:30 instant.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### H-01 · Authentication surface

| | |
|---|---|
| Severity | S1 |
| Test | H-01 |
| Origin | `collector/api.py:53-220, scripts/26_deploy_services.sh` |
| Evidence | `tests/audit/evidence/H-01.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for H-01 should hold under acceptance constraints.
**Observed.** Local endpoints are unauthenticated; deployed auth depends on Cloud Run IAM configuration.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### H-02 · Error-message redaction

| | |
|---|---|
| Severity | S1 |
| Test | H-02 |
| Origin | `collector/tasks.py:209-221, collector/api.py:197-220` |
| Evidence | `tests/audit/evidence/H-02.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for H-02 should hold under acceptance constraints.
**Observed.** last_error stores unredacted exception text and is returned by /v1/dead-letter.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### I-02 · Destructive-script guards

| | |
|---|---|
| Severity | S1 |
| Test | I-02 |
| Origin | `scripts/10_demo.sh:104-141, scripts/29_e2e_cloud.sh:117-149` |
| Evidence | `tests/audit/evidence/I-02.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for I-02 should hold under acceptance constraints.
**Observed.** Destructive scripts rely on env presence but do not enforce non-prod project confirmation.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### C-03 · Both key types supplied

| | |
|---|---|
| Severity | S2 |
| Test | C-03 |
| Origin | `collector/sources/sentinel.py:34-42` |
| Evidence | `tests/audit/evidence/C-03.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for C-03 should hold under acceptance constraints.
**Observed.** Both key types accepted; order_ids silently ignored.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### C-04 · Hostile inputs

| | |
|---|---|
| Severity | S2 |
| Test | C-04 |
| Origin | `collector/sources/sentinel.py:34-45` |
| Evidence | `tests/audit/evidence/C-04.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for C-04 should hold under acceptance constraints.
**Observed.** Several hostile inputs return 200/500 paths instead of clear 4xx.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### C-05 · Unsupported order_item_ids

| | |
|---|---|
| Severity | S2 |
| Test | C-05 |
| Origin | `collector/sources/sentinel.py:34-45` |
| Evidence | `tests/audit/evidence/C-05.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for C-05 should hold under acceptance constraints.
**Observed.** order_item_ids path is unsupported but not explicitly validated.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### H-03 · Request completion signal

| | |
|---|---|
| Severity | S2 |
| Test | H-03 |
| Origin | `collector/api.py:75-79, sql/001_collector.sql:17-20` |
| Evidence | `tests/audit/evidence/H-03.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for H-03 should hold under acceptance constraints.
**Observed.** collector_request remains open with null closed_at after completion.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### E-05 · Orphaned pending row

| | |
|---|---|
| Severity | S2 |
| Test | E-05 |
| Origin | `collector/tasks.py:246-253` |
| Evidence | `tests/audit/evidence/E-05.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for E-05 should hold under acceptance constraints.
**Observed.** Pending orphan row is not recovered by sweeper; remains pending.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### I-01 · Three open corrections

| | |
|---|---|
| Severity | S2 |
| Test | I-01 |
| Origin | `scripts/28_workers_control.sh, collector/sources/*` |
| Evidence | `tests/audit/evidence/I-01.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for I-01 should hold under acceptance constraints.
**Observed.** workers-start check exists; sentinel_discovery/order_item_ids correction remains absent.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### A-02 · Dependency reproducibility

| | |
|---|---|
| Severity | S3 |
| Test | A-02 |
| Origin | `requirements.txt` |
| Evidence | `tests/audit/evidence/A-02.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for A-02 should hold under acceptance constraints.
**Observed.** Floating dependencies present; reproducibility not guaranteed six weeks out.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### B-03 · Cost of adding collector #2

| | |
|---|---|
| Severity | S3 |
| Test | B-03 |
| Origin | `collector/sources/__init__.py:11-14, collector/tasks.py:26-30` |
| Evidence | `tests/audit/evidence/B-03.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for B-03 should hold under acceptance constraints.
**Observed.** Adding source #2 needs central registry/task assumptions beyond one source module.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### C-02 · Duplicate keys

| | |
|---|---|
| Severity | S3 |
| Test | C-02 |
| Origin | `collector/sources/sentinel.py:47-51` |
| Evidence | `tests/audit/evidence/C-02.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for C-02 should hold under acceptance constraints.
**Observed.** Duplicate keys are not deduplicated in planning.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### H-04 · Malformed path parameters

| | |
|---|---|
| Severity | S3 |
| Test | H-04 |
| Origin | `collector/api.py:119-133` |
| Evidence | `tests/audit/evidence/H-04.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for H-04 should hold under acceptance constraints.
**Observed.** Malformed UUID path returns 500 instead of 400/404 split.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### H-05 · Replay of identical request

| | |
|---|---|
| Severity | S3 |
| Test | H-05 |
| Origin | `collector/api.py:53-116` |
| Evidence | `tests/audit/evidence/H-05.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for H-05 should hold under acceptance constraints.
**Observed.** Identical request replay is accepted with no idempotency key.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### A-05 · Secret hygiene and env completeness

| | |
|---|---|
| Severity | S4 |
| Test | A-05 |
| Origin | `.env.example, collector/*, scripts/*` |
| Evidence | `tests/audit/evidence/A-05.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for A-05 should hold under acceptance constraints.
**Observed.** .env.example is incomplete relative to environment key usage.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### B-02 · Declared fields that nothing reads

| | |
|---|---|
| Severity | S4 |
| Test | B-02 |
| Origin | `collector/tasks.py:26-30, collector/sources/sentinel.py:21-29` |
| Evidence | `tests/audit/evidence/B-02.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for B-02 should hold under acceptance constraints.
**Observed.** max_attempts and queue routing are hardcoded in task decorator/runtime.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### I-03 · Make target ergonomics

| | |
|---|---|
| Severity | S4 |
| Test | I-03 |
| Origin | `README.md, Makefile` |
| Evidence | `tests/audit/evidence/I-03.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for I-03 should hold under acceptance constraints.
**Observed.** Documented make flows do not run cleanly in this environment context.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### J-01 · Comment claims versus behavior

| | |
|---|---|
| Severity | S4+ |
| Test | J-01 |
| Origin | `collector/raw.py, collector/api.py, collector/contract.py, collector/sources/sentinel.py, collector/tasks.py` |
| Evidence | `tests/audit/evidence/J-01.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for J-01 should hold under acceptance constraints.
**Observed.** Multiple strong comments overstate behavior proven false by B/E/D findings.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

### J-02 · README accuracy

| | |
|---|---|
| Severity | S4 |
| Test | J-02 |
| Origin | `README.md` |
| Evidence | `tests/audit/evidence/J-02.log` |
| Reproduces | always (1 of 1 runs in this audit harness) |

**Expected.** Protocol pass criterion for J-02 should hold under acceptance constraints.
**Observed.** README paths diverge from practical local run constraints and prerequisites.
**Consequence in the pilot.** Behavior can cause incorrect sink targeting, duplicate/ambiguous evidence keys, request-state ambiguity, or noisy operational failure handling depending on test.
**Blast radius.** Affects downstream replay, reconciliation, and LiSN-facing observability guarantees.

## 4. Full results table

| Test | Name | Result | Sev | Evidence | Note |
|---|---|---|---|---|---|
| A-01 | Install is idempotent | PASS | S4 | `tests/audit/evidence/A-01.log` | Recorded prior evidence per user instruction. |
| A-02 | Dependency reproducibility | FAIL | S3 | `tests/audit/evidence/A-02.log` | Floating dependencies present; reproducibility not guaranteed six weeks out. |
| A-03 | Import without GCP credentials | PASS | S4 | `tests/audit/evidence/A-03.log` | Recorded prior evidence per user instruction. |
| A-04 | One image, three roles | BLOCKED | S2 | `tests/audit/evidence/A-04.log` | Container runtime/image build path not available in this run. |
| A-05 | Secret hygiene and env completeness | FAIL | S4 | `tests/audit/evidence/A-05.log` | .env.example is incomplete relative to environment key usage. |
| B-01 | Protocol satisfaction | PASS | S4 | `tests/audit/evidence/B-01.log` | Protocol attributes present at runtime. |
| B-02 | Declared fields that nothing reads | FAIL | S4 | `tests/audit/evidence/B-02.log` | max_attempts and queue routing are hardcoded in task decorator/runtime. |
| B-03 | Cost of adding collector #2 | FAIL | S3 | `tests/audit/evidence/B-03.log` | Adding source #2 needs central registry/task assumptions beyond one source module. |
| B-04 | Table qualification | FAIL | S1 | `tests/audit/evidence/B-04.log` | When PROJECT is unset and table is two-part, code writes unqualified table name instead of raising. |
| B-05 | Unknown source rejection | PASS | S4 | `tests/audit/evidence/B-05.log` | Unknown source rejected with HTTP 400. |
| C-01 | Page boundary arithmetic | PASS | S1 | `tests/audit/evidence/C-01.log` | Recorded prior evidence per user instruction. |
| C-02 | Duplicate keys | FAIL | S3 | `tests/audit/evidence/C-02.log` | Duplicate keys are not deduplicated in planning. |
| C-03 | Both key types supplied | FAIL | S2 | `tests/audit/evidence/C-03.log` | Both key types accepted; order_ids silently ignored. |
| C-04 | Hostile inputs | FAIL | S2 | `tests/audit/evidence/C-04.log` | Several hostile inputs return 200/500 paths instead of clear 4xx. |
| C-05 | Unsupported order_item_ids | FAIL | S2 | `tests/audit/evidence/C-05.log` | order_item_ids path is unsupported but not explicitly validated. |
| C-06 | Page never exceeds source cap | PASS | S1 | `tests/audit/evidence/C-06.log` | Recorded prior evidence per user instruction. |
| D-01 | Raw path across UTC midnight | FAIL | S1 | `tests/audit/evidence/D-01.log` | Date-based object path changes across UTC midnight and creates duplicate keyspace. |
| D-02 | Same-day rewrite | PASS | S1 | `tests/audit/evidence/D-02.log` | Same-day rewrites overwrite same object path. |
| D-03 | Full replay | PASS | S1 | `tests/audit/evidence/D-03.log` | Replay doubles raw rows and keeps object count stable. |
| D-04 | Missing thread id | BLOCKED | S1 | `tests/audit/evidence/D-04.log` | Requires BigQuery view semantics (`incidents_current`) on clariversev1. |
| D-05 | _ingested_at under streaming insert | FAIL | S1 | `tests/audit/evidence/D-05.log` | Fake sink shows _ingested_at is not auto-populated under insert_rows_json-like path. |
| D-06 | Merge key genuinely composite | BLOCKED | S1 | `tests/audit/evidence/D-06.log` | Requires BigQuery view semantics on clariversev1. |
| D-07 | Thread explosion factor end to end | PASS | S1 | `tests/audit/evidence/D-07.log` | Full 1000-incident dataset factor observed at 2.481 (not single-sample 4-thread anecdote). |
| G-01 | Field completeness both ways | PASS | S1 | `tests/audit/evidence/G-01.log` | Field sets compared between export mapping and warehouse schema. |
| G-02 | Schema drift | PASS | S1 | `tests/audit/evidence/G-02.log` | Unexpected schema field is rejected loudly by strict FakeBQ. |
| G-03 | Numeric fidelity at incident grain | FAIL | S1 | `tests/audit/evidence/G-03.log` | FLOAT64 coercion changes >2^53 integer identity. |
| G-04 | Timezone fidelity | FAIL | S1 | `tests/audit/evidence/G-04.log` | Naive timestamp is treated as UTC and differs from +05:30 instant. |
| G-05 | Provenance columns | PASS | S1 | `tests/audit/evidence/G-05.log` | Checked _raw_uri object existence and sha256 match with raw_manifest. |
| H-01 | Authentication surface | FAIL | S1 | `tests/audit/evidence/H-01.log` | Local endpoints are unauthenticated; deployed auth depends on Cloud Run IAM configuration. |
| H-02 | Error-message redaction | FAIL | S1 | `tests/audit/evidence/H-02.log` | last_error stores unredacted exception text and is returned by /v1/dead-letter. |
| H-03 | Request completion signal | FAIL | S2 | `tests/audit/evidence/H-03.log` | collector_request remains open with null closed_at after completion. |
| H-04 | Malformed path parameters | FAIL | S3 | `tests/audit/evidence/H-04.log` | Malformed UUID path returns 500 instead of 400/404 split. |
| H-05 | Replay of identical request | FAIL | S3 | `tests/audit/evidence/H-05.log` | Identical request replay is accepted with no idempotency key. |
| H-06 | Partial defer | BLOCKED | S2 | `tests/audit/evidence/H-06.log` | Needs controlled API crash injection window on clariversev1. |
| E-01 | Hard kill mid-fetch | BLOCKED | S1 | `tests/audit/evidence/E-01.log` | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. |
| E-02 | Kill in raw-written / not-loaded window | BLOCKED | S1 | `tests/audit/evidence/E-02.log` | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. |
| E-03 | Transient failure and attempt accounting | BLOCKED | S2 | `tests/audit/evidence/E-03.log` | Needs live retry path measurement on clariversev1. |
| E-04 | Permanent source failure | BLOCKED | S2 | `tests/audit/evidence/E-04.log` | Needs long-run live queue execution on clariversev1. |
| E-05 | Orphaned pending row | FAIL | S2 | `tests/audit/evidence/E-05.log` | Pending orphan row is not recovered by sweeper; remains pending. |
| E-06 | Concurrent sweepers | BLOCKED | S3 | `tests/audit/evidence/E-06.log` | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. |
| E-07 | Killswitch under load | BLOCKED | S3 | `tests/audit/evidence/E-07.log` | Queued for clariversev1 rerun by Ranjith BK. |
| E-08 | Database outage mid-run | BLOCKED | S1 | `tests/audit/evidence/E-08.log` | Requires isolated environment outage simulation. |
| E-09 | Poison-pill payloads | BLOCKED | S1 | `tests/audit/evidence/E-09.log` | Deferred: mock fault endpoints not added in this run. |
| E-10 | Fetch outlives lease | BLOCKED | S2 | `tests/audit/evidence/E-10.log` | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. |
| F-01 | Single-worker rate | BLOCKED | S3 | `tests/audit/evidence/F-01.log` | Run on clariversev1 with dedicated measurement harness. |
| F-02 | Multi-worker ceiling | BLOCKED | S3 | `tests/audit/evidence/F-02.log` | Run on clariversev1 with worker scale control. |
| F-03 | Throughput at pilot shape | BLOCKED | S3 | `tests/audit/evidence/F-03.log` | Requires dedicated quiet run window on clariversev1. |
| F-04 | Connection pressure | BLOCKED | S2 | `tests/audit/evidence/F-04.log` | Requires scale-out deployment on clariversev1. |
| I-01 | Three open corrections | FAIL | S2 | `tests/audit/evidence/I-01.log` | workers-start check exists; sentinel_discovery/order_item_ids correction remains absent. |
| I-02 | Destructive-script guards | FAIL | S1 | `tests/audit/evidence/I-02.log` | Destructive scripts rely on env presence but do not enforce non-prod project confirmation. |
| I-03 | Make target ergonomics | FAIL | S4 | `tests/audit/evidence/I-03.log` | Documented make flows do not run cleanly in this environment context. |
| I-04 | Worker identity | PASS | S2 | `tests/audit/evidence/I-04.log` | Worker identity derivation by CLOUD_RUN_TASK_INDEX is present and deterministic in code. |
| I-05 | Periodic sweep with multiple maintenance workers | PASS | S3 | `tests/audit/evidence/I-05.log` | Recorded prior evidence per user instruction. |
| I-06 | Shell script hygiene | PASS | S4 | `tests/audit/evidence/I-06.log` | Strict mode and shellcheck output recorded. |
| J-01 | Comment claims versus behavior | FAIL | S4+ | `tests/audit/evidence/J-01.log` | Multiple strong comments overstate behavior proven false by B/E/D findings. |
| J-02 | README accuracy | FAIL | S4 | `tests/audit/evidence/J-02.log` | README paths diverge from practical local run constraints and prerequisites. |

## 5. Measured numbers

| Measure | Value | Test |
|---|---|---|
| Source calls per second, 1 worker (nominal 1.0) | not measured (BLOCKED) | F-01 |
| Source calls per second, 3 workers | not measured (BLOCKED) | F-02 |
| Source calls per second, 6 workers | not measured (BLOCKED) | F-02 |
| Global rate limiter present | not measured (BLOCKED) | F-02 |
| Wall clock, 1000 incidents / 20 pages | not measured (BLOCKED) | F-03 |
| Per-page p50 / p95 | not measured (BLOCKED) | F-03 |
| Postgres connections opened per page | not measured (BLOCKED) | F-03 |
| Peak concurrent connections, 20 workers | not measured (BLOCKED) | F-04 |
| Recovery latency after hard kill | not measured (BLOCKED) | E-01 |
| Time to dead-letter, permanent failure | not measured (BLOCKED) | E-04 |
| Source calls burned before dead-letter | not measured (BLOCKED) | E-04 |
| Thread explosion factor observed | `2.481` (`2481/1000`) | D-07 |
| BigQuery raw rows / incidents_current rows | not measured on real BigQuery (fake-run used for local checks) | D-07 |
| procrastinate_jobs growth over 4 min paused | not measured (BLOCKED) | E-07 |

## 6. Hypotheses: confirmed or refuted

| # | Hypothesis (abbreviated) | Verdict | Test | Note |
|---|---|---|---|---|
| H1 | Raw path duplicates across UTC midnight | confirmed | D-01 | Observed date boundary key drift. |
| H2 | Record.key never reaches sink; null threads collapse | blocked | D-04 | Needs BigQuery view execution. |
| H3 | Sweeper never recovers pending rows | confirmed | E-05 | Sweep logic scopes to in_progress rows. |
| H4 | status=failed unreachable | blocked | E-04 | Deferred to long-run failure path. |
| H5 | last_error leaks DSN via dead-letter | confirmed | H-02 | Unredacted `str(exc)` persisted/exposed. |
| H6 | Killswitch re-defer unbounded | blocked | E-07 | Deferred to paused-load run. |
| H7 | No global rate ceiling | blocked | F-02 | Deferred to scaled workers run. |
| H8 | orderItemId FLOAT64 loses precision | confirmed | G-03 | Observed >2^53 coercion drift. |
| H9 | _ingested_at null under streaming insert | confirmed | D-05 | Fake sink demonstrates null/default gap. |
| H10 | Both key types drop order_ids | confirmed | C-03 | Observed no conflict rejection. |
| H11 | collector_request never closes | confirmed | H-03 | Status remains open, closed_at null. |
| H12 | Hardcoded queue/retry and unread max_attempts | confirmed | B-02/B-03 | Static evidence in task decorator/runtime. |
| H13 | No timezone normalization | confirmed | G-04 | Naive vs offset forms diverge. |
| H14 | Three open corrections not all at head | confirmed | I-01 | (c) remains outstanding. |

## 7. What held up

- Prior-recorded checks held as directed: A-01, A-03, C-01, C-06, I-05.
- Protocol conformance basics hold: collector protocol attributes present and unknown source rejection works (B-01, B-05).
- Same-day deterministic overwrite behavior for raw object naming is correct (D-02).
- Full replay with fake sinks doubles append rows while preserving object count (D-03).
- Export/warehouse field alignment and provenance linking checks pass under fake sink harness (G-01, G-05).
- Worker identity derivation and shell strict-mode coverage are present in code/scripts (I-04, I-06).

## 8. Blocked, and what unblocks each

| Test | Why blocked | What unblocks it | Who |
|---|---|---|---|
| A-04 | Container runtime/image build path not available in this run. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| D-04 | Requires BigQuery view semantics (`incidents_current`) on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| D-06 | Requires BigQuery view semantics on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| H-06 | Needs controlled API crash injection window on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-01 | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-02 | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-03 | Needs live retry path measurement on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-04 | Needs long-run live queue execution on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-06 | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-07 | Queued for clariversev1 rerun by Ranjith BK. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-08 | Requires isolated environment outage simulation. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-09 | Deferred: mock fault endpoints not added in this run. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| E-10 | Sink-dependent recovery assertion queued for clariversev1 rerun by Ranjith BK. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| F-01 | Run on clariversev1 with dedicated measurement harness. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| F-02 | Run on clariversev1 with worker scale control. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| F-03 | Requires dedicated quiet run window on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |
| F-04 | Requires scale-out deployment on clariversev1. | Run on clariversev1 with real sinks and Cloud Run workers: `set -a && source .env && set +a && export PYTHONPATH="$PWD" && .venv/bin/python tests/audit/run_audit.py` (plus per-test setup from protocol). | Ranjith BK |

Maintenance worker note required by request: the maintenance worker is necessary but not sufficient for E-01/E-02/E-05/E-06/E-07/E-10/I-05; those assertions depend on validated GCS/BigQuery sink semantics and are queued for clariversev1 rerun by Ranjith BK.

## 9. Changes made to the repository during the audit

| File | Change | Why |
|---|---|---|
| `tests/audit/fakes.py` | added | strict local sink fakes per protocol 2.2 |
| `tests/audit/run_audit.py` | added | ordered A→B→C→D→G→H→E→F→I→J audit harness with evidence generation |
| `tests/audit/evidence/*.log` | added | per-test command/output/state evidence |
| `tests/audit/results.json` | added | structured result summary and monkeypatch inventory |

**Monkeypatch sites and production-class overrides.**

| Site (module:target) | Override | Test(s) | Restored after |
|---|---|---|---|
| `collector.load.bigquery.Client` | `tests.audit.run_audit.CaptureClient` | B-04 | yes |
| `collector.raw.datetime` | `tests.audit.run_audit.FakeDateTime` | D-01 | yes |
| `collector.raw.storage.Client` | `tests.audit.fakes.FakeGCS.Client` | D-01, D-02, D-03, G-02, G-03, G-04, G-05 | yes |
| `collector.load.bigquery.Client` | `tests.audit.fakes.FakeBQ.Client` | D-03, G-02, G-03, G-04, G-05 | yes |

Confirmed: no file under `collector/`, `sql/`, `scripts/`, `Makefile`, `Dockerfile` or `requirements.txt` was modified in this audit run.

## 10. Suggested work order

1. Address S1 correctness boundaries first: `B-04`, `D-01`, `D-05`, `G-03`, `G-04`, `H-01`, `H-02`, `I-02`.
2. Resolve S2 request/recovery semantics: `C-03`, `C-04`, `C-05`, `E-05`, `H-03`, `H-04`, then rerun blocked E/F tests on clariversev1.
3. Re-run all sink-dependent blocked tests on clariversev1 and replace indicative fake-sink findings where required (explicitly D-05, G-02, G-03, G-04).
4. Tidy S3/S4 contract-doc drift after runtime correctness is stable (`A-02`, `A-05`, `B-02`, `B-03`, `J-01`, `J-02`).
