"""BigQuery landing-table loads for parsed collector records."""

from __future__ import annotations

import os

from google.cloud import bigquery

from collector.contract import Record


def append_records(
    table: str,
    records: list[Record],
    request_id: str,
    page_no: int,
    raw_uri: str,
) -> int:
    """Append parsed records to the landing table.

    Builds rows from record.data plus metadata columns _request_id, _page_no,
    _raw_uri. _ingested_at is left unset so BigQuery can default it.

    APPEND ONLY. We never upsert per page. Frequent small MERGE DML is where
    BigQuery cost and quota pressure concentrates, and append-only matches the
    decision that raw is always appended while the serving view is what updates.
    The merge happens later, in the sentinel_core view.

    Streaming inserts are used for demo simplicity. For production volumes,
    batch load jobs read from the GCS objects instead.
    """
    if not records:
        return 0

    project = os.environ.get("PROJECT", "")
    if table.count(".") == 1 and project:
        table_id = f"{project}.{table}"
    else:
        table_id = table

    rows = []
    for record in records:
        row = dict(record.data)
        row["_request_id"] = request_id
        row["_page_no"] = page_no
        row["_raw_uri"] = raw_uri
        rows.append(row)

    client = bigquery.Client(project=project or None)
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        raise RuntimeError(f"BigQuery insert_rows_json errors for {table_id}: {errors}")
    return len(rows)
