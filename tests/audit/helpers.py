"""Shared helpers for acceptance-audit pytest files."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

from collector.db import connect
from collector.sources.sentinel import SentinelCollector
from tests.audit.fakes import FakeBQ

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = REPO_ROOT / "tests" / "audit" / "evidence"
SMOKE_PROOF_FILE = EVIDENCE_DIR / "_pipeline_smoke_ok.txt"
SELFTEST_PROOF_FILE = EVIDENCE_DIR / "_fakes_selftest_ok.txt"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_path(test_id: str) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    return EVIDENCE_DIR / f"{test_id}.log"


def write_evidence(test_id: str, lines: list[str]) -> None:
    payload = "\n".join(lines).rstrip() + "\n"
    evidence_path(test_id).write_text(payload, encoding="utf-8")


def append_evidence(test_id: str, lines: list[str]) -> None:
    payload = "\n".join(lines).rstrip() + "\n"
    with evidence_path(test_id).open("a", encoding="utf-8") as handle:
        handle.write(payload)


def blocked(test_id: str, reason: str) -> None:
    append_evidence(test_id, [f"BLOCKED: {reason}"])
    pytest.skip(f"BLOCKED: {reason}")


@contextmanager
def timed_window(test_id: str, floor_seconds: int) -> Iterator[dict[str, float]]:
    start = datetime.now(timezone.utc)
    start_ts = start.timestamp()
    write_evidence(
        test_id,
        [
            f"TEST {test_id}",
            f"start={start.isoformat()}",
            f"floor_seconds={floor_seconds}",
        ],
    )
    bag = {"elapsed_seconds": 0.0}
    try:
        yield bag
    finally:
        end = datetime.now(timezone.utc)
        end_ts = end.timestamp()
        elapsed = end_ts - start_ts
        bag["elapsed_seconds"] = elapsed
        append_evidence(
            test_id,
            [
                f"end={end.isoformat()}",
                f"elapsed_seconds={elapsed:.3f}",
            ],
        )
        try:
            assert elapsed >= floor_seconds, (
                f"elapsed {elapsed:.3f}s below floor {floor_seconds}s"
            )
        except AssertionError as exc:
            blocked(test_id, str(exc))


def reset_state() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE raw_manifest, collector_job, collector_request
                RESTART IDENTITY CASCADE
                """
            )
            cur.execute(
                """
                TRUNCATE TABLE procrastinate_periodic_defers, procrastinate_events,
                               procrastinate_jobs, procrastinate_workers
                RESTART IDENTITY CASCADE
                """
            )
            cur.execute(
                """
                DO $$
                BEGIN
                  IF to_regclass('public.collector_control') IS NOT NULL THEN
                    TRUNCATE TABLE collector_control RESTART IDENTITY CASCADE;
                  END IF;
                END$$;
                """
            )
        conn.commit()
    FakeBQ.Client.truncate_rows()
    gcs_root = REPO_ROOT / "tests" / "audit" / "_gcs"
    if gcs_root.exists():
        shutil.rmtree(gcs_root)


def resolved_table_id(two_part: str = "sentinel_raw.incidents") -> str:
    project = os.environ.get("PROJECT", "").strip()
    if two_part.count(".") == 1 and project:
        return f"{project}.{two_part}"
    return two_part


def fetch_incident_ids(limit: int) -> list[str]:
    dsn = os.environ["SENTINEL_MOCK_DSN"]
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT %s", (limit,))
            return [row[0] for row in cur.fetchall()]


def seed_jobs_for_incident_ids(incident_ids: list[str]) -> tuple[str, list[str]]:
    request_id = str(uuid.uuid4())
    pages = SentinelCollector().plan({"incident_ids": incident_ids})
    job_ids: list[str] = []
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_request (request_id, source, query_spec, total_pages, status)
                VALUES (%s::uuid, 'sentinel', %s::jsonb, %s, 'open')
                """,
                (request_id, json.dumps({"incident_ids": incident_ids}), len(pages)),
            )
            for page in pages:
                job_id = str(uuid.uuid4())
                job_ids.append(job_id)
                cur.execute(
                    """
                    INSERT INTO collector_job (job_id, request_id, source, page_no, page_payload, status)
                    VALUES (%s::uuid, %s::uuid, 'sentinel', %s, %s::jsonb, 'pending')
                    """,
                    (job_id, request_id, page.page_no, json.dumps(page.payload)),
                )
        conn.commit()
    return request_id, job_ids
