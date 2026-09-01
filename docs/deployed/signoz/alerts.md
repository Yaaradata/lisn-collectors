# LiSN collector — SigNoz alerts

Each alert maps to a failure that **actually occurred**. The description is what
the on-call reads first — keep the history in the rule.

Severity: CRITICAL / WARNING / INFO as specified.

---

## CRITICAL · workers.live == 0 for 5 minutes

**History:** 27 and 29 August — all workers failed and stayed dead for four days
with nothing alerting. This alert alone would have caught it.

| Field | Value |
|---|---|
| PromQL | `max(lisn_workers_live{source="all"})` or `sum(lisn_workers_live{source!="all"})` |
| Condition | Below or equal to **0** |
| Match | **all the times** |
| Window | Rolling **5 minutes** |
| Check every | 1 minute |
| Also enable | “Alert when data stops coming” after **5 minutes** (maintenance dead ⇒ gauges stop) |

Notification body should mention: check Cloud Logging for
`maximum timeout of 86400 seconds` / `exit(1)` — see
`docs/deployed/signoz_platform_logs.md`.

---

## CRITICAL · discovery.gaps > 0

**History:** 104 incidents lost across five windows with every health surface
green (`/v1/reconcile`, dead-letter, health/detail all zero).

| Field | Value |
|---|---|
| PromQL | `max(lisn_discovery_gaps)` |
| Condition | Above **0** |
| Match | **at least once** |
| Window | Rolling **5 minutes** |

---

## CRITICAL · reconcile.unloaded > 0 for 15 minutes

**History:** raw written to GCS, warehouse never loaded it, no error anywhere —
the silent failure reconcile exists to catch.

| Field | Value |
|---|---|
| PromQL | `max(lisn_reconcile_unloaded{source="all"})` |
| Condition | Above **0** |
| Match | **all the times** |
| Window | Rolling **15 minutes** |

---

## WARNING · dead_lettered rate > 0

**History:** pages exhausted attempts; nothing retries them without a human.

| Field | Value |
|---|---|
| PromQL | `sum(rate(lisn_jobs_dead_lettered_total[5m]))` |
| Condition | Above **0** |
| Match | **at least once** |
| Window | Rolling **5 minutes** |

---

## WARNING · source.calls per second above agreed ceiling

**History:** rate arithmetic assumes 3 enrichment tasks × 1 call/s. Breaching the
ceiling risks Flipkart throttling / shared-quota pain.

| Field | Value |
|---|---|
| PromQL | `sum(rate(lisn_source_calls_total{source="sentinel"}[1m]))` |
| Condition | Above **3** |
| Match | **on average** (or at least once — prefer average to ignore blips) |
| Window | Rolling **5 minutes** |
| Marked line | **3.0** req/s on the dashboard panel |

Do **not** apply this ceiling to `sentinel_discovery` (different interval / fan-out).

---

## WARNING · pending queue depth growing 30 minutes without pages completing

**History:** work piles up while nothing finishes — paused killswitch, source
down, or workers wedged.

| Field | Value |
|---|---|
| Query A | `sum(lisn_jobs_pending)` |
| Query B | `sum(rate(lisn_pages_completed_total{status="done"}[5m]))` |
| Condition | A **Above** previous / rising — practical form: A Above **0** AND B Equal **0** (use multi-query / formula if available), Match **all the times**, Window **30 minutes** |
| Fallback | Two linked alerts: (1) `pages_completed` done rate == 0 for 30m while (2) `jobs_pending` Above 0 |

---

## WARNING · worker heartbeat age > 60 seconds

**History:** stalling worker before death — sweeper / ops need a head start.

| Field | Value |
|---|---|
| PromQL | `max(lisn_worker_heartbeat_age_seconds)` or histogram quantile from `lisn_worker_heartbeat_age_*` |
| Condition | Above **60** |
| Match | **at least once** |
| Window | Rolling **5 minutes** |

(Maintenance scrape also records the histogram each ~30s.)

---

## INFO · no completed page in 60 minutes for an active source

**History:** quiet source that should still be moving — discovery windows or
enrichment backlog stalled without a hard error.

| Field | Value |
|---|---|
| PromQL | `min(lisn_page_seconds_since_complete)` (or per-`source` group) |
| Condition | Above **3600** |
| Match | **all the times** |
| Window | Rolling **5 minutes** (value itself is already an age) |
| Note | Only meaningful when that source has pending work; tune with a pending>0 guard when SigNoz supports multi-condition |

---

## After creating CRITICAL workers.live

Verify by stopping the fleet (`./scripts/28_workers_control.sh stop`), wait for
the gauge to hit 0, confirm the alert fires within ~5–6 minutes, screenshot,
then `start` again. That is the alert that would have saved four days.
