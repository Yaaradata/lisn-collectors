"""Discovery gap detection — acceptance scenario (skip third hour)."""

from __future__ import annotations

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
from collector.discovery_gaps import gap_summary, list_gaps
from tests._sql_apply import apply_sql_file

SOURCE = "sentinel_discovery"
FIELD = "updated_on"
T0 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


def _hour(i: int) -> tuple[datetime, datetime]:
    start = T0 + timedelta(hours=i)
    return start, start + timedelta(hours=1)


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


def _insert_window(
    cur,
    *,
    request_id: uuid.UUID,
    hour: int,
    status: str = "complete",
    allow_gap: bool = False,
    gap_reason: str | None = None,
) -> None:
    start, end = _hour(hour)
    cur.execute(
        """
        INSERT INTO collector_request (
          request_id, source, query_spec, total_pages, status
        ) VALUES (%s, %s, '{}'::jsonb, 1, 'open')
        """,
        (request_id, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO discovery_window (
          source, window_field, window_from, window_to,
          request_id, status, allow_gap, gap_reason, completed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        """,
        (
            SOURCE,
            FIELD,
            start,
            end,
            request_id,
            status,
            allow_gap,
            gap_reason,
        ),
    )


@pytest.fixture
def db():
    try:
        from google.cloud.sql.connector import Connector
    except ImportError:
        pytest.skip("cloud-sql-python-connector not installed")

    _load_dotenv()
    conn_name = os.environ.get("CONN")
    dbpw = os.environ.get("DBPW")
    if not conn_name or not dbpw:
        pytest.skip("CONN/DBPW not set")

    connector = Connector()
    conn = connector.connect(
        conn_name, "pg8000", user="postgres", password=dbpw, db="collector"
    )
    cur = conn.cursor()
    sql_path = os.path.join(
        os.path.dirname(__file__), "..", "sql", "010_discovery_window.sql"
    )
    apply_sql_file(cur, sql_path)
    conn.commit()

    # Isolate from other discovery_window rows on this shared Cloud SQL DB.
    cur.execute(
        "DELETE FROM discovery_window WHERE source = %s",
        (SOURCE,),
    )
    conn.commit()

    yield conn, connector, _ConnProxy(conn)

    cur = conn.cursor()
    cur.execute(
        "DELETE FROM discovery_window WHERE source = %s",
        (SOURCE,),
    )
    cur.execute(
        """
        DELETE FROM collector_request r
        WHERE r.source = %s
          AND NOT EXISTS (
            SELECT 1 FROM collector_job j WHERE j.request_id = r.request_id
          )
          AND NOT EXISTS (
            SELECT 1 FROM discovery_window d WHERE d.request_id = r.request_id
          )
        """,
        (SOURCE,),
    )
    conn.commit()
    cur.close()
    conn.close()
    connector.close()


def test_five_windows_third_skipped_one_gap(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    ids: dict[int, uuid.UUID] = {}
    for hour in (0, 1, 3, 4, 5):
        rid = uuid.uuid4()
        ids[hour] = rid
        _insert_window(cur, request_id=rid, hour=hour, status="complete")
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        gaps = list_gaps(source=SOURCE)
    match = [
        g
        for g in gaps
        if "02:00:00" in g["gap_from"] and "03:00:00" in g["gap_to"]
    ]
    assert len(match) == 1, gaps
    g = match[0]
    assert g["before_request_id"] == str(ids[1])
    assert g["after_request_id"] == str(ids[3])
    assert g["gap_duration_seconds"] == 3600.0


def test_health_detail_discovery_gaps_count(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    for hour in (0, 1, 3):
        _insert_window(cur, request_id=uuid.uuid4(), hour=hour, status="complete")
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        summary = gap_summary()
    assert summary["count"] >= 1
    assert summary["oldest"] is not None
    assert "02:00:00" in summary["oldest"]["gap_from"]


def test_submit_fourth_window_without_allow_gap_409(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    for hour in (0, 1):
        _insert_window(cur, request_id=uuid.uuid4(), hour=hour, status="complete")
    conn.commit()

    client = TestClient(api)
    start, end = _hour(3)
    with (
        patch("collector.discovery_gaps.connect", return_value=proxy),
        patch("collector.api.connect", return_value=proxy),
    ):
        r = client.post(
            "/v1/collect",
            json={
                "source": SOURCE,
                "query_spec": {
                    "updated_from": start.isoformat().replace("+00:00", "Z"),
                    "updated_to": end.isoformat().replace("+00:00", "Z"),
                },
            },
        )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["gap_duration_seconds"] == 3600.0
    assert "02:00:00" in detail["gap_from"]
    assert "03:00:00" in detail["gap_to"]


def test_submit_with_allow_gap_succeeds_and_records(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    for hour in (0, 1):
        _insert_window(cur, request_id=uuid.uuid4(), hour=hour, status="complete")
    conn.commit()

    start, end = _hour(3)
    defer = MagicMock()
    configured = MagicMock()
    configured.defer = defer
    fetch = MagicMock()
    fetch.configure.return_value = configured
    app_cm = MagicMock()
    app_cm.__enter__.return_value = None
    app_cm.__exit__.return_value = False

    client = TestClient(api)
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
                },
                "allow_gap": True,
                "gap_reason": "acceptance: intentionally skipped 02:00-03:00",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("allow_gap") is True
    rid = body["request_id"]

    cur.execute(
        """
        SELECT allow_gap, gap_reason, status
        FROM discovery_window
        WHERE request_id = %s::uuid
        """,
        (rid,),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] is True
    assert "skipped" in row[1]
    assert row[2] == "running"


def test_contiguous_windows_zero_gaps(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    for hour in (0, 1, 2, 3, 4, 5):
        _insert_window(cur, request_id=uuid.uuid4(), hour=hour, status="complete")
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        gaps = list_gaps(source=SOURCE)
    # Only assert contiguity inside the six hours we just wrote — other
    # ledger rows on this shared DB may sit outside the span.
    span = [
        g
        for g in gaps
        if g["gap_from"] >= T0.isoformat()
        and g["gap_to"] <= (T0 + timedelta(hours=6)).isoformat()
    ]
    assert span == []

def test_failed_window_shows_as_gap(db) -> None:
    conn, _, proxy = db
    cur = conn.cursor()
    _insert_window(cur, request_id=uuid.uuid4(), hour=0, status="complete")
    _insert_window(cur, request_id=uuid.uuid4(), hour=1, status="failed")
    _insert_window(cur, request_id=uuid.uuid4(), hour=2, status="complete")
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        gaps = list_gaps(source=SOURCE)
    match = [
        g
        for g in gaps
        if "01:00:00" in g["gap_from"] and "02:00:00" in g["gap_to"]
    ]
    assert len(match) == 1, gaps
