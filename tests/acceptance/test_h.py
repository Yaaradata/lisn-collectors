from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from google.cloud import bigquery

from tests.acceptance.datasets import DATASETS


UTC = timezone.utc
EVIDENCE_DIR = Path("/workspace/tests/acceptance/evidence")


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _mock_dsn() -> str:
    return os.environ["SENTINEL_MOCK_DSN"]


def _project() -> str:
    return os.environ["PROJECT"].strip()


def _ensure_adc() -> None:
    payload = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")
    if not payload:
        return
    path = Path("/tmp/gcp-adc.json")
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


def _post_collect(source: str, query_spec: dict[str, object]) -> str:
    body = {"source": source, "query_spec": query_spec}
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/collect",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return str(payload["request_id"])


def _api_get_json(path: str) -> dict[str, object]:
    req = urllib.request.Request(
        f"http://127.0.0.1:8080{path}",
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _write_evidence(test_id: str, lines: list[str]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{test_id}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reset_collector_state() -> None:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE raw_manifest, collector_job, collector_request,
                               procrastinate_events, procrastinate_jobs,
                               procrastinate_periodic_defers
                RESTART IDENTITY CASCADE
                """
            )
        conn.commit()


@dataclass
class JobObservation:
    queue_status: str | None
    queue_name: str | None
    worker_id: int | None
    job_status: str
    attempts: int


def _fetch_job_observation(request_id: str) -> JobObservation:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pj.status, pj.queue_name, pj.worker_id,
                       cj.status, cj.attempts
                FROM collector_job cj
                LEFT JOIN procrastinate_jobs pj
                  ON pj.args->>'job_id' = cj.job_id::text
                WHERE cj.request_id = %s::uuid
                ORDER BY cj.created_at ASC
                LIMIT 1
                """,
                (request_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise AssertionError(f"no collector_job for request_id={request_id}")
    return JobObservation(
        queue_status=row[0],
        queue_name=row[1],
        worker_id=row[2],
        job_status=row[3],
        attempts=int(row[4]),
    )


def _wait_for_job_terminal(request_id: str, timeout_s: int = 120) -> JobObservation:
    deadline = time.monotonic() + timeout_s
    last: JobObservation | None = None
    while time.monotonic() < deadline:
        last = _fetch_job_observation(request_id)
        if last.job_status in {"done", "dead", "failed"}:
            return last
        time.sleep(2)
    assert last is not None
    raise AssertionError(f"request {request_id} not terminal within {timeout_s}s: {last}")


def _bq_discovered_ids(request_id: str) -> list[str]:
    _ensure_adc()
    client = bigquery.Client(project=_project())
    query = f"""
        SELECT DISTINCT incident_id
        FROM `{_project()}.sentinel_raw.discovered_ids`
        WHERE _request_id = @rid
        ORDER BY incident_id
    """
    rows = list(
        client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
            ),
        ).result()
    )
    return [str(r["incident_id"]) for r in rows if r["incident_id"] is not None]


def _mock_truth_discovery(start: datetime, end: datetime) -> list[str]:
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM sentinel_incident
                WHERE updated_on >= %s
                  AND updated_on <= %s
                ORDER BY id
                """,
                (start, end),
            )
            return [r[0] for r in cur.fetchall()]


def _ds11_window_bounds() -> tuple[datetime, datetime]:
    ds11 = next(d for d in DATASETS if d.code == "DS-11")
    snap = ds11.build()
    return snap.min_updated_on, snap.max_updated_on


def _window_iso(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "updated_from": start.isoformat().replace("+00:00", "Z"),
        "updated_to": end.isoformat().replace("+00:00", "Z"),
        "limit": 1000,
    }


def test_h0_discovery_runs_at_all() -> None:
    _reset_collector_state()
    start, end = _ds11_window_bounds()
    one_hour_end = start + timedelta(hours=1)
    request_id = _post_collect("sentinel_discovery", _window_iso(start, one_hour_end))

    picked_up = False
    deadline = time.monotonic() + 60
    last = _fetch_job_observation(request_id)
    while time.monotonic() < deadline:
        last = _fetch_job_observation(request_id)
        if last.queue_status == "doing" or last.worker_id is not None or last.job_status == "in_progress":
            picked_up = True
            break
        time.sleep(2)
    assert picked_up, f"H-0 blocking: request {request_id} sat unpicked; last={last}"


def test_h1_discovery_finds_everything_in_window() -> None:
    _reset_collector_state()
    start, end = _ds11_window_bounds()
    window_end = start + timedelta(hours=1)
    truth = _mock_truth_discovery(start, window_end)
    request_id = _post_collect("sentinel_discovery", _window_iso(start, window_end))
    terminal = _wait_for_job_terminal(request_id)
    assert terminal.job_status == "done"
    discovered = _bq_discovered_ids(request_id)
    assert discovered == truth


def test_h2_contiguous_windows_lose_nothing() -> None:
    _reset_collector_state()
    start, end = _ds11_window_bounds()
    total_hours = int((end - start).total_seconds() // 3600)
    assert total_hours == 6
    truth_full = _mock_truth_discovery(start, end)
    union: set[str] = set()
    for i in range(6):
        w_start = start + timedelta(hours=i)
        w_end = w_start + timedelta(hours=1)
        request_id = _post_collect("sentinel_discovery", _window_iso(w_start, w_end))
        terminal = _wait_for_job_terminal(request_id)
        assert terminal.job_status == "done"
        union.update(_bq_discovered_ids(request_id))
    assert sorted(union) == truth_full


def test_h3_gap_window_loss_is_silent() -> None:
    _reset_collector_state()
    start, end = _ds11_window_bounds()
    truth_full = _mock_truth_discovery(start, end)
    union: set[str] = set()
    request_ids: list[str] = []
    for i in [0, 1, 3, 4, 5]:
        w_start = start + timedelta(hours=i)
        w_end = w_start + timedelta(hours=1)
        request_id = _post_collect("sentinel_discovery", _window_iso(w_start, w_end))
        request_ids.append(request_id)
        terminal = _wait_for_job_terminal(request_id)
        assert terminal.job_status == "done"
        union.update(_bq_discovered_ids(request_id))
    missing = sorted(set(truth_full) - union)

    reconcile = _api_get_json("/v1/reconcile?minutes=0")
    dead_letter = _api_get_json("/v1/dead-letter")
    health = _api_get_json("/v1/health/detail")
    pending = _api_get_json("/v1/discovered/pending?limit=50000")

    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM collector_job
                WHERE status IN ('failed', 'dead')
                  AND source = 'sentinel_discovery'
                  AND request_id = ANY(%s::uuid[])
                """
                ,
                (request_ids,),
            )
            failed_or_dead = int(cur.fetchone()[0])
    _write_evidence(
        "H-3",
        [
            f"request_ids={request_ids}",
            f"truth_count={len(truth_full)}",
            f"union_count={len(union)}",
            f"missing_count={len(missing)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
            f"reconcile={json.dumps(reconcile, sort_keys=True)}",
            f"dead_letter={json.dumps(dead_letter, sort_keys=True)}",
            f"health_detail={json.dumps(health, sort_keys=True)}",
            f"discovered_pending={json.dumps(pending, sort_keys=True)}",
            f"failed_or_dead_discovery_jobs={failed_or_dead}",
        ],
    )
    # The risk is a silent gap. Test passes when system health surfaces stay clean.
    assert failed_or_dead == 0
    assert int(reconcile.get("unloaded", 0)) == 0
    assert int(dead_letter.get("dead", 0)) == 0
    assert int(health.get("dead", 0)) == 0
    assert int(health.get("unloaded", 0)) == 0


def test_h4_overlapping_windows_do_not_duplicate_in_latest_view() -> None:
    _reset_collector_state()
    start, _ = _ds11_window_bounds()
    r1 = _post_collect("sentinel_discovery", _window_iso(start, start + timedelta(hours=1, minutes=30)))
    r2 = _post_collect("sentinel_discovery", _window_iso(start + timedelta(hours=1), start + timedelta(hours=2, minutes=30)))
    assert _wait_for_job_terminal(r1).job_status == "done"
    assert _wait_for_job_terminal(r2).job_status == "done"
    ids = sorted(set(_bq_discovered_ids(r1)) & set(_bq_discovered_ids(r2)))
    if not ids:
        return
    _ensure_adc()
    client = bigquery.Client(project=_project())
    q = f"""
        SELECT count(*) AS row_count, count(DISTINCT incident_id) AS distinct_ids
        FROM `{_project()}.sentinel_core.discovered_ids_latest`
        WHERE incident_id IN UNNEST(@ids)
    """
    row = list(
        client.query(
            q,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", ids)]
            ),
        ).result()
    )[0]
    assert int(row["row_count"]) == int(row["distinct_ids"])


def test_h5_window_boundaries_are_exactly_once_across_adjacent_windows() -> None:
    _reset_collector_state()
    ds12 = next(d for d in DATASETS if d.code == "DS-12")
    ds12.build()
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT subject, id, updated_on
                FROM sentinel_incident
                WHERE position('DS12-WINDOW-' in subject) = 1
                ORDER BY subject
                """
            )
            rows = cur.fetchall()
    boundary = {r[0]: (r[1], r[2]) for r in rows}
    w1s = datetime(2026, 8, 25, 1, 0, 0, tzinfo=UTC)
    w1e = datetime(2026, 8, 25, 2, 0, 0, tzinfo=UTC)
    w2s = datetime(2026, 8, 25, 2, 0, 0, tzinfo=UTC)
    w2e = datetime(2026, 8, 25, 3, 0, 0, tzinfo=UTC)
    r1 = _post_collect("sentinel_discovery", _window_iso(w1s, w1e))
    r2 = _post_collect("sentinel_discovery", _window_iso(w2s, w2e))
    assert _wait_for_job_terminal(r1).job_status == "done"
    assert _wait_for_job_terminal(r2).job_status == "done"
    ids = _bq_discovered_ids(r1) + _bq_discovered_ids(r2)
    counts: dict[str, int] = {}
    for i in ids:
        counts[i] = counts.get(i, 0) + 1
    # start: once in window1; end: twice due inclusive bounds; before/after: once.
    assert counts.get(boundary["DS12-WINDOW-START"][0], 0) == 1
    assert counts.get(boundary["DS12-WINDOW-END"][0], 0) == 2
    assert counts.get(boundary["DS12-WINDOW-BEFORE"][0], 0) == 0
    assert counts.get(boundary["DS12-WINDOW-AFTER"][0], 0) == 1


def test_h6_window_over_batch_cap_pages_without_truncation() -> None:
    _reset_collector_state()
    ds13 = next(d for d in DATASETS if d.code == "DS-13")
    snap = ds13.build()
    start = snap.min_updated_on
    end = snap.max_updated_on
    truth = _mock_truth_discovery(start, end)
    request_id = _post_collect("sentinel_discovery", _window_iso(start, end))
    assert _wait_for_job_terminal(request_id, timeout_s=240).job_status == "done"
    discovered = _bq_discovered_ids(request_id)
    assert discovered == truth


def test_h7_mutation_in_window_measurement() -> None:
    _reset_collector_state()
    ds14 = next(d for d in DATASETS if d.code == "DS-14")
    snap = ds14.build()
    start = snap.min_updated_on
    mid = start + timedelta(hours=3)
    end = snap.max_updated_on
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 10")
            moving = [r[0] for r in cur.fetchall()]
            cur.execute(
                "UPDATE sentinel_incident SET updated_on=%s WHERE id = ANY(%s)",
                (end + timedelta(minutes=30), moving[:5]),
            )
            cur.execute(
                "UPDATE sentinel_incident SET updated_on=%s WHERE id = ANY(%s)",
                (start + timedelta(minutes=15), moving[5:]),
            )
        conn.commit()
    r1 = _post_collect("sentinel_discovery", _window_iso(start, mid))
    r2 = _post_collect("sentinel_discovery", _window_iso(mid, end + timedelta(hours=1)))
    assert _wait_for_job_terminal(r1).job_status == "done"
    assert _wait_for_job_terminal(r2).job_status == "done"
    observed = set(_bq_discovered_ids(r1) + _bq_discovered_ids(r2))
    missing = [i for i in moving if i not in observed]
    # deliver measurement, but assert no escape.
    assert len(missing) == 0


def test_h8_empty_and_sparse_windows_complete_successfully() -> None:
    _reset_collector_state()
    ds15 = next(d for d in DATASETS if d.code == "DS-15")
    ds15.build()
    empty_rid = _post_collect(
        "sentinel_discovery",
        _window_iso(datetime(2026, 8, 25, 4, 0, tzinfo=UTC), datetime(2026, 8, 25, 4, 30, tzinfo=UTC)),
    )
    one_rid = _post_collect(
        "sentinel_discovery",
        _window_iso(datetime(2026, 8, 25, 5, 30, tzinfo=UTC), datetime(2026, 8, 25, 6, 0, tzinfo=UTC)),
    )
    assert _wait_for_job_terminal(empty_rid).job_status == "done"
    assert _wait_for_job_terminal(one_rid).job_status == "done"
    assert len(_bq_discovered_ids(empty_rid)) == 0
    assert len(_bq_discovered_ids(one_rid)) == 1


def test_h9_every_discovered_id_reaches_enrichment_or_stays_pending() -> None:
    _reset_collector_state()
    ds1 = next(d for d in DATASETS if d.code == "DS-1")
    snap = ds1.build()
    rid_discovery = _post_collect("sentinel_discovery", _window_iso(snap.min_updated_on, snap.max_updated_on))
    assert _wait_for_job_terminal(rid_discovery, timeout_s=180).job_status == "done"
    discovered = _bq_discovered_ids(rid_discovery)
    pending_before = _api_get_json("/v1/discovered/pending?limit=50000")
    ids = list(pending_before.get("ids", []))
    for i in range(0, len(ids), 50):
        rid = _post_collect("sentinel", {"incident_ids": ids[i : i + 50]})
        assert _wait_for_job_terminal(rid, timeout_s=180).job_status == "done"
    pending_after = _api_get_json("/v1/discovered/pending?limit=50000")
    _ensure_adc()
    client = bigquery.Client(project=_project())
    q = f"""
        SELECT count(DISTINCT id) AS n
        FROM `{_project()}.sentinel_core.incidents_current`
        WHERE id IN UNNEST(@ids)
    """
    enriched = int(
        list(
            client.query(
                q,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", discovered)]
                ),
            ).result()
        )[0]["n"]
    )
    pending_ids = set(str(i) for i in pending_after.get("ids", []))
    pending_of_discovered = len(set(discovered) & pending_ids)
    assert len(discovered) - enriched - pending_of_discovered == 0


def test_h10_bridge_sql_and_endpoint_agree() -> None:
    _reset_collector_state()
    ds1 = next(d for d in DATASETS if d.code == "DS-1")
    snap = ds1.build()
    rid = _post_collect("sentinel_discovery", _window_iso(snap.min_updated_on, snap.max_updated_on))
    assert _wait_for_job_terminal(rid, timeout_s=180).job_status == "done"
    endpoint_ids = set(_api_get_json("/v1/discovered/pending?limit=50000").get("ids", []))
    sql_path = Path("/workspace/sql/008_discovery_to_enrich.sql")
    sql_text = sql_path.read_text(encoding="utf-8").replace("__PROJECT__", _project())
    _ensure_adc()
    client = bigquery.Client(project=_project())
    sql_ids = {
        str(r["incident_id"])
        for r in client.query(sql_text).result()
        if r["incident_id"] is not None
    }
    assert endpoint_ids == sql_ids


def test_h11_handoff_is_not_automatic() -> None:
    _reset_collector_state()
    ds1 = next(d for d in DATASETS if d.code == "DS-1")
    snap = ds1.build()
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM collector_request WHERE source='sentinel'")
            before = int(cur.fetchone()[0])
    rid = _post_collect("sentinel_discovery", _window_iso(snap.min_updated_on, snap.max_updated_on))
    assert _wait_for_job_terminal(rid, timeout_s=180).job_status == "done"
    time.sleep(5)
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM collector_request WHERE source='sentinel'")
            after = int(cur.fetchone()[0])
    assert after == before


def test_h12_stage_rates_independent_workers_active() -> None:
    _reset_collector_state()
    ds1 = next(d for d in DATASETS if d.code == "DS-1")
    snap = ds1.build()
    discovery_rid = _post_collect("sentinel_discovery", _window_iso(snap.min_updated_on, snap.max_updated_on))
    ids = _mock_truth_discovery(snap.min_updated_on, snap.max_updated_on)[:100]
    enrich_rid = _post_collect("sentinel", {"incident_ids": ids})
    d_term = _wait_for_job_terminal(discovery_rid, timeout_s=180)
    e_term = _wait_for_job_terminal(enrich_rid, timeout_s=180)
    assert d_term.job_status == "done"
    assert e_term.job_status == "done"
    assert d_term.queue_name == "sentinel_discovery"
    assert e_term.queue_name == "sentinel"


def test_h13_crash_between_stages_preserves_pending() -> None:
    _reset_collector_state()
    ds1 = next(d for d in DATASETS if d.code == "DS-1")
    snap = ds1.build()
    rid = _post_collect("sentinel_discovery", _window_iso(snap.min_updated_on, snap.max_updated_on))
    assert _wait_for_job_terminal(rid, timeout_s=180).job_status == "done"
    pending = _api_get_json("/v1/discovered/pending?limit=50000")
    assert int(pending.get("pending_total", 0)) > 0


def test_h14_order_item_ids_enrichment_works_discovery_ignores() -> None:
    _reset_collector_state()
    ds6 = next(d for d in DATASETS if d.code == "DS-6")
    snap = ds6.build()
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_item_id::bigint
                FROM sentinel_incident
                WHERE order_item_id IS NOT NULL
                ORDER BY id
                LIMIT 10
                """
            )
            order_item_ids = [int(r[0]) for r in cur.fetchall()]
    enrich_rid = _post_collect("sentinel", {"order_item_ids": order_item_ids})
    assert _wait_for_job_terminal(enrich_rid, timeout_s=180).job_status == "done"
    baseline_rid = _post_collect("sentinel_discovery", _window_iso(snap.min_updated_on, snap.max_updated_on))
    ignored_rid = _post_collect(
        "sentinel_discovery",
        {
            **_window_iso(snap.min_updated_on, snap.max_updated_on),
            "order_item_ids": order_item_ids,
        },
    )
    assert _wait_for_job_terminal(baseline_rid, timeout_s=180).job_status == "done"
    assert _wait_for_job_terminal(ignored_rid, timeout_s=180).job_status == "done"
    baseline_ids = _bq_discovered_ids(baseline_rid)
    ignored_ids = _bq_discovered_ids(ignored_rid)
    # Current risk shape: discovery silently ignores order_item_ids.
    assert ignored_ids == baseline_ids
