"""Structured, levelled, redacted logging for the LiSN collector.

LEVEL POLICY — follow exactly. Undisciplined levels make logs useless.

  DEBUG    — high-volume detail, off in production.
             page payload sizes, cursor tokens, SQL statement timings,
             individual HTTP request and response sizes, sleep durations.

  INFO     — lifecycle events. One line per meaningful state change, no more.
             worker starting (identity + connect budget);
             request accepted with page count; page claimed by worker;
             page completed with record count and duration; sweep result;
             discovery window completed with id count;
             opentelemetry enabled.

  WARNING  — something recovered, or something is trending wrong.
             page retry with attempt number; sweeper requeued a stranded job;
             shortfall — fewer records returned than keys requested;
             rate ceiling within 20% of its limit;
             discovery window overlaps the previous one;
             SigNoz export failed (logged to stdout only, obviously).

  ERROR    — an operation failed and will be retried.
             source returned 5xx; source returned malformed payload;
             GCS write failed; BigQuery insert failed;
             page exhausted its attempts (before dead-letter).

  CRITICAL — needs a human now.
             worker cannot reach the database at startup after full backoff;
             page dead-lettered;
             DISCOVERY GAP DETECTED — acceptance run lost 104 incidents with
               every surface green;
             reconcile found raw-without-load rows.

  CRITICAL choices: these three (DB unreachable after backoff, dead-letter,
  discovery gap, reconcile unloaded) were previously silent or easy to miss.
  Anything that can lose data without erroring belongs at CRITICAL, regardless
  of how quiet it looks.

Structured fields travel as LogRecord attributes (not embedded in the message
string) so SigNoz / Cloud Logging can filter on them. JSON on the wire.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from collector.redact import redact_secrets

# Keys every collector log may carry as attributes (omit when None).
STRUCTURED_KEYS = (
    "request_id",
    "job_id",
    "source",
    "page_no",
    "worker_id",
    "attempt",
    "status",
    "duration_ms",
    "record_count",
    "requested_count",
    "returned_count",
    "error",
    "service",
    "endpoint",
)

_configured = False


class RedactFilter(logging.Filter):
    """Scrub credentials from every log record at WRITE time.

    A credential must never reach Cloud Logging or SigNoz. Read-time scrubbing
    is too late — see acceptance finding: DSN password in /v1/dead-letter.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg) or ""
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )
        for key in STRUCTURED_KEYS:
            val = getattr(record, key, None)
            if isinstance(val, str):
                setattr(record, key, redact_secrets(val))
        err = getattr(record, "exc_text", None)
        if isinstance(err, str):
            record.exc_text = redact_secrets(err)
        return True


class JsonFormatter(logging.Formatter):
    """JSON lines for Cloud Run (parses into structured fields) and SigNoz."""

    def format(self, record: logging.LogRecord) -> str:
        # LoggingInstrumentor(set_logging_format=True) injects these names.
        trace_id = getattr(record, "otelTraceID", None) or getattr(
            record, "trace_id", None
        )
        span_id = getattr(record, "otelSpanID", None) or getattr(
            record, "span_id", None
        )
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if trace_id and trace_id != "0" * 32:
            payload["trace_id"] = trace_id
        if span_id and span_id != "0" * 16:
            payload["span_id"] = span_id
        for key in STRUCTURED_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def debug_sample_n() -> int:
    """1 in N pages emit per-page DEBUG. Never samples WARNING+."""
    raw = os.environ.get("LOG_DEBUG_SAMPLE_N", "100")
    try:
        n = int(raw)
    except ValueError:
        return 100
    return max(1, n)


def should_sample_debug(*, page_no: int | None = None) -> bool:
    """Return True if this DEBUG line should be emitted."""
    n = debug_sample_n()
    if n <= 1:
        return True
    if page_no is None:
        return True
    return int(page_no) % n == 0


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def bind(logger: logging.Logger, **ctx: Any) -> logging.LoggerAdapter:
    """Return an adapter that attaches structured context to every record."""
    clean = {k: v for k, v in ctx.items() if v is not None}
    return logging.LoggerAdapter(logger, clean)


def log(
    logger: logging.Logger | logging.LoggerAdapter,
    level: int,
    msg: str,
    *,
    page_no: int | None = None,
    **ctx: Any,
) -> None:
    """Log *msg* at *level* with structured attributes.

    DEBUG lines honour LOG_DEBUG_SAMPLE_N (page_no % N == 0). WARNING and
    above are never sampled.
    """
    if level <= logging.DEBUG and not should_sample_debug(page_no=page_no):
        return
    extra = {k: v for k, v in ctx.items() if v is not None}
    if page_no is not None:
        extra.setdefault("page_no", page_no)
    # LoggerAdapter.process overwrites kwargs['extra'] with its own context
    # on 3.12 — merge explicitly onto the underlying logger.
    if isinstance(logger, logging.LoggerAdapter):
        merged = {**(logger.extra or {}), **extra}
        logger.logger.log(level, msg, extra=merged)
    else:
        logger.log(level, msg, extra=extra)


def _quiet_third_party() -> None:
    """Keep library chatter out of the ops stream.

    Collector lifecycle is INFO. Procrastinate blueprint registration,
    uvicorn access lines, and OTel exporter retries are noise at INFO —
    surface only WARNING+ from those packages.
    """
    for name in (
        "procrastinate",
        "procrastinate.worker",
        "procrastinate.periodic",
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    # OTLP export failures still matter, but at most one line per minute —
    # BatchSpanProcessor otherwise floods Cloud Logging on a blip.
    for name in (
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.metrics.export",
        "opentelemetry.sdk._logs.export",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)
        if not any(isinstance(f, _RateLimitFilter) for f in lg.filters):
            lg.addFilter(_RateLimitFilter(interval_s=60.0))


class _RateLimitFilter(logging.Filter):
    """Drop repeat messages from the same logger within *interval_s*."""

    def __init__(self, interval_s: float = 60.0) -> None:
        super().__init__()
        self._interval_s = interval_s
        self._last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = f"{record.name}:{record.getMessage()[:120]}"
        now = time.monotonic()
        prev = self._last.get(key)
        if prev is not None and (now - prev) < self._interval_s:
            return False
        self._last[key] = now
        return True


def configure_logging() -> None:
    """Install JSON formatter + redaction on the root logger. Idempotent."""
    global _configured
    if _configured:
        return

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Replace handlers so we do not double-emit after basicConfig / re-import.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Filter on every handler so child loggers (collector.*) are scrubbed.
    # Logger filters on root alone do NOT apply to child loggers in stdlib
    # logging — only the originating logger's filters run.
    if not any(isinstance(f, RedactFilter) for f in root.filters):
        root.addFilter(RedactFilter())

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter())
    root.addHandler(handler)

    # Ensure collector.* loggers propagate to root — no per-module handlers.
    logging.getLogger("collector").setLevel(level)
    logging.getLogger("collector").propagate = True

    _quiet_third_party()
    _configured = True
