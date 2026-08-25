"""Pytest fixtures for audit tests."""

from __future__ import annotations

import os

import pytest

from tests.audit.helpers import SELFTEST_PROOF_FILE, SMOKE_PROOF_FILE


@pytest.fixture(autouse=True)
def _ensure_raw_bucket_env() -> None:
    if not os.environ.get("RAW_BUCKET"):
        os.environ["RAW_BUCKET"] = "audit-bucket"


@pytest.fixture
def require_pipeline_smoke() -> None:
    if not SMOKE_PROOF_FILE.exists():
        pytest.skip(
            "BLOCKED: run tests/audit/test_pipeline_smoke.py::test_pipeline_one_page_nonzero_rows first"
        )


@pytest.fixture
def require_fakes_selftest() -> None:
    if not SELFTEST_PROOF_FILE.exists():
        pytest.skip(
            "BLOCKED: run tests/audit/test_fakes_selftest.py first and ensure it passes"
        )
