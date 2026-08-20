"""GCS raw-zone landing for collector pages."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from google.cloud import storage


def write_raw(
    source: str,
    request_id: str,
    page_no: int,
    body: bytes,
    content_type: str,
) -> tuple[str, int, str]:
    """Write one page body to the shared raw bucket.

    Returns (uri, byte_size, sha256).

    The path derives ONLY from source, request, page and date — never from a
    UUID or timestamp generated inside the function. This is what makes retries
    safe: re-running the same job overwrites the same object instead of creating
    a duplicate and a phantom manifest row.

    One bucket serves every collector, partitioned by the source= prefix.
    """
    bucket_name = os.environ["RAW_BUCKET"]
    utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    object_name = (
        f"raw/source={source}/dt={utc_date}/"
        f"request={request_id}/page={page_no:05d}.json"
    )
    digest = hashlib.sha256(body).hexdigest()
    byte_size = len(body)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_string(body, content_type=content_type)

    uri = f"gs://{bucket_name}/{object_name}"
    return uri, byte_size, digest
