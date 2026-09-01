"""Metrics instruments and ENABLE_PERIODIC gauge gating."""

from __future__ import annotations

import os

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util._once import Once

import collector.metrics as m


def _force_meter_provider(provider: MeterProvider) -> None:
    metrics._METER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    metrics._METER_PROVIDER = None  # type: ignore[attr-defined]
    metrics.set_meter_provider(provider)


@pytest.fixture()
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> InMemoryMetricReader:
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.delenv("ENABLE_PERIODIC", raising=False)
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    _force_meter_provider(provider)
    # Reset module state so instruments bind to this provider.
    m._initialized = False
    m._gauges_registered = False
    m._scrape_cache = None
    m.init_metrics()
    yield reader
    m._initialized = False
    m._gauges_registered = False


def _datapoints(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    out = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    for pt in metric.data.data_points:
                        out.append(pt)
    return out


def test_counters_and_histograms_record(metric_reader: InMemoryMetricReader) -> None:
    m.record_request_received(source="sentinel", key_type="incident_ids")
    m.record_request_pages(source="sentinel", page_count=20)
    m.record_source_call(source="sentinel", http_status=200)
    m.record_source_latency(source="sentinel", duration_ms=12.5)
    m.record_page_stage_duration(
        source="sentinel", stage="source_fetch", duration_ms=40.0
    )
    m.record_page_completed(source="sentinel", status="done")
    m.record_records_landed(source="sentinel", destination="bigquery", count=10)
    m.record_jobs_requeued(source="sentinel", count=2)
    m.record_jobs_dead_lettered(source="sentinel", count=1)
    m.record_page_shortfall(source="sentinel")

    assert _datapoints(metric_reader, "lisn.requests.received")
    assert _datapoints(metric_reader, "lisn.source.calls")
    assert _datapoints(metric_reader, "lisn.page.duration")
    assert _datapoints(metric_reader, "lisn.source.latency")
    assert _datapoints(metric_reader, "lisn.request.pages")
    assert _datapoints(metric_reader, "lisn.pages.completed")
    assert _datapoints(metric_reader, "lisn.records.landed")
    assert _datapoints(metric_reader, "lisn.jobs.requeued")
    assert _datapoints(metric_reader, "lisn.jobs.dead_lettered")
    assert _datapoints(metric_reader, "lisn.pages.shortfall")


def test_gauges_skipped_without_enable_periodic(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.delenv("ENABLE_PERIODIC", raising=False)
    m.register_observable_gauges()
    assert m._gauges_registered is False


def test_gauges_register_with_enable_periodic(
    metric_reader: InMemoryMetricReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "1")
    monkeypatch.setenv("ENABLE_PERIODIC", "1")
    # Avoid a real DB scrape during collect — patch observe to empty.
    monkeypatch.setattr(m, "_observe_health_state", lambda options: [])
    m.register_observable_gauges()
    assert m._gauges_registered is True


def test_source_from_worker_id() -> None:
    assert m._source_from_worker_id("sentinel-task0") == "sentinel"
    assert m._source_from_worker_id("sentinel_discovery-task0") == (
        "sentinel_discovery"
    )
    assert m._source_from_worker_id("maintenance-local") == "maintenance"


def test_periodic_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_PERIODIC", "1")
    assert m.periodic_enabled() is True
    monkeypatch.setenv("ENABLE_PERIODIC", "0")
    assert m.periodic_enabled() is False
