"""GCS raw-path determinism — proves retries cannot duplicate raw objects."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from google.cloud import storage

from collector.raw import write_raw


def _load_dotenv() -> None:
    env_path = Path(".env")
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


_load_dotenv()


@pytest.fixture(scope="module")
def raw_bucket() -> str:
    bucket = os.environ.get("RAW_BUCKET")
    if not bucket:
        pytest.fail("RAW_BUCKET is required")
    return bucket


def test_write_raw_is_deterministic(raw_bucket: str) -> None:
    """This test is what proves retries cannot duplicate raw objects."""
    request_id = str(uuid.uuid4())
    source = "sentinel"
    page_no = 0
    body = b'{"incidents":[],"count":0}'
    content_type = "application/json"

    uri1, size1, sha1 = write_raw(source, request_id, page_no, body, content_type)
    uri2, size2, sha2 = write_raw(source, request_id, page_no, body, content_type)

    assert uri1 == uri2
    assert size1 == size2 == len(body)
    assert sha1 == sha2

    # Prefix is everything under the request folder for this write.
    # Object: raw/source=.../dt=.../request=.../page=00000.json
    prefix = uri1.split(f"gs://{raw_bucket}/", 1)[1].rsplit("/", 1)[0] + "/"
    client = storage.Client()
    blobs = list(client.list_blobs(raw_bucket, prefix=prefix))
    assert len(blobs) == 1, f"expected exactly one object under {prefix}, got {len(blobs)}"
    assert blobs[0].name.endswith(f"page={page_no:05d}.json")
