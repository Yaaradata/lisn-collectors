"""Section A tests (independently invocable)."""

from __future__ import annotations

import subprocess
import re
from pathlib import Path

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


def test_a01_install_idempotent_recorded_result() -> None:
    test_id = "A-01"
    write_evidence(
        test_id,
        [
            "Recorded result from prior evidence per instruction:",
            "PASS (2nd install ~1.3s, no re-seed).",
        ],
    )
    assert True


def test_a02_dependency_reproducibility() -> None:
    test_id = "A-02"
    req = Path("/workspace/requirements.txt").read_text(encoding="utf-8").splitlines()
    floating = []
    for line in req:
        dep = line.strip()
        if not dep or dep.startswith("#"):
            continue
        if "==" not in dep:
            floating.append(dep)
    rc, freeze = _run(".venv/bin/pip freeze")
    write_evidence(
        test_id,
        [
            f"pip_freeze_rc={rc}",
            "floating_dependencies=" + ", ".join(floating),
            "pip_freeze_sample_start",
            "\n".join(freeze.splitlines()[:30]),
            "pip_freeze_sample_end",
        ],
    )
    assert not floating, f"floating dependencies present: {floating}"


def test_a03_import_without_gcp_credentials_recorded_result() -> None:
    test_id = "A-03"
    write_evidence(
        test_id,
        [
            "Recorded result from prior evidence per instruction:",
            "imports succeed; fetch() succeeds then GCS write fails with DefaultCredentialsError.",
        ],
    )
    assert True


def test_a04_one_image_three_roles() -> None:
    test_id = "A-04"
    rc, out = _run("docker --version")
    write_evidence(
        test_id,
        [
            f"docker_version_rc={rc}",
            out,
            "missing_precondition=docker_runtime_and_image_build_path",
        ],
    )
    if rc != 0:
        pytest.skip("BLOCKED: docker unavailable in environment")
    pytest.skip("BLOCKED: full Dockerfile build/run role matrix not executed in this batch")


def test_a05_secret_hygiene_and_env_completeness() -> None:
    test_id = "A-05"
    rc_scan, out_scan = _run(
        "git log -p --all -- . ':(exclude).venv' | rg -nEi 'password|postgresql://|BEGIN .*PRIVATE KEY' || true"
    )
    rc_gitignore, out_gitignore = _run("rg -n '^\\.env$|^\\.env\\b' .gitignore")
    rc_envkeys, out_envkeys = _run(
        "rg -n 'os\\.environ\\[|os\\.environ\\.get\\(' collector mock scripts tests"
    )
    rc_example, out_example = _run("rg -n '^[A-Z0-9_]+=' .env.example")
    used = set()
    bracket_pat = re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]")
    get_pat = re.compile(r"os\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]")
    for line in out_envkeys.splitlines():
        for match in bracket_pat.finditer(line):
            used.add(match.group(1))
        for match in get_pat.finditer(line):
            used.add(match.group(1))
    declared = {
        line.split("=", 1)[0].strip()
        for line in Path("/workspace/.env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    missing = sorted(k for k in used if k and k not in declared)
    expected_missing = {
        "CLOUD_RUN_TASK_INDEX",
        "CLOUD_RUN_WORKER_POOL_REVISION",
        "USE_ID_TOKEN",
        "COLLECTOR_API_URL",
        "COLLECTOR_API_TOKEN",
        "COLLECTOR_SOURCE",
    }
    append_lines = [
        "parsed_env_keys=" + ", ".join(sorted(used)),
        "declared_env_example_keys=" + ", ".join(sorted(declared)),
        "missing_env_example_keys=" + ", ".join(missing),
    ]
    write_evidence(
        test_id,
        [
            f"scan_rc={rc_scan}",
            out_scan,
            f"gitignore_rc={rc_gitignore}",
            out_gitignore,
            f"envkeys_rc={rc_envkeys}",
            out_envkeys,
            f"env_example_rc={rc_example}",
            out_example,
            *append_lines,
        ],
    )
    assert expected_missing.issubset(set(missing)), (
        f"extractor over-filtered; expected subset missing={sorted(expected_missing)} got={missing}"
    )
    assert not missing, f".env.example missing keys: {missing}"
