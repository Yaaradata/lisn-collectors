"""Section B tests (independently invocable)."""

from __future__ import annotations

import os
import subprocess

import httpx

from collector.contract import Record, SourceCollector
from collector.load import append_records
from collector.sources.sentinel import SentinelCollector
from tests.audit.helpers import write_evidence


def test_b01_protocol_satisfaction() -> None:
    test_id = "B-01"
    sentinel = SentinelCollector()
    required = [
        "name",
        "batch_cap",
        "min_interval_s",
        "lease_seconds",
        "max_attempts",
        "bq_table",
        "plan",
        "fetch",
        "parse",
    ]
    lines = [f"isinstance={isinstance(sentinel, SourceCollector)}"]
    for attr in required:
        lines.append(f"{attr}_present={hasattr(sentinel, attr)}")
    write_evidence(test_id, lines)
    assert isinstance(sentinel, SourceCollector)
    assert all(hasattr(sentinel, attr) for attr in required)


def test_b02_declared_fields_are_behavioral() -> None:
    test_id = "B-02"
    proc = subprocess.run(
        "rg -n 'max_attempts|@app.task\\(|RetryStrategy\\(' collector/tasks.py collector/sources/sentinel.py",
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        cwd="/workspace",
    )
    write_evidence(
        test_id,
        [
            f"rg_rc={proc.returncode}",
            proc.stdout + proc.stderr,
        ],
    )
    text = proc.stdout
    assert "max_attempts = 3" in text, "expected max_attempts declaration in source"
    assert "RetryStrategy(max_attempts=3" not in text, (
        "max_attempts hardcoded in task decorator rather than source behavior"
    )


def test_b03_cost_of_adding_collector_two() -> None:
    test_id = "B-03"
    proc = subprocess.run(
        "rg -n 'REGISTRY|@app.task\\(|queue=\"sentinel\"|get\\(source\\)' collector",
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        cwd="/workspace",
    )
    out = proc.stdout + proc.stderr
    write_evidence(test_id, [f"rg_rc={proc.returncode}", out])
    assert 'queue="sentinel"' not in out, (
        "collector #2 not one-module cost because queue binding is hardcoded to sentinel"
    )


def test_b04_table_qualification_rules() -> None:
    test_id = "B-04"
    import collector.load as load_module

    class CapturingClient:
        seen: list[tuple[str | None, str]] = []

        def __init__(self, project: str | None = None):
            self.project = project

        def insert_rows_json(self, table_id: str, rows: list[dict]) -> list[dict]:
            del rows
            self.seen.append((self.project, table_id))
            return []

    original = load_module.bigquery.Client
    try:
        load_module.bigquery.Client = CapturingClient
        os.environ.pop("PROJECT", None)
        append_records("sentinel_raw.incidents", [Record(key="k", data={"id": "x"})], "r1", 0, "gs://x")
        os.environ["PROJECT"] = "proj-x"
        append_records("sentinel_raw.incidents", [Record(key="k", data={"id": "x"})], "r2", 0, "gs://x")
        append_records("proj-y.sentinel_raw.incidents", [Record(key="k", data={"id": "x"})], "r3", 0, "gs://x")
    finally:
        load_module.bigquery.Client = original
    write_evidence(
        test_id,
        [f"seen_{idx}={seen}" for idx, seen in enumerate(CapturingClient.seen, start=1)],
    )
    assert CapturingClient.seen[0][1].count(".") == 2, (
        "unqualified two-part table with no PROJECT should raise, not write unqualified target"
    )


def test_b05_unknown_source_rejection() -> None:
    test_id = "B-05"
    response = httpx.post(
        "http://127.0.0.1:8080/v1/collect",
        json={"source": "ekart", "query_spec": {"incident_ids": ["IN1"]}},
        timeout=30,
    )
    write_evidence(
        test_id,
        [
            f"status={response.status_code}",
            f"body={response.text}",
        ],
    )
    assert response.status_code == 400
    assert "known sources" in response.text
