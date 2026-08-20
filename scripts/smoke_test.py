#!/usr/bin/env python3
"""Sprint 1 Python reachability checks for Postgres, GCS, and BigQuery.

This Python block exists to catch Application Default Credentials problems NOW
rather than in Sprint 3 while debugging the collector task body.
If it fails, the fix is `gcloud auth application-default login`.
"""

from __future__ import annotations

import os
import sys
import uuid

import psycopg
from google.cloud import bigquery, storage


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def check_postgres() -> None:
    dsn = os.environ.get("COLLECTOR_DSN")
    if not dsn:
        fail("COLLECTOR_DSN is not set")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            if row is None or row[0] != 1:
                fail(f"unexpected SELECT 1 result: {row!r}")
    print("postgres OK")


def check_gcs() -> None:
    bucket_name = os.environ.get("RAW_BUCKET")
    if not bucket_name:
        fail("RAW_BUCKET is not set")
    demo_source = os.environ.get("DEMO_SOURCE", "sentinel")
    object_name = f"raw/source={demo_source}/_smoke_py.txt"
    payload = f"smoke-py-{uuid.uuid4()}".encode("utf-8")

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_string(payload, content_type="text/plain")
    downloaded = blob.download_as_bytes()
    if downloaded != payload:
        fail(f"GCS content mismatch: {downloaded!r} != {payload!r}")
    blob.delete()
    print("gcs OK")


def check_bigquery() -> None:
    client = bigquery.Client()
    rows = list(client.query("SELECT 1 AS ok").result())
    if not rows or rows[0]["ok"] != 1:
        fail(f"unexpected BigQuery result: {rows!r}")
    print("bigquery OK")


def main() -> None:
    check_postgres()
    check_gcs()
    check_bigquery()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — plain script, any failure exits 1
        fail(str(exc))
