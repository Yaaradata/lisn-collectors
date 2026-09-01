"""Distributed tracing helpers for the collection pipeline.

DO NOT emit one span per record / incident. One span per page (and per
discovery cursor page inside fetch). At 300,000 incidents a row-level span
budget would make SigNoz unusable and the bill unbearable.

Propagation: API and worker are different processes connected only by
Postgres. There is no HTTP call between them, so W3C ``traceparent`` travels
on ``collector_job.trace_context``. Get that wrong and every page is its own
root — the fan-out that answers "which stage is slow" disappears.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Status, StatusCode

from collector.redact import redact_secrets

TRACER_NAME = "lisn.collector"


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def task_index() -> str | None:
    return os.environ.get("CLOUD_RUN_TASK_INDEX")


def url_host(url: str) -> str:
    """Host only — never the full URL (paths/query can carry keys)."""
    try:
        return urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001 — defensive; never break a span for this
        return ""


def inject_trace_context() -> str:
    """Serialize the current W3C context for storage on collector_job."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return json.dumps(carrier, separators=(",", ":"))


def parse_trace_context(raw: str | None) -> Mapping[str, str] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Legacy / accidental plain traceparent string.
        if isinstance(raw, str) and raw.startswith("00-"):
            return {"traceparent": raw}
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items() if v is not None}


def parent_context(raw: str | None) -> otel_context.Context | None:
    """Return an OTel context extracted from a stored carrier, or None."""
    carrier = parse_trace_context(raw)
    if not carrier:
        return None
    return extract(carrier)


def _set_attrs(span: trace.Span, attributes: Mapping[str, Any] | None) -> None:
    if not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        # OTel attributes must be primitives / homogeneous sequences.
        if isinstance(value, (bool, int, float, str)):
            span.set_attribute(key, value)
        else:
            span.set_attribute(key, str(value))


def record_span_error(span: trace.Span, exc: BaseException) -> None:
    span.record_exception(exc)
    msg = redact_secrets(str(exc)) or type(exc).__name__
    span.set_status(Status(StatusCode.ERROR, msg[:500]))


@contextmanager
def traced_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    parent: otel_context.Context | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> Iterator[trace.Span]:
    """Start a span; on exception record it and set ERROR status, then re-raise.

    When *parent* is set (worker continuing an API trace), the span is a child
    of that remote context rather than of whatever is current in this process.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(
        name,
        context=parent,
        kind=kind,
    ) as span:
        _set_attrs(span, attributes)
        try:
            yield span
        except Exception as exc:
            # Client/validation HTTP errors (4xx) are expected outcomes of
            # collect_request — do not paint the span ERROR for those.
            status = getattr(exc, "status_code", None)
            if not (isinstance(status, int) and 400 <= status < 500):
                record_span_error(span, exc)
            raise


def key_type_and_count(
    query_spec: Mapping[str, Any], source: str
) -> tuple[str, int]:
    for field in ("incident_ids", "order_item_ids", "order_ids"):
        values = query_spec.get(field)
        if isinstance(values, list):
            return field, len(values)
    if source == "sentinel_discovery":
        return "discovery_filter", 0
    return "unknown", 0
