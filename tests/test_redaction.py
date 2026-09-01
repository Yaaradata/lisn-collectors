"""Unit tests for collector.redact.redact_secrets."""

from __future__ import annotations

from collector.redact import redact_secrets


def test_uri_dsn_password_redacted() -> None:
    # psycopg-style connection error that embeds a URI DSN
    raw = (
        'connection failed: postgresql://postgres:s3cret-pass@127.0.0.1:5432/collector '
        "could not connect to server"
    )
    out = redact_secrets(raw)
    assert out is not None
    assert "s3cret-pass" not in out
    assert "postgresql://postgres:***@127.0.0.1:5432/collector" in out


def test_key_value_password_redacted() -> None:
    raw = (
        "connection to server failed: "
        "host=127.0.0.1 port=5432 dbname=collector "
        "user=postgres password=s3cret-pass"
    )
    out = redact_secrets(raw)
    assert out is not None
    assert "s3cret-pass" not in out
    assert "password=***" in out


def test_bearer_token_redacted() -> None:
    raw = "upstream 401: Authorization: Bearer ya29.a0AfH6SMB-secret-token"
    out = redact_secrets(raw)
    assert out is not None
    assert "ya29.a0AfH6SMB-secret-token" not in out
    assert "Authorization: Bearer ***" in out


def test_api_key_style_unchanged_when_no_match() -> None:
    raw = "injected poison for dead-letter test"
    assert redact_secrets(raw) == raw


def test_signoz_header_pattern() -> None:
    out = redact_secrets("signoz-ingestion-key=deadbeefcafebabe")
    assert out == "signoz-ingestion-key=***"


def test_none_returns_none() -> None:
    assert redact_secrets(None) is None
