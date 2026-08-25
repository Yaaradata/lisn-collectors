"""Section F tests (independently invocable)."""

from __future__ import annotations

from tests.audit.helpers import blocked, timed_window


def test_f01_single_worker_rate_floor_enforced(require_pipeline_smoke: None) -> None:
    with timed_window("F-01", floor_seconds=60):
        # Manual procedure must run for >= 60s.
        pass


def test_f02_multi_worker_ceiling(require_pipeline_smoke: None) -> None:
    blocked("F-02", "3-worker and 6-worker ceiling run pending")


def test_f03_throughput_at_pilot_shape_run_to_conclusion(require_pipeline_smoke: None) -> None:
    blocked("F-03", "run-to-conclusion execution pending (must not be shortened)")


def test_f04_connection_pressure(require_pipeline_smoke: None) -> None:
    blocked("F-04", "20-worker connection pressure run pending")
