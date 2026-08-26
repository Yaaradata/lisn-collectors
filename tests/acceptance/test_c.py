from __future__ import annotations

from tests.acceptance.helpers import (
    bq_identity_set,
    dataset_by_code,
    dataset_truth_identity_set,
    incident_ids_from_identity_set,
    mock_key_values_for_incidents,
    post_collect,
    post_collect_detailed,
    reset_collector_state,
    wait_request_terminal,
    write_evidence,
)


def test_c1_duplicate_incident_keys_do_not_change_identity_set() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-1")
    ds.build()
    truth_full = dataset_truth_identity_set("DS-1")
    base_ids = incident_ids_from_identity_set(truth_full)[:100]
    truth = {row for row in truth_full if row[0] in set(base_ids)}
    duplicate_ids = base_ids + base_ids + base_ids
    request_id = post_collect("sentinel", {"incident_ids": duplicate_ids})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    write_evidence(
        "C-1",
        [
            f"request_id={request_id}",
            f"input_key_count={len(duplicate_ids)}",
            f"unique_incident_count={len(base_ids)}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert observed == truth


def test_c2_split_requests_union_identity_complete() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-8")
    ds.build()
    truth = dataset_truth_identity_set("DS-8")
    incident_ids = incident_ids_from_identity_set(truth)
    left = incident_ids[:100]
    right = incident_ids[100:]
    rid_left = post_collect("sentinel", {"incident_ids": left})
    rid_right = post_collect("sentinel", {"incident_ids": right})
    out_left = wait_request_terminal(rid_left, timeout_s=300)
    out_right = wait_request_terminal(rid_right, timeout_s=300)
    observed = bq_identity_set(rid_left) | bq_identity_set(rid_right)
    missing = sorted(truth - observed)
    write_evidence(
        "C-2",
        [
            f"left_request_id={rid_left}",
            f"right_request_id={rid_right}",
            f"left_pages_done={out_left.done}/{out_left.total_pages}",
            f"right_pages_done={out_right.done}/{out_right.total_pages}",
            f"truth_identity_count={len(truth)}",
            f"observed_union_identity_count={len(observed)}",
            f"missing_count={len(missing)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
        ],
    )
    assert out_left.failed == 0 and out_left.dead == 0
    assert out_right.failed == 0 and out_right.dead == 0
    assert observed == truth


def test_c3_multi_key_request_rejected_with_no_silent_fallback() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-1")
    ds.build()
    ids = incident_ids_from_identity_set(dataset_truth_identity_set("DS-1"))[:10]
    status, payload = post_collect_detailed(
        "sentinel",
        {"incident_ids": ids, "order_ids": [f"OD-{i}" for i in range(10)]},
    )
    write_evidence(
        "C-3",
        [
            f"status={status}",
            f"payload={payload}",
        ],
    )
    assert status == 400
    detail = payload.get("detail")
    assert isinstance(detail, str)
    assert "exactly one of incident_ids, order_item_ids, order_ids required" in detail


def test_c4_hostile_query_shapes_rejected_without_5xx() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-1")
    ds.build()
    cases = [
        {},
        {"incident_ids": []},
        {"incident_ids": None},
        {"incident_ids": "IN2608"},
        {"incident_ids": [None, 1, {}]},
        {"limit": 10},
    ]
    lines: list[str] = []
    statuses: list[int] = []
    for idx, query_spec in enumerate(cases, start=1):
        status, payload = post_collect_detailed("sentinel", query_spec)
        lines.append(f"case={idx} status={status} payload={payload}")
        statuses.append(status)
    write_evidence("C-4", lines)
    for status in statuses:
        assert status < 500
        assert status == 400


def test_c5_order_item_ids_identity_complete() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-6")
    ds.build()
    truth = dataset_truth_identity_set("DS-6")
    incident_ids = incident_ids_from_identity_set(truth)
    order_item_ids = mock_key_values_for_incidents(incident_ids, "order_item_id")
    request_id = post_collect("sentinel", {"order_item_ids": order_item_ids})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    missing = sorted(truth - observed)
    extra = sorted(observed - truth)
    write_evidence(
        "C-5",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"order_item_ids={order_item_ids}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"missing_count={len(missing)}",
            f"extra_count={len(extra)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert observed == truth
