# LiSN Collectors — Acceptance Audit Protocol

**Repository under test:** `github.com/Yaaradata/lisn-collectors` (branch `main`, head `fff7187`)
**Built by:** Ranjith BK · **Audit commissioned by:** workstream lead
**Executor:** Cursor cloud agent · **Version:** v1 · 25 August 2026

This document is the agent's brief. It says what to run, what counts as a pass, and what to write down. It is deliberately adversarial: the code is well written and lints clean, so the value of this exercise is in behaviour under failure, replay, drift and load — not in style.

Nothing in here is a fix instruction. The agent tests and reports. The developer fixes.

---

## 1. Operating rules for the agent

**R1 — Read-only on the code under test.** Do not modify `collector/`, `sql/`, `scripts/`, `Makefile`, `Dockerfile`, `requirements.txt`. If a test cannot run without a change, mark it BLOCKED and state the one-line change that would unblock it. Do not make the change.

**R2 — New files only in `tests/audit/` and `mock/`.** Test code, fixtures and sink fakes go in `tests/audit/`. If a test needs a new fault knob (slow response, malformed body, extra field), add it to `mock/sentinel_api.py` under an `/admin/` path, and list every mock change in section 9 of the report. Mock changes are permitted because the mock is scaffolding, not the deliverable.

**R3 — No fixes, no refactors, no "while I was there".** A failing test is the output. A passing test after a quiet fix is a lie to the reviewer.

**R4 — Evidence or it did not happen.** Every test records: the exact command, the raw output (trimmed to the relevant lines), and the SQL/HTTP state that proves the assertion. Store transcripts under `tests/audit/evidence/<TEST-ID>.log`. Reference the file in the report.

**R5 — Three outcomes only.** PASS, FAIL, BLOCKED. Never "partial". If a test has two assertions and one fails, it is FAIL and the report says which assertion.

**R6 — BLOCKED is not FAIL.** No GCP credentials means the real-GCS and real-BigQuery tests cannot run. Those are BLOCKED, listed in section 8 of the report, and handed back for the developer to run on `clariversev1`. Do not fake a result and call it a pass.

**R7 — Determinism.** Every test must be re-runnable from a clean database. Reset with `bash .cursor/install.sh` plus a truncate of `collector_request`, `collector_job`, `raw_manifest` and the `procrastinate_*` tables before any test that counts rows.

**R8 — Timebox.** If a single test exceeds 15 minutes wall clock, stop it, record the elapsed time and mark it FAIL with reason `timeout`, unless the test explicitly says otherwise (E-04, F-03).

**R9 — Record versions.** At the start, capture `pip freeze`, `python --version`, `psql --version`, `git rev-parse HEAD`. Pin them at the top of the report. Floating version specifiers in `requirements.txt` mean a later run may not reproduce this one.

---

## 2. Environment

The production stack is Cloud SQL, GCS, BigQuery and Cloud Run in `clariversev1` / `asia-south1`. The agent environment has none of that. It has local Postgres, the mock Sentinel, the request API and a Procrastinate worker — which `.cursor/install.sh` already sets up.

### 2.1 Bootstrap

```bash
bash .cursor/install.sh          # postgres, venv, schemas, 1000 seeded incidents
bash .cursor/start.sh            # cluster online
set -a && source .env && set +a
export PYTHONPATH="$PWD"
```

Three long-running processes, one terminal each (the `.cursor/environment.json` terminals do this already):

| Process | Command | Port |
|---|---|---|
| mock Sentinel | `.venv/bin/python -m uvicorn mock.sentinel_api:app --port 8081` | 8081 |
| request API | `.venv/bin/python -m uvicorn collector.api:api --port 8080` | 8080 |
| sentinel worker | `.venv/bin/python -m procrastinate worker -q sentinel -c 1 --delete-jobs never` | — |
| maintenance worker | `.venv/bin/python -m procrastinate worker -q maintenance -c 1 --delete-jobs never` | — |

### 2.2 The sink problem, and the shim

`collector/raw.py` calls `storage.Client()` directly and `collector/load.py` calls `bigquery.Client()` directly. There is no injection seam, so with no GCP credentials **every existing test in `tests/` fails at import or at first write.** Confirm this first (A-03), then build the shim.

Create `tests/audit/fakes.py` providing:

- **`FakeGCS`** — monkeypatches `collector.raw.storage.Client` to write objects under `tests/audit/_gcs/<bucket>/<object_name>`, preserving overwrite semantics (same path = one object) and exposing a list function.
- **`FakeBQ`** — monkeypatches `collector.load.bigquery.Client` so `insert_rows_json` appends rows to a local Postgres table `audit_bq_rows(table_id text, row jsonb, inserted_at timestamptz default now())` in the `collector` database, and returns `[]` on success or an error list when a row contains a field not in a declared schema (parsed from `sql/003_bigquery.sql`).

`FakeBQ` **must** enforce the declared schema — unknown-field rejection and type coercion are the whole point of G-02 and G-03. A permissive fake turns a real defect into a false pass.

**Fidelity caveat, to be printed verbatim in the report:** results from D-05, G-02, G-03 and G-04 under the fake sinks are indicative only. BigQuery's streaming insert path handles column defaults, partitioning and numeric coercion differently from any local fake. Those four must be re-run against `clariversev1` by the developer before the finding is treated as settled.

---

## 3. Severity scale

| Sev | Meaning | Example |
|---|---|---|
| **S1** | Silent data loss or corruption, or a credential exposure. Blocks pilot. | A page reaches BigQuery twice and the merge view does not collapse it |
| **S2** | Loud failure with no automatic recovery; needs a human every time it happens | A row that can never be recovered by the sweeper |
| **S3** | Correct but unbounded, unenforced, or contradicting a stated commitment | Rate ceiling is per-worker with nothing enforcing the global limit |
| **S4** | Contract drift, dead code, missing validation, ergonomics | A declared contract field nothing reads |

Assign severity from observed behaviour, not from how hard it looks to fix.

---

## 4. Pre-audit hypotheses

These came out of reading the code. They are **hypotheses, not findings.** Each is bound to a test below. The agent's job is to confirm or refute each one with evidence. Refuting one is as valuable as confirming it — say so plainly in the report.

| # | Hypothesis | Test |
|---|---|---|
| H1 | `write_raw` derives the object path from `datetime.now(utc)` inside the function, so a retry that crosses UTC midnight writes a **second** object, a second `raw_manifest` row, and loads the page to BigQuery twice. The existing determinism test writes twice in the same second and cannot see this. | D-01 |
| H2 | `Record.key` — including the `"none"` fallback for a missing thread id — is never read by `load.py`. Rows with no `threads.id` land in BigQuery with `threads_id` NULL, and the merge view's `PARTITION BY id, threads_id` collapses all of an incident's null-thread rows to one. | D-04 |
| H3 | The sweeper recovers `in_progress` rows with an expired lease and stalled Procrastinate jobs. It never looks at `pending` rows. A row written by `/v1/collect` whose `defer` did not complete is orphaned permanently — which contradicts the comment in `api.py` claiming the sweeper finds them. | E-05 |
| H4 | The exception handler in `fetch_page` writes `last_error` and re-raises but never sets `status='failed'`. The `'failed'` value in the CHECK constraint is unreachable. A permanently failing page only dies via lease expiry plus five attempts. | E-04 |
| H5 | `last_error` stores `str(exc)[:4000]` unredacted. A psycopg connection error carries the DSN including the password, and `/v1/dead-letter` returns `last_error` to any caller. | H-02 |
| H6 | The killswitch path re-defers a paused job every 15 seconds with no backoff and no cap. With `--delete-jobs never` and a few hundred paused pages, `procrastinate_jobs` grows without bound while paused. | E-07 |
| H7 | `min_interval_s` is a per-worker sleep. Nothing enforces a global ceiling. With three Cloud Run tasks the real rate is 3×, and a rolling deploy briefly makes it 6×. Multi Track's calls-per-second ceiling is the one binding constraint in the architecture. | F-02 |
| H8 | `orderItemId` and `orderItemUnitId` are `FLOAT64` in `sql/003_bigquery.sql`. The grain of an incident is `order_item_id`. Any value above 2^53 loses precision, which silently changes the key. | G-03 |
| H9 | `_ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()` is populated by `insert_rows_json`, the legacy streaming path, which does not apply column defaults. If the value lands NULL, the table partitions into `__NULL__` and the merge view's tiebreak `ORDER BY updatedOn DESC, _ingested_at DESC` degenerates. | D-05 |
| H10 | `plan()` takes `incident_ids` when both key types are supplied and silently discards `order_ids`. | C-03 |
| H11 | `collector_request.status` is set to `'open'` at insert and never changed; `closed_at` is never written. LiSN has no way to ask "is my request finished" except by counting jobs itself. | H-03 |
| H12 | `@app.task(queue="sentinel")` and `RetryStrategy(max_attempts=3)` are hardcoded rather than read from the source object, and `SourceCollector.max_attempts` is never read anywhere. Adding collector #2 is not "one new source module" as the contract docstring claims. | B-02, B-03 |
| H13 | No timezone normalisation exists anywhere between the mock's naive ISO strings and BigQuery `TIMESTAMP`. If Sentinel emits IST, every timestamp is 5h30m wrong and nothing detects it. | G-04 |
| H14 | The three open corrections from the Vinodh call (`workers-start` active-execution check, reset ordering FK violation, `sentinel_discovery` source and `order_item_ids` key type) are not all present at head. | I-01 |

---

## 5. Test catalogue

Format per test: **intent → procedure → pass criterion**. If the pass criterion is not met, it is FAIL at the stated severity.

### A — Environment and build integrity

**A-01 · Install is idempotent**
Run `.cursor/install.sh` twice from a clean clone. Second run must exit 0, must not re-seed, must not duplicate `.env` keys, and must leave `sentinel_incident` at exactly 1000 rows.
*Pass:* exit 0 both times, 1000 rows, no duplicate keys in `.env`. *Sev if fail:* S4.

**A-02 · Dependency reproducibility**
`requirements.txt` pins `procrastinate==3.9.0` exactly but leaves `psycopg`, `fastapi`, `uvicorn`, `google-cloud-*`, `httpx`, `pytest` on floating minor/major ranges, and there is no lock file. Capture `pip freeze`. State whether a build six weeks from now is guaranteed to resolve identically.
*Pass:* every runtime dependency resolves to a version pinned or constrained such that a rebuild is deterministic. *Sev if fail:* S3.

**A-03 · Import without GCP credentials**
`python -c "import collector.api; import collector.tasks; import collector.raw; import collector.load"` with no `GOOGLE_APPLICATION_CREDENTIALS` and empty `PROJECT`/`RAW_BUCKET`.
*Pass:* imports succeed (client construction must be lazy). Separately record whether any existing test in `tests/` can run without GCP — expected: none. *Sev if fail:* S4 for import failure; the untestability itself is reported as a standalone S3 observation.

**A-04 · One image, three roles**
Build the Dockerfile. Run the same image three ways with overridden commands: mock, API, worker. Confirm `PYTHONPATH=/app` is sufficient and no CMD is baked in.
*Pass:* all three start and answer (`/health` for the two services, a heartbeat row in `procrastinate_workers` for the worker). *Sev if fail:* S2.

**A-05 · Secret hygiene and env completeness**
(a) `git log -p --all | grep -nEi "password|postgresql://|BEGIN .*PRIVATE KEY"` — no live credential may appear in history.
(b) Confirm `.env` is git-ignored and not committed.
(c) Grep every `os.environ[...]` / `os.environ.get(...)` key across `collector/`, `mock/`, `scripts/`, `tests/` and diff the set against `.env.example`. Expect gaps including `COLLECTOR_API_URL`, `MOCK_SENTINEL_URL`, `USE_ID_TOKEN`, `COLLECTOR_API_TOKEN`.
*Pass:* no credentials in history, `.env` ignored, `.env.example` complete. *Sev if fail:* S1 for a real credential in history, S4 for env drift.

### B — Contract conformance

**B-01 · Protocol satisfaction**
`isinstance(SentinelCollector(), SourceCollector)` is True, and every attribute declared in the Protocol is present with the declared type.
*Pass:* both hold. *Sev if fail:* S4.

**B-02 · Declared fields that nothing reads**
For each of `batch_cap`, `min_interval_s`, `lease_seconds`, `max_attempts`, `bq_table`, `name`: grep for the reads and prove by experiment which ones actually govern behaviour. Set `SentinelCollector.max_attempts = 1` in a test-local subclass, run a page that always fails, and observe how many attempts occur.
*Pass:* every declared field changes behaviour when changed. *Sev if fail:* S4 (H12).

**B-03 · Cost of adding collector #2**
Add a trivial second source in `tests/audit/` (`name="probe"`, returns a fixed payload) and register it. Drive one page through `/v1/collect` end to end.
*Pass:* it works with **zero** edits outside the new source module and the registry. Report the exact list of files that had to change. The contract docstring claims one module; test the claim. *Sev if fail:* S3.

**B-04 · Table qualification**
Call `append_records` with `PROJECT` unset, with `PROJECT` set and a two-part `bq_table`, and with a three-part `bq_table`. Confirm the target resolved in each case.
*Pass:* no silent write to the wrong project; unqualified with no PROJECT raises rather than guessing. *Sev if fail:* S1.

**B-05 · Unknown source rejection**
`POST /v1/collect {"source":"ekart",...}`.
*Pass:* HTTP 400, message names the known sources, no row written to `collector_request`. *Sev if fail:* S4.

### C — Planner and paging

**C-01 · Page boundary arithmetic**
For key counts 0, 1, 49, 50, 51, 99, 100, 101, 999, 1000: assert page count equals `ceil(n/50)`, every page has ≤ 50 keys, the concatenation of all pages equals the input list exactly (same order, no loss, no duplication), and `page_no` runs 0..n-1 with no gaps.
*Pass:* all ten cases. *Sev if fail:* S1.

**C-02 · Duplicate keys**
Send 60 keys of which 20 are duplicates. Record whether they are deduplicated, and if not, how many extra source calls and BigQuery rows result.
*Pass:* either deduplicated, or the behaviour is documented and the merge view collapses the duplicates. *Sev if fail:* S3.

**C-03 · Both key types supplied**
`{"incident_ids":[...10...], "order_ids":[...10...]}`.
*Pass:* HTTP 400 naming the conflict. Silently dropping `order_ids` is a FAIL at S2 (H10).

**C-04 · Hostile inputs**
`query_spec` variants: `{}`, `{"incident_ids":[]}`, `{"incident_ids":null}`, `{"incident_ids":"C-1"}` (string not list), `{"incident_ids":[null,1,{}]}`, one key of 10,000 characters, and 100,000 keys.
*Pass:* every case returns 4xx with a clear message, or completes without unbounded memory use. Record wall-clock time, peak memory, row count, and the size of the single transaction for the 100k case. An uncaught 500 is FAIL at S2.

**C-05 · Unsupported key type `order_item_ids`**
`{"order_item_ids":[...]}` — the grain correction from the Vinodh call.
*Pass:* HTTP 400 with a clear message (the key type is not implemented yet, and must not be silently ignored). Record in the report that the correction is outstanding. *Sev if fail:* S2.

**C-06 · Page never exceeds the source cap**
Property test: for 200 random key-count values in 1..5000, no page exceeds `MAX_IDS_PER_CALL` (50) and the mock never returns 400 for size.
*Pass:* zero violations. *Sev if fail:* S1.

### D — Idempotency, determinism, merge correctness

**D-01 · Raw path across UTC midnight** *(H1 — priority test)*
Monkeypatch `collector.raw.datetime` so the first `write_raw` sees `23:59:59Z` and the second sees `00:00:01Z` the next day, with identical `(source, request_id, page_no, body)`.
*Pass:* one object, one `raw_manifest` row, one BigQuery load. Two objects with different `dt=` prefixes is FAIL at **S1** — it is a duplicate load with no dedup key, and the existing `test_raw_determinism.py` cannot detect it.

**D-02 · Same-day rewrite**
Two `write_raw` calls, same second, same inputs.
*Pass:* identical URI, identical sha256, exactly one object under the prefix. *Sev if fail:* S1.

**D-03 · Full replay**
Run a 200-key request to completion. Reset only `collector_job.status` to `pending` and re-defer every job. Let it complete again.
*Pass:* GCS object count unchanged; BigQuery raw rows exactly doubled; `incidents_current` row count **identical to the first run**, and every `(id, threads_id)` pair appears exactly once. *Sev if fail:* S1.

**D-04 · Missing thread id** *(H2)*
Seed three incidents into the mock with no thread rows (so `threads.id` is absent from the export). Drive them through.
*Pass:* each incident is retrievable from `incidents_current` without loss, and rows with no thread are distinguishable. If N null-thread rows for one incident collapse to 1, that is FAIL at **S1** — and the report must note that `Record.key`'s `"none"` fallback never reaches the sink.

**D-05 · `_ingested_at` under streaming insert** *(H9, fidelity-caveated)*
After a run, `SELECT count(*) WHERE _ingested_at IS NULL`.
*Pass:* zero. Non-zero is FAIL at S1 (null partition plus a degenerate merge tiebreak). If the fake sink cannot represent BigQuery default behaviour faithfully, mark BLOCKED and hand to the developer with the exact query.

**D-06 · Merge key is genuinely composite**
Craft two rows: same `id`, different `threads_id`, different `updatedOn`. Then two rows: same `id`, same `threads_id`, different `updatedOn`.
*Pass:* the first pair yields 2 rows in the view; the second yields 1, the newer. *Sev if fail:* S1.

**D-07 · Thread explosion factor holds end to end**
Full 1000-incident run.
*Pass:* raw BigQuery rows / distinct `id` is within ±2% of the 2.481 seed factor, and `incidents_current` row count equals the distinct `(id, threads_id)` count in the mock database. *Sev if fail:* S1.

### E — Failure and recovery

**E-01 · Hard kill mid-fetch**
Start a 100-key request. `SIGKILL` the worker at roughly page 5. Restart the worker and the sweeper.
*Pass:* every job reaches `done`; GCS object count equals page count exactly; `incidents_current` count matches a clean run. Record recovery latency. *Sev if fail:* S1.

**E-02 · Kill in the raw-written / not-loaded window**
Patch the fake BQ sink to block on a semaphore, kill the worker while a page is between `write_raw` and the `collector_job` update, then call `/v1/reconcile?minutes=0`.
*Pass:* reconcile reports exactly that page; after the sweeper runs, the page loads and reconcile returns zero. *Sev if fail:* S1 — this is the failure mode the review called non-negotiable.

**E-03 · Transient source failure and attempt accounting**
Inject a fault for one incident id via `/admin/fault/{id}`, let it fail twice, then clear it.
*Pass:* the page recovers; `collector_job.attempts` equals the number of real source calls counted at `/admin/stats`. If `attempts` is incremented both by the task body's claim and by sweeper-driven requeue after Procrastinate's own retry, the count inflates and the job dead-letters early — FAIL at S2.

**E-04 · Permanent source failure** *(H4, no 15-minute timebox — run to conclusion, cap 30 min)*
Fault an id permanently.
*Pass:* the job reaches `status='dead'`, appears in `/v1/dead-letter`, and stops consuming source calls. Record wall-clock time to dead and total source calls burned. If the row passes through `'failed'` — or if `'failed'` is proven unreachable — record it. Never reaching `dead` is FAIL at S2.

**E-05 · Orphaned pending row** *(H3 — priority test)*
Insert a valid `collector_request` and one `collector_job` at `pending` directly in SQL, with **no** Procrastinate job. Run `sweep_now` three times over five minutes.
*Pass:* the row is picked up and processed. Still `pending` after three sweeps is FAIL at **S2**, and the report must quote the `api.py` comment that claims the sweeper finds orphan rows.

**E-06 · Concurrent sweepers**
Run two maintenance workers. Create 20 expired-lease rows. Trigger both sweeps within the same second.
*Pass:* each page is fetched at most once (verify against `/admin/stats` delta); no row is double-deferred. *Sev if fail:* S3.

**E-07 · Killswitch under load** *(H6)*
Queue 300 pages. `make pause SOURCE=sentinel`. Hold for four minutes, sampling `SELECT count(*) FROM procrastinate_jobs` and `/admin/stats` every 15 seconds. Then resume.
*Pass:* zero source calls while paused; `procrastinate_jobs` growth is bounded; every page completes after resume. Unbounded growth is FAIL at S3. Any source call during pause is FAIL at S1.

**E-08 · Database outage mid-run**
Stop the Postgres cluster for 30 seconds during an active run, then restart.
*Pass:* no job is marked `done` without a corresponding sink write; all pages eventually complete; the API returns 5xx rather than a wrong answer while the DB is down. *Sev if fail:* S1.

**E-09 · Poison-pill payloads**
Add mock fault modes returning: truncated JSON, an HTML error page with `content-type: text/html`, a 200 with an empty body, and a valid JSON envelope with `incidents` as a string.
*Pass:* for each, the raw bytes still land in GCS (evidence preserved), the parse failure is loud, the page retries a bounded number of times and then dead-letters, and the rest of the request completes. A page that blocks the request indefinitely, or a parse error that marks the page `done`, is FAIL at S1.

**E-10 · Fetch outlives the lease**
Add a mock slow mode. Set the test source's `lease_seconds` to 5 via a subclass and make one fetch take 20 seconds so the lease expires while work is genuinely in flight.
*Pass:* the sweeper does not requeue a page that is actually being worked; or if it does, the double fetch is detected and only one load occurs. Two source calls plus two loads for one page is FAIL at S2 — and note that the production value is 300 s against a real Sentinel with no measured p99.

### F — Rate and throughput

**F-01 · Single-worker rate**
One worker, 120 pages, `min_interval_s = 1.0`. Reset `/admin/stats`, run 60 seconds, read the counter.
*Pass:* observed calls/second ≤ 1.0. Record the actual figure — the sleep precedes the fetch, so per-page cost is `interval + fetch_duration` and the real rate will be below the nominal ceiling. Report both numbers.

**F-02 · Multi-worker ceiling** *(H7 — priority test)*
Repeat with 3 workers, then 6.
*Pass:* the report states the measured ceiling for each and answers directly: **is there any global rate limiter?** If the answer is no, that is a finding at S3 against Multi Track's calls-per-second constraint, with the measured 3× and 6× numbers as evidence. Also state what a rolling deploy does to the ceiling.

**F-03 · Throughput at pilot shape** *(no 15-minute timebox — cap 30 min)*
Full 1000 incidents / 20 pages, single worker. Record wall clock, per-page p50 and p95, rows landed, and **peak concurrent Postgres connections** (`SELECT count(*) FROM pg_stat_activity WHERE datname='collector'`, sampled every second).
*Pass:* completes with no errors. Then extrapolate to the tiering commitment (hot every 30 min, warm every 2 h, cold daily) and state whether the measured per-page cost supports the cycle cadence. Report connections per page — the task body opens a fresh connection at each of four steps and there is no pool.

**F-04 · Connection pressure**
20 workers concurrently against `max_connections = 200`.
*Pass:* no `too many connections` error; record peak. Failure is S2 and the report must state the worker count at which the pilot would break.

### G — Data quality against the LiSN contract

**G-01 · Field completeness both ways**
Compare the 34 export field names from `mock/sentinel_api.py::_row_to_export`, after dot-to-underscore flattening, against the column list in `sql/003_bigquery.sql`.
*Pass:* exact set match in both directions — no source field silently dropped, no declared column never populated. *Sev if fail:* S1 for a dropped field, S4 for an unused column.

**G-02 · Schema drift** *(fidelity-caveated)*
Add one unexpected field to the mock export (`slaBreachReason`). Drive one page.
*Pass:* the failure is loud, the raw object is preserved, and reconcile flags the page. If the whole page is rejected by BigQuery, state plainly that one new Sentinel field halts the pipeline for every page and requires a code change, and record whether any data is lost versus merely delayed. Silent per-row drop is FAIL at S1.

**G-03 · Numeric fidelity at the incident grain** *(H8, fidelity-caveated)*
Seed incidents with `orderItemId` values of 9007199254740993 (2^53+1), a 19-digit value, and a leading-zero string form. Drive them through and compare the value in the sink against the value in the mock database.
*Pass:* byte-identical round trip. Any drift is FAIL at **S1** — the grain of an incident is `order_item_id`, and a key that changes in transit breaks the join to every downstream entity in the LiSN model.

**G-04 · Timezone fidelity** *(H13)*
Emit `updatedOn`, `resolutionDeadline` and `threads.createdAt` from the mock as naive ISO strings (no offset), as `+05:30`, and as `Z`. Compare stored values.
*Pass:* all three round-trip to the same instant, or a documented normalisation exists. If a naive string is silently read as UTC, that is FAIL at S1 and the report must state the size of the error if Sentinel emits IST.

**G-05 · Provenance columns**
For every row: `_request_id`, `_page_no` and `_raw_uri` are non-null and the `_raw_uri` points at an object that exists and whose sha256 matches `raw_manifest`.
*Pass:* 100% of rows. *Sev if fail:* S1 — provenance is the field-level requirement the observation log rests on.

### H — Request API robustness

**H-01 · Authentication surface**
Call every endpoint with no credentials against the local API.
*Pass:* the report states plainly, per endpoint, what protects it. Nothing in `collector/api.py` authenticates; if the only control is Cloud Run IAM, say so and confirm against `scripts/26_deploy_services.sh` whether the deployed service requires authentication. Unauthenticated `/v1/collect` on a service with `ingress=all` is FAIL at S1.

**H-02 · Error-message redaction** *(H5 — priority test)*
Force a failure whose exception text contains the DSN (point `COLLECTOR_DSN` at a wrong password for one worker run). Then read `/v1/dead-letter` and `collector_job.last_error`.
*Pass:* no password, DSN, bearer token or internal IP appears in stored error text or API output. Exposure is FAIL at **S1**.

**H-03 · Request completion signal** *(H11)*
Complete a request, then inspect `collector_request`.
*Pass:* `status` reflects completion and `closed_at` is set. If neither ever changes, that is FAIL at S2 — LiSN cannot ask "is my request done" without reimplementing the count itself, and `total_pages` alone does not distinguish `done` from `dead`.

**H-04 · Malformed path parameters**
`GET /v1/requests/not-a-uuid/counts`, and a valid but unknown UUID.
*Pass:* 400 and 404 respectively. An uncaught 500 from a `::uuid` cast is FAIL at S3.

**H-05 · Replay of an identical request**
POST the same `query_spec` twice.
*Pass:* the behaviour is documented. Record the duplicate source-call count and duplicate BigQuery rows produced. If there is no idempotency key, state the cost in source calls of a LiSN retry — against Multi Track's ceiling this is the expensive kind of accident. *Sev:* S3.

**H-06 · Partial defer**
Simulate a crash between the `collector_job` inserts and the `defer` loop (kill the API process mid-loop with a patched delay).
*Pass:* every inserted row eventually runs. Rows left `pending` forever links back to E-05 and is FAIL at S2.

### I — Deployment and operations

Most of section I is BLOCKED without GCP. Run what can be run statically; mark the rest.

**I-01 · The three open corrections** *(H14)*
Verify at head, with file and line evidence for each:
(a) `scripts/28_workers_control.sh` — the `workers-start` active-execution check;
(b) the reset ordering that kills live workers via a foreign-key violation on `procrastinate_workers` (see `scripts/10_demo.sh` around the `DELETE FROM procrastinate_jobs` / `DELETE FROM procrastinate_workers` block, and `scripts/29_e2e_cloud.sh`);
(c) the `sentinel_discovery` source module and the `order_item_ids` key type.
*Pass:* all three present and, where testable, exercised. Report each as done / not done with the evidence line. *Sev if fail:* S2 for (c) — it blocks the grain correction.

**I-02 · Destructive-script guards**
`scripts/10_demo.sh` truncates `collector_job`, `raw_manifest` **and the BigQuery table**. `scripts/29_e2e_cloud.sh` truncates too.
*Pass:* neither can run against a non-development project without an explicit confirmation, and both refuse when `PROJECT` is unset or unexpected. A script that silently truncates production BigQuery is FAIL at S1.

**I-03 · Make target ergonomics**
Run `make demo --reset` exactly as the README documents it, and `make workers-start` with `SOURCE` unset.
*Pass:* documented invocations work as documented, or fail with a clear message. A flag swallowed by `make` rather than reaching the script is FAIL at S4.

**I-04 · Worker identity**
Set `CLOUD_RUN_TASK_INDEX=0..2` and confirm `WORKER_ID` is stable across restarts for the same index and distinct across indices.
*Pass:* both hold. *Sev if fail:* S2 — stable identity is what lets Procrastinate's own recovery find stranded jobs.

**I-05 · Periodic sweep with multiple maintenance workers**
Three maintenance workers, observe two cron ticks.
*Pass:* exactly one `sweep` execution per tick (Procrastinate's `procrastinate_periodic_defers` should guarantee this — verify, do not assume). *Sev if fail:* S3.

**I-06 · Shell script hygiene**
Every script under `scripts/` sets `set -euo pipefail`; `shellcheck` at default severity across all scripts.
*Pass:* no missing strict mode; record shellcheck findings by severity. *Sev if fail:* S4.

### J — Documentation truth

**J-01 · Comment claims versus behaviour**
The source carries unusually strong load-bearing comments. Test each of these five as an assertion and mark true or false with evidence:

1. `raw.py` — "The path derives ONLY from source, request, page and date — never from a UUID or timestamp generated inside the function."
2. `api.py` — "If the process dies between the two, the sweeper finds orphan rows."
3. `contract.py` — "Adding collector #2 should cost one new source module."
4. `sentinel.py` — "Keying on incident id alone would silently collapse conversation history."
5. `tasks.py` — "The task body is idempotent so nothing corrupts."

*Pass:* all five true. Each false one is a finding at the severity of the behaviour it misdescribes, plus an S4 for the misleading comment — a wrong comment in this codebase is worse than none, because the next developer will trust it.

**J-02 · README accuracy**
Follow the README from a clean clone as written, changing nothing.
*Pass:* a new developer reaches a working local stack without external knowledge. Record every undocumented step. *Sev if fail:* S4.

---

## 6. Execution order

Run in this order so failures surface cheaply:

1. **A** (environment) — if A-03 blocks, build the shim before continuing.
2. **B, C** (contract, planner) — pure, fast, no live stack.
3. **D** (idempotency) — the highest-value section. D-01, D-03, D-04 first.
4. **G** (data quality) — reuses the D fixtures.
5. **H** (API) — H-02 early; a credential leak changes what may be shared.
6. **E** (failure) — slowest. E-05 and E-07 first.
7. **F** (rate) — must run with a quiet machine; nothing else concurrent.
8. **I, J** (ops, documentation) — mostly static.

---

## 7. What to produce

One file: `LiSN_Collectors_Audit_Report_v1.md`, following the template shipped alongside this protocol. Plus `tests/audit/` containing every test written, and `tests/audit/evidence/` containing the transcripts.

The report is written for the developer who built this. It must be usable as a work list: each failure states what was expected, what happened, the evidence file, the file and line where the behaviour originates, and the severity. It must not state how to fix it — that is the developer's call, and prescribing a fix from the outside is how good architecture gets damaged by a reviewer who has less context than the author.

Every number in the report carries its source: the test ID that produced it, or the file and line it was read from. No bare assertions.
