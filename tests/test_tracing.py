"""Propagation: API inject → Postgres carrier → worker child spans."""

from __future__ import annotations

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.util._once import Once

from collector.tracing import (
    inject_trace_context,
    key_type_and_count,
    parent_context,
    parse_trace_context,
    traced_span,
)


def _force_tracer_provider(provider: TracerProvider) -> None:
    """Tests need a fresh SDK provider; production never calls this."""
    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace.set_tracer_provider(provider)


@pytest.fixture()
def memory_spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _force_tracer_provider(provider)
    yield exporter
    exporter.clear()


def test_inject_extract_roundtrip_links_child_to_collect_request(
    memory_spans: InMemorySpanExporter,
) -> None:
    """The fan-out only works if fetch_page is a CHILD of collect_request."""
    with traced_span(
        "collect_request",
        attributes={"lisn.source": "sentinel", "lisn.page_count": 20},
    ):
        with traced_span("plan_pages"):
            pass
        carrier = inject_trace_context()
        with traced_span("write_job_rows"):
            pass
        with traced_span("defer_jobs"):
            pass

    assert "traceparent" in (parse_trace_context(carrier) or {})

    # Simulate a worker process reading the carrier from collector_job.
    with traced_span(
        "fetch_page",
        parent=parent_context(carrier),
        attributes={
            "lisn.job_id": "job-1",
            "lisn.page_no": 0,
            "lisn.attempt": 1,
        },
    ):
        with traced_span("claim_job"):
            pass
        with traced_span("rate_wait"):
            pass
        with traced_span("source_fetch"):
            pass
        with traced_span("write_raw_gcs"):
            pass
        with traced_span("parse_records"):
            pass
        with traced_span("load_bigquery"):
            pass
        with traced_span("mark_complete"):
            pass

    spans = list(memory_spans.get_finished_spans())
    by_name = {s.name: s for s in spans}
    assert set(by_name) >= {
        "collect_request",
        "plan_pages",
        "write_job_rows",
        "defer_jobs",
        "fetch_page",
        "claim_job",
        "source_fetch",
        "load_bigquery",
        "mark_complete",
    }

    collect = by_name["collect_request"]
    fetch = by_name["fetch_page"]
    assert fetch.context.trace_id == collect.context.trace_id
    assert fetch.parent is not None
    assert fetch.parent.span_id == collect.context.span_id

    # Stage children share the fetch_page parent (same process).
    assert by_name["source_fetch"].parent.span_id == fetch.context.span_id
    assert by_name["load_bigquery"].parent.span_id == fetch.context.span_id


def test_span_error_status_on_failure(
    memory_spans: InMemorySpanExporter,
) -> None:
    try:
        with traced_span("fetch_page"):
            with traced_span("source_fetch"):
                raise RuntimeError("source returned 503")
    except RuntimeError:
        pass

    spans = {s.name: s for s in memory_spans.get_finished_spans()}
    assert spans["source_fetch"].status.status_code == trace.StatusCode.ERROR
    assert spans["fetch_page"].status.status_code == trace.StatusCode.ERROR


def test_plain_traceparent_string_accepted(
    memory_spans: InMemorySpanExporter,
) -> None:
    with traced_span("collect_request"):
        carrier = inject_trace_context()
    parsed = parse_trace_context(carrier)
    assert parsed is not None
    tp = parsed["traceparent"]
    assert parse_trace_context(tp) == {"traceparent": tp}


def test_key_type_and_count() -> None:
    assert key_type_and_count({"incident_ids": ["a", "b"]}, "sentinel") == (
        "incident_ids",
        2,
    )
    assert key_type_and_count({}, "sentinel_discovery") == ("discovery_filter", 0)


def test_carrier_is_json(memory_spans: InMemorySpanExporter) -> None:
    with traced_span("collect_request"):
        raw = inject_trace_context()
    data = json.loads(raw)
    assert "traceparent" in data
    assert data["traceparent"].startswith("00-")
