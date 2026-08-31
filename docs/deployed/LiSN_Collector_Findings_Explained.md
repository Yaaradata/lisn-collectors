# LiSN Collector — What We Tested and What We Found

A plain-language walkthrough of the deployed acceptance run. Written to be read aloud in a review, not to be filed.

---

## The question we were answering

Ranjith BK built the data collector. Before it carries Flipkart pilot traffic, someone has to be able to say: *when LiSN asks this thing for incident data, does it come back — complete, correct, fast enough, and every time, including when something breaks?*

That is not a code review. Nothing here inspects source files or checks whether comments are accurate. Every test drove real data through the real deployed system and measured what came out the other end.

---

## Where we tested

The real deployment on `clariversev1`: Cloud Run Jobs for the workers, Cloud SQL for state, GCS for raw evidence, BigQuery for the warehouse. Requests went to the deployed API over the network with authentication, exactly as LiSN will call it.

**One important limit.** The real Flipkart Sentinel is an internal application on a private network that Yaaralabs cannot reach. So the *source* in these tests is our own mock, deployed on Cloud Run, which answers instantly. Everything else is real. This means every speed number is optimistic — the real Sentinel will be slower.

---

## How we checked whether data was lost

This is the part that matters most, so it is worth explaining the method.

We never trusted the collector's own count of what it collected. If the collector loses a record and also forgets it ever existed, its own count would look perfect.

Instead we asked the **source** what should exist, asked **BigQuery** what actually arrived, and compared the two lists item by item — not the totals, the actual identities. A count can only catch loss. An identity list catches loss, substitution, duplication and corruption.

One wrinkle worth knowing: an incident with three conversation threads produces three rows, not one. So the identity is `(incident, thread)`, and 1,000 incidents legitimately becomes about 2,477 rows.

---

## What passed

**Retrieval is correct.** We asked for 1,000 incidents. The source said there should be 2,477 identity rows. BigQuery had 2,477 — the same ones. Then 5,000 incidents: 12,575 expected, 12,575 arrived, across 100 pages with zero failures.

Request `855cb5db-0448-4cf3-8364-525af697ce97` is the 1,000-incident run — 20 pages, `done=20, failed=0, dead=0, records=2477`. Request `4a0f2559-2665-4d87-94f6-63b1b8d7d68a` is the 5,000 sample, 100 pages, 271 seconds. To check either:

```sql
SELECT COUNT(*) FROM `clariversev1.sentinel_raw.incidents`
WHERE _request_id = '855cb5db-0448-4cf3-8364-525af697ce97'
```

The 2,477 is also a useful cross-check in itself: 2,477 ÷ 1,000 = 2.477, and the seed generator's thread explosion factor is 2.481. Two independent measurements agreeing.

**Discovery is correct.** Asked for everything updated in a one-hour window: 1,784 incidents expected, 1,784 found. Request `c658fb48-c732-4a9d-a9c6-eed3216719ef`.

**The two stages connect.** Discovery finds incident IDs, enrichment fetches their detail. We checked that nothing falls between: 1,788 discovered, 1,788 enriched, 0 left pending. Nothing lost in the handoff.

The arithmetic that has to hold is `discovered − enriched − pending = 0`. Anything left over is an ID that was found, never fetched, and is not queued to be — invisible to both stages.

**Recovery after induced failure was only observed after a manual restart.** Tests that killed workers mid-collection, then completed the run, restarted the workers themselves before asserting success. Unattended recovery — whether pages finish with no human action after a worker dies — was not measured. That measurement waits on Pass 12's scheduler (automatic re-execution after the Cloud Run Jobs 24-hour ceiling / unexpected termination). Until then, do not read a green cancel-and-complete test as proof that the system recovers on its own.

**Failures that can't be fixed are visible.** When we made the source fail permanently, the page ended up in the dead-letter queue where an operator can see it. It failed loudly rather than silently, which is the right behaviour.

**A permanently failing page gives up cleanly.** It reached a terminal dead state in about 100 seconds after burning 5 source calls, rather than retrying forever.

**The dead-letter endpoint is protected.** Unauthenticated calls get 403. Locally it had been wide open.

---

## What failed, and why it matters

### Blocking — must be resolved before pilot traffic

#### 1. Large order-item IDs change in transit

**What we did.** We put three specific numbers into the source and checked what arrived in BigQuery.

| Sent | Arrived | Changed? |
|---|---|---|
| 9,007,199,254,740,991 | 9,007,199,254,740,991 | No |
| 9,007,199,254,740,993 | 9,007,199,254,740,992 | **Yes, by 1** |
| 1,234,567,890,123,456,789 | 1,234,567,890,123,456,778 | **Yes, by 11** |

**Why it happens.** The BigQuery column is declared `FLOAT64` — a decimal type. Floating point can represent whole numbers exactly only up to about 9.007 quadrillion, which is 2^53. Above that it rounds to the nearest value it can hold. The first number is just below that line and survives. The other two are above it and don't.

**Why it matters.** `order_item_id` is the grain of an incident — every incident is created against one, and it is the key that joins an incident to its shipment, its promise dates and everything else in the LiSN model. A key that changes in transit means the join silently finds the wrong record or no record. Nothing errors. The data just quietly stops lining up.

**Why we nearly missed it.** An earlier version of this test compared the *text* of the numbers rather than their values, and flagged `4000000000299190.0` versus `4.00000000029919E15` as corruption. Those are the same number written differently — a false alarm. When we retested properly with values chosen to straddle the 2^53 boundary, the real problem appeared.

**See it for yourself.** Request `96af1585-1eef-46a0-a9f2-74ca6739586d` drove the three seeded values through. To read what landed:

```sql
SELECT id, orderItemId
FROM `clariversev1.sentinel_raw.incidents`
WHERE _request_id = '96af1585-1eef-46a0-a9f2-74ca6739586d'
ORDER BY orderItemId
```

The column declaration is in `sql/003_bigquery.sql`:

```sql
orderItemId FLOAT64,
orderItemUnitId FLOAT64,
threads_communicationId FLOAT64,
```

All three are identifiers stored as floating point. The same rounding applies to each.

A one-line check of the boundary in Python, if it helps to demonstrate:

```python
>>> float(9007199254740993)
9007199254740992.0
>>> float(9007199254740991)
9007199254740991.0
```

#### 2. A skipped discovery window loses records silently

**What we did.** Discovery works by time window — "give me everything updated between 2pm and 3pm." We took a six-hour span, ran five one-hour windows, and deliberately skipped the third.

**What happened.** The source held 1,094 incidents across the full span. The five windows found 990. **104 incidents were never discovered.**

Then we checked every place an operator would look for a problem:

| Where you'd look | What it said |
|---|---|
| `/v1/reconcile` | no issues |
| `/v1/dead-letter` | no failures |
| `/v1/health/detail` | nothing stuck, nothing orphaned, nothing dead |
| The jobs themselves | all completed successfully |

Everything green. 104 incidents gone.

**Why it happens.** The collector does exactly what it is told — it collects the windows you ask for. It does not remember where it got to last time, and nothing checks that consecutive windows join up. So if a scheduler misfires, an outage skips a run, or someone types a wrong timestamp, that hour of incidents is never collected and never missed.

**Why it matters.** This is not a bug in the code — nothing is broken. It is a design gap. In a control tower, an incident that was never collected is an incident nobody works on, and there is no signal that would prompt anyone to look.

**See it for yourself.** Five discovery requests were submitted across `2026-08-20T00:00:00Z` to `2026-08-20T06:00:00Z`, covering hours 1, 2, 4, 5 and 6 — hour 3 was skipped on purpose:

```
9d1b72f1-ffaa-4c20-8812-09a848b7ca80    00:00 → 01:00
087023fc-9ac4-4941-8109-98f658bb6900    01:00 → 02:00
                                        02:00 → 03:00   ← skipped
c23f9b5a-c6af-4f10-941a-5e8c5641e8d5    03:00 → 04:00
965b5d9f-fd8c-471d-99dd-4716ecd74813    04:00 → 05:00
5f824596-1fef-4fa4-a9de-1a7280fd4e37    05:00 → 06:00
```

What the five windows found, versus what the source holds for the whole span:

```sql
SELECT COUNT(DISTINCT incident_id)
FROM `clariversev1.sentinel_raw.discovered_ids`
WHERE _request_id IN (
  '9d1b72f1-ffaa-4c20-8812-09a848b7ca80',
  '087023fc-9ac4-4941-8109-98f658bb6900',
  'c23f9b5a-c6af-4f10-941a-5e8c5641e8d5',
  '965b5d9f-fd8c-471d-99dd-4716ecd74813',
  '5f824596-1fef-4fa4-a9de-1a7280fd4e37'
)
-- returns 990; the source holds 1,094 for that span
```

Three of the 104 that were never collected — none of these exist anywhere downstream:

```
IN26081800000000005138
IN26081800000000005387
IN26081800000000006206
```

And the health surfaces, checked immediately afterwards:

```json
/v1/reconcile        {"rows": [], "unloaded": 0}
/v1/dead-letter      {"dead": 0, "rows": []}
/v1/health/detail    {"stuck": 0, "orphans": 0, "unloaded": 0, "dead": 0}
```

Full transcript in `tests/deployed/evidence/Q3-gap.log`.

### P1 — needs resolution before scaling up

**Capacity may be short.** A sustained full sweep collects about 193,484 incidents per 30 minutes. Our test population is 299,190, so it does not fit in one cycle. **But the real number of open cases per cycle is unconfirmed** — that figure has never come from Flipkart. If it is 50,000 there is plenty of headroom. This needs the real number before it can be called a problem.

**A big sweep starves everything else.** A single urgent request submitted while a full sweep was running waited 371 seconds — over six minutes — because it queues behind everything already in flight. For a control tower where an agent is waiting on screen, that matters.

**Some interruptions need a human.** In two scenarios where workers were cancelled mid-run, collection stopped and did not resume until someone restarted them.

### P2 — record and address later

**You cannot tell "nothing found" from "something broke."** We asked for an incident ID that does not exist. The response was `done: 1, records: 0` — success, zero records, no error. That is the correct outcome, but a genuine failure would look identical to the caller.

**Garbage responses are handled inconsistently.** We made the source return four kinds of broken data:

| What the source returned | What the collector did |
|---|---|
| `{"incidents":[` — cut off mid-JSON | dead-lettered |
| An HTML error page with `content-type: text/html` | dead-lettered |
| HTTP 200 with an empty body | dead-lettered |
| `{"incidents": "not-a-list", "count": 11}` | **completed as successful, 7 records** |

Three of the four failed loudly, which is right. The fourth — where the incident list is a text string rather than a list — was accepted. The fault modes are injectable on the deployed mock at `POST /admin/payload-fault/{incident_id}/{mode}` with modes `truncated_json`, `html_error_page`, `empty_body_200` and `incidents_string`, so this one is reproducible in a minute.

---

## Problems with the deployment itself

These were not produced by tests. They came from reading the Cloud Run logs of the actual deployed jobs.

**Workers die every 24 hours.** Cloud Run caps a task at 24 hours and then terminates it. Both the enrichment and discovery workers ran cleanly for a full day and were then killed. Nothing restarts them. So collection stops once a day, and the only sign is a red row in a console nobody is watching.

Execution `col-sentinel-j4wkc` started 25 Aug 16:15:18 UTC and ended 26 Aug 16:15:32 UTC — a day to the second. Its last three log lines:

```
Terminating task because it has reached the maximum timeout of 86400 seconds
INFO:procrastinate.worker.worker:Stop requested
INFO:procrastinate.worker.worker:Stopped worker on queues sentinel
```

Immediately before that it had been sweeping every two minutes without a single error for the whole 24 hours. `col-sentinel-discovery-z82fd` shows the identical pattern. To see it:

```
gcloud run jobs executions list --job=col-sentinel --region=asia-south1 --project=clariversev1
```

**Workers die if the database restarts.** The maintenance worker tried to start while Cloud SQL was unavailable, waited 30 seconds for a connection, gave up and exited. Cloud Run does not restart a failed task, so it stayed dead. Google performs maintenance on Cloud SQL instances on its own schedule, which means this will happen without anyone touching anything.

Execution `col-maintenance-qmd84` — started 16:03:55 UTC, dead by 16:04:38, 43 seconds total:

```
Error 409: The instance or operation is not in an appropriate state
           to handle the request., invalidState
WARNING:psycopg.pool:error connecting in 'pool-1': connection is bad
pool initialization incomplete after 30.0 sec
Container called exit(1).
```

The instance `lisn-collector-db` was stopped at the time. It was started again at 16:06:05 UTC. The worker did not come back with it.

**The cleanup sweep runs from every worker.** It is supposed to run from the maintenance worker only. Both the enrichment and discovery worker logs show sweep entries, with interleaved sequence numbers — 1633 and 1634 on `col-sentinel`, 1636 and 1637 on `col-sentinel-discovery`:

```
INFO:procrastinate.periodic:Periodic job sweep[1633](timestamp=...) deferred
```

`@app.periodic` is registered at import time on the shared app object, so every worker process schedules it regardless of which queue it consumes.

---

## What we did not test, and why

| Not run | Why |
|---|---|
| Stop Cloud SQL mid-run | The database is shared with other systems on the project |
| Restart Cloud SQL mid-run | Same |
| Revoke storage access mid-run | Would affect other services |
| The admin delete endpoint | Deletes the pilot's data |

These four are the destructive tests. They were excluded deliberately, not skipped by accident. They can be run in a scratch environment when one exists.

Also unmeasured: full-population equality — we sampled 5,000 incidents rather than all 299,190 — and a formal data-lag metric.

---

## Two things we got wrong

Worth saying openly, because it is how the findings above earned their credibility.

**We reported 350 records lost between discovery and enrichment.** It was our test's fault. The test declared a request finished as soon as its *first* page completed, then counted results while the remaining pages were still running. Fixed and re-run: nothing lost.

**We reported 150 records lost for incidents with no conversation threads.** That came from the earlier local test environment. Re-tested on the deployed system, the incident arrived intact. The earlier result did not hold.

Both were corrected before the report was finalised. Neither is a defect in the collector.

---

## The summary, in one paragraph

The collector retrieves data correctly and completely at the scales we tested, and fails loudly when it cannot. Recovery after induced worker failure was observed only after the tests themselves restarted workers — unattended recovery is unmeasured until Pass 12's scheduler exists. Two things must be fixed before pilot traffic: order-item IDs above 9 quadrillion change value in transit and break the joins everything downstream depends on, and a skipped discovery window loses incidents permanently with every health surface reporting normal. Separately, the deployment has no mechanism to survive its own platform's 24-hour worker limit or a routine database restart. Capacity against the full test population is short, but the real population size has never been confirmed with Flipkart, so that number cannot yet be called a problem.
