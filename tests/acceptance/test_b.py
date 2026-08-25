from __future__ import annotations

from tests.acceptance.helpers import (
    bq_identity_set,
    dataset_by_code,
    dataset_truth_identity_set,
    incident_ids_from_identity_set,
    mock_key_values_for_incidents,
    post_collect,
    reset_collector_state,
    wait_request_terminal,
    write_evidence,
)


def test_b1_ds4_null_thread_identities_preserved() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-4")
    ds.build()
    truth = dataset_truth_identity_set("DS-4")
    incident_ids = incident_ids_from_identity_set(truth)
    request_id = post_collect("sentinel", {"incident_ids": incident_ids})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    truth_null = sorted([i for i, t in truth if t is None])
    observed_null = sorted([i for i, t in observed if t is None])
    write_evidence(
        "B-1",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"truth_null_identity_count={len(truth_null)}",
            f"observed_null_identity_count={len(observed_null)}",
            f"truth_null_first_3={truth_null[:3]}",
            f"truth_null_last_3={truth_null[-3:] if truth_null else []}",
            f"observed_null_first_3={observed_null[:3]}",
            f"observed_null_last_3={observed_null[-3:] if observed_null else []}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    missing = sorted(truth - observed)
    extra = sorted(observed - truth)
    if observed != truth:
        raise AssertionError(
            f"identity mismatch: missing={len(missing)} extra={len(extra)} "
            f"missing_first_3={missing[:3]} missing_last_3={missing[-3:] if missing else []} "
            f"extra_first_3={extra[:3]} extra_last_3={extra[-3:] if extra else []}"
        )


def test_b2_ds6_order_item_id_query_identity_complete() -> None:
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
        "B-2",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"order_item_ids={order_item_ids}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"missing_count={len(missing)}",
            f"extra_count={len(extra)}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert observed == truth


def test_b3_ds7_collision_cohort_identity_complete() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-7")
    ds.build()
    truth = dataset_truth_identity_set("DS-7")
    incident_ids = incident_ids_from_identity_set(truth)
    order_ids = mock_key_values_for_incidents(incident_ids, "order_id")
    request_id = post_collect("sentinel", {"order_ids": order_ids})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    missing = sorted(truth - observed)
    extra = sorted(observed - truth)
    write_evidence(
        "B-3",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"order_ids={order_ids}",
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
