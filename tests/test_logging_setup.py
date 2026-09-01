"""Unit tests for structured JSON logging, redaction filter, DEBUG sampling."""

from __future__ import annotations

import json
import logging

import pytest

from collector.logging_setup import (
    JsonFormatter,
    RedactFilter,
    configure_logging,
    log,
    should_sample_debug,
)
from collector.redact import redact_secrets


@pytest.fixture(autouse=True)
def _reset_logging_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import collector.logging_setup as ls

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_DEBUG_SAMPLE_N", "100")
    ls._configured = False
    configure_logging()
    yield
    ls._configured = False
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for f in list(root.filters):
        root.removeFilter(f)


def test_json_formatter_includes_structured_keys() -> None:
    record = logging.LogRecord(
        name="collector.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="page completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "rid-1"
    record.job_id = "jid-1"
    record.source = "sentinel"
    record.page_no = 3
    record.worker_id = "sentinel-local"
    record.attempt = 1
    record.status = "done"
    record.duration_ms = 42
    record.record_count = 10
    record.otelTraceID = "a" * 32
    record.otelSpanID = "b" * 16

    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["message"] == "page completed"
    assert payload["request_id"] == "rid-1"
    assert payload["source"] == "sentinel"
    assert payload["page_no"] == 3
    assert payload["trace_id"] == "a" * 32
    assert payload["span_id"] == "b" * 16


def test_redact_filter_scrubs_dsn_in_message() -> None:
    filt = RedactFilter()
    record = logging.LogRecord(
        name="collector.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="fail postgresql://u:secret@host/db",
        args=(),
        exc_info=None,
    )
    assert filt.filter(record) is True
    assert "secret" not in record.msg
    assert "***" in record.msg


def test_signoz_ingestion_key_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNOZ_INGESTION_KEY", "sk_live_super_secret")
    raw = "export failed key=sk_live_super_secret in header"
    out = redact_secrets(raw)
    assert out is not None
    assert "sk_live_super_secret" not in out
    assert "***" in out
    assert "signoz-ingestion-key=abc" not in (
        redact_secrets("signoz-ingestion-key=abc123xyz") or ""
    )
    scrubbed = redact_secrets("signoz-ingestion-key=abc123xyz")
    assert scrubbed == "signoz-ingestion-key=***"


def test_debug_sampling_by_page_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_DEBUG_SAMPLE_N", "10")
    assert should_sample_debug(page_no=0) is True
    assert should_sample_debug(page_no=10) is True
    assert should_sample_debug(page_no=1) is False
    assert should_sample_debug(page_no=None) is True


def test_configure_logging_redacts_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SIGNOZ_INGESTION_KEY", "sk_live_test_key")
    import collector.logging_setup as ls

    ls._configured = False
    configure_logging()
    lg = logging.getLogger("collector.redact_stdout")
    log(
        lg,
        logging.ERROR,
        "boom postgresql://u:hunter2@db/collector Authorization: Bearer tok123",
        source="sentinel",
        status="error",
    )
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "tok123" not in out
    assert "postgresql://u:***@db/collector" in out
    assert "Authorization: Bearer ***" in out


def test_warning_never_sampled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_DEBUG_SAMPLE_N", "1000")
    import collector.logging_setup as ls

    ls._configured = False
    configure_logging()
    lg = logging.getLogger("collector.sample_test")
    log(
        lg,
        logging.WARNING,
        "page retry",
        page_no=1,
        source="sentinel",
        attempt=2,
        status="retry",
    )
    out = capsys.readouterr().out
    assert "page retry" in out
    assert '"level":"WARNING"' in out
    assert '"source":"sentinel"' in out


def test_configure_logging_quiets_procrastinate_info() -> None:
    import collector.logging_setup as ls

    ls._configured = False
    configure_logging()
    assert logging.getLogger("procrastinate").level == logging.WARNING
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_json_formatter_includes_error_field() -> None:
    record = logging.LogRecord(
        name="collector.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="database connect attempt failed",
        args=(),
        exc_info=None,
    )
    record.error = "connection refused"
    record.attempt = 2
    payload = json.loads(JsonFormatter().format(record))
    assert payload["error"] == "connection refused"
    assert payload["attempt"] == 2
