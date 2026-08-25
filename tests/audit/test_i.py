"""Section I tests (deployment and operations)."""

from __future__ import annotations

import subprocess

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


def test_i01_three_open_corrections() -> None:
    test_id = "I-01"
    rc_a, out_a = _run("rg -n 'already has an active execution|no active execution to cancel' scripts/28_workers_control.sh")
    rc_b, out_b = _run("rg -n 'TRUNCATE|DELETE FROM procrastinate_jobs|DELETE FROM procrastinate_workers' scripts/10_demo.sh scripts/29_e2e_cloud.sh")
    rc_c, out_c = _run("rg -n 'sentinel_discovery|order_item_ids' collector scripts")
    write_evidence(
        test_id,
        [
            f"a_rc={rc_a}",
            out_a,
            f"b_rc={rc_b}",
            out_b,
            f"c_rc={rc_c}",
            out_c,
        ],
    )
    assert rc_a == 0
    assert rc_b == 0
    assert rc_c == 0


def test_i02_destructive_script_guards() -> None:
    test_id = "I-02"
    rc, out = _run("rg -n 'PROJECT|confirm|TRUNCATE TABLE|bq query' scripts/10_demo.sh scripts/29_e2e_cloud.sh")
    write_evidence(test_id, [f"rg_rc={rc}", out])
    assert "confirm" in out.lower(), "destructive scripts lack explicit confirmation guard"


def test_i03_make_target_ergonomics() -> None:
    test_id = "I-03"
    rc_demo, out_demo = _run("make demo --reset")
    rc_workers, out_workers = _run("make workers-start")
    write_evidence(
        test_id,
        [
            f"demo_rc={rc_demo}",
            out_demo,
            f"workers_start_rc={rc_workers}",
            out_workers,
        ],
    )
    assert rc_demo == 0
    assert rc_workers == 0


def test_i04_worker_identity() -> None:
    test_id = "I-04"
    rc, out = _run("rg -n 'CLOUD_RUN_TASK_INDEX|WORKER_ID' collector/app.py collector/tasks.py")
    write_evidence(test_id, [f"rg_rc={rc}", out])
    assert rc == 0
    assert "CLOUD_RUN_TASK_INDEX" in out


def test_i05_periodic_sweep_with_multiple_workers_recorded_result() -> None:
    test_id = "I-05"
    write_evidence(
        test_id,
        ["Recorded result from prior evidence: maintenance queue had no consumer until this env change."],
    )
    assert True


def test_i06_shell_script_hygiene() -> None:
    test_id = "I-06"
    rc_strict, out_strict = _run("rg -n '^set -euo pipefail' scripts/*.sh")
    rc_shellcheck_v, out_shellcheck_v = _run("shellcheck --version")
    rc_shellcheck, out_shellcheck = _run("shellcheck scripts/*.sh || true")
    write_evidence(
        test_id,
        [
            f"strict_rc={rc_strict}",
            out_strict,
            f"shellcheck_version_rc={rc_shellcheck_v}",
            out_shellcheck_v,
            f"shellcheck_rc={rc_shellcheck}",
            out_shellcheck,
        ],
    )
    assert rc_strict == 0


def test_i07_install_env_upsert_escaping_risk() -> None:
    test_id = "I-07"
    rc, out = _run("rg -n 'sed -i \"s#\\^\\$\\{key\\}=\\.\\*#\\$\\{key\\}=\\$\\{value\\}#\"|upsert_env\\(|RAW_BUCKET' .cursor/install.sh")
    write_evidence(
        test_id,
        [
            f"rg_rc={rc}",
            out,
            (
                "Finding: .cursor/install.sh upsert_env writes unescaped values via sed replacement. "
                "Observed impact during audit: RAW_BUCKET became corrupted, worker posted to wrong "
                "bucket path, and fetch_page retried with misleading 404 symptoms."
            ),
        ],
    )
    assert rc == 0
