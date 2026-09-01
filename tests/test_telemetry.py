"""Telemetry init guards — no network; OTEL packages may be loaded."""

from __future__ import annotations

import os
from unittest.mock import patch

# Ensure app/telemetry imports do not require a live DB.
os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)
os.environ["OTEL_ENABLED"] = "0"

from collector import telemetry


def test_init_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "0")
    telemetry._initialized = False
    with patch.object(telemetry, "_configure") as configure:
        telemetry.init_telemetry()
        configure.assert_not_called()


def test_init_fail_open_does_not_raise(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "1")
    telemetry._initialized = False
    with patch.object(
        telemetry, "_configure", side_effect=RuntimeError("signoz down")
    ):
        telemetry.init_telemetry()  # must not raise
    assert telemetry._initialized is False


def test_service_name_prefers_explicit(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "lisn-collector-api")
    monkeypatch.setenv("COLLECTOR_SOURCE", "sentinel")
    assert telemetry._service_name() == "lisn-collector-api"


def test_service_name_defaults_to_source(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setenv("COLLECTOR_SOURCE", "sentinel_discovery")
    assert telemetry._service_name() == "lisn-sentinel_discovery"


def test_shutdown_telemetry_is_idempotent(monkeypatch) -> None:
    import collector.telemetry as tel

    tel._shutdown_done = False
    tel._tracer_provider = None
    tel._meter_provider = None
    tel._logger_provider = None
    tel.shutdown_telemetry()
    tel.shutdown_telemetry()  # second call no-ops
    assert tel._shutdown_done is True


def test_logs_http_endpoint_from_grpc_host(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)
    assert (
        telemetry._logs_http_endpoint("ingest.us2.signoz.cloud:443")
        == "https://ingest.us2.signoz.cloud/v1/logs"
    )


def test_logs_http_endpoint_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "https://custom.example/v1/logs",
    )
    assert (
        telemetry._logs_http_endpoint("ingest.us2.signoz.cloud:443")
        == "https://custom.example/v1/logs"
    )
