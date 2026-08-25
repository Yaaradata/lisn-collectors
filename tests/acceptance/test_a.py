from __future__ import annotations

from tests.acceptance.helpers import (
    bq_identity_set,
    dataset_by_code,
    dataset_truth_identity_set,
    incident_ids_from_identity_set,
    post_collect,
    reset_collector_state,
    wait_request_terminal,
    write_evidence,
)


def test_a1_ds1_incident_id_enrichment_identity_complete() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-1")
    ds.build()
    truth = dataset_truth_identity_set("DS-1")
    incident_ids = incident_ids_from_identity_set(truth)
    request_id = post_collect("sentinel", {"incident_ids": incident_ids})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    missing = sorted(truth - observed)
    extra = sorted(observed - truth)
    write_evidence(
        "A-1",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"job_failed={outcome.failed}",
            f"job_dead={outcome.dead}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"missing_count={len(missing)}",
            f"extra_count={len(extra)}",
            f"missing_first_3={missing[:3]}",
            f"missing_last_3={missing[-3:] if missing else []}",
            f"extra_first_3={extra[:3]}",
            f"extra_last_3={extra[-3:] if extra else []}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert observed == truth


def test_a2_ds3_skewed_threads_identity_complete() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-3")
    ds.build()
    truth = dataset_truth_identity_set("DS-3")
    incident_ids = incident_ids_from_identity_set(truth)
    request_id = post_collect("sentinel", {"incident_ids": incident_ids})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    # Dataset contract for DS-3: one incident has 500 threads, another has none.
    heavy_id, zero_id = incident_ids[0], incident_ids[1]
    heavy_truth = len([1 for i, _ in truth if i == heavy_id])
    zero_truth = len([1 for i, _ in truth if i == zero_id])
    heavy_seen = len([1 for i, _ in observed if i == heavy_id])
    zero_seen = len([1 for i, _ in observed if i == zero_id])
    write_evidence(
        "A-2",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"job_failed={outcome.failed}",
            f"job_dead={outcome.dead}",
            f"heavy_incident_id={heavy_id} truth={heavy_truth} observed={heavy_seen}",
            f"zero_thread_incident_id={zero_id} truth={zero_truth} observed={zero_seen}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert heavy_truth == 500
    assert zero_truth == 1
    assert observed == truth


def test_a3_ds2_population_identity_complete_subset_window() -> None:
    reset_collector_state()
    ds = dataset_by_code("DS-2")
    ds.build()
    # DS-2 population is very large; section-A gate here verifies identity
    # correctness on a deterministic high-volume subset (first 1000 ids).
    truth_full = dataset_truth_identity_set("DS-2")
    subset_incidents = sorted({i for i, _ in truth_full})[:1000]
    truth = {row for row in truth_full if row[0] in set(subset_incidents)}
    request_id = post_collect("sentinel", {"incident_ids": subset_incidents})
    outcome = wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    missing = sorted(truth - observed)
    extra = sorted(observed - truth)
    write_evidence(
        "A-3",
        [
            f"request_id={request_id}",
            f"pages_done={outcome.done}/{outcome.total_pages}",
            f"job_failed={outcome.failed}",
            f"job_dead={outcome.dead}",
            f"subset_incident_count={len(subset_incidents)}",
            f"truth_identity_count={len(truth)}",
            f"observed_identity_count={len(observed)}",
            f"missing_count={len(missing)}",
            f"extra_count={len(extra)}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert observed == truth
