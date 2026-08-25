"""Mandatory post-selftest smoke: prove one page reaches fake sink."""

from __future__ import annotations

from pathlib import Path

from collector.tasks import fetch_page
from tests.audit.fakes import FakeBQ, SinkPatch
from tests.audit.helpers import (
    SMOKE_PROOF_FILE,
    fetch_incident_ids,
    reset_state,
    resolved_table_id,
    seed_jobs_for_incident_ids,
    write_evidence,
)


def test_pipeline_one_page_nonzero_rows(require_fakes_selftest: None) -> None:
    test_id = "SMOKE-PIPELINE-01"
    reset_state()
    incident_ids = fetch_incident_ids(1)
    request_id, job_ids = seed_jobs_for_incident_ids(incident_ids)
    table_id = resolved_table_id()

    with SinkPatch():
        fetch_page(job_id=job_ids[0])
        rows = FakeBQ.Client.fetch_rows(table_id)

    total_rows = len(rows)
    distinct_ids = len({row.get("id") for row in rows if row.get("id") is not None})
    write_evidence(
        test_id,
        [
            f"request_id={request_id}",
            f"job_id={job_ids[0]}",
            f"table_id={table_id}",
            f"total_rows={total_rows}",
            f"distinct_ids={distinct_ids}",
        ],
    )

    assert total_rows > 0, (
        "FakeBQ row count is zero after one-page run; "
        "stop: downstream sink assertions are meaningless"
    )
    SMOKE_PROOF_FILE.write_text(
        f"ok request_id={request_id} rows={total_rows}\n", encoding="utf-8"
    )
