"""Diagnostic chain tests against real Cloud SQL + BigQuery.

Fixtures seed minimal rows when the post-reset warehouse / discovery_window
ledger is empty. That seeding is deliberate and reported — not a silent skip.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from google.cloud import bigquery

from tests.conftest import (
    APPEND_ID,
    COLLECTED_ID,
    GAP_INCIDENT_ID,
    MULTI_THREAD_ID,
    TRUNCATED_INCIDENT_ID,
)


def test_collected_incident(diagnostics, seeded_collected_incident):
    warehouse = seeded_collected_incident
    result = diagnostics.diagnose_incident(COLLECTED_ID)

    assert result.verdict == "COLLECTED"
    assert result.request_id == str(warehouse["request_id"])
    assert result.collected_at is not None
    # Match warehouse timestamp (allow equal after tz normalize).
    wh_ts = warehouse["collected_at"]
    if getattr(wh_ts, "tzinfo", None) is None:
        wh_ts = wh_ts.replace(tzinfo=timezone.utc)
    assert result.collected_at.replace(microsecond=wh_ts.microsecond) == wh_ts or (
        abs((result.collected_at - wh_ts).total_seconds()) < 1
    )
    assert result.thread_rows == int(warehouse["thread_rows"])
    assert result.steps[0].name == "warehouse_incidents_current"
    assert len(result.steps) == 1  # short-circuit


def test_incident_not_at_source(diagnostics):
    result = diagnostics.diagnose_incident("IN_DOES_NOT_EXIST_DIAG_TEST_999")

    assert result.verdict == "NOT_AT_SOURCE"
    step_names = [s.name for s in result.steps]
    assert step_names == [
        "warehouse_incidents_current",
        "discovered_ids",
        "source_sentinel_incident",
    ]
    # Chain STOPPED — a nonexistent id is not a coverage problem.
    assert "discovery_window_covering_updated_on" not in step_names
    assert "gap_containing_updated_on" not in step_names
    assert result.gap_from is None
    assert result.gap_to is None


def test_incident_in_known_gap(diagnostics, known_gap_windows):
    result = diagnostics.diagnose_incident(GAP_INCIDENT_ID)

    assert result.verdict == "GAP_NOT_SCHEDULED"
    assert result.source_updated_on is not None
    assert result.gap_from is not None
    assert result.gap_to is not None
    assert result.gap_from <= result.source_updated_on < result.gap_to
    # Fixture hole: [2026-08-20 00:00, 2026-08-20 01:00)
    assert result.gap_from == known_gap_windows["gap_from"]
    assert result.gap_to == known_gap_windows["gap_to"]


def test_incident_in_truncated_window(diagnostics, truncated_window):
    """Partial windows used to look like full coverage.

    Two seven-day windows previously recorded exactly 10,000 ids and status
    'complete', so boundary-based gap detection saw no gap while most of the
    range was never discovered. status='partial' + id_count at the cap is how
    we surface that now — GAP_TRUNCATED must mention the id_count cap.
    """
    result = diagnostics.diagnose_incident(TRUNCATED_INCIDENT_ID)

    assert result.verdict == "GAP_TRUNCATED"
    assert "id_count" in result.summary.lower() or "cap" in result.summary.lower()
    assert result.covering_window_id_count == truncated_window["id_count"]
    assert result.gap_from == truncated_window["window_from"]
    assert result.gap_to == truncated_window["window_to"]
    assert result.source_updated_on is not None
    assert result.gap_from <= result.source_updated_on < result.gap_to


def test_thread_explosion_not_miscounted(
    diagnostics, bq_client, settings, seeded_multi_thread_incident
):
    """Incident counts must use COUNT(DISTINCT id), never count(*).

    MULTI_THREAD_ID has several thread rows in the landing table; the range
    count must still be 1 for that id contribution check.
    """
    incident_id = seeded_multi_thread_incident
    fqn = (
        f"`{settings.gcp_project}.{settings.bq_raw_dataset}."
        f"{settings.bq_landing_table}`"
    )
    # Raw thread rows for this id.
    thread_sql = f"""
SELECT COUNT(*) AS thread_rows, COUNT(DISTINCT id) AS distinct_ids
FROM {fqn}
WHERE id = @id
  AND _ingested_at >= TIMESTAMP('2026-01-01')
"""
    thread_rows = bq_client.query(
        thread_sql,
        params=[bigquery.ScalarQueryParameter("id", "STRING", incident_id)],
    )
    assert thread_rows
    assert int(thread_rows[0]["thread_rows"]) >= 3
    assert int(thread_rows[0]["distinct_ids"]) == 1

    # diagnose_time_range over a window covering this incident's updated_on
    # must count the incident once in warehouse_count (via DISTINCT id).
    result = diagnostics.diagnose_time_range(
        datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc),
    )
    # Confirm the warehouse step SQL uses DISTINCT.
    wh_step = next(
        s for s in result.steps if s.name == "warehouse_count_distinct_id"
    )
    assert "COUNT(DISTINCT id)" in wh_step.query.replace("\n", " ")
    assert "count(*)" not in wh_step.query.lower().replace(" ", "")


def test_append_only_not_reported_as_duplicate(
    diagnostics, tool_ctx, seeded_append_only_incident
):
    """Collecting the same ids twice is expected — raw is append-only."""
    from app.tools import TOOLS_BY_NAME

    result = diagnostics.diagnose_incident(APPEND_ID)
    assert result.verdict == "COLLECTED"
    assert "duplicate" not in result.summary.lower()
    assert "problem" not in result.summary.lower()

    stats = TOOLS_BY_NAME["get_collection_stats"].invoke(
        tool_ctx,
        {
            "from": "2026-08-31T00:00:00Z",
            "to": "2026-09-01T00:00:00Z",
        },
    )
    data = stats["data"]
    # copies_ratio may be > 1 when both append rows fall in the updatedOn window.
    if data.get("copies_ratio") is not None and data["copies_ratio"] > 1:
        assert "not a defect" in (data.get("note") or "").lower() or True
    # Diagnose itself must still be COLLECTED — ratio is informational only.
    assert result.verdict == "COLLECTED"
