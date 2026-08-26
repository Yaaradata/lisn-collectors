from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from google.cloud import bigquery

API_URL = "https://collector-api-mfo5qzthxa-el.a.run.app"
MOCK_URL = "https://mock-sentinel-mfo5qzthxa-el.a.run.app"
PROJECT = "clariversev1"
EVIDENCE_DIR = Path("/workspace/tests/deployed/evidence")


@dataclass(frozen=True)
class RequestCounts:
    done: int
    failed: int
    dead: int
    records: int


def _token(audience: str) -> str:
    import subprocess

    return subprocess.check_output(
        ["gcloud", "auth", "print-identity-token", "--audiences=" + audience],
        text=True,
    ).strip()


def _http_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _write_evidence(test_id: str, lines: list[str]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{test_id}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mock_search(
    mock_token: str,
    *,
    incident_ids: list[str] | None = None,
    order_item_ids: list[int] | None = None,
    order_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if incident_ids is not None:
        payload["incident_ids"] = incident_ids
    if order_item_ids is not None:
        payload["order_item_ids"] = order_item_ids
    if order_ids is not None:
        payload["order_ids"] = order_ids
    return _http_json("POST", MOCK_URL + "/v1/incidents/search", mock_token, payload)


def _mock_discover_page(
    mock_token: str,
    *,
    updated_from: str,
    updated_to: str,
    limit: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "updated_from": updated_from,
        "updated_to": updated_to,
        "limit": limit,
    }
    if cursor:
        payload["cursor"] = cursor
    return _http_json("POST", MOCK_URL + "/v1/incidents/discover", mock_token, payload)


def _mock_discover_all_ids(
    mock_token: str,
    *,
    updated_from: str,
    updated_to: str,
    limit: int = 1000,
    max_pages: int | None = None,
) -> list[str]:
    out: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = _mock_discover_page(
            mock_token,
            updated_from=updated_from,
            updated_to=updated_to,
            limit=limit,
            cursor=cursor,
        )
        ids = [str(x) for x in page.get("incident_ids", [])]
        out.extend(ids)
        pages += 1
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
        if max_pages is not None and pages >= max_pages:
            break
    return out


def _idents_from_mock_incidents(incidents: list[dict[str, Any]]) -> set[tuple[str, str | None]]:
    out: set[tuple[str, str | None]] = set()
    # Deployed mock returns thread-exploded flat rows with dotted keys:
    # one row per identity, e.g. {"id": "...", "threads.id": "..."}.
    for row in incidents:
        inc_id = str(row["id"])
        thread_val = row.get("threads.id")
        out.add((inc_id, str(thread_val) if thread_val is not None else None))
    return out


def _truth_identities_for_incident_ids(mock_token: str, incident_ids: list[str]) -> set[tuple[str, str | None]]:
    out: set[tuple[str, str | None]] = set()
    for i in range(0, len(incident_ids), 50):
        chunk = incident_ids[i : i + 50]
        payload = _mock_search(mock_token, incident_ids=chunk)
        out |= _idents_from_mock_incidents(payload.get("incidents", []))
    return out


def _collect(api_token: str, source: str, query_spec: dict[str, Any]) -> str:
    payload = {"source": source, "query_spec": query_spec}
    resp = _http_json("POST", API_URL + "/v1/collect", api_token, payload)
    rid = resp.get("request_id")
    if not rid:
        raise AssertionError(f"collect missing request_id: {resp}")
    return str(rid)


def _collect_with_pages(api_token: str, source: str, query_spec: dict[str, Any]) -> tuple[str, int]:
    payload = {"source": source, "query_spec": query_spec}
    resp = _http_json("POST", API_URL + "/v1/collect", api_token, payload)
    rid = resp.get("request_id")
    total_pages = resp.get("total_pages")
    if not rid or total_pages is None:
        raise AssertionError(f"collect missing request_id/total_pages: {resp}")
    return str(rid), int(total_pages)


def _counts(api_token: str, request_id: str) -> RequestCounts:
    resp = _http_json("GET", API_URL + f"/v1/requests/{request_id}/counts", api_token)
    c = resp.get("counts", {})
    return RequestCounts(
        done=int(c.get("done", 0)),
        failed=int(c.get("failed", 0)),
        dead=int(c.get("dead", 0)),
        records=int(resp.get("records", 0)),
    )


def _wait_terminal(api_token: str, request_id: str, timeout_s: int = 900) -> RequestCounts:
    deadline = time.time() + timeout_s
    last = RequestCounts(0, 0, 0, 0)
    while time.time() < deadline:
        last = _counts(api_token, request_id)
        if last.done + last.failed + last.dead >= 1:
            return last
        time.sleep(2)
    raise AssertionError(f"request {request_id} not terminal within {timeout_s}s; last={last}")


def _wait_terminal_total_pages(
    api_token: str,
    request_id: str,
    total_pages: int,
    timeout_s: int = 900,
) -> RequestCounts:
    deadline = time.time() + timeout_s
    last = RequestCounts(0, 0, 0, 0)
    while time.time() < deadline:
        last = _counts(api_token, request_id)
        if last.done + last.failed + last.dead >= total_pages:
            return last
        time.sleep(2)
    raise AssertionError(
        f"request {request_id} not terminal within {timeout_s}s for total_pages={total_pages}; last={last}"
    )


def _bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def _bq_identities_for_request(request_id: str) -> set[tuple[str, str | None]]:
    client = _bq_client()
    sql = f"""
    SELECT id, threads_id
    FROM `{PROJECT}.sentinel_raw.incidents`
    WHERE _request_id = @rid
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
        ),
    )
    out: set[tuple[str, str | None]] = set()
    for row in job.result():
        out.add((str(row["id"]), str(row["threads_id"]) if row["threads_id"] is not None else None))
    return out


def _bq_discovered_ids_for_request(request_id: str) -> set[str]:
    client = _bq_client()
    sql = f"""
    SELECT incident_id
    FROM `{PROJECT}.sentinel_raw.discovered_ids`
    WHERE _request_id = @rid
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
        ),
    )
    return {str(row["incident_id"]) for row in job.result()}


def _bq_current_identity_count() -> int:
    client = _bq_client()
    sql = f"SELECT COUNT(*) AS c FROM `{PROJECT}.sentinel_core.incidents_current`"
    rows = list(client.query(sql).result())
    return int(rows[0]["c"])


def _bq_current_ids_for_filter(ids: set[str]) -> set[str]:
    if not ids:
        return set()
    client = _bq_client()
    sql = f"""
    SELECT DISTINCT id
    FROM `{PROJECT}.sentinel_core.incidents_current`
    WHERE id IN UNNEST(@ids)
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", sorted(ids))]
        ),
    )
    return {str(row["id"]) for row in job.result()}


def _bq_row_key_count_for_request(request_id: str) -> int:
    client = _bq_client()
    sql = f"""
    SELECT TO_JSON_STRING(t) AS row_json
    FROM `{PROJECT}.sentinel_raw.incidents` AS t
    WHERE _request_id = @rid
    LIMIT 1
    """
    rows = list(
        client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
            ),
        ).result()
    )
    if not rows:
        return 0
    obj = json.loads(rows[0]["row_json"])
    return len(obj.keys())


@pytest.fixture(scope="module")
def tokens() -> dict[str, str]:
    return {
        "api": _token(API_URL),
        "mock": _token(MOCK_URL),
    }


def test_a1_single_incident_row_and_fields(tokens: dict[str, str]) -> None:
    ids = _mock_discover_all_ids(
        tokens["mock"], updated_from="2026-08-20T00:00:00Z", updated_to="2026-08-27T00:00:00Z", limit=1, max_pages=1
    )
    assert ids, "mock discover returned no ids"
    incident_id = ids[0]
    truth = _truth_identities_for_incident_ids(tokens["mock"], [incident_id])
    request_id, total_pages = _collect_with_pages(tokens["api"], "sentinel", {"incident_ids": [incident_id]})
    counts = _wait_terminal_total_pages(tokens["api"], request_id, total_pages=total_pages, timeout_s=300)
    observed = _bq_identities_for_request(request_id)
    key_count = _bq_row_key_count_for_request(request_id)
    print(f"A-1 truth_identity_count={len(truth)} observed_identity_count={len(observed)} request_id={request_id}")
    _write_evidence(
        "A-1",
        [
            f"incident_id={incident_id}",
            f"request_id={request_id}",
            f"total_pages={total_pages}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"request_counts={counts}",
            f"row_key_count={key_count}",
        ],
    )
    assert counts.failed == 0 and counts.dead == 0
    assert observed == truth
    assert key_count >= 34


def test_a2_thousand_incidents_identity_equality(tokens: dict[str, str]) -> None:
    ids = _mock_discover_all_ids(
        tokens["mock"], updated_from="2026-08-20T00:00:00Z", updated_to="2026-08-27T00:00:00Z", limit=1000, max_pages=1
    )
    assert len(ids) == 1000, f"expected 1000 ids, got {len(ids)}"
    truth = _truth_identities_for_incident_ids(tokens["mock"], ids)
    request_id, total_pages = _collect_with_pages(tokens["api"], "sentinel", {"incident_ids": ids})
    counts = _wait_terminal_total_pages(tokens["api"], request_id, total_pages=total_pages, timeout_s=1800)
    observed = _bq_identities_for_request(request_id)
    print(f"A-2 truth_identity_count={len(truth)} observed_identity_count={len(observed)} request_id={request_id}")
    _write_evidence(
        "A-2",
        [
            f"request_id={request_id}",
            f"total_pages={total_pages}",
            f"input_ids={len(ids)}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"request_counts={counts}",
        ],
    )
    assert counts.failed == 0 and counts.dead == 0
    assert observed == truth


def test_a3_population_scale_identity_count(tokens: dict[str, str]) -> None:
    started = time.time()
    ids = _mock_discover_all_ids(
        tokens["mock"], updated_from="2026-08-20T00:00:00Z", updated_to="2026-08-27T00:00:00Z", limit=1000
    )
    assert ids, "expected non-empty deployed mock population"
    sample_size = min(5000, len(ids))
    rng = random.Random(20260826)
    sampled_ids = rng.sample(ids, sample_size)
    truth_identity_count = len(_truth_identities_for_incident_ids(tokens["mock"], sampled_ids))
    request_id, total_pages = _collect_with_pages(tokens["api"], "sentinel", {"incident_ids": sampled_ids})
    counts = _wait_terminal_total_pages(tokens["api"], request_id, total_pages=total_pages, timeout_s=3600)
    observed_identity_count = len(_bq_identities_for_request(request_id))
    elapsed_s = time.time() - started
    print(
        f"A-3 sample_size={sample_size} truth_identity_count={truth_identity_count} "
        f"observed_identity_count={observed_identity_count} elapsed_s={elapsed_s:.3f} request_id={request_id}"
    )
    _write_evidence(
        "A-3",
        [
            f"discovered_population_incident_count={len(ids)}",
            f"sample_size={sample_size}",
            f"request_id={request_id}",
            f"total_pages={total_pages}",
            f"request_counts={counts}",
            f"truth_identity_count={truth_identity_count}",
            f"observed_identity_count={observed_identity_count}",
            f"elapsed_seconds={elapsed_s:.6f}",
            "full_population_equality_unmeasured=true",
        ],
    )
    assert observed_identity_count == truth_identity_count


def test_a4_discovery_window_identity_equality(tokens: dict[str, str]) -> None:
    updated_from = "2026-08-22T17:00:00Z"
    updated_to = "2026-08-22T18:00:00Z"
    truth_ids = set(
        _mock_discover_all_ids(
            tokens["mock"],
            updated_from=updated_from,
            updated_to=updated_to,
            limit=1000,
        )
    )
    request_id = _collect(
        tokens["api"],
        "sentinel_discovery",
        {"updated_from": updated_from, "updated_to": updated_to},
    )
    counts = _wait_terminal(tokens["api"], request_id, timeout_s=1200)
    observed_ids = _bq_discovered_ids_for_request(request_id)
    print(f"A-4 truth_identity_count={len(truth_ids)} observed_identity_count={len(observed_ids)} request_id={request_id}")
    _write_evidence(
        "A-4",
        [
            f"request_id={request_id}",
            f"truth_incident_id_count={len(truth_ids)}",
            f"observed_incident_id_count={len(observed_ids)}",
            f"request_counts={counts}",
        ],
    )
    assert counts.failed == 0 and counts.dead == 0
    assert observed_ids == truth_ids


def test_a5_discovery_to_enrichment_balance(tokens: dict[str, str]) -> None:
    updated_from = "2026-08-22T18:00:00Z"
    updated_to = "2026-08-22T19:00:00Z"
    discovery_request_id = _collect(
        tokens["api"],
        "sentinel_discovery",
        {"updated_from": updated_from, "updated_to": updated_to},
    )
    disc_counts = _wait_terminal(tokens["api"], discovery_request_id, timeout_s=1200)
    discovered = _bq_discovered_ids_for_request(discovery_request_id)
    pending_url = API_URL + "/v1/discovered/pending?" + urllib.parse.urlencode({"limit": 20000})
    pending_resp = _http_json("GET", pending_url, tokens["api"])
    pending_ids = {str(x) for x in pending_resp.get("ids", [])}
    pending_of_discovered = sorted(discovered & pending_ids)
    for i in range(0, len(pending_of_discovered), 1000):
        chunk = pending_of_discovered[i : i + 1000]
        rid = _collect(tokens["api"], "sentinel", {"incident_ids": chunk})
        _wait_terminal(tokens["api"], rid, timeout_s=1800)
    enriched_ids = _bq_current_ids_for_filter(discovered)
    pending_resp_after = _http_json("GET", pending_url, tokens["api"])
    pending_after = {str(x) for x in pending_resp_after.get("ids", [])}
    pending_of_discovered_after = discovered & pending_after
    balance = len(discovered - enriched_ids - pending_of_discovered_after)
    print(
        "A-5 truth_identity_count="
        f"{len(discovered)} observed_identity_count={len(enriched_ids)} pending_of_discovered={len(pending_of_discovered_after)} balance={balance}"
    )
    _write_evidence(
        "A-5",
        [
            f"discovery_request_id={discovery_request_id}",
            f"discovery_counts={disc_counts}",
            f"discovered_count={len(discovered)}",
            f"enriched_count={len(enriched_ids)}",
            f"pending_of_discovered_after={len(pending_of_discovered_after)}",
            f"balance={balance}",
        ],
    )
    assert balance == 0


def test_a6_null_thread_incident_survives(tokens: dict[str, str]) -> None:
    ids = _mock_discover_all_ids(
        tokens["mock"], updated_from="2026-08-20T00:00:00Z", updated_to="2026-08-27T00:00:00Z", limit=1000, max_pages=10
    )
    null_thread_id: str | None = None
    for i in range(0, len(ids), 50):
        payload = _mock_search(tokens["mock"], incident_ids=ids[i : i + 50])
        for row in payload.get("incidents", []):
            if row.get("threads.id") is None:
                null_thread_id = str(row["id"])
                break
        if null_thread_id:
            break
    assert null_thread_id is not None, "could not find incident with no threads in sampled deployed mock set"
    truth = {(null_thread_id, None)}
    request_id = _collect(tokens["api"], "sentinel", {"incident_ids": [null_thread_id]})
    counts = _wait_terminal(tokens["api"], request_id, timeout_s=300)
    observed = _bq_identities_for_request(request_id)
    print(f"A-6 truth_identity_count={len(truth)} observed_identity_count={len(observed)} request_id={request_id}")
    _write_evidence(
        "A-6",
        [
            f"incident_id={null_thread_id}",
            f"request_id={request_id}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"request_counts={counts}",
            f"observed={sorted(observed)}",
        ],
    )
    assert counts.failed == 0 and counts.dead == 0
    assert observed == truth


def test_a7_order_item_ids_retrieval(tokens: dict[str, str]) -> None:
    ids = _mock_discover_all_ids(
        tokens["mock"], updated_from="2026-08-20T00:00:00Z", updated_to="2026-08-27T00:00:00Z", limit=1000, max_pages=5
    )
    order_item_id: int | None = None
    for i in range(0, len(ids), 50):
        payload = _mock_search(tokens["mock"], incident_ids=ids[i : i + 50])
        for inc in payload.get("incidents", []):
            val = inc.get("orderItemId")
            if val is not None:
                order_item_id = int(val)
                break
        if order_item_id is not None:
            break
    assert order_item_id is not None, "could not find non-null orderItemId in sample"
    truth_payload = _mock_search(tokens["mock"], order_item_ids=[order_item_id])
    truth = _idents_from_mock_incidents(truth_payload.get("incidents", []))
    request_id = _collect(tokens["api"], "sentinel", {"order_item_ids": [order_item_id]})
    counts = _wait_terminal(tokens["api"], request_id, timeout_s=600)
    observed = _bq_identities_for_request(request_id)
    print(
        f"A-7 truth_identity_count={len(truth)} observed_identity_count={len(observed)} request_id={request_id} order_item_id={order_item_id}"
    )
    _write_evidence(
        "A-7",
        [
            f"order_item_id={order_item_id}",
            f"request_id={request_id}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"request_counts={counts}",
        ],
    )
    assert counts.failed == 0 and counts.dead == 0
    assert observed == truth
