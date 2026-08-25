"""Protocol gate: prove fake sink behavior before audit usage."""

from __future__ import annotations

import pytest

from collector.contract import Record
from collector.load import append_records
from tests.audit.fakes import FakeBQ, SinkPatch
from tests.audit.helpers import SELFTEST_PROOF_FILE, resolved_table_id, reset_state, write_evidence


def test_fakebq_rejects_unknown_field_with_per_row_error() -> None:
    test_id = "SELF-01"
    reset_state()
    table_id = resolved_table_id()
    with SinkPatch():
        with pytest.raises(RuntimeError) as exc:
            append_records(
                "sentinel_raw.incidents",
                [Record(key="k", data={"id": "I-1", "slaBreachReason": "late"})],
                "req-1",
                0,
                "gs://audit/x",
            )
    message = str(exc.value)
    write_evidence(
        test_id,
        [
            f"table_id={table_id}",
            f"runtime_error={message}",
            "expect per-row error payload from FakeBQ insert_rows_json",
        ],
    )
    assert "insert_rows_json errors" in message
    assert "slaBreachReason" in message


def test_fakebq_float64_coercion_demonstrates_precision_loss() -> None:
    test_id = "SELF-02"
    reset_state()
    table_id = resolved_table_id()
    original = "9007199254740993"
    with SinkPatch():
        append_records(
            "sentinel_raw.incidents",
            [
                Record(
                    key="k",
                    data={
                        "id": "I-2",
                        "orderItemId": original,
                        "orderItemUnitId": "1234567890123456789",
                        "threads_communicationId": "9007199254740993",
                    },
                )
            ],
            "req-2",
            0,
            "gs://audit/x",
        )
    stored = FakeBQ.Client.fetch_rows(table_id)
    assert stored, "expected one row in FakeBQ"
    stored_value = stored[0]["orderItemId"]
    write_evidence(
        test_id,
        [
            f"original={original}",
            f"stored_orderItemId={stored_value}",
            "precision_loss_expected_for_FLOAT64_above_2^53",
        ],
    )
    assert str(stored_value) != original


def test_fakebq_does_not_autopopulate_ingested_at() -> None:
    test_id = "SELF-03"
    reset_state()
    table_id = resolved_table_id()
    with SinkPatch():
        append_records(
            "sentinel_raw.incidents",
            [Record(key="k", data={"id": "I-3"})],
            "req-3",
            0,
            "gs://audit/x",
        )
    stored = FakeBQ.Client.fetch_rows(table_id)
    assert stored, "expected one row in FakeBQ"
    row = stored[0]
    write_evidence(
        test_id,
        [
            f"row_keys={sorted(row.keys())}",
            f"_ingested_at_present={'_ingested_at' in row}",
            f"_ingested_at_value={row.get('_ingested_at')}",
        ],
    )
    assert "_ingested_at" not in row or row["_ingested_at"] is None
    SELFTEST_PROOF_FILE.write_text("ok\n", encoding="utf-8")
