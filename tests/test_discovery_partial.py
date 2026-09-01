"""Partial discovery windows — truncation at the ID cap must not masquerade as complete."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)

from collector.api import api
from collector.discovery_gaps import (
    effective_discovery_limit,
    gap_summary,
    list_gaps,
    maybe_finalize_window,
    partial_windows_summary,
)
from collector.sources.sentinel_discovery import CURSOR_PAGE_CAP
from tests._sql_apply import apply_sql_file

SOURCE = "sentinel_discovery"
FIELD = "updated_on"
T0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
PRODUCTION_WINDOW_HOURS = 168
DEFAULT_LIMIT = 1000
EFFECTIVE_CAP = DEFAULT_LIMIT * CURSOR_PAGE_CAP  # 10000 — production observation


def _load_dotenv() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(env_path):
        return
    for raw in open(env_path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


class _CursorCtx:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self._cur

    def __exit__(self, *args):
        return False

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _ConnProxy:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _CursorCtx(self._conn.cursor())

    def commit(self):
        self._conn.commit()


@pytest.fixture
def db():
    _load_dotenv()
    dsn = os.environ.get("COLLECTOR_DSN")
    if not dsn or "127.0.0.1" in dsn:
        pytest.skip("COLLECTOR_DSN with direct IP required")

    import psycopg

    conn = psycopg.connect(dsn)
    cur = conn.cursor()
    for sql_file in ("010_discovery_window.sql", "001_collector.sql"):
        path = os.path.join(os.path.dirname(__file__), "..", "sql", sql_file)
        if os.path.isfile(path):
            apply_sql_file(cur, path)
    conn.commit()

    cur.execute("DELETE FROM discovery_window WHERE source = %s", (SOURCE,))
    conn.commit()

    proxy = _ConnProxy(conn)
    yield conn, None, proxy

    cur = conn.cursor()
    cur.execute("DELETE FROM discovery_window WHERE source = %s", (SOURCE,))
    cur.execute(
        """
        DELETE FROM collector_job j
        USING collector_request r
        WHERE j.request_id = r.request_id AND r.source = %s
        """,
        (SOURCE,),
    )
    cur.execute("DELETE FROM collector_request WHERE source = %s", (SOURCE,))
    conn.commit()
    cur.close()
    conn.close()


def test_effective_discovery_limit_default() -> None:
    assert effective_discovery_limit({"limit": 1000}) == 10000
    assert effective_discovery_limit({}) == 1000 * CURSOR_PAGE_CAP


def _seed_running_window(
    cur,
    *,
    request_id: uuid.UUID,
    job_id: uuid.UUID,
    window_from: datetime,
    window_to: datetime,
    record_count: int,
    limit: int = DEFAULT_LIMIT,
) -> None:
    query_spec = {
        "updated_from": window_from.isoformat().replace("+00:00", "Z"),
        "updated_to": window_to.isoformat().replace("+00:00", "Z"),
        "limit": limit,
    }
    cur.execute(
        """
        INSERT INTO collector_request (
          request_id, source, query_spec, total_pages, status
        ) VALUES (%s, %s, %s::jsonb, 1, 'open')
        """,
        (request_id, SOURCE, json.dumps(query_spec)),
    )
    cur.execute(
        """
        INSERT INTO collector_job (
          job_id, request_id, source, page_no, page_payload, status,
          record_count
        ) VALUES (%s, %s, %s, 0, %s::jsonb, 'done', %s)
        """,
        (job_id, request_id, SOURCE, json.dumps(query_spec), record_count),
    )
    cur.execute(
        """
        INSERT INTO discovery_window (
          source, window_field, window_from, window_to, request_id, status
        ) VALUES (%s, %s, %s, %s, %s, 'running')
        """,
        (SOURCE, FIELD, window_from, window_to, request_id),
    )


def test_production_168h_window_partial_at_cap(db) -> None:
    """Exact production case: 168h window, id_count == round limit → partial."""
    conn, _, proxy = db
    rid = uuid.uuid4()
    jid = uuid.uuid4()
    window_from = T0
    window_to = T0 + timedelta(hours=PRODUCTION_WINDOW_HOURS)
    cur = conn.cursor()
    _seed_running_window(
        cur,
        request_id=rid,
        job_id=jid,
        window_from=window_from,
        window_to=window_to,
        record_count=EFFECTIVE_CAP,
    )
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        status = maybe_finalize_window(str(rid))

    assert status == "partial"
    cur.execute(
        "SELECT status, id_count FROM discovery_window WHERE request_id = %s",
        (rid,),
    )
    row = cur.fetchone()
    assert row == ("partial", EFFECTIVE_CAP)

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        gaps = list_gaps(source=SOURCE)
        partial = partial_windows_summary(source=SOURCE)
        health_gaps = gap_summary()

    truncated = [g for g in gaps if g.get("reason") == "truncated"]
    assert len(truncated) >= 1
    match = [g for g in truncated if g["before_request_id"] == str(rid)]
    assert len(match) == 1
    g = match[0]
    assert g["uncertain"] is True
    assert g["gap_from"] == window_from.isoformat()
    assert g["gap_to"] == window_to.isoformat()

    assert partial["count"] >= 1
    assert any(w["request_id"] == str(rid) for w in partial["windows"])
    assert health_gaps["count"] >= 1


def test_below_cap_stays_complete(db) -> None:
    conn, _, proxy = db
    rid = uuid.uuid4()
    jid = uuid.uuid4()
    window_from = T0
    window_to = T0 + timedelta(hours=1)
    cur = conn.cursor()
    _seed_running_window(
        cur,
        request_id=rid,
        job_id=jid,
        window_from=window_from,
        window_to=window_to,
        record_count=445,
    )
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        status = maybe_finalize_window(str(rid))
    assert status == "complete"


def test_submit_truncation_warning_from_recent_windows(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    # Hourly windows returning ~445 ids/h → 168h ≈ 74k > 10k cap
    for hour in range(6):
        rid = uuid.uuid4()
        start = T0 + timedelta(hours=hour)
        end = start + timedelta(hours=1)
        cur.execute(
            """
            INSERT INTO collector_request (
              request_id, source, query_spec, total_pages, status
            ) VALUES (%s, %s, '{}'::jsonb, 1, 'open')
            """,
            (rid, SOURCE),
        )
        cur.execute(
            """
            INSERT INTO discovery_window (
              source, window_field, window_from, window_to,
              request_id, status, id_count, completed_at
            ) VALUES (%s, %s, %s, %s, %s, 'complete', 445, now())
            """,
            (SOURCE, FIELD, start, end, rid),
        )
    conn.commit()

    client = TestClient(api)
    start = T0
    end = T0 + timedelta(hours=PRODUCTION_WINDOW_HOURS)
    defer = MagicMock()
    configured = MagicMock()
    configured.defer = defer
    fetch = MagicMock()
    fetch.configure.return_value = configured
    app_cm = MagicMock()
    app_cm.__enter__.return_value = None
    app_cm.__exit__.return_value = False

    with (
        patch("collector.discovery_gaps.connect", return_value=proxy),
        patch("collector.api.connect", return_value=proxy),
        patch("collector.api.procrastinate_app.open", return_value=app_cm),
        patch("collector.api.fetch_page", fetch),
    ):
        r = client.post(
            "/v1/collect",
            json={
                "source": SOURCE,
                "query_spec": {
                    "updated_from": start.isoformat().replace("+00:00", "Z"),
                    "updated_to": end.isoformat().replace("+00:00", "Z"),
                    "limit": DEFAULT_LIMIT,
                },
                "allow_gap": True,
                "gap_reason": "test: truncation warning probe",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "truncation_warnings" in body
    warn = body["truncation_warnings"][0]
    assert warn["truncation_likely"] is True
    assert warn["effective_limit"] == EFFECTIVE_CAP
    assert warn["estimated_ids"] > EFFECTIVE_CAP


def test_health_detail_includes_partial_windows(db) -> None:
    conn, _, proxy = db
    rid = uuid.uuid4()
    jid = uuid.uuid4()
    window_from = T0
    window_to = T0 + timedelta(hours=PRODUCTION_WINDOW_HOURS)
    cur = conn.cursor()
    _seed_running_window(
        cur,
        request_id=rid,
        job_id=jid,
        window_from=window_from,
        window_to=window_to,
        record_count=EFFECTIVE_CAP,
    )
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        maybe_finalize_window(str(rid))

    client = TestClient(api)
    with (
        patch("collector.discovery_gaps.connect", return_value=proxy),
        patch("collector.api.connect", return_value=proxy),
    ):
        r = client.get("/v1/health/detail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partial_windows"]["count"] >= 1
    assert any(
        w["request_id"] == str(rid) for w in body["partial_windows"]["windows"]
    )
