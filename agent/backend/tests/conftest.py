"""Shared fixtures — real Cloud SQL + BigQuery, not mocks.

Post-reset the warehouse and discovery_window ledger were empty. Fixtures here
seed the minimum rows the diagnostic tests need, then tear them down. Seeding
uses the application DSN / BQ ADC (writes happen only in tests, never in the
agent runtime).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from google.cloud import bigquery
from psycopg.rows import dict_row

from app.clients.bq import BigQueryClient
from app.clients.gcp import GcpRunClient
from app.clients.signoz import SignozClient
from app.clients.sql import SqlClient
from app.config import get_settings
from app.diagnostics import Diagnostics
from app.tools.base import ToolContext

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")

# Agent settings require *_READONLY; fall back to the app DSNs for local/pilot.
os.environ.setdefault(
    "COLLECTOR_DSN_READONLY", os.environ.get("COLLECTOR_DSN", "")
)
os.environ.setdefault(
    "SENTINEL_MOCK_DSN_READONLY", os.environ.get("SENTINEL_MOCK_DSN", "")
)
os.environ.setdefault("GCP_PROJECT", os.environ.get("PROJECT", "clariversev1"))
os.environ.setdefault("GCP_REGION", os.environ.get("REGION", "asia-south1"))

# Stable ids used across diagnostic fixtures.
COLLECTED_ID = "IN270827PRECISION01"
MULTI_THREAD_ID = "IN26081800000000000012"  # 4 threads at source
GAP_INCIDENT_ID = "IN26081900000000066454"  # updated_on 2026-08-20 00:05:19Z
TRUNCATED_INCIDENT_ID = "IN26081900000000070737"  # 2026-08-20 00:05:38Z
APPEND_ID = "IN270827PRECISION02"

FIXTURE_REQUEST_ID = uuid.UUID("a1111111-1111-4111-8111-111111111111")
FIXTURE_TAG = "agent-diag-fixture"


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def write_collector_dsn() -> str:
    dsn = os.environ.get("COLLECTOR_DSN") or os.environ["COLLECTOR_DSN_READONLY"]
    return dsn


@pytest.fixture(scope="session")
def sql_client(settings):
    client = SqlClient(
        settings.collector_dsn_readonly, settings.sentinel_mock_dsn_readonly
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def bq_client(settings):
    client = BigQueryClient(
        project=settings.gcp_project,
        location=settings.gcp_region,
        max_bytes_billed=settings.bq_max_bytes_billed,
        raw_dataset=settings.bq_raw_dataset,
        core_dataset=settings.bq_core_dataset,
        landing_table=settings.bq_landing_table,
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def signoz_client(settings):
    client = SignozClient(
        base_url=settings.signoz_base_url, api_key=settings.signoz_api_key
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def gcp_client(settings):
    client = GcpRunClient(
        project=settings.gcp_project,
        region=settings.gcp_region,
        job_names=settings.cloud_run_jobs,
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def diagnostics(sql_client, bq_client, settings, gcp_client):
    return Diagnostics(
        sql=sql_client, bq=bq_client, settings=settings, gcp=gcp_client
    )


@pytest.fixture(scope="session")
def tool_ctx(sql_client, bq_client, signoz_client, gcp_client, settings, diagnostics):
    return ToolContext(
        sql=sql_client,
        bq=bq_client,
        signoz=signoz_client,
        gcp=gcp_client,
        settings=settings,
        diagnostics=diagnostics,
    )


def _bq_insert(rows: list[dict]) -> None:
    client = bigquery.Client(project="clariversev1", location="asia-south1")
    errors = client.insert_rows_json("clariversev1.sentinel_raw.incidents_v2", rows)
    if errors:
        raise RuntimeError(f"BQ seed failed: {errors}")


def _ensure_fixture_request(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_request (
                  request_id, source, query_spec, total_pages, status
                ) VALUES (%s, 'sentinel', '{"fixture": true}'::jsonb, 0, 'open')
                ON CONFLICT (request_id) DO NOTHING
                """,
                (str(FIXTURE_REQUEST_ID),),
            )
        conn.commit()


@pytest.fixture(scope="session")
def seeded_collected_incident(settings, bq_client, write_collector_dsn):
    """Ensure COLLECTED_ID is in incidents_current (seed if warehouse empty)."""
    fqn = (
        f"`{settings.gcp_project}.{settings.bq_core_dataset}.incidents_current`"
    )
    sql = f"""
SELECT id, COUNT(*) AS thread_rows, MAX(_ingested_at) AS collected_at,
       ARRAY_AGG(_request_id ORDER BY _ingested_at DESC LIMIT 1)[OFFSET(0)] AS request_id
FROM {fqn}
WHERE id = @id AND _ingested_at >= TIMESTAMP('2026-01-01')
GROUP BY id
"""
    params = [bigquery.ScalarQueryParameter("id", "STRING", COLLECTED_ID)]
    rows = bq_client.query(sql, params=params)
    if rows:
        return rows[0]

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _bq_insert(
        [
            {
                "id": COLLECTED_ID,
                "threads_id": "diag-test-thread-1",
                "updatedOn": "2026-08-31T09:34:50.179422Z",
                "subject": "Precision probe",
                "_request_id": "00000000-0000-4000-8000-00000000diag",
                "_page_no": 0,
                "_raw_uri": f"gs://lisn-raw-zone-clariversev1/{FIXTURE_TAG}/{COLLECTED_ID}.json",
                "_ingested_at": now,
            }
        ]
    )
    # Streaming buffer — re-query; may take a moment to appear in the view.
    rows = bq_client.query(sql, params=params)
    if not rows:
        pytest.fail(
            f"Could not seed {COLLECTED_ID} into incidents_current — "
            "warehouse empty after reset and insert did not become queryable"
        )
    return rows[0]


@pytest.fixture(scope="session")
def seeded_multi_thread_incident(settings, bq_client):
    """Seed MULTI_THREAD_ID with 3 thread rows so DISTINCT-id counting is testable."""
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": MULTI_THREAD_ID,
            "threads_id": f"diag-mt-{i}",
            "updatedOn": "2026-08-18T09:30:38Z",
            "subject": "multi-thread fixture",
            "_request_id": "00000000-0000-4000-8000-00000000mthr",
            "_page_no": 0,
            "_raw_uri": f"gs://lisn-raw-zone-clariversev1/{FIXTURE_TAG}/{MULTI_THREAD_ID}-{i}.json",
            "_ingested_at": (now + timedelta(milliseconds=i)).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        for i in range(3)
    ]
    _bq_insert(rows)
    return MULTI_THREAD_ID


@pytest.fixture(scope="session")
def seeded_append_only_incident(settings, bq_client):
    """Same incident collected twice (two _request_id) — copies_ratio > 1 by design."""
    now = datetime.now(timezone.utc)
    base = {
        "id": APPEND_ID,
        "threads_id": "diag-append-thread",
        "updatedOn": "2026-08-31T09:34:50.179422Z",
        "subject": "append-only fixture",
        "_page_no": 0,
        "_raw_uri": f"gs://lisn-raw-zone-clariversev1/{FIXTURE_TAG}/{APPEND_ID}.json",
    }
    _bq_insert(
        [
            {
                **base,
                "_request_id": "00000000-0000-4000-8000-00000000ap01",
                "_ingested_at": now.isoformat().replace("+00:00", "Z"),
            },
            {
                **base,
                "_request_id": "00000000-0000-4000-8000-00000000ap02",
                "_ingested_at": (now + timedelta(seconds=1)).isoformat().replace(
                    "+00:00", "Z"
                ),
            },
        ]
    )
    return APPEND_ID


@pytest.fixture
def known_gap_windows(write_collector_dsn, sql_client):
    """Two complete windows with a hole containing GAP_INCIDENT_ID's updated_on.

    discovery_window was empty after the reset — without this fixture there is
    no boundary gap row for diagnose_incident to return as gap_from/gap_to.
    """
    _ensure_fixture_request(write_collector_dsn)
    # Incident updated_on ≈ 2026-08-20 00:05:19 — put the hole around it.
    before_id = str(uuid.uuid4())
    after_id = str(uuid.uuid4())
    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_window (
                  window_id, source, request_id, window_field,
                  window_from, window_to, id_count, status, allow_gap
                ) VALUES
                  (%s, 'sentinel', %s, 'updated_on',
                   '2026-08-19 00:00:00+00', '2026-08-20 00:00:00+00',
                   100, 'complete', false),
                  (%s, 'sentinel', %s, 'updated_on',
                   '2026-08-20 01:00:00+00', '2026-08-21 00:00:00+00',
                   100, 'complete', false)
                """,
                (
                    before_id,
                    str(FIXTURE_REQUEST_ID),
                    after_id,
                    str(FIXTURE_REQUEST_ID),
                ),
            )
        conn.commit()
    yield {
        "gap_from": datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc),
        "gap_to": datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc),
        "window_ids": [before_id, after_id],
    }
    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM discovery_window WHERE window_id = ANY(%s::uuid[])",
                ([before_id, after_id],),
            )
        conn.commit()


@pytest.fixture
def truncated_window(write_collector_dsn):
    """A partial window covering TRUNCATED_INCIDENT_ID's updated_on.

    This is the case that previously looked like full coverage: two seven-day
    windows recorded exactly 10,000 ids and status 'complete', so boundary-based
    gap detection saw no gap while most of the range was never discovered.
    status='partial' + id_count at the cap is how we now surface that.
    """
    _ensure_fixture_request(write_collector_dsn)
    window_id = str(uuid.uuid4())
    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO discovery_window (
                  window_id, source, request_id, window_field,
                  window_from, window_to, id_count, status, allow_gap
                ) VALUES (
                  %s, 'sentinel', %s, 'updated_on',
                  '2026-08-20 00:00:00+00', '2026-08-27 00:00:00+00',
                  10000, 'partial', false
                )
                """,
                (window_id, str(FIXTURE_REQUEST_ID)),
            )
        conn.commit()
    yield {
        "window_id": window_id,
        "window_from": datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc),
        "window_to": datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc),
        "id_count": 10000,
    }
    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM discovery_window WHERE window_id = %s::uuid",
                (window_id,),
            )
        conn.commit()


@pytest.fixture
def failed_job_with_dsn(write_collector_dsn):
    """A dead collector_job whose last_error embeds a DSN password."""
    _ensure_fixture_request(write_collector_dsn)
    job_id = str(uuid.uuid4())
    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_job (
                  job_id, request_id, source, page_no, page_payload,
                  status, last_error
                ) VALUES (
                  %s, %s, 'sentinel', 99,
                  '{"incident_ids":["FIXTURE-REDACT"]}'::jsonb,
                  'dead',
                  'connect failed postgresql://postgres:s3cretPASS@10.0.0.1:5432/collector'
                )
                """,
                (job_id, str(FIXTURE_REQUEST_ID)),
            )
        conn.commit()
    yield job_id
    with psycopg.connect(write_collector_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM collector_job WHERE job_id = %s::uuid", (job_id,)
            )
        conn.commit()
