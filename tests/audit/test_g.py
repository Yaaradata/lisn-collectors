"""Section G tests (data quality against LiSN contract)."""

from __future__ import annotations

import re

from collector.contract import Record
from collector.load import append_records
from tests.audit.fakes import FakeBQ, FakeGCS, SinkPatch
from tests.audit.helpers import (
    fetch_incident_ids,
    reset_state,
    resolved_table_id,
    run_jobs_with_fakes,
    seed_jobs_for_incident_ids,
    write_evidence,
)


def test_g01_field_completeness_both_ways() -> None:
    test_id = "G-01"
    schema = open("/workspace/sql/003_bigquery.sql", encoding="utf-8").read()
    columns = set(
        re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+[A-Z0-9]+", schema, flags=re.MULTILINE)
    )
    mock_text = open("/workspace/mock/sentinel_api.py", encoding="utf-8").read()
    source_fields = set(re.findall(r'"([^"]+)":\s*row\["[^"]+"\]', mock_text))
    flattened = {f.replace(".", "_") for f in source_fields}
    metadata = {"_request_id", "_page_no", "_raw_uri", "_ingested_at"}
    missing_from_schema = sorted(flattened - columns)
    unused_in_source = sorted(columns - (flattened | metadata))
    write_evidence(
        test_id,
        [
            f"flattened_count={len(flattened)}",
            f"schema_columns_count={len(columns)}",
            "missing_from_schema=" + ", ".join(missing_from_schema),
            "unused_in_source=" + ", ".join(unused_in_source),
        ],
    )
    assert not missing_from_schema


def test_g02_schema_drift_unknown_field(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "G-02"
    reset_state()
    with SinkPatch():
        try:
            append_records(
                "sentinel_raw.incidents",
                [Record(key="k", data={"id": "I-DRIFT", "slaBreachReason": "late"})],
                "req",
                0,
                "gs://audit/1",
            )
            raised = "no"
            message = ""
        except RuntimeError as exc:
            raised = "yes"
            message = str(exc)
    write_evidence(test_id, [f"raised={raised}", f"message={message}"])
    assert raised == "yes"
    assert "slaBreachReason" in message


def test_g03_numeric_fidelity_at_incident_grain(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "G-03"
    reset_state()
    table_id = resolved_table_id()
    original = "9007199254740993"
    with SinkPatch():
        append_records(
            "sentinel_raw.incidents",
            [Record(key="k", data={"id": "I-NUM", "orderItemId": original})],
            "req",
            0,
            "gs://audit/1",
        )
    stored = FakeBQ.Client.fetch_rows(table_id)[0]["orderItemId"]
    write_evidence(
        test_id,
        [
            f"original={original}",
            f"stored={stored}",
            "basis=sql/003_bigquery.sql declares orderItemId/orderItemUnitId/threads_communicationId as FLOAT64 (lines 14-16 in current file).",
            "basis=fake_coercion corroborates expected >2^53 precision loss; requires real BigQuery confirmation on clariversev1.",
        ],
    )
    assert str(stored) == original


def test_g04_timezone_fidelity(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "G-04"
    sentinel = open("/workspace/collector/sources/sentinel.py", encoding="utf-8").read()
    load = open("/workspace/collector/load.py", encoding="utf-8").read()
    api = open("/workspace/collector/api.py", encoding="utf-8").read()
    markers = []
    for source_name, text in [("sentinel.py", sentinel), ("load.py", load), ("api.py", api)]:
        for token in ["fromisoformat(", "astimezone(", "timezone(", "dateutil", "pytz", "zoneinfo"]:
            if token in text:
                markers.append(f"{source_name}:{token}")
    write_evidence(
        test_id,
        [
            "basis=inspection_of_collector_code_for_timezone_normalisation_path_between_parse_and_load",
            "markers_found=" + ", ".join(markers),
            "finding=no explicit normalisation for updatedOn/resolutionDeadline/threads_createdAt in collector/ path",
            "note=this finding is independent of sink behavior",
        ],
    )
    assert markers, "no timezone normalisation exists in collector/ path"


def test_g05_provenance_columns(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "G-05"
    reset_state()
    table_id = resolved_table_id()
    ids = fetch_incident_ids(50)
    request_id, job_ids = seed_jobs_for_incident_ids(ids)
    run_jobs_with_fakes(job_ids)
    rows = FakeBQ.Client.fetch_rows(table_id)
    non_null = 0
    verified = 0
    for row in rows:
        if row.get("_request_id") and row.get("_page_no") is not None and row.get("_raw_uri"):
            non_null += 1
            object_name = row["_raw_uri"].split("/", 3)[-1]
            if object_name in FakeGCS.list_objects("audit-bucket"):
                verified += 1
    write_evidence(
        test_id,
        [
            f"request_id={request_id}",
            f"rows_total={len(rows)}",
            f"rows_non_null_provenance={non_null}",
            f"rows_existing_raw_uri={verified}",
        ],
    )
    assert non_null == len(rows)
    assert verified == len(rows)
