"""Audit sink fakes for local acceptance testing."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
GCS_ROOT = REPO_ROOT / "tests" / "audit" / "_gcs"
SCHEMA_SQL = REPO_ROOT / "sql" / "003_bigquery.sql"

_TABLE_BODY_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+`[^`]+`\s*\((?P<body>.*?)\)\s*PARTITION BY",
    re.DOTALL,
)
_COLUMN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s+([A-Z0-9]+)")


def _parse_declared_schema() -> dict[str, str]:
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    match = _TABLE_BODY_RE.search(text)
    if match is None:
        raise RuntimeError(f"Could not parse BigQuery schema from {SCHEMA_SQL}")

    declared: dict[str, str] = {}
    for raw in match.group("body").splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        field = _COLUMN_RE.match(line)
        if field is None:
            continue
        name, dtype = field.group(1), field.group(2)
        declared[name] = dtype
    if not declared:
        raise RuntimeError("No columns parsed from sql/003_bigquery.sql")
    return declared


def _coerce_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"cannot coerce {type(value).__name__} to TIMESTAMP")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _coerce_value(value: Any, declared_type: str) -> Any:
    if value is None:
        return None
    if declared_type == "STRING":
        return str(value)
    if declared_type == "FLOAT64":
        return float(value)
    if declared_type == "INT64":
        if isinstance(value, bool):
            raise ValueError("bool is not valid INT64")
        return int(value)
    if declared_type == "TIMESTAMP":
        return _coerce_timestamp(value)
    if declared_type == "BOOL":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "t", "1", "yes"}:
                return True
            if lowered in {"false", "f", "0", "no"}:
                return False
        if isinstance(value, (int, float)):
            if value == 1:
                return True
            if value == 0:
                return False
        raise ValueError(f"cannot coerce {value!r} to BOOL")
    raise ValueError(f"unsupported declared type: {declared_type}")


def _error_row(index: int, field: str, message: str) -> dict[str, Any]:
    return {
        "index": index,
        "errors": [
            {
                "reason": "invalid",
                "location": field,
                "message": message,
            }
        ],
    }


@dataclass
class _FakeBlob:
    bucket_name: str
    object_name: str

    def upload_from_string(self, body: bytes | str, content_type: str | None = None) -> None:
        del content_type
        target = GCS_ROOT / self.bucket_name / self.object_name
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        target.write_bytes(payload)


@dataclass
class _FakeBucket:
    name: str

    def blob(self, object_name: str) -> _FakeBlob:
        return _FakeBlob(bucket_name=self.name, object_name=object_name)


class FakeGCSClient:
    def bucket(self, bucket_name: str) -> _FakeBucket:
        return _FakeBucket(name=bucket_name)


class FakeGCS:
    Client = FakeGCSClient

    @staticmethod
    def list_objects(bucket_name: str) -> list[str]:
        root = GCS_ROOT / bucket_name
        if not root.exists():
            return []
        return sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file()
        )


class FakeBQClient:
    _schema = _parse_declared_schema()

    def __init__(self, project: str | None = None):
        del project

    @staticmethod
    def _dsn() -> str:
        dsn = os.environ.get("COLLECTOR_DSN")
        if not dsn:
            raise RuntimeError("COLLECTOR_DSN is required for FakeBQ")
        return dsn

    @classmethod
    def ensure_table(cls) -> None:
        with psycopg.connect(cls._dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_bq_rows (
                        table_id text NOT NULL,
                        row jsonb NOT NULL,
                        inserted_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
            conn.commit()

    @classmethod
    def truncate_rows(cls) -> None:
        cls.ensure_table()
        with psycopg.connect(cls._dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE audit_bq_rows")
            conn.commit()

    @classmethod
    def fetch_rows(cls, table_id: str) -> list[dict[str, Any]]:
        cls.ensure_table()
        with psycopg.connect(cls._dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT row::text
                    FROM audit_bq_rows
                    WHERE table_id = %s
                    ORDER BY inserted_at
                    """,
                    (table_id,),
                )
                items = cur.fetchall()
        return [json.loads(row[0]) for row in items]

    def insert_rows_json(self, table_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.ensure_table()

        errors: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []

        for index, row in enumerate(rows):
            unknown = [field for field in row.keys() if field not in self._schema]
            if unknown:
                errors.append(
                    _error_row(index, unknown[0], f"no such field: {unknown[0]}")
                )
                continue

            coerced: dict[str, Any] = {}
            current_field = "<unknown>"
            try:
                for field, value in row.items():
                    current_field = field
                    coerced[field] = _coerce_value(value, self._schema[field])
            except Exception as exc:  # noqa: BLE001
                errors.append(_error_row(index, current_field, str(exc)))
                continue
            accepted.append(coerced)

        if accepted:
            with psycopg.connect(self._dsn()) as conn:
                with conn.cursor() as cur:
                    for row in accepted:
                        cur.execute(
                            """
                            INSERT INTO audit_bq_rows (table_id, row)
                            VALUES (%s, %s::jsonb)
                            """,
                            (table_id, json.dumps(row)),
                        )
                conn.commit()

        return errors


class FakeBQ:
    Client = FakeBQClient


class SinkPatch:
    """Patch collector sink clients and restore on exit."""

    def __init__(self) -> None:
        self._orig_bq: Any = None
        self._orig_gcs: Any = None

    def __enter__(self) -> "SinkPatch":
        from collector import load, raw

        self._orig_bq = load.bigquery.Client
        self._orig_gcs = raw.storage.Client
        load.bigquery.Client = FakeBQ.Client
        raw.storage.Client = FakeGCS.Client
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        from collector import load, raw

        if self._orig_bq is not None:
            load.bigquery.Client = self._orig_bq
        if self._orig_gcs is not None:
            raw.storage.Client = self._orig_gcs
