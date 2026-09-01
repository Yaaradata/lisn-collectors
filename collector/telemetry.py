"""OpenTelemetry foundation — export traces/logs/metrics to SigNoz over OTLP/gRPC.

FAIL OPEN (do not "improve" this by raising):
  If SigNoz is unreachable, slow, or the ingestion key is wrong, the collector
  must keep collecting. Observability outages must never become collection
  outages. init_telemetry() catches all exceptions, logs a WARNING to stdout,
  and returns without re-raising.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading

_log = logging.getLogger("collector.telemetry")
_initialized = False
_shutdown_lock = threading.Lock()
_shutdown_done = False

# Kept so SIGTERM can flush buffers. Batch processors lose unexported data on
# hard kill — this matters more here than usual because the interesting
# telemetry is often emitted right before a worker dies at the 24-hour ceiling.
_tracer_provider = None
_meter_provider = None
_logger_provider = None
_FLUSH_TIMEOUT_MS = 5_000  # short — never delay shutdown enough to strand a job


def _worker_id() -> str:
    """Same identity rules as collector.app.WORKER_ID — kept here so telemetry
    can run before app.py finishes importing (no circular import)."""
    task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")
    instance = os.environ.get("CLOUD_RUN_WORKER_POOL_REVISION")
    source = os.environ.get("COLLECTOR_SOURCE", "local")
    if task_index is not None:
        return f"{source}-task{task_index}"
    if instance:
        return f"{source}-{instance}"
    return f"{source}-local"


def _service_name() -> str:
    explicit = os.environ.get("OTEL_SERVICE_NAME", "").strip()
    if explicit:
        return explicit
    source = os.environ.get("COLLECTOR_SOURCE", "local")
    return f"lisn-{source}"


def shutdown_telemetry(signum: int | None = None, frame: object | None = None) -> None:
    """Flush tracer + meter (+ logs) providers. Safe to call more than once.

    Registered for SIGTERM / SIGINT / atexit. Timeout is intentionally short so
    a Cloud Run shutdown never waits long enough to strand an in-flight page —
    sweeper recovery beats a hung flush.
    """
    global _shutdown_done
    del frame
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True

    why = f"signal={signum}" if signum is not None else "atexit"
    try:
        if _tracer_provider is not None:
            _tracer_provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        if _meter_provider is not None:
            _meter_provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        if _logger_provider is not None:
            _logger_provider.force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
        # stderr print — logging may already be torn down at atexit.
        print(
            f"WARNING:collector.telemetry: otel flush ok ({why})",
            file=sys.stderr,
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 — never block process exit
        print(
            f"WARNING:collector.telemetry: otel flush failed ({why}): {exc}",
            file=sys.stderr,
            flush=True,
        )


def _register_shutdown_hooks() -> None:
    # SIGTERM is what Cloud Run sends at the 24h job ceiling / scale-down.
    # Chain the previous handler — replacing it would break uvicorn /
    # Procrastinate graceful shutdown and strand in-flight pages.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous = signal.getsignal(sig)

            def _handler(
                signum: int,
                frame: object | None,
                *,
                _prev: object = previous,
            ) -> None:
                shutdown_telemetry(signum, frame)
                if callable(_prev) and _prev not in (
                    signal.SIG_DFL,
                    signal.SIG_IGN,
                    signal.SIG_HOLD if hasattr(signal, "SIG_HOLD") else object(),
                ):
                    _prev(signum, frame)  # type: ignore[operator]
                elif _prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # signal.signal only works in the main thread.
            pass
    atexit.register(shutdown_telemetry)


def init_telemetry() -> None:
    """Configure OTel SDK + auto-instrumentation, or no-op.

    Guard: OTEL_ENABLED must be exactly \"1\". Local/tests stay quiet.
    Safe to call more than once — subsequent calls are no-ops.
    """
    global _initialized

    # Structured JSON + redaction always — even when OTel is off (Cloud Logging).
    from collector.logging_setup import configure_logging, log

    configure_logging()

    if _initialized:
        return
    if os.environ.get("OTEL_ENABLED") != "1":
        return

    # FAIL OPEN — wrap the entire setup. A broken exporter, bad key, or missing
    # dependency must never take down the API or a worker. Do not raise here.
    try:
        _configure()
        _register_shutdown_hooks()
        _initialized = True
    except Exception as exc:  # noqa: BLE001 — intentional fail-open
        log(
            _log,
            logging.WARNING,
            "opentelemetry init failed — continuing without telemetry",
            status="otel_disabled",
            error=str(exc),
        )


def _configure() -> None:
    # Imports live here (not at module top) so OTEL_ENABLED!=1 pays zero import
    # cost and a missing/broken OTel install cannot break collectors that leave
    # the flag off. Fail-open still wraps the caller.
    global _tracer_provider, _meter_provider, _logger_provider

    from opentelemetry import metrics as metrics_api
    from opentelemetry import trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from collector.logging_setup import RedactFilter, log as structured_log
    from collector.metrics import init_metrics, register_observable_gauges

    source = os.environ.get("COLLECTOR_SOURCE", "local")
    worker_id = _worker_id()
    task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")
    execution = os.environ.get("CLOUD_RUN_EXECUTION")
    region = os.environ.get("REGION", "asia-south1")
    environment = os.environ.get("DEPLOYMENT_ENV", "dev")
    version = (
        os.environ.get("SERVICE_VERSION")
        or os.environ.get("GIT_SHA")
        or os.environ.get("K_REVISION")
        or "unknown"
    )
    endpoint = os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "ingest.us2.signoz.cloud:443"
    )
    ingestion_key = os.environ.get("SIGNOZ_INGESTION_KEY", "")

    attrs: dict[str, str] = {
        "service.name": _service_name(),
        "service.version": version,
        "deployment.environment": environment,
        "cloud.provider": "gcp",
        "cloud.region": region,
        "cloud.platform": "gcp_cloud_run",
        "lisn.worker_id": worker_id,
        "lisn.source": source,
    }
    if execution:
        attrs["faas.instance"] = execution
    # lisn.task_index matters: three enrichment tasks run identical code.
    # Distinguishing them answers "is one worker doing all the work" and
    # "did task 0 come back with the same identity after a restart" — both
    # real questions from the acceptance run (F-3 / capacity).
    if task_index is not None:
        attrs["lisn.task_index"] = task_index

    resource = Resource.create(attrs)

    otlp_headers = (
        {"signoz-ingestion-key": ingestion_key} if ingestion_key else None
    )
    # asia-south1 → us2 can exceed the SDK default (~10s) on cold paths;
    # DEADLINE_EXCEEDED without this shows up as noisy Failed to export lines.
    otlp_timeout_s = float(os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "30"))

    # --- Traces ---
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(
        endpoint=endpoint,
        headers=otlp_headers,
        insecure=False,  # TLS always — never insecure against SigNoz Cloud
        timeout=otlp_timeout_s,
    )
    # BatchSpanProcessor, not Simple — Simple is for tests; Batch is production.
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider

    # --- Metrics (never sampled — cheap vs logs/traces) ---
    # 30s export matches the gauge scrape cadence the acceptance alert needs.
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(
            endpoint=endpoint,
            headers=otlp_headers,
            insecure=False,
            timeout=otlp_timeout_s,
        ),
        export_interval_millis=30_000,
    )
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[metric_reader]
    )
    metrics_api.set_meter_provider(meter_provider)
    _meter_provider = meter_provider

    init_metrics()
    # Gauges only on maintenance (ENABLE_PERIODIC=1). Enrichment workers that
    # also registered them would triple-count global DB state.
    register_observable_gauges()

    # --- Logs ---
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)
    _logger_provider = logger_provider
    log_exporter = OTLPLogExporter(
        endpoint=endpoint,
        headers=otlp_headers,
        insecure=False,
        timeout=otlp_timeout_s,
    )
    # BatchLogRecordProcessor, not Simple — same reason as spans.
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter)
    )
    # Export stdlib logging through OTLP; LoggingInstrumentor below injects
    # trace_id / span_id onto LogRecord so JsonFormatter can link logs ↔ traces.
    # Only add once — a second handler would double-ship every log to SigNoz.
    root = logging.getLogger()
    if not any(isinstance(h, LoggingHandler) for h in root.handlers):
        otel_handler = LoggingHandler(
            level=logging.NOTSET, logger_provider=logger_provider
        )
        otel_handler.addFilter(RedactFilter())
        root.addHandler(otel_handler)

    # --- Auto-instrumentation ---
    FastAPIInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    PsycopgInstrumentor().instrument()
    # set_logging_format=False — keep our JSON formatter; instrumentor still
    # injects otelTraceID / otelSpanID onto LogRecord for JsonFormatter.
    LoggingInstrumentor().instrument(set_logging_format=False)

    structured_log(
        _log,
        logging.INFO,
        "opentelemetry enabled",
        worker_id=worker_id,
        source=source,
        status="otel_ready",
        service=attrs["service.name"],
        endpoint=endpoint,
    )
