"""LiSN collector metrics — chosen against acceptance-run failures, not generically.

Do not sample. Metrics are cheap next to logs and traces; every event counts.

Gauges (workers.live, queue depth, unloadeds, gaps) are DATABASE STATE, not
events. An in-process counter cannot produce them. The maintenance worker
registers OTel observable callbacks that run the same queries as
``/v1/health/detail`` every export interval (~30s).

WHY MAINTENANCE ONLY (ENABLE_PERIODIC=1):
  A gauge reported by three enrichment processes shows three overlapping
  series for the same global DB state, and every alert on it is wrong.
  Enrichment workers emit counters/histograms for work they themselves do;
  only maintenance scrapes shared state.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from collector.db import connect
from collector.discovery_gaps import gap_summary
from collector.logging_setup import log

logger = logging.getLogger("collector.metrics")

_METER_NAME = "lisn.collector"
_initialized = False

# Instruments (populated by init_metrics).
_requests_received: Any = None
_pages_completed: Any = None
_source_calls: Any = None
_records_landed: Any = None
_jobs_requeued: Any = None
_jobs_dead_lettered: Any = None
_pages_shortfall: Any = None
_page_duration: Any = None
_source_latency: Any = None
_request_pages: Any = None
_worker_heartbeat_age: Any = None

_gauges_registered = False
_scrape_cache: list[Observation] | None = None
_scrape_cache_at = 0.0
_SCRAPE_CACHE_S = 2.0


def _enabled() -> bool:
    return os.environ.get("OTEL_ENABLED") == "1"


def periodic_enabled() -> bool:
    """True when this process may scrape global gauges / run periodic tasks.

    Set ENABLE_PERIODIC=1 on the maintenance worker only.
    """
    return os.environ.get("ENABLE_PERIODIC", "").strip() == "1"


def init_metrics() -> None:
    """Create instruments against the global MeterProvider. Idempotent."""
    global _initialized
    global _requests_received, _pages_completed, _source_calls, _records_landed
    global _jobs_requeued, _jobs_dead_lettered, _pages_shortfall
    global _page_duration, _source_latency, _request_pages, _worker_heartbeat_age

    if _initialized:
        return

    meter = metrics.get_meter(_METER_NAME)

    _requests_received = meter.create_counter(
        "lisn.requests.received",
        description="LiSN collect requests accepted",
        unit="1",
    )
    _pages_completed = meter.create_counter(
        "lisn.pages.completed",
        description="Pages reaching a terminal or attempt outcome",
        unit="1",
    )
    _source_calls = meter.create_counter(
        "lisn.source.calls",
        description=(
            "Outbound source HTTP calls — the rate ceiling made observable "
            "(claim: 3 req/s at three enrichment tasks)"
        ),
        unit="1",
    )
    _records_landed = meter.create_counter(
        "lisn.records.landed",
        description="Records / bytes written to a destination",
        unit="1",
    )
    _jobs_requeued = meter.create_counter(
        "lisn.jobs.requeued",
        description="Sweeper requeues of stranded collector_job rows",
        unit="1",
    )
    _jobs_dead_lettered = meter.create_counter(
        "lisn.jobs.dead_lettered",
        description="Pages dead-lettered by the sweeper",
        unit="1",
    )
    _pages_shortfall = meter.create_counter(
        "lisn.pages.shortfall",
        description=(
            "Pages where returned_count < requested_count "
            "(anomaly, not always an error)"
        ),
        unit="1",
    )
    _page_duration = meter.create_histogram(
        "lisn.page.duration",
        description="Per-stage page duration (ms)",
        unit="ms",
    )
    _source_latency = meter.create_histogram(
        "lisn.source.latency",
        description="Source HTTP round-trip latency (ms)",
        unit="ms",
    )
    _request_pages = meter.create_histogram(
        "lisn.request.pages",
        description="Page count distribution per collect request",
        unit="1",
    )
    _worker_heartbeat_age = meter.create_histogram(
        "lisn.worker.heartbeat_age",
        description="Age of procrastinate_workers.last_heartbeat (seconds)",
        unit="s",
    )

    _initialized = True


def _ensure() -> bool:
    if not _initialized:
        # No-op instruments against the proxy provider when OTel is off.
        init_metrics()
    return _initialized


# --- Counters / histograms (event path) -------------------------------------


def record_request_received(*, source: str, key_type: str) -> None:
    _ensure()
    _requests_received.add(1, {"source": source, "key_type": key_type})


def record_request_pages(*, source: str, page_count: int) -> None:
    _ensure()
    _request_pages.record(page_count, {"source": source})


def record_page_completed(*, source: str, status: str) -> None:
    """status: done | failed | dead"""
    _ensure()
    _pages_completed.add(1, {"source": source, "status": status})


def record_source_call(*, source: str, http_status: int) -> None:
    _ensure()
    _source_calls.add(
        1, {"source": source, "http.status_code": str(http_status)}
    )


def record_source_latency(*, source: str, duration_ms: float) -> None:
    _ensure()
    _source_latency.record(duration_ms, {"source": source})


def record_records_landed(
    *, source: str, destination: str, count: int
) -> None:
    """destination: gcs | bigquery"""
    if count <= 0:
        return
    _ensure()
    _records_landed.add(
        count, {"source": source, "destination": destination}
    )


def record_jobs_requeued(*, source: str, count: int = 1) -> None:
    if count <= 0:
        return
    _ensure()
    _jobs_requeued.add(count, {"source": source})


def record_jobs_dead_lettered(*, source: str, count: int = 1) -> None:
    if count <= 0:
        return
    _ensure()
    _jobs_dead_lettered.add(count, {"source": source})


def record_page_shortfall(*, source: str) -> None:
    _ensure()
    _pages_shortfall.add(1, {"source": source})


def record_page_stage_duration(
    *, source: str, stage: str, duration_ms: float
) -> None:
    _ensure()
    _page_duration.record(
        duration_ms, {"source": source, "stage": stage}
    )


def record_worker_heartbeat_age(
    *, worker_id: str, age_seconds: float, source: str | None = None
) -> None:
    _ensure()
    attrs: dict[str, str] = {"worker_id": worker_id}
    if source:
        attrs["source"] = source
    _worker_heartbeat_age.record(age_seconds, attrs)


@contextmanager
def timed_stage(*, source: str, stage: str) -> Iterator[None]:
    """Record lisn.page.duration for one pipeline stage."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        record_page_stage_duration(
            source=source,
            stage=stage,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )


# --- Observable gauges (maintenance / ENABLE_PERIODIC only) -----------------


def _source_from_worker_id(worker_id: str) -> str:
    """sentinel-task0 → sentinel; maintenance-local → maintenance."""
    if "-task" in worker_id:
        return worker_id.rsplit("-task", 1)[0]
    if worker_id.endswith("-local"):
        return worker_id[: -len("-local")]
    parts = worker_id.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else worker_id


def _observe_health_state(
    options: CallbackOptions,
) -> Sequence[Observation]:
    """Shared DB scrape for all gauges — one round-trip set per export."""
    del options  # unused; signature required by OTel
    observations: list[Observation] = []
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                # Live workers by source (parsed from stable WORKER_ID).
                cur.execute(
                    """
                    SELECT id
                    FROM procrastinate_workers
                    WHERE now() - last_heartbeat < interval '60 seconds'
                    """
                )
                live_by_source: dict[str, int] = {}
                for (wid,) in cur.fetchall():
                    src = _source_from_worker_id(str(wid))
                    live_by_source[src] = live_by_source.get(src, 0) + 1
                for src, n in live_by_source.items():
                    observations.append(
                        Observation(n, {"source": src, "metric": "workers.live"})
                    )
                # Always emit a zero for known empty? Skip — absent = 0 in alerts
                # that treat missing as missing. Emit total under source=all too.
                observations.append(
                    Observation(
                        sum(live_by_source.values()),
                        {"source": "all", "metric": "workers.live"},
                    )
                )

                # Heartbeat ages (histogram recorded here; also gauge-friendly).
                cur.execute(
                    """
                    SELECT id,
                           EXTRACT(EPOCH FROM (now() - last_heartbeat))::float
                    FROM procrastinate_workers
                    """
                )
                for wid, age in cur.fetchall():
                    src = _source_from_worker_id(str(wid))
                    # Histogram (lisn.worker.heartbeat_age) — surfaces a
                    # stalling worker before it dies.
                    record_worker_heartbeat_age(
                        worker_id=str(wid),
                        age_seconds=float(age),
                        source=src,
                    )

                # Queue depth / in_progress by source.
                cur.execute(
                    """
                    SELECT source, status, count(*)::int
                    FROM collector_job
                    WHERE status IN ('pending', 'in_progress')
                    GROUP BY source, status
                    """
                )
                for source, status, count in cur.fetchall():
                    metric = (
                        "jobs.pending"
                        if status == "pending"
                        else "jobs.in_progress"
                    )
                    observations.append(
                        Observation(
                            count, {"source": source, "metric": metric}
                        )
                    )

                # Reconcile unloaded (raw without load) — by source.
                cur.execute(
                    """
                    SELECT source, count(*)::int
                    FROM collector_job
                    WHERE raw_written_at IS NOT NULL
                      AND loaded_at IS NULL
                      AND raw_written_at < now() - interval '15 minutes'
                    GROUP BY source
                    """
                )
                unloaded_rows = cur.fetchall()
                total_unloaded = 0
                for source, count in unloaded_rows:
                    total_unloaded += count
                    observations.append(
                        Observation(
                            count,
                            {
                                "source": source,
                                "metric": "reconcile.unloaded",
                            },
                        )
                    )
                observations.append(
                    Observation(
                        total_unloaded,
                        {"source": "all", "metric": "reconcile.unloaded"},
                    )
                )

                # Seconds since last completed page per source — "is it alive".
                cur.execute(
                    """
                    SELECT source,
                           EXTRACT(
                             EPOCH FROM (now() - max(loaded_at))
                           )::float
                    FROM collector_job
                    WHERE status = 'done'
                      AND loaded_at IS NOT NULL
                    GROUP BY source
                    """
                )
                for source, age in cur.fetchall():
                    observations.append(
                        Observation(
                            float(age),
                            {
                                "source": source,
                                "metric": "page.seconds_since_complete",
                            },
                        )
                    )

                # Shortfall page count (current stock) for the ops dashboard.
                cur.execute(
                    """
                    SELECT source, count(*)::int
                    FROM collector_job
                    WHERE status = 'done'
                      AND requested_count IS NOT NULL
                      AND returned_count IS NOT NULL
                      AND returned_count < requested_count
                    GROUP BY source
                    """
                )
                for source, count in cur.fetchall():
                    observations.append(
                        Observation(
                            count,
                            {"source": source, "metric": "pages.shortfall"},
                        )
                    )

        gaps = gap_summary()
        observations.append(
            Observation(
                int(gaps.get("count") or 0),
                {"source": "all", "metric": "discovery.gaps"},
            )
        )
    except Exception as exc:  # noqa: BLE001 — never break the metric reader
        logger.warning("metrics gauge scrape failed: %s", exc)
    return observations


def _cached_health_observations(
    options: CallbackOptions,
) -> Sequence[Observation]:
    """One DB scrape shared by all gauge callbacks in an export tick."""
    global _scrape_cache, _scrape_cache_at
    now = time.monotonic()
    if _scrape_cache is not None and (now - _scrape_cache_at) < _SCRAPE_CACHE_S:
        return _scrape_cache
    _scrape_cache = list(_observe_health_state(options))
    _scrape_cache_at = now
    return _scrape_cache


def _filter_metric(name: str):
    def _cb(options: CallbackOptions) -> Sequence[Observation]:
        return [
            Observation(
                o.value,
                {
                    k: v
                    for k, v in (o.attributes or {}).items()
                    if k != "metric"
                },
            )
            for o in _cached_health_observations(options)
            if (o.attributes or {}).get("metric") == name
        ]

    return _cb


def register_observable_gauges() -> None:
    """Register DB-backed gauges. Call only when ENABLE_PERIODIC=1.

    WHY not on enrichment workers: three processes reporting the same global
    gauge produce three overlapping series; every alert on it is wrong.
    """
    global _gauges_registered
    if _gauges_registered:
        return
    if not _enabled():
        return
    if not periodic_enabled():
        return

    _ensure()
    meter = metrics.get_meter(_METER_NAME)

    meter.create_observable_gauge(
        "lisn.workers.live",
        callbacks=[_filter_metric("workers.live")],
        description=(
            "Heartbeating procrastinate workers (<60s). "
            "THE alert that should have fired when workers died 27 Aug."
        ),
        unit="1",
    )
    meter.create_observable_gauge(
        "lisn.jobs.pending",
        callbacks=[_filter_metric("jobs.pending")],
        description="collector_job rows awaiting a worker",
        unit="1",
    )
    meter.create_observable_gauge(
        "lisn.jobs.in_progress",
        callbacks=[_filter_metric("jobs.in_progress")],
        description="collector_job rows currently leased",
        unit="1",
    )
    meter.create_observable_gauge(
        "lisn.reconcile.unloaded",
        callbacks=[_filter_metric("reconcile.unloaded")],
        description="Raw written to GCS but never loaded to BigQuery (silent failure)",
        unit="1",
    )
    meter.create_observable_gauge(
        "lisn.discovery.gaps",
        callbacks=[_filter_metric("discovery.gaps")],
        description="Detected discovery window gaps (not auto-backfilled)",
        unit="1",
    )
    meter.create_observable_gauge(
        "lisn.page.seconds_since_complete",
        callbacks=[_filter_metric("page.seconds_since_complete")],
        description="Seconds since the last done page per source",
        unit="s",
    )
    meter.create_observable_gauge(
        "lisn.pages.shortfall_stock",
        callbacks=[_filter_metric("pages.shortfall")],
        description="Current count of done pages with returned < requested",
        unit="1",
    )

    _gauges_registered = True
    log(
        logger,
        logging.INFO,
        "observable gauges registered",
        source="maintenance",
        status="gauges_ready",
    )
