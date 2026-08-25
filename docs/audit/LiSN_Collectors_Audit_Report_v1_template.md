# LiSN Collectors — Acceptance Audit Report

Repository: github.com/Yaaradata/lisn-collectors · Commit under test: `<git rev-parse HEAD>`
Protocol: LiSN_Collectors_Audit_Protocol_v1 · Executed by: Cursor cloud agent
Date: `<date>` · Total elapsed: `<hh:mm>`

> Agent: fill every angle-bracket placeholder. Delete no section. A section with nothing to report says "None." — an omitted section reads as an oversight.

## 1. Environment of record

| Item | Value |
|---|---|
| Commit | `<sha>` |
| Python | `<version>` |
| PostgreSQL | `<version>` |
| procrastinate | `<version>` |
| psycopg | `<version>` |
| fastapi / uvicorn | `<versions>` |
| google-cloud-storage / bigquery | `<versions>` |
| GCS sink | real / fake (`tests/audit/fakes.py`) |
| BigQuery sink | real / fake (`tests/audit/fakes.py`) |
| Seeded incidents | `<n>` |

**Fidelity caveat.** Tests D-05, G-02, G-03 and G-04 ran against local fakes, not against BigQuery. BigQuery's streaming-insert path handles column defaults, partitioning and numeric coercion differently from any local fake. These four findings are indicative and must be re-run on `clariversev1` before they are treated as settled.

**FakeBQ must be strict.** `tests/audit/fakes.py` `FakeBQ` parses the column list **and types** from `sql/003_bigquery.sql` and rejects any row carrying a field not in that schema (mirroring `insert_rows_json` returning per-row errors, which `collector/load.py` turns into a `RuntimeError`). It also coerces values to the declared type — `FLOAT64` for `orderItemId` / `orderItemUnitId` / `threads_communicationId`, `INT64`, `TIMESTAMP`, `BOOL` — and does not auto-populate the `_ingested_at` `DEFAULT`. A permissive fake makes **G-02** (schema drift) and **G-03** (numeric fidelity) false passes and hides **D-05**.

Full dependency snapshot: `tests/audit/evidence/pip-freeze.txt`.

## 2. Headline

`<Three to five sentences. What state is this code in, judged as a component the pilot will depend on. Lead with the single most consequential finding. No praise padding, no hedging.>`

| | Count |
|---|---|
| Tests executed | `<n>` |
| Passed | `<n>` |
| Failed | `<n>` |
| Blocked | `<n>` |
| S1 defects | `<n>` |
| S2 defects | `<n>` |
| S3 defects | `<n>` |
| S4 defects | `<n>` |

## 3. Defects, S1 first

One block per defect, ordered by severity then by test ID. Do not merge two defects into one block because they share a file.

### D-`<nn>` · `<one-line title>`

| | |
|---|---|
| Severity | S`<n>` |
| Test | `<TEST-ID>` |
| Origin | `<file>:<line>` |
| Evidence | `tests/audit/evidence/<TEST-ID>.log` |
| Reproduces | always / intermittently (`<n>` of `<n>` runs) |

**Expected.** `<What the protocol required, and why — the design commitment or contract it comes from.>`

**Observed.** `<What actually happened. Concrete: counts, row values, exit codes, timings.>`

**Consequence in the pilot.** `<What this does to real Flipkart data or to a demo. Be specific: which incidents, how many, detected or silent.>`

**Blast radius.** `<Which other tests or components depend on this behaviour.>`

(Repeat this block for every defect.)

## 4. Full results table

| Test | Name | Result | Sev | Evidence | Note |
|---|---|---|---|---|---|
| A-01 | Install idempotent | | | | |
| A-02 | Dependency reproducibility | | | | |
| A-03 | Import without GCP | | | | |
| A-04 | One image, three roles | | | | |
| A-05 | Secret hygiene / env completeness | | | | |
| B-01 | Protocol satisfaction | | | | |
| B-02 | Declared fields nothing reads | | | | |
| B-03 | Cost of adding collector #2 | | | | |
| B-04 | Table qualification | | | | |
| B-05 | Unknown source rejection | | | | |
| C-01 | Page boundary arithmetic | | | | |
| C-02 | Duplicate keys | | | | |
| C-03 | Both key types supplied | | | | |
| C-04 | Hostile inputs | | | | |
| C-05 | Unsupported order_item_ids | | | | |
| C-06 | Page never exceeds source cap | | | | |
| D-01 | Raw path across UTC midnight | | | | |
| D-02 | Same-day rewrite | | | | |
| D-03 | Full replay | | | | |
| D-04 | Missing thread id | | | | |
| D-05 | _ingested_at under streaming insert | | | | |
| D-06 | Merge key genuinely composite | | | | |
| D-07 | Thread explosion factor end to end | | | | |
| E-01 | Hard kill mid-fetch | | | | |
| E-02 | Kill in raw-written / not-loaded window | | | | |
| E-03 | Transient failure, attempt accounting | | | | |
| E-04 | Permanent source failure | | | | |
| E-05 | Orphaned pending row | | | | |
| E-06 | Concurrent sweepers | | | | |
| E-07 | Killswitch under load | | | | |
| E-08 | Database outage mid-run | | | | |
| E-09 | Poison-pill payloads | | | | |
| E-10 | Fetch outlives the lease | | | | |
| F-01 | Single-worker rate | | | | |
| F-02 | Multi-worker ceiling | | | | |
| F-03 | Throughput at pilot shape | | | | |
| F-04 | Connection pressure | | | | |
| G-01 | Field completeness both ways | | | | |
| G-02 | Schema drift | | | | |
| G-03 | Numeric fidelity at incident grain | | | | |
| G-04 | Timezone fidelity | | | | |
| G-05 | Provenance columns | | | | |
| H-01 | Authentication surface | | | | |
| H-02 | Error-message redaction | | | | |
| H-03 | Request completion signal | | | | |
| H-04 | Malformed path parameters | | | | |
| H-05 | Replay of identical request | | | | |
| H-06 | Partial defer | | | | |
| I-01 | Three open corrections | | | | |
| I-02 | Destructive-script guards | | | | |
| I-03 | Make target ergonomics | | | | |
| I-04 | Worker identity | | | | |
| I-05 | Periodic sweep, multiple workers | | | | |
| I-06 | Shell script hygiene | | | | |
| J-01 | Comment claims versus behaviour | | | | |
| J-02 | README accuracy | | | | |

## 5. Measured numbers

Every figure here is quoted downstream, so each carries its test ID. Anything not measured is marked "not measured", never estimated.

| Measure | Value | Test |
|---|---|---|
| Source calls per second, 1 worker (nominal 1.0) | `<n>` | F-01 |
| Source calls per second, 3 workers | `<n>` | F-02 |
| Source calls per second, 6 workers | `<n>` | F-02 |
| Global rate limiter present | yes / no | F-02 |
| Wall clock, 1000 incidents / 20 pages | `<mm:ss>` | F-03 |
| Per-page p50 / p95 | `<s>` / `<s>` | F-03 |
| Postgres connections opened per page | `<n>` | F-03 |
| Peak concurrent connections, 20 workers | `<n>` | F-04 |
| Recovery latency after hard kill | `<s>` | E-01 |
| Time to dead-letter, permanent failure | `<mm:ss>` | E-04 |
| Source calls burned before dead-letter | `<n>` | E-04 |
| Thread explosion factor observed | `<n.nnn>` | D-07 |
| BigQuery raw rows / incidents_current rows | `<n>` / `<n>` | D-07 |
| procrastinate_jobs growth over 4 min paused | `<n>` → `<n>` | E-07 |

## 6. Hypotheses: confirmed or refuted

The protocol carried fourteen hypotheses from a code read. Each gets a verdict. Refuted ones matter — they tell the developer the read was wrong, not the code.

| # | Hypothesis (abbreviated) | Verdict | Test | Note |
|---|---|---|---|---|
| H1 | Raw path duplicates across UTC midnight | confirmed / refuted / blocked | D-01 | |
| H2 | Record.key never reaches the sink; null threads collapse | | D-04 | |
| H3 | Sweeper never recovers pending rows | | E-05 | |
| H4 | status='failed' unreachable | | E-04 | |
| H5 | last_error leaks the DSN via /v1/dead-letter | | H-02 | |
| H6 | Killswitch re-defer is unbounded | | E-07 | |
| H7 | No global rate ceiling | | F-02 | |
| H8 | orderItemId as FLOAT64 loses precision | | G-03 | |
| H9 | _ingested_at null under streaming insert | | D-05 | |
| H10 | Both key types → order_ids silently dropped | | C-03 | |
| H11 | collector_request never closes | | H-03 | |
| H12 | Queue name and retry policy hardcoded, max_attempts unread | | B-02, B-03 | |
| H13 | No timezone normalisation | | G-04 | |
| H14 | Three open corrections not all at head | | I-01 | |

## 7. What held up

Not a courtesy section. The developer needs to know which behaviour is proven so it is not disturbed by the fixes, and the lead needs it to know what can be presented to Flipkart's tech team.

`<List each proven property with its test ID. For example: raw bytes are preserved unmodified as evidence of what the source returned (D-02, E-09); mark-done happens only after the sink write commits (E-02); page boundaries never exceed the source cap (C-06).>`

## 8. Blocked, and what unblocks each

| Test | Why blocked | What unblocks it | Who |
|---|---|---|---|
| `<ID>` | `<no GCP credentials / no Cloud Run>` | `<the exact command to run on clariversev1>` | Ranjith BK |

These are handed back unresolved. The audit does not claim the code passes them.

## 9. Changes made to the repository during the audit

Per rule R2, the agent may add test code and mock fault knobs only. Every change is listed here so the developer can review or discard it.

| File | Change | Why |
|---|---|---|
| `tests/audit/...` | added | audit test |
| `mock/sentinel_api.py` | `<the specific admin endpoint added>` | needed by `<TEST-ID>` |

**Monkeypatch sites and production-class overrides.** List every runtime override applied during the audit — not just the files created. Each monkeypatch/patch site (module and target), the replacement, the tests that use it, and whether it is restored after the test. Substituting `collector.raw`'s `storage` / `collector.load`'s `bigquery` (or `write_raw` / `append_records`) with a fake is a behavioural substitution and must appear here — an override left off this list reads as a hidden change to the code under test.

| Site (module:target) | Override | Test(s) | Restored after |
|---|---|---|---|
| `<e.g. collector.load.bigquery.Client>` | `<FakeBQ>` | `<TEST-IDs>` | yes / no |
| `<e.g. collector.raw.storage.Client>` | `<FakeGCS>` | `<TEST-IDs>` | yes / no |

Confirm and state explicitly: no file under `collector/`, `sql/`, `scripts/`, `Makefile`, `Dockerfile` or `requirements.txt` was modified. If any was, say which and why, and expect the finding to be treated as invalid.

## 10. Suggested work order

Sequenced by severity and by dependency, not by effort. No fix is prescribed — `<...>`.
