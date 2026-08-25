"""Destructive admin reset of collector *output* only.

The HARD ALLOWLIST below is the only set of targets this module may clear.
Never discover tables dynamically, never pattern-match names, never scan
information_schema. If a name is not in these lists, it is not touched.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from typing import Any

import psycopg
from google.auth import default as google_auth_default
from google.cloud import bigquery, storage

from collector.db import connect

log = logging.getLogger(__name__)

# HARD ALLOWLIST — explicit; never derived.
RESET_CONFIRM_PHRASE = "reset-collector-data"

RESET_CLOUD_SQL_TABLES: list[str] = [
    "raw_manifest",
    "collector_job",
    "collector_request",
    "collector_control",  # truncate only if the relation exists
]

RESET_BQ_TABLES: list[str] = [
    "sentinel_raw.incidents",
    "sentinel_raw.discovered_ids",
]

GCS_RAW_PREFIX = "raw/"


def _mock_dsn() -> str | None:
    raw = (os.environ.get("SENTINEL_MOCK_DSN") or "").strip()
    return raw or None


def _live_sample_counts() -> tuple[int | None, int | None, str | None]:
    """Read-only counts from sentinel_mock when SENTINEL_MOCK_DSN is set.

    Returns (incidents, threads, error). Never writes. If the DSN is unset
    (typical on Cloud Run — API has no mock access), returns (None, None, None).
    """
    dsn = _mock_dsn()
    if not dsn:
        return None, None, None
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*)::int FROM sentinel_incident")
                incidents = int(cur.fetchone()[0])
                cur.execute("SELECT count(*)::int FROM sentinel_thread")
                threads = int(cur.fetchone()[0])
        return incidents, threads, None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)


def _preserved_sample_counts(warnings: list[str]) -> dict[str, int | None]:
    """Live sample-data footprint (read-only) when SENTINEL_MOCK_DSN is set."""
    incidents, threads, err = _live_sample_counts()
    if err:
        warnings.append(f"sample-data count read failed: {err}")
    return {
        "sentinel_incident": incidents,
        "sentinel_thread": threads,
    }


def _warn_if_sample_changed(
    before: dict[str, Any],
    after: dict[str, Any],
    warnings: list[str],
) -> None:
    """Warn only when live sample counts changed across the reset."""
    for key in ("sentinel_incident", "sentinel_thread"):
        b = before.get(key)
        a = after.get(key)
        if b is None or a is None:
            continue
        if b != a:
            warnings.append(
                f"LOUD: {key} changed across reset ({b} → {a}) — "
                "source sample data may have been touched"
            )


def admin_reset_enabled() -> bool:
    return os.environ.get("ALLOW_ADMIN_RESET") == "1"


def acting_identity() -> str:
    """ADC / runtime identity that will touch GCS and BigQuery."""
    # Cloud Run / GCE expose the real SA email via the metadata server; ADC
    # often reports service_account_email="default" until refreshed.
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            email = resp.read().decode().strip()
            if email and email != "default":
                return f"serviceAccount:{email}"
    except Exception:  # noqa: BLE001 — not on GCE/Cloud Run
        pass

    try:
        creds, project = google_auth_default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        email = getattr(creds, "service_account_email", None)
        if email and email != "default":
            return f"serviceAccount:{email}"
        sa = getattr(creds, "_service_account_email", None)
        if sa and sa != "default":
            return f"serviceAccount:{sa}"
        return f"adc_user project={project or os.environ.get('PROJECT', '?')}"
    except Exception as exc:  # noqa: BLE001
        return f"identity_unknown ({exc})"


def _project() -> str:
    return os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""


def _region() -> str:
    return os.environ.get("REGION", "asia-south1")


def _bucket() -> str:
    return os.environ.get("RAW_BUCKET") or os.environ.get("BUCKET") or ""


def _table_exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",))
    return bool(cur.fetchone()[0])


def _count_table(cur: Any, name: str) -> int | None:
    if not _table_exists(cur, name):
        return None
    if name not in RESET_CLOUD_SQL_TABLES:
        raise RuntimeError(f"refusing to count non-allowlisted table {name!r}")
    cur.execute(f"SELECT count(*)::int FROM {name}")  # noqa: S608 — allowlist gated
    return int(cur.fetchone()[0])


def _count_sql(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    cur.execute(sql, params)
    return int(cur.fetchone()[0])


def _count_gcs_raw(bucket_name: str, project: str | None) -> int:
    client = storage.Client(project=project or None)
    n = 0
    for blob in client.list_blobs(bucket_name, prefix=GCS_RAW_PREFIX):
        if blob.name.startswith(GCS_RAW_PREFIX):
            n += 1
    return n


def _bq_client() -> bigquery.Client:
    # Datasets live in asia-south1 — location must match or counts/TRUNCATE miss.
    return bigquery.Client(project=_project() or None, location=_region())


def _count_bq_table(client: bigquery.Client, table_id: str) -> int:
    job = client.query(
        f"SELECT count(*) AS n FROM `{table_id}`",
        location=_region(),
    )
    return int(list(job.result())[0].n)


def collect_live_state(warnings: list[str] | None = None) -> dict[str, Any]:
    """Read-only live counts for every store the reset touches (+ sample sizes)."""
    warnings = warnings if warnings is not None else []
    project = _project()
    region = _region()
    bucket = _bucket()
    sample = _preserved_sample_counts(warnings)
    out: dict[str, Any] = {
        "identity": acting_identity(),
        "project": project,
        "region": region,
        "bucket": bucket,
        "gcs_prefix": GCS_RAW_PREFIX,
        "cloud_sql": {},
        "collector_job_by_status": {},
        "recent_jobs": [],
        "unloaded": None,
        "procrastinate": {},
        "workers": [],
        "live_workers": None,
        "gcs_objects_raw": None,
        "bigquery": {},
        "bigquery_distinct": {},
        "sample_data": sample,
        "warnings": warnings,
    }

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                for name in RESET_CLOUD_SQL_TABLES:
                    out["cloud_sql"][name] = _count_table(cur, name)
                cur.execute(
                    """
                    SELECT status, count(*)::int
                    FROM collector_job
                    GROUP BY status
                    ORDER BY status
                    """
                )
                out["collector_job_by_status"] = {
                    status: count for status, count in cur.fetchall()
                }
                cur.execute(
                    """
                    SELECT count(*)::int
                    FROM collector_job
                    WHERE raw_written_at IS NOT NULL
                      AND loaded_at IS NULL
                      AND raw_written_at < now() - interval '15 minutes'
                    """
                )
                out["unloaded"] = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT page_no, status, owner, record_count,
                           raw_written_at, loaded_at, source, request_id::text
                    FROM collector_job
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC
                    LIMIT 5
                    """
                )
                out["recent_jobs"] = [
                    {
                        "page_no": row[0],
                        "status": row[1],
                        "owner": row[2],
                        "record_count": row[3],
                        "raw_written_at": row[4].isoformat() if row[4] else None,
                        "loaded_at": row[5].isoformat() if row[5] else None,
                        "source": row[6],
                        "request_id": row[7],
                    }
                    for row in cur.fetchall()
                ]
                out["procrastinate"]["jobs_total"] = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_jobs"
                )
                out["procrastinate"]["jobs_doing"] = _count_sql(
                    cur,
                    "SELECT count(*)::int FROM procrastinate_jobs WHERE status = 'doing'",
                )
                out["procrastinate"]["jobs_not_doing"] = _count_sql(
                    cur,
                    "SELECT count(*)::int FROM procrastinate_jobs WHERE status <> 'doing'",
                )
                out["procrastinate"]["events"] = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_events"
                )
                out["procrastinate"]["periodic_defers"] = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_periodic_defers"
                )
                out["procrastinate"]["workers"] = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_workers"
                )
                cur.execute(
                    """
                    SELECT count(*)::int
                    FROM procrastinate_workers
                    WHERE now() - last_heartbeat < interval '60 seconds'
                    """
                )
                out["live_workers"] = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT id,
                           EXTRACT(EPOCH FROM (now() - last_heartbeat))::float
                             AS heartbeat_age_seconds
                    FROM procrastinate_workers
                    ORDER BY id
                    """
                )
                out["workers"] = [
                    {"id": wid, "heartbeat_age_seconds": age}
                    for wid, age in cur.fetchall()
                ]
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Cloud SQL state read failed: {exc}")

    if bucket:
        try:
            out["gcs_objects_raw"] = _count_gcs_raw(bucket, project or None)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"GCS state read failed: {exc}")
    else:
        warnings.append("RAW_BUCKET/BUCKET unset")

    try:
        client = _bq_client()
        for rel in RESET_BQ_TABLES:
            table_id = f"{project}.{rel}" if project else rel
            try:
                out["bigquery"][rel] = _count_bq_table(client, table_id)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"BigQuery count {table_id} failed: {exc}")
                out["bigquery"][rel] = None
        # Distinct incidents in the enrichment landing table (thread-exploded).
        try:
            table_id = f"{project}.sentinel_raw.incidents"
            out["bigquery_distinct"]["sentinel_raw.incidents"] = int(
                list(
                    client.query(
                        f"SELECT count(DISTINCT id) AS n FROM `{table_id}`",
                        location=region,
                    ).result()
                )[0].n
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"BigQuery distinct incidents failed: {exc}")
            out["bigquery_distinct"]["sentinel_raw.incidents"] = None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"BigQuery client failed: {exc}")

    out["warnings"] = warnings
    return out


def collect_before_counts(warnings: list[str]) -> dict[str, Any]:
    """Snapshot counts for every allowlisted target + preserved resources."""
    project = _project()
    bucket = _bucket()

    cleared: dict[str, Any] = {}
    preserved: dict[str, Any] = {}

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                for name in RESET_CLOUD_SQL_TABLES:
                    n = _count_table(cur, name)
                    if n is None:
                        cleared[name] = {"before": None, "exists": False}
                    else:
                        cleared[name] = {"before": n, "would_delete": n}

                jobs_total = _count_sql(cur, "SELECT count(*)::int FROM procrastinate_jobs")
                jobs_doing = _count_sql(
                    cur,
                    "SELECT count(*)::int FROM procrastinate_jobs WHERE status = 'doing'",
                )
                cleared["procrastinate_jobs"] = {
                    "before": jobs_total,
                    "would_delete": jobs_total - jobs_doing,
                    "skipped_doing": jobs_doing,
                }

                events_total = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_events"
                )
                events_deletable = _count_sql(
                    cur,
                    """
                    SELECT count(*)::int FROM procrastinate_events
                    WHERE job_id IN (
                      SELECT id FROM procrastinate_jobs WHERE status <> 'doing'
                    )
                    """,
                )
                cleared["procrastinate_events"] = {
                    "before": events_total,
                    "would_delete": events_deletable,
                }

                periodics = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_periodic_defers"
                )
                cleared["procrastinate_periodic_defers"] = {
                    "before": periodics,
                    "would_delete": periodics,
                }

                preserved["procrastinate_workers"] = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_workers"
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Cloud SQL before-counts failed: {exc}")

    if bucket:
        try:
            gcs_n = _count_gcs_raw(bucket, project or None)
            cleared["gcs_objects"] = {
                "before": gcs_n,
                "would_delete": gcs_n,
                "bucket": bucket,
                "prefix": GCS_RAW_PREFIX,
            }
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"GCS before-count failed: {exc}")
            cleared["gcs_objects"] = {
                "before": None,
                "bucket": bucket,
                "prefix": GCS_RAW_PREFIX,
            }
    else:
        warnings.append("RAW_BUCKET/BUCKET unset — skipping GCS counts")
        cleared["gcs_objects"] = {"before": None}

    try:
        bq = _bq_client()
        for rel in RESET_BQ_TABLES:
            table_id = f"{project}.{rel}" if project else rel
            try:
                n = _count_bq_table(bq, table_id)
                cleared[rel] = {
                    "before": n,
                    "would_truncate": True,
                    "table_id": table_id,
                    "location": _region(),
                }
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"BigQuery before-count {table_id} failed: {exc}")
                cleared[rel] = {
                    "before": None,
                    "would_truncate": False,
                    "table_id": table_id,
                    "location": _region(),
                }
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"BigQuery before-counts failed: {exc}")

    sample = _preserved_sample_counts(warnings)
    preserved.update(sample)
    # Snapshot only — corruption check happens after mutate (before vs after).

    return {"cleared": cleared, "preserved": preserved}


def in_progress_jobs() -> tuple[int, list[str]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text
                FROM collector_job
                WHERE status = 'in_progress'
                ORDER BY updated_at
                """
            )
            ids = [row[0] for row in cur.fetchall()]
    return len(ids), ids


def _truncate_cloud_sql(cleared: dict[str, Any], warnings: list[str]) -> None:
    """Truncate allowlisted SQL tables in ONE statement.

    raw_manifest → collector_job → collector_request form an FK chain.
    Truncating them one-by-one fails with FeatureNotSupported and rolls the
    whole transaction back — which previously looked like a silent SQL miss
    when callers only checked GCS/BQ, or left SQL rows while other stages
    succeeded. PostgreSQL allows TRUNCATE a,b,c together across FKs.
    """
    log.warning(
        "stage=cloud_sql action=TRUNCATE_MULTI tables=%s",
        RESET_CLOUD_SQL_TABLES,
    )
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                present: list[str] = []
                for name in RESET_CLOUD_SQL_TABLES:
                    if not _table_exists(cur, name):
                        cleared.setdefault(name, {})["after"] = None
                        cleared[name]["skipped"] = "relation does not exist"
                        log.warning("stage=cloud_sql skip %s (missing)", name)
                        continue
                    present.append(name)

                if present:
                    # Single statement — required for FK-linked allowlist tables.
                    stmt = (
                        "TRUNCATE "
                        + ", ".join(present)
                        + " RESTART IDENTITY"
                    )
                    log.warning(
                        "stage=cloud_sql executing %s",
                        stmt,
                    )
                    cur.execute(stmt)  # noqa: S608 — allowlist-gated names only

                for name in present:
                    cur.execute(f"SELECT count(*)::int FROM {name}")  # noqa: S608
                    after = int(cur.fetchone()[0])
                    cleared.setdefault(name, {})["after"] = after
                    log.warning("stage=cloud_sql %s after=%s", name, after)
                    if after != 0:
                        warnings.append(
                            f"Cloud SQL {name} after truncate is {after}, expected 0"
                        )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Cloud SQL truncate failed: {exc}")
        log.exception("stage=cloud_sql FAILED")
        # Re-read whatever is left — never invent after=0.
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    for name in RESET_CLOUD_SQL_TABLES:
                        if _table_exists(cur, name):
                            cleared.setdefault(name, {})["after"] = _count_table(
                                cur, name
                            )
        except Exception as re_exc:  # noqa: BLE001
            warnings.append(f"Cloud SQL after-count re-read failed: {re_exc}")


def _clear_procrastinate(cleared: dict[str, Any], warnings: list[str]) -> None:
    """Delete idle queue rows only — never touch procrastinate_workers."""
    log.warning(
        "stage=procrastinate action=DELETE jobs status<>doing "
        "(never truncate procrastinate_workers)"
    )
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*)::int FROM procrastinate_jobs WHERE status = 'doing'"
                )
                skipped_doing = int(cur.fetchone()[0])

                cur.execute(
                    """
                    DELETE FROM procrastinate_events
                    WHERE job_id IN (
                      SELECT id FROM procrastinate_jobs WHERE status <> 'doing'
                    )
                    """
                )
                events_deleted = cur.rowcount

                cur.execute(
                    "DELETE FROM procrastinate_jobs WHERE status <> 'doing'"
                )
                jobs_deleted = cur.rowcount

                cur.execute("DELETE FROM procrastinate_periodic_defers")
                periodics_deleted = cur.rowcount

                cur.execute("SELECT count(*)::int FROM procrastinate_jobs")
                jobs_after = int(cur.fetchone()[0])
                cur.execute("SELECT count(*)::int FROM procrastinate_events")
                events_after = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT count(*)::int FROM procrastinate_periodic_defers"
                )
                periodics_after = int(cur.fetchone()[0])
            conn.commit()

        cleared.setdefault("procrastinate_jobs", {}).update(
            {
                "deleted": jobs_deleted,
                "skipped_doing": skipped_doing,
                "after": jobs_after,
            }
        )
        cleared.setdefault("procrastinate_events", {}).update(
            {"deleted": events_deleted, "after": events_after}
        )
        cleared.setdefault("procrastinate_periodic_defers", {}).update(
            {"deleted": periodics_deleted, "after": periodics_after}
        )
        log.warning(
            "stage=procrastinate done jobs_deleted=%s skipped_doing=%s "
            "jobs_after=%s",
            jobs_deleted,
            skipped_doing,
            jobs_after,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Procrastinate clear failed: {exc}")
        log.exception("stage=procrastinate FAILED")
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    cleared.setdefault("procrastinate_jobs", {})["after"] = _count_sql(
                        cur, "SELECT count(*)::int FROM procrastinate_jobs"
                    )
                    cleared.setdefault("procrastinate_events", {})["after"] = _count_sql(
                        cur, "SELECT count(*)::int FROM procrastinate_events"
                    )
                    cleared.setdefault("procrastinate_periodic_defers", {})[
                        "after"
                    ] = _count_sql(
                        cur,
                        "SELECT count(*)::int FROM procrastinate_periodic_defers",
                    )
        except Exception as re_exc:  # noqa: BLE001
            warnings.append(f"Procrastinate after-count re-read failed: {re_exc}")


def _clear_gcs(cleared: dict[str, Any], warnings: list[str]) -> None:
    project = _project()
    bucket_name = _bucket()
    if not bucket_name:
        warnings.append("RAW_BUCKET/BUCKET unset — GCS not cleared")
        return
    uri = f"gs://{bucket_name}/{GCS_RAW_PREFIX}"
    log.warning(
        "stage=gcs action=DELETE prefix=%s identity=%s",
        uri,
        acting_identity(),
    )
    try:
        client = storage.Client(project=project or None)
        deleted = 0
        for blob in client.list_blobs(bucket_name, prefix=GCS_RAW_PREFIX):
            if not blob.name.startswith(GCS_RAW_PREFIX):
                warnings.append(
                    f"refused to delete object outside raw/: {blob.name!r}"
                )
                continue
            log.warning("stage=gcs delete gs://%s/%s", bucket_name, blob.name)
            blob.delete()
            deleted += 1
        after = _count_gcs_raw(bucket_name, project or None)
        cleared.setdefault("gcs_objects", {}).update(
            {
                "deleted": deleted,
                "after": after,
                "bucket": bucket_name,
                "prefix": GCS_RAW_PREFIX,
            }
        )
        log.warning("stage=gcs done deleted=%s after=%s", deleted, after)
        if after != 0:
            warnings.append(
                f"GCS gs://{bucket_name}/{GCS_RAW_PREFIX} after delete is "
                f"{after}, expected 0"
            )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"GCS clear failed: {exc}")
        log.exception("stage=gcs FAILED prefix=%s", uri)
        try:
            after = _count_gcs_raw(bucket_name, project or None)
            cleared.setdefault("gcs_objects", {})["after"] = after
            log.warning("stage=gcs after-count despite failure: %s", after)
        except Exception as re_exc:  # noqa: BLE001
            warnings.append(f"GCS after-count re-read failed: {re_exc}")
            cleared.setdefault("gcs_objects", {})["after"] = None


def _truncate_bigquery(cleared: dict[str, Any], warnings: list[str]) -> None:
    project = _project()
    region = _region()
    log.warning(
        "stage=bigquery action=TRUNCATE tables=%s location=%s identity=%s",
        RESET_BQ_TABLES,
        region,
        acting_identity(),
    )
    try:
        client = _bq_client()
        for rel in RESET_BQ_TABLES:
            table_id = f"{project}.{rel}" if project else rel
            try:
                log.warning(
                    "stage=bigquery TRUNCATE TABLE `%s` location=%s",
                    table_id,
                    region,
                )
                # TRUNCATE TABLE — never DROP. Schema / partitioning / clustering stay.
                client.query(
                    f"TRUNCATE TABLE `{table_id}`",
                    location=region,
                ).result()
                after = _count_bq_table(client, table_id)
                cleared.setdefault(rel, {}).update(
                    {
                        "after": after,
                        "table_id": table_id,
                        "location": region,
                    }
                )
                log.warning("stage=bigquery %s after=%s", table_id, after)
                if after != 0:
                    warnings.append(
                        f"BigQuery {table_id} after truncate is {after}, expected 0"
                    )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"BigQuery truncate {rel} failed: {exc}")
                log.exception("stage=bigquery FAILED table=%s", table_id)
                try:
                    after = _count_bq_table(client, table_id)
                    cleared.setdefault(rel, {})["after"] = after
                except Exception as re_exc:  # noqa: BLE001
                    warnings.append(
                        f"BigQuery after-count {table_id} failed: {re_exc}"
                    )
                    cleared.setdefault(rel, {})["after"] = None
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"BigQuery client failed: {exc}")
        log.exception("stage=bigquery client FAILED")


def run_reset(*, dry_run: bool, force: bool, caller: str) -> dict[str, Any]:
    """Execute (or simulate) an allowlisted collector-output reset."""
    warnings: list[str] = []
    identity = acting_identity()

    log.warning(
        "admin reset requested caller=%s identity=%s dry_run=%s force=%s "
        "project=%s region=%s bucket=%s",
        caller,
        identity,
        dry_run,
        force,
        _project(),
        _region(),
        _bucket(),
    )

    snapshot = collect_before_counts(warnings)
    cleared: dict[str, Any] = snapshot["cleared"]
    preserved: dict[str, Any] = snapshot["preserved"]

    if dry_run:
        log.warning(
            "admin reset dry_run complete caller=%s identity=%s warnings=%s",
            caller,
            identity,
            len(warnings),
        )
        return {
            "dry_run": True,
            "success": len(warnings) == 0,
            "identity": identity,
            "cleared": cleared,
            "preserved": preserved,
            "warnings": warnings,
        }

    try:
        n_in_flight, job_ids = in_progress_jobs()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"in_progress check failed (Cloud SQL unreachable?): {exc}")
        log.exception("stage=in_progress_check FAILED")
        return {
            "dry_run": False,
            "success": False,
            "identity": identity,
            "cleared": cleared,
            "preserved": preserved,
            "warnings": warnings,
        }

    if n_in_flight > 0 and not force:
        raise ResetInProgressError(n_in_flight, job_ids)

    # Order: Cloud SQL collector tables → Procrastinate queue → GCS → BQ.
    _truncate_cloud_sql(cleared, warnings)
    _clear_procrastinate(cleared, warnings)
    _clear_gcs(cleared, warnings)
    _truncate_bigquery(cleared, warnings)

    try:
        with connect() as conn:
            with conn.cursor() as cur:
                preserved["procrastinate_workers"] = _count_sql(
                    cur, "SELECT count(*)::int FROM procrastinate_workers"
                )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"post-reset worker count failed: {exc}")

    sample_before = {
        "sentinel_incident": preserved.get("sentinel_incident"),
        "sentinel_thread": preserved.get("sentinel_thread"),
    }
    sample_after = _preserved_sample_counts(warnings)
    preserved.update(sample_after)
    _warn_if_sample_changed(sample_before, sample_after, warnings)

    success = len(warnings) == 0
    log.warning(
        "admin reset complete caller=%s identity=%s success=%s warnings=%s",
        caller,
        identity,
        success,
        warnings,
    )
    return {
        "dry_run": False,
        "success": success,
        "identity": identity,
        "cleared": cleared,
        "preserved": preserved,
        "warnings": warnings,
    }


class ResetInProgressError(Exception):
    def __init__(self, count: int, job_ids: list[str]) -> None:
        super().__init__(f"{count} collector_job rows in_progress")
        self.count = count
        self.job_ids = job_ids
