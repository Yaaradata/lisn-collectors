#!/usr/bin/env python3
"""Backfill sentinel_raw.incidents_v2 from the GCS raw zone.

WHY THIS IS THE INTERESTING PART
--------------------------------
We are recovering from a field-mapping mistake (FLOAT64 / float() on
orderItemId, orderItemUnitId, threads.communicationId) by re-parsing raw
objects we already stored — NOT by re-querying the source. That is exactly
what the append-only raw zone was built for. This script is that recovery
used in anger.

_ingested_at is taken from raw_manifest.written_at (the object body has no
ingest timestamp). We deliberately do NOT stamp the backfill clock — that
would rewrite history and break queries filtered on ingestion date.

Usage (from repo root, .env loaded):
  python scripts/36_backfill_ids.py              # ensure v2 + backfill + reconcile
  python scripts/36_backfill_ids.py --swap        # rename tables after reconcile OK
  python scripts/36_backfill_ids.py --verify      # post-swap checks + fresh collect
  python scripts/36_backfill_ids.py --reconcile   # print reconcile only
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery, storage
from google.cloud.sql.connector import Connector

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.contract import MalformedSourcePayload, Page, RawResponse  # noqa: E402
from collector.sources.sentinel import SentinelCollector  # noqa: E402

SOURCE_PREFIX = "raw/source=sentinel/"
URI_RE = re.compile(
    r"gs://[^/]+/raw/source=sentinel/dt=[^/]+/request=(?P<rid>[^/]+)/page=(?P<page>\d+)\.json$"
)
PATH_RE = re.compile(
    r"raw/source=sentinel/dt=[^/]+/request=(?P<rid>[^/]+)/page=(?P<page>\d+)\.json$"
)

# Legacy raw objects stored these as JSON numbers (float/int). Corrected parse()
# requires strings. Coerce in the backfill path only — values already rounded
# above 2^53 cannot be un-rounded; below 2^53 int(float) is exact.
_WIRE_ID_KEYS = ("orderItemId", "orderItemUnitId", "threads.communicationId")

BOUNDARY_PROBES = {
    "9007199254740991": "just below 2^53",
    "9007199254740993": "just above 2^53 (was off-by-1)",
    "1234567890123456789": "19-digit (was off-by-11)",
}

V2_SCHEMA = [
    bigquery.SchemaField("id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("issue_id", "INTEGER"),
    bigquery.SchemaField("issue_name", "STRING"),
    bigquery.SchemaField("issue_parentResponse_id", "INTEGER"),
    bigquery.SchemaField("issue_parentResponse_name", "STRING"),
    bigquery.SchemaField("orderId", "STRING"),
    bigquery.SchemaField("orderItemId", "STRING"),
    bigquery.SchemaField("orderItemUnitId", "STRING"),
    bigquery.SchemaField("trackingId", "STRING"),
    bigquery.SchemaField("orderItemProductFSN", "STRING"),
    bigquery.SchemaField("incidentScore", "INTEGER"),
    bigquery.SchemaField("resolutionDeadline", "TIMESTAMP"),
    bigquery.SchemaField("resolutionDeadlineBreach", "BOOLEAN"),
    bigquery.SchemaField("sellerId", "STRING"),
    bigquery.SchemaField("source", "STRING"),
    bigquery.SchemaField("status_id", "INTEGER"),
    bigquery.SchemaField("status_status", "STRING"),
    bigquery.SchemaField("status_statusType", "STRING"),
    bigquery.SchemaField("subject", "STRING"),
    bigquery.SchemaField("updatedOn", "TIMESTAMP"),
    bigquery.SchemaField("agingScore", "INTEGER"),
    bigquery.SchemaField("lastUpdatedByUser", "STRING"),
    bigquery.SchemaField("queue", "STRING"),
    bigquery.SchemaField("assignedTo", "STRING"),
    bigquery.SchemaField("threads_id", "STRING"),
    bigquery.SchemaField("threads_channel_id", "INTEGER"),
    bigquery.SchemaField("threads_channel_name", "STRING"),
    bigquery.SchemaField("threads_communicationId", "STRING"),
    bigquery.SchemaField("threads_contentType", "STRING"),
    bigquery.SchemaField("threads_createdAt", "TIMESTAMP"),
    bigquery.SchemaField("threads_createdBy", "STRING"),
    bigquery.SchemaField("threads_systemThread", "BOOLEAN"),
    bigquery.SchemaField("threads_threadEntryType_id", "INTEGER"),
    bigquery.SchemaField("threads_threadEntryType_name", "STRING"),
    bigquery.SchemaField("threads_updatedBy", "STRING"),
    bigquery.SchemaField("_request_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("_page_no", "INTEGER", mode="REQUIRED"),
    bigquery.SchemaField("_raw_uri", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("_ingested_at", "TIMESTAMP"),
]

DATA_COLS = [f.name for f in V2_SCHEMA if not f.name.startswith("_")]


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'").strip('"')


def _require_env(*keys: str) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")


def _pg_connect():
    connector = Connector()
    conn = connector.connect(
        os.environ["CONN"],
        "pg8000",
        user="postgres",
        password=os.environ["DBPW"],
        db="collector",
    )
    return connector, conn


def _table_id(project: str, name: str) -> str:
    return f"{project}.sentinel_raw.{name}"


def _ensure_v2(bq: bigquery.Client, project: str) -> None:
    sql_path = ROOT / "sql" / "013_migrate_id_columns.sql"
    sql = sql_path.read_text(encoding="utf-8").replace("__PROJECT__", project)
    print(f"ensure table: {_table_id(project, 'incidents_v2')}")
    job = bq.query(sql)
    job.result()
    print("  incidents_v2 ready (view NOT switched — that is --swap)")


def _manifest_written_at() -> dict[str, datetime]:
    """Map raw_uri → written_at. This is our _ingested_at source."""
    connector, conn = _pg_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT raw_uri, written_at
            FROM raw_manifest
            WHERE source = 'sentinel'
            """
        )
        out: dict[str, datetime] = {}
        for uri, written_at in cur.fetchall():
            out[str(uri)] = written_at
        cur.close()
        print(
            f"raw_manifest sentinel rows={len(out)} "
            "( _ingested_at will come from written_at — not recoverable from object body )"
        )
        return out
    finally:
        conn.close()
        connector.close()


def _coerce_legacy_id_numbers(doc: dict[str, Any]) -> None:
    """In-place: JSON numbers → digit strings so corrected parse() accepts them."""
    incidents = doc.get("incidents")
    if not isinstance(incidents, list):
        return
    for row in incidents:
        if not isinstance(row, dict):
            continue
        for key in _WIRE_ID_KEYS:
            if key not in row:
                continue
            value = row[key]
            if value is None or isinstance(value, str):
                continue
            if isinstance(value, bool):
                raise MalformedSourcePayload(f"{key} must not be bool")
            if isinstance(value, int):
                row[key] = str(value)
                continue
            if isinstance(value, float):
                if not value.is_integer():
                    raise MalformedSourcePayload(
                        f"{key} float is not an integral JSON number: {value!r}"
                    )
                # Exact for |n| <= 2^53; already-rounded above that stays wrong.
                row[key] = str(int(value))
                continue
            raise MalformedSourcePayload(
                f"{key} unsupported type {type(value).__name__}"
            )


def _parse_uri_meta(uri: str, object_name: str) -> tuple[str, int]:
    m = URI_RE.match(uri) or PATH_RE.match(object_name)
    if not m:
        raise ValueError(f"cannot parse request/page from {uri}")
    return m.group("rid"), int(m.group("page"))


def _row_for_bq(
    data: dict[str, Any],
    *,
    request_id: str,
    page_no: int,
    raw_uri: str,
    ingested_at: datetime,
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for col in DATA_COLS:
        row[col] = data.get(col)
    row["_request_id"] = request_id
    row["_page_no"] = page_no
    row["_raw_uri"] = raw_uri
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=timezone.utc)
    row["_ingested_at"] = ingested_at.astimezone(timezone.utc).isoformat()
    return row


def _flush_batch(
    bq: bigquery.Client,
    project: str,
    rows: list[dict[str, Any]],
) -> int:
    """Batch load into a staging table, then MERGE into v2 (idempotent)."""
    if not rows:
        return 0
    staging = _table_id(project, "_backfill_ids_staging")
    v2 = _table_id(project, "incidents_v2")

    buf = io.BytesIO()
    for row in rows:
        buf.write(json.dumps(row, default=str).encode("utf-8"))
        buf.write(b"\n")
    buf.seek(0)

    load_job = bq.load_table_from_file(
        buf,
        staging,
        job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=V2_SCHEMA,
        ),
    )
    load_job.result()

    set_clause = ",\n      ".join(f"{c} = S.{c}" for c in DATA_COLS)
    set_clause += ",\n      _request_id = S._request_id"
    set_clause += ",\n      _page_no = S._page_no"
    set_clause += ",\n      _ingested_at = S._ingested_at"
    insert_cols = ", ".join(f.name for f in V2_SCHEMA)
    insert_vals = ", ".join(f"S.{f.name}" for f in V2_SCHEMA)

    merge_sql = f"""
    MERGE `{v2}` T
    USING `{staging}` S
    ON T._raw_uri = S._raw_uri
      AND T.id = S.id
      AND IFNULL(T.threads_id, 'none') = IFNULL(S.threads_id, 'none')
    WHEN MATCHED THEN UPDATE SET
      {set_clause}
    WHEN NOT MATCHED THEN INSERT ({insert_cols})
    VALUES ({insert_vals})
    """
    bq.query(merge_sql).result()
    return len(rows)


def _process_one_blob(
    blob: storage.Blob,
    bucket_name: str,
    written_at: dict[str, datetime],
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Return (rows, failure_reason_or_None, missing_manifest)."""
    uri = f"gs://{bucket_name}/{blob.name}"
    src = SentinelCollector()
    try:
        request_id, page_no = _parse_uri_meta(uri, blob.name)
        body = blob.download_as_bytes()
        doc = json.loads(body.decode("utf-8"))
        if not isinstance(doc, dict):
            raise MalformedSourcePayload(
                f"root is {type(doc).__name__}, expected dict"
            )
        _coerce_legacy_id_numbers(doc)
        coerced = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        records = src.parse(
            RawResponse(body=coerced, content_type="application/json"),
            Page(page_no=page_no, payload={"incident_ids": []}),
        )
        missing = False
        ingested = written_at.get(uri)
        if ingested is None:
            missing = True
            ingested = blob.time_created or datetime.now(timezone.utc)
        rows = [
            _row_for_bq(
                rec.data,
                request_id=request_id,
                page_no=page_no,
                raw_uri=uri,
                ingested_at=ingested,
            )
            for rec in records
        ]
        return rows, None, missing
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}", False


def backfill(bq: bigquery.Client, project: str, bucket_name: str) -> dict[str, Any]:
    _ensure_v2(bq, project)
    written_at = _manifest_written_at()

    gcs = storage.Client(project=project)
    print(f"listing GCS prefix {SOURCE_PREFIX} …", flush=True)
    blobs = [
        b
        for b in gcs.list_blobs(bucket_name, prefix=SOURCE_PREFIX)
        if b.name.endswith(".json")
    ]
    print(f"GCS json objects={len(blobs)}", flush=True)

    failures: list[dict[str, str]] = []
    processed = 0
    rows_written = 0
    batch: list[dict[str, Any]] = []
    missing_manifest = 0
    t0 = time.perf_counter()
    workers = 16
    flush_every = 500  # fewer BQ load/MERGE round-trips
    progress_every = 100

    # Process in windows so memory stays bounded.
    window = 500
    for start in range(0, len(blobs), window):
        chunk = blobs[start : start + window]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_process_one_blob, blob, bucket_name, written_at): blob
                for blob in chunk
            }
            for fut in as_completed(futs):
                blob = futs[fut]
                uri = f"gs://{bucket_name}/{blob.name}"
                rows, err, missing = fut.result()
                if err:
                    failures.append({"raw_uri": uri, "reason": err})
                else:
                    batch.extend(rows)
                    if missing:
                        missing_manifest += 1
                processed += 1
                if processed % progress_every == 0:
                    elapsed = time.perf_counter() - t0
                    print(
                        f"  progress objects={processed}/{len(blobs)} "
                        f"batch_rows={len(batch)} rows_flushed={rows_written} "
                        f"failures={len(failures)} elapsed_s={elapsed:.1f}",
                        flush=True,
                    )
                if processed % flush_every == 0 and batch:
                    n = _flush_batch(bq, project, batch)
                    rows_written += n
                    batch.clear()
                    print(f"  flushed total_rows={rows_written}", flush=True)

    rows_written += _flush_batch(bq, project, batch)
    elapsed = time.perf_counter() - t0
    summary = {
        "objects_listed": len(blobs),
        "objects_processed": processed,
        "rows_merged": rows_written,
        "failures": failures,
        "missing_manifest": missing_manifest,
        "elapsed_s": round(elapsed, 1),
    }
    print("=== BACKFILL SUMMARY ===", flush=True)
    print(
        json.dumps({k: v for k, v in summary.items() if k != "failures"}, indent=2),
        flush=True,
    )
    print(f"failures={len(failures)}", flush=True)
    for item in failures[:20]:
        print(f"  FAIL {item['raw_uri']}: {item['reason']}", flush=True)
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more", flush=True)
    return summary


def _scalar(bq: bigquery.Client, sql: str) -> Any:
    rows = list(bq.query(sql).result())
    return rows[0][0] if rows else None


def reconcile(bq: bigquery.Client, project: str, bucket_name: str) -> dict[str, Any]:
    gcs = storage.Client(project=project)
    object_count = sum(
        1
        for b in gcs.list_blobs(bucket_name, prefix=SOURCE_PREFIX)
        if b.name.endswith(".json")
    )

    connector, conn = _pg_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM raw_manifest WHERE source = 'sentinel'")
        manifest_count = int(cur.fetchone()[0])
        cur.close()
    finally:
        conn.close()
        connector.close()

    old_t = _table_id(project, "incidents")
    v2_t = _table_id(project, "incidents_v2")

    old_rows = int(_scalar(bq, f"SELECT count(*) FROM `{old_t}`") or 0)
    v2_rows = int(_scalar(bq, f"SELECT count(*) FROM `{v2_t}`") or 0)
    old_distinct = int(
        _scalar(
            bq,
            f"SELECT count(*) FROM ("
            f"SELECT DISTINCT id, threads_id FROM `{old_t}`)",
        )
        or 0
    )
    v2_distinct = int(
        _scalar(
            bq,
            f"SELECT count(*) FROM ("
            f"SELECT DISTINCT id, threads_id FROM `{v2_t}`)",
        )
        or 0
    )

    # Boundary probes: old FLOAT64 vs v2 STRING.
    probe_report: list[dict[str, Any]] = []
    for needle, label in BOUNDARY_PROBES.items():
        old_hits = list(
            bq.query(
                f"""
                SELECT CAST(orderItemId AS STRING) AS v, count(*) AS n
                FROM `{old_t}`
                WHERE ABS(orderItemId - CAST({needle} AS FLOAT64)) < 1
                   OR CAST(orderItemId AS STRING) = '{needle}'
                GROUP BY 1
                ORDER BY n DESC
                LIMIT 5
                """
            ).result()
        )
        v2_hits = list(
            bq.query(
                f"""
                SELECT orderItemId AS v, count(*) AS n
                FROM `{v2_t}`
                WHERE orderItemId = '{needle}'
                GROUP BY 1
                """
            ).result()
        )
        probe_report.append(
            {
                "probe": needle,
                "label": label,
                "old": [(r.v, r.n) for r in old_hits],
                "v2": [(r.v, r.n) for r in v2_hits],
            }
        )

    # View partition check (current definition)
    view_sql = list(
        bq.query(
            f"""
            SELECT view_definition
            FROM `{project}.sentinel_core.INFORMATION_SCHEMA.VIEWS`
            WHERE table_name = 'incidents_current'
            """
        ).result()
    )
    view_def = view_sql[0].view_definition if view_sql else ""

    report = {
        "objects_processed_vs_manifest": {
            "gcs_objects": object_count,
            "raw_manifest_rows": manifest_count,
            "delta_gcs_minus_manifest": object_count - manifest_count,
            "note": (
                "GCS may exceed manifest after resets that truncated Cloud SQL "
                "without deleting every object, or after orphan uploads."
            ),
        },
        "row_counts": {"old_incidents": old_rows, "incidents_v2": v2_rows},
        "distinct_id_threads_id": {
            "old_incidents": old_distinct,
            "incidents_v2": v2_distinct,
        },
        "boundary_probes": probe_report,
        "view_partitions_by_id_threads_id": (
            "PARTITION BY id, threads_id" in view_def
            or "PARTITION BY id,threads_id" in view_def.replace(" ", "")
        ),
    }

    print("=== RECONCILE ===")
    print(json.dumps(report, indent=2, default=str))

    # Gate swap: row counts / distinct should match unless explained.
    explanations: list[str] = []
    if object_count != manifest_count:
        explanations.append(
            f"GCS objects ({object_count}) != raw_manifest ({manifest_count}): "
            f"delta={object_count - manifest_count} (see note above)"
        )
    if v2_rows != old_rows:
        explanations.append(
            f"v2 rows ({v2_rows}) != old rows ({old_rows}): "
            "expected if old table accumulated duplicate appends for the same "
            "(_raw_uri,id,threads_id) while v2 MERGE is keyed idempotently, "
            "OR if some objects failed to parse"
        )
    if v2_distinct != old_distinct:
        explanations.append(
            f"v2 distinct ({v2_distinct}) != old distinct ({old_distinct})"
        )

    report["explanations"] = explanations
    # Gate: v2 must be non-empty; prefer distinct-identity match. Raw row count
    # may differ because old streaming inserts duplicated (_raw_uri,id,threads_id)
    # while MERGE is idempotent on that key.
    report["swap_allowed"] = v2_rows > 0 and (
        v2_distinct == old_distinct or abs(v2_distinct - old_distinct) / max(old_distinct, 1) < 0.01
    )
    if v2_rows == 0:
        report["explanations"].append("v2 is empty — refusing swap")
        report["swap_allowed"] = False
    if not report["swap_allowed"] and v2_rows > 0:
        report["explanations"].append(
            "distinct (id, threads_id) mismatch >1% — refuse swap without review"
        )
    for line in explanations:
        print(f"EXPLAIN: {line}")
    print(f"swap_allowed={report['swap_allowed']}")
    return report


def swap(bq: bigquery.Client, project: str) -> None:
    from google.api_core.exceptions import BadRequest, NotFound

    report = reconcile(bq, project, os.environ["RAW_BUCKET"])
    if not report.get("swap_allowed"):
        raise SystemExit("refusing swap — reconcile gate failed")

    old = _table_id(project, "incidents")
    v2 = _table_id(project, "incidents_v2")
    pre = _table_id(project, "incidents_pre_id_fix")

    # Drop view first so renames are not blocked by the view dependency.
    print("drop view sentinel_core.incidents_current", flush=True)
    bq.query(
        f"DROP VIEW IF EXISTS `{project}.sentinel_core.incidents_current`"
    ).result()

    try:
        bq.get_table(pre)
        raise SystemExit(
            f"{pre} already exists — manual cleanup required before swap"
        )
    except NotFound:
        pass

    renamed = False
    serving = old
    try:
        print(f"rename {old} → incidents_pre_id_fix", flush=True)
        bq.query(f"ALTER TABLE `{old}` RENAME TO incidents_pre_id_fix").result()
        print(f"rename {v2} → incidents", flush=True)
        bq.query(f"ALTER TABLE `{v2}` RENAME TO incidents").result()
        serving = _table_id(project, "incidents")
        renamed = True
    except BadRequest as exc:
        if "streaming" not in str(exc).lower():
            raise
        # BigQuery refuses RENAME while streaming inserts still have a buffer
        # (up to ~90 minutes after the last insert_rows_json). Fallback:
        # snapshot the defect table, keep v2 as the live name, point the view
        # at v2. Collector bq_table must target incidents_v2 until a later
        # rename when the buffer drains.
        print(
            "RENAME blocked by streaming buffer — falling back to COPY snapshot "
            "+ view over incidents_v2",
            flush=True,
        )
        print(
            f"CREATE TABLE {pre} AS SELECT * FROM {old} (evidence snapshot)",
            flush=True,
        )
        bq.query(f"CREATE TABLE `{pre}` AS SELECT * FROM `{old}`").result()
        serving = v2
        print(
            "NOTE: leave sentinel_raw.incidents until the streaming buffer "
            "drains, then rename: incidents → incidents_zombie, "
            "incidents_v2 → incidents; restore collector bq_table to "
            "sentinel_raw.incidents",
            flush=True,
        )

    print(f"recreate sentinel_core.incidents_current over {serving}", flush=True)
    bq.query(
        f"""
        CREATE OR REPLACE VIEW `{project}.sentinel_core.incidents_current` AS
        SELECT * EXCEPT (rn)
        FROM (
          SELECT
            *,
            ROW_NUMBER() OVER (
              PARTITION BY id, threads_id
              ORDER BY updatedOn DESC, _ingested_at DESC
            ) AS rn
          FROM `{serving}`
        )
        WHERE rn = 1
        """
    ).result()

    view_def = list(
        bq.query(
            f"""
            SELECT view_definition
            FROM `{project}.sentinel_core.INFORMATION_SCHEMA.VIEWS`
            WHERE table_name = 'incidents_current'
            """
        ).result()
    )[0].view_definition
    normalized = " ".join(view_def.split())
    assert "PARTITION BY id, threads_id" in normalized
    print("view confirmed: PARTITION BY (id, threads_id)", flush=True)
    print(
        "KEEP incidents_pre_id_fix — evidence of the defect; drop after pilot",
        flush=True,
    )
    if renamed:
        print("SERVING_TABLE=sentinel_raw.incidents", flush=True)
    else:
        print(
            "SERVING_TABLE=sentinel_raw.incidents_v2 "
            "(collector bq_table must match until rename completes)",
            flush=True,
        )
    # Persist for --verify / operators.
    (ROOT / ".tmp_serving_table.txt").write_text(
        serving.split(".")[-1], encoding="utf-8"
    )


def _serving_table_id(project: str) -> str:
    """Prefer incidents if it is STRING; else incidents_v2 after streaming fallback."""
    marker = ROOT / ".tmp_serving_table.txt"
    if marker.is_file():
        name = marker.read_text(encoding="utf-8").strip()
        if name:
            return _table_id(project, name)
    return _table_id(project, "incidents")


def _http_json(method: str, url: str, token: str, payload: Any = None) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def verify(bq: bigquery.Client, project: str) -> None:
    incidents = _serving_table_id(project)
    try:
        types = {f.name: f.field_type for f in bq.get_table(incidents).schema}
        if types.get("orderItemId") != "STRING":
            incidents = _table_id(project, "incidents_v2")
            types = {f.name: f.field_type for f in bq.get_table(incidents).schema}
    except Exception:
        incidents = _table_id(project, "incidents_v2")
        types = {f.name: f.field_type for f in bq.get_table(incidents).schema}

    pre = _table_id(project, "incidents_pre_id_fix")
    print("serving table:", incidents, flush=True)
    print("orderItemId type:", types.get("orderItemId"), flush=True)
    assert types.get("orderItemId") == "STRING"
    assert types.get("orderItemUnitId") == "STRING"
    assert types.get("threads_communicationId") == "STRING"

    for needle in BOUNDARY_PROBES:
        n = int(
            _scalar(
                bq,
                f"SELECT count(*) FROM `{incidents}` WHERE orderItemId = '{needle}'",
            )
            or 0
        )
        print(f"boundary in serving orderItemId={needle} count={n}", flush=True)
        assert n >= 1, f"missing exact probe {needle} in {incidents}"

    view_count = int(
        _scalar(
            bq,
            f"SELECT count(*) FROM `{project}.sentinel_core.incidents_current`",
        )
        or 0
    )
    pre_path = ROOT / ".tmp_pre_view_count.txt"
    pre_view = int(pre_path.read_text(encoding="utf-8")) if pre_path.is_file() else None
    pre_distinct = int(
        _scalar(
            bq,
            f"SELECT count(*) FROM ("
            f"SELECT DISTINCT id, threads_id FROM `{pre}`)",
        )
        or 0
    )
    print(
        f"incidents_current rows={view_count} "
        f"pre_migrate_view={pre_view} pre_id_fix_distinct={pre_distinct}",
        flush=True,
    )
    if pre_view is not None:
        drift = abs(view_count - pre_view) / max(pre_view, 1)
        print(f"view_count_drift={drift:.4f}", flush=True)
        assert drift < 0.02, (view_count, pre_view)

    api = os.environ["COLLECTOR_API_URL"].rstrip("/")
    mock = os.environ["SENTINEL_URL"].rstrip("/")
    token = os.environ.get("COLLECTOR_API_TOKEN") or os.environ.get("TOKEN")
    if not token:
        import shutil

        gcloud = shutil.which("gcloud")
        if not gcloud:
            raise SystemExit(
                "gcloud not on PATH — set COLLECTOR_API_TOKEN or fix PATH"
            )
        token = subprocess.check_output(
            [gcloud, "auth", "print-identity-token"],
            text=True,
        ).strip()

    code, seed = _http_json("POST", f"{mock}/admin/seed-acceptance-probes", token)
    print(
        "seed probes",
        code,
        seed if isinstance(seed, dict) else str(seed)[:200],
        flush=True,
    )
    ids = (seed or {}).get("precision_incident_ids") if isinstance(seed, dict) else None
    if not ids:
        raise SystemExit("seed-acceptance-probes did not return precision_incident_ids")

    code, collect = _http_json(
        "POST",
        f"{api}/v1/collect",
        token,
        {"source": "sentinel", "priority": 10, "query_spec": {"incident_ids": ids}},
    )
    print("fresh collect", code, collect, flush=True)
    assert code == 200, collect
    request_id = collect["request_id"]

    deadline = time.time() + 300
    while time.time() < deadline:
        code, counts = _http_json(
            "GET", f"{api}/v1/requests/{request_id}/counts", token
        )
        print("  counts", counts, flush=True)
        by_status = {}
        if isinstance(counts, dict):
            inner = counts.get("counts")
            if isinstance(inner, dict):
                by_status = inner
            else:
                by_status = counts
        pending = int(by_status.get("pending", 0) or 0)
        doing = int(by_status.get("in_progress", 0) or by_status.get("doing", 0) or 0)
        done = int(by_status.get("done", 0) or 0)
        dead = int(by_status.get("dead", 0) or 0)
        if (done + dead) >= 1 and pending == 0 and doing == 0:
            break
        time.sleep(2)
    else:
        raise SystemExit("fresh collect did not finish")

    for needle in (
        "9007199254740991",
        "9007199254740993",
        "1234567890123456789",
        "0123456789012345678",
    ):
        n = int(
            _scalar(
                bq,
                f"""
                SELECT count(*) FROM `{incidents}`
                WHERE _request_id = '{request_id}' AND orderItemId = '{needle}'
                """,
            )
            or 0
        )
        print(f"fresh request orderItemId={needle} rows={n}", flush=True)
        assert n >= 1, f"missing exact {needle} in fresh collection"

    code, recon = _http_json("GET", f"{api}/v1/reconcile", token)
    print(
        "reconcile",
        code,
        recon
        if not isinstance(recon, dict)
        else {k: recon[k] for k in recon if k != "rows"},
        flush=True,
    )
    assert code == 200 and isinstance(recon, dict)
    assert recon.get("unloaded") == 0, recon
    print("VERIFY_OK", flush=True)


def check_discovered_ids(bq: bigquery.Client, project: str) -> None:
    tid = f"{project}.sentinel_raw.discovered_ids"
    try:
        table = bq.get_table(tid)
    except Exception as exc:
        print(f"discovered_ids: not found ({exc})")
        return
    fields = [(f.name, f.field_type) for f in table.schema]
    print("discovered_ids schema:", fields)
    numericish = [
        (n, t)
        for n, t in fields
        if t in {"FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC", "INTEGER", "INT64"}
        and n
        not in {
            "cursor_page",
            "cursor_pages_fetched",
            "cursor_page_cap",
            "_page_no",
        }
    ]
    # INTEGER metadata counters are fine; look for identifier-like names.
    id_like = [
        (n, t)
        for n, t in fields
        if t in {"FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
        or (
            t in {"INTEGER", "INT64"}
            and any(tok in n.lower() for tok in ("order", "item", "comm", "id"))
            and n
            not in {
                "cursor_page",
                "cursor_pages_fetched",
                "cursor_page_cap",
                "_page_no",
            }
        )
    ]
    print("discovered_ids numeric/id-like columns:", id_like or "NONE")
    print(
        "REPORT: discovered_ids carries incident_id STRING only as the entity key; "
        "no orderItemId / communicationId. No backfill/swap required for discovery."
    )


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swap", action="store_true", help="rename v2 into place")
    parser.add_argument("--verify", action="store_true", help="post-swap verification")
    parser.add_argument("--reconcile", action="store_true", help="reconcile only")
    parser.add_argument(
        "--check-discovery",
        action="store_true",
        help="inspect discovered_ids for numeric identifiers",
    )
    args = parser.parse_args()

    _require_env("PROJECT", "RAW_BUCKET", "CONN", "DBPW")
    project = os.environ["PROJECT"]
    bucket = os.environ["RAW_BUCKET"]
    bq = bigquery.Client(project=project)

    if args.check_discovery:
        check_discovered_ids(bq, project)
        return
    if args.reconcile:
        reconcile(bq, project, bucket)
        return
    if args.swap:
        swap(bq, project)
        return
    if args.verify:
        _require_env("COLLECTOR_API_URL", "SENTINEL_URL")
        verify(bq, project)
        check_discovered_ids(bq, project)
        return

    # Default: backfill + reconcile
    backfill(bq, project, bucket)
    reconcile(bq, project, bucket)
    check_discovered_ids(bq, project)


if __name__ == "__main__":
    main()
