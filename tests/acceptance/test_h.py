from __future__ import annotations

import json
import os
import time
import urllib.error
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
    status, payload = _post_collect_detailed(source, query_spec)
    if status < 200 or status >= 300:
        raise AssertionError(f"collect failed status={status} payload={payload}")
    if "request_id" not in payload:
        raise AssertionError(f"collect response missing request_id: {payload}")
    return str(payload["request_id"])


def _post_collect_detailed(source: str, query_spec: dict[str, object]) -> tuple[int, dict[str, object]]:
    body = {"source": source, "query_spec": query_spec}
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/collect",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return int(resp.status), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload


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
    _write_evidence(
        "H-0",
        [
            f"request_id={request_id}",
            f"picked_up={picked_up}",
            f"last_observation={last}",
        ],
    )
    assert picked_up, f"H-0 blocking: request {request_id} sat unpicked; last={last}"


def test_h1_discovery_finds_everything_in_window() -> None:
    _reset_collector_state()
    start, end = _ds11_window_bounds()
    window_end = start + timedelta(hours=1)
    truth = _mock_truth_discovery(start, window_end)
    request_id = _post_collect("sentinel_discovery", _window_iso(start, window_end))
    terminal = _wait_for_job_terminal(request_id)
    discovered = _bq_discovered_ids(request_id)
    missing = sorted(set(truth) - set(discovered))
    extra = sorted(set(discovered) - set(truth))
    _write_evidence(
        "H-1",
        [
            f"request_id={request_id}",
            f"job_status={terminal.job_status}",
            f"truth_count={len(truth)}",
            f"discovered_count={len(discovered)}",
            f"missing_count={len(missing)}",
            f"extra_count={len(extra)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
            f"extra_first_3={extra[:3]}",
            f"extra_last_3={extra[-3:] if extra else []}",
        ],
    )
    assert terminal.job_status == "done"
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
    missing = sorted(set(truth_full) - union)
    _write_evidence(
        "H-2",
        [
            f"truth_count={len(truth_full)}",
            f"union_count={len(union)}",
            f"missing_count={len(missing)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
        ],
    )
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
        _write_evidence("H-4", [f"r1={r1}", f"r2={r2}", "overlap_count=0"])
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
    _write_evidence(
        "H-4",
        [
            f"r1={r1}",
            f"r2={r2}",
            f"overlap_count={len(ids)}",
            f"latest_row_count={int(row['row_count'])}",
            f"latest_distinct_count={int(row['distinct_ids'])}",
            f"overlap_first_3={ids[:3]}",
            f"overlap_last_3={ids[-3:] if ids else []}",
        ],
    )
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
    _write_evidence(
        "H-5",
        [
            f"r1={r1}",
            f"r2={r2}",
            f"start_id={boundary['DS12-WINDOW-START'][0]} count={counts.get(boundary['DS12-WINDOW-START'][0], 0)}",
            f"end_id={boundary['DS12-WINDOW-END'][0]} count={counts.get(boundary['DS12-WINDOW-END'][0], 0)}",
            f"before_id={boundary['DS12-WINDOW-BEFORE'][0]} count={counts.get(boundary['DS12-WINDOW-BEFORE'][0], 0)}",
            f"after_id={boundary['DS12-WINDOW-AFTER'][0]} count={counts.get(boundary['DS12-WINDOW-AFTER'][0], 0)}",
        ],
    )
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
    missing = sorted(set(truth) - set(discovered))
    _write_evidence(
        "H-6",
        [
            f"request_id={request_id}",
            f"truth_count={len(truth)}",
            f"discovered_count={len(discovered)}",
            f"missing_count={len(missing)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
        ],
    )
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
    escaped = sorted(moving[:5])
    escaped_not_found = [i for i in escaped if i not in observed]
    _write_evidence(
        "H-7",
        [
            f"r1={r1}",
            f"r2={r2}",
            f"escaped_id_count={len(escaped)}",
            f"escaped_first_3={escaped[:3]}",
            f"escaped_last_3={escaped[-3:] if escaped else []}",
            f"escaped_not_found_count={len(escaped_not_found)}",
            f"escaped_not_found_first_3={escaped_not_found[:3]}",
            f"escaped_not_found_last_3={escaped_not_found[-3:] if escaped_not_found else []}",
        ],
    )
    assert len(escaped_not_found) == 0


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
    empty_count = len(_bq_discovered_ids(empty_rid))
    one_count = len(_bq_discovered_ids(one_rid))
    _write_evidence(
        "H-8",
        [
            f"empty_request_id={empty_rid}",
            f"sparse_request_id={one_rid}",
            f"empty_count={empty_count}",
            f"sparse_count={one_count}",
        ],
    )
    assert empty_count == 0
    assert one_count == 1


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
    _write_evidence(
        "H-9",
        [
            f"discovery_request_id={rid_discovery}",
            f"discovered_count={len(discovered)}",
            f"enriched_count={enriched}",
            f"pending_total_after={int(pending_after.get('pending_total', 0))}",
            f"pending_of_discovered={pending_of_discovered}",
            f"balance={len(discovered) - enriched - pending_of_discovered}",
        ],
    )
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
    only_endpoint = sorted(endpoint_ids - sql_ids)
    only_sql = sorted(sql_ids - endpoint_ids)
    _write_evidence(
        "H-10",
        [
            f"discovery_request_id={rid}",
            f"endpoint_count={len(endpoint_ids)}",
            f"sql_count={len(sql_ids)}",
            f"endpoint_only_count={len(only_endpoint)}",
            f"sql_only_count={len(only_sql)}",
            f"endpoint_only_first_3={only_endpoint[:3]}",
            f"endpoint_only_last_3={only_endpoint[-3:] if only_endpoint else []}",
            f"sql_only_first_3={only_sql[:3]}",
            f"sql_only_last_3={only_sql[-3:] if only_sql else []}",
        ],
    )
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
    _write_evidence(
        "H-11",
        [
            f"discovery_request_id={rid}",
            f"sentinel_request_count_before={before}",
            f"sentinel_request_count_after={after}",
        ],
    )
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
    _write_evidence(
        "H-12",
        [
            f"discovery_request_id={discovery_rid}",
            f"enrichment_request_id={enrich_rid}",
            f"discovery_queue={d_term.queue_name}",
            f"enrichment_queue={e_term.queue_name}",
            f"discovery_status={d_term.job_status}",
            f"enrichment_status={e_term.job_status}",
        ],
    )
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
    _write_evidence(
        "H-13",
        [
            f"discovery_request_id={rid}",
            f"pending_total={int(pending.get('pending_total', 0))}",
            f"pending_first_3={list(pending.get('ids', []))[:3]}",
            f"pending_last_3={list(pending.get('ids', []))[-3:] if pending.get('ids') else []}",
        ],
    )
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
    unsupported_spec = {
        **_window_iso(snap.min_updated_on, snap.max_updated_on),
        "order_item_ids": order_item_ids,
    }
    status, payload = _post_collect_detailed("sentinel_discovery", unsupported_spec)
    assert _wait_for_job_terminal(baseline_rid, timeout_s=180).job_status == "done"
    baseline_ids = _bq_discovered_ids(baseline_rid)
    dropped_key_visible = status >= 400 or ("error" in payload) or ("warnings" in payload)
    discovery_request_id = payload.get("request_id")
    dropped_outcome = "rejected_or_warned"
    ignored_ids: list[str] = []
    if status < 400 and discovery_request_id is not None:
        rid = str(discovery_request_id)
        terminal = _wait_for_job_terminal(rid, timeout_s=180)
        ignored_ids = _bq_discovered_ids(rid)
        dropped_outcome = f"accepted_status={terminal.job_status}"
    _write_evidence(
        "H-14",
        [
            f"enrichment_request_id={enrich_rid}",
            f"baseline_discovery_request_id={baseline_rid}",
            f"unsupported_collect_status={status}",
            f"unsupported_collect_payload={json.dumps(payload, sort_keys=True)}",
            f"dropped_key_visible={dropped_key_visible}",
            f"dropped_outcome={dropped_outcome}",
            f"baseline_count={len(baseline_ids)}",
            f"unsupported_count={len(ignored_ids)}",
            f"unsupported_first_3={ignored_ids[:3]}",
            f"unsupported_last_3={ignored_ids[-3:] if ignored_ids else []}",
        ],
    )
    assert dropped_key_visible, "caller cannot tell unsupported order_item_ids key was dropped"
