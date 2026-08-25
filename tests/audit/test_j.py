"""Section J tests (documentation truth)."""

from __future__ import annotations

import subprocess

import pytest

from tests.audit.helpers import write_evidence


def _run(cmd: str) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        shell=True,
        check=False,
        capture_output=True,
        text=True,
        cwd="/workspace",
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_j01_comment_claims_vs_behavior() -> None:
    test_id = "J-01"
    rc, out = _run(
        "rg -n 'derives ONLY|sweeper finds orphan rows|one new source module|incident id alone|idempotent so nothing corrupts' "
        "collector/raw.py collector/api.py collector/contract.py collector/sources/sentinel.py collector/tasks.py"
    )
    write_evidence(test_id, [f"rg_rc={rc}", out])
    assert rc == 0
    assert "sweeper finds orphan rows" not in out


def test_j02_readme_accuracy() -> None:
    test_id = "J-02"
    write_evidence(
        test_id,
        [
            "BLOCKED: requires clean-clone execution of README path end-to-end without external prior setup.",
            "missing_precondition=fresh_clone_isolated_environment_run",
        ],
    )
    pytest.skip("BLOCKED: missing fresh-clone isolated environment precondition")
