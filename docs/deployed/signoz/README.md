# LiSN collector — SigNoz operational surface

Importable dashboard + alert specs for the person answering
**"is it working right now"**.

## Metric name note

OTLP → Prometheus naming usually turns dots into underscores:

| OTel instrument | SigNoz Cloud name (this pilot) |
|---|---|
| `lisn.workers.live` (gauge) | `lisn.workers.live` |
| `lisn.source.calls` (counter) | `lisn.source.calls` |
| `lisn.page.duration` (histogram) | `lisn.page.duration.bucket` (+ `.count`, `.sum`, `.max`) |
| `lisn.source.latency` (histogram) | `lisn.source.latency.bucket` |
| `lisn.worker.heartbeat_age` (histogram) | `lisn.worker.heartbeat_age.max` (scalar panel) |
| HTTP status on source calls | attribute **`http.status_code`** (dot, not underscore) |

If a panel is empty, open **Metrics → Explorer**, search `lisn.`, and match the
exact name from the catalog. Histogram percentiles always use the **`.bucket`**
series in SigNoz v6 dashboards.

**Agreed source ceiling (enrichment):** `3` calls/second  
(= 3 Cloud Run tasks × `min_interval_s=1`). Discovery is slower (`min_interval_s=2`,
1 task) — do not use the same ceiling line for `sentinel_discovery`.

## Files

| File | Purpose |
|---|---|
| `dashboard_lisn_ops.json` | SigNoz **schemaVersion `v6`** (Perses) — Import JSON |
| `alerts.md` | Every alert with history, PromQL, threshold, severity |
| `../signoz_platform_logs.md` | Platform-log gap — **recommend Option 2, do not build yet** |

## Import dashboard

SigNoz Cloud rejects Grafana dashboard JSON (`title` / `panels` / `schemaVersion: 39`).
This file matches official templates (`schemaVersion: "v6"`, `spec.panels`, `spec.layouts`).

1. SigNoz → Dashboards → open the existing dashboard → **⋮ → Import JSON** (or delete and re-import).
2. Upload or paste `dashboard_lisn_ops.json` from this repo (regenerate with `python scripts/_gen_signoz_dashboard.py` after edits).
3. Set time range **Last 1 hour**, refresh. Panels that stay empty with **no events** in the window: `jobs.dead_lettered rate`, `retry rate (status=failed)` — that is healthy.
4. Optionally set a red threshold on the `workers.live` number panel for **&lt; 5**

## Create alerts

SigNoz Cloud alert JSON schemas move between V1/V2. Use **Alerts → New alert →
Metrics** and copy each block from `alerts.md` (query, condition, window,
description). Descriptions cite the acceptance-run failure on purpose.

## Verify `workers.live` (the four-day alert)

After deploy with `OTEL_ENABLED=1` and `ENABLE_PERIODIC=1` on maintenance:

```bash
# Baseline — expect ~5
curl -sH "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "$COLLECTOR_API_URL/v1/health/detail" | jq .live_workers

# Stop workers (scale / cancel executions)
./scripts/28_workers_control.sh stop

# Within ~1 minute gauges should drop; alert fires after 5 minutes at 0
# Screenshot the firing CRITICAL alert in SigNoz, then:
./scripts/28_workers_control.sh start
```
