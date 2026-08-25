"""Section G tests (independently invocable)."""

from __future__ import annotations

from tests.audit.helpers import blocked


def test_g01_field_completeness_both_ways() -> None:
    blocked("G-01", "source/schema field parity check pending")


def test_g02_schema_drift_unknown_field(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    blocked("G-02", "schema-drift one-page run pending")


def test_g03_numeric_fidelity_at_incident_grain(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    blocked("G-03", "numeric round-trip fidelity run pending")


def test_g04_timezone_fidelity(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    blocked("G-04", "timezone normalization run pending")


def test_g05_provenance_columns(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    blocked("G-05", "provenance/raw-manifest integrity run pending")
