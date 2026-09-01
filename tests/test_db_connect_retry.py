"""Unit tests for DB connect retry / backoff (no shared Cloud SQL touched)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from collector.db import connect, dsn_for_log, wait_for_db


def test_dsn_for_log_redacts_password() -> None:
    out = dsn_for_log("postgresql://postgres:s3cret@127.0.0.1:5432/collector")
    assert "s3cret" not in out
    assert "***" in out
    assert "127.0.0.1" in out


def test_wait_for_db_retries_then_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="collector.db")
    calls = {"n": 0}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return None

    def fake_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise psycopg.OperationalError("connection refused")
        return _Conn()

    with (
        patch.dict("os.environ", {"COLLECTOR_DSN": "postgresql://u:p@127.0.0.1:1/db"}),
        patch("collector.db.psycopg.connect", side_effect=fake_connect),
        patch("collector.db.time.sleep", return_value=None),
    ):
        wait_for_db(budget_s=60)

    assert calls["n"] == 3
    failures = [
        r for r in caplog.records if "database connect attempt failed" in r.getMessage()
    ]
    assert len(failures) >= 2
    assert any("database reachable after backoff" in r.getMessage() for r in caplog.records)
    # One structured line per failure — no separate "connect detail" twin.
    assert not any("connect detail" in r.getMessage() for r in caplog.records)


def test_wait_for_db_first_try_silent_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Healthy first connect must not WARN — that flooded every worker restart."""
    caplog.set_level(logging.DEBUG, logger="collector.db")

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args, **kwargs):
            return None

    with (
        patch.dict("os.environ", {"COLLECTOR_DSN": "postgresql://u:p@127.0.0.1:1/db"}),
        patch("collector.db.psycopg.connect", return_value=_Conn()),
    ):
        wait_for_db(budget_s=60)

    assert not any(
        "database reachable after backoff" in r.getMessage() for r in caplog.records
    )
    assert any("database connected" in r.getMessage() for r in caplog.records)
    assert all(
        r.levelno < logging.WARNING
        for r in caplog.records
        if "database connected" in r.getMessage()
    )


def test_wait_for_db_exits_after_budget(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.CRITICAL, logger="collector.db")
    with (
        patch.dict("os.environ", {"COLLECTOR_DSN": "postgresql://u:p@127.0.0.1:1/db"}),
        patch(
            "collector.db.psycopg.connect",
            side_effect=psycopg.OperationalError("connection refused"),
        ),
        patch("collector.db.time.sleep", return_value=None),
        patch("collector.db.time.monotonic", side_effect=[0.0, 0.0, 5.0, 5.0, 61.0, 61.0]),
    ):
        with pytest.raises(SystemExit) as ei:
            wait_for_db(budget_s=60)
        assert ei.value.code == 1
    assert any(
        "cannot reach the database" in r.getMessage() for r in caplog.records
    )
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_connect_retries_mid_run(caplog: pytest.LogCaptureFixture) -> None:
    """Mid-run path: connect() retries rather than failing the first blip."""
    caplog.set_level(logging.WARNING, logger="collector.db")
    real = MagicMock(name="conn")
    calls = {"n": 0}

    def fake_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return real

    with (
        patch.dict("os.environ", {"COLLECTOR_DSN": "postgresql://u:p@127.0.0.1:1/db"}),
        patch("collector.db.psycopg.connect", side_effect=fake_connect),
        patch("collector.db.time.sleep", return_value=None),
    ):
        got = connect()

    assert got is real
    assert calls["n"] == 2
    assert any(
        "database connect attempt failed" in r.getMessage() for r in caplog.records
    )
