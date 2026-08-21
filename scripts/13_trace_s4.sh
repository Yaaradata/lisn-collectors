#!/usr/bin/env bash

# Regenerates docs/trace/S4.md — Sprint 4 reliability exit evidence.
# Mostly read-only checks against a live local stack; also runs one manual
# sweep and the five failure tests (those mutate state by design).

source scripts/_common.sh

TRACE_FILE="docs/trace/S4.md"
mkdir -p docs/trace

need curl
need python3
need psql

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${PROJECT:?PROJECT required}"

ROOT="$(pwd)"
export PYTHONPATH="${ROOT}"
# Always hit the LOCAL mock for trace/failure demos — .env may hold the
# internal Cloud Run URL which is unreachable from a laptop.
export SENTINEL_URL="http://127.0.0.1:8081"
export COLLECTOR_API_URL="http://127.0.0.1:8080"
export MOCK_SENTINEL_URL="http://127.0.0.1:8081"
export PROCRASTINATE_APP="collector.app.app"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

OPERATOR="$(gcloud config get-value account 2>/dev/null || echo unknown)"
TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

PASS_N=0
FAIL_N=0
declare -A CHECKLIST

section() {
  local id="$1"
  local title="$2"
  printf '\n## %s — %s\n\n' "$id" "$title" >>"$TRACE_FILE"
}

record_cmd() {
  printf '### Command\n\n```bash\n%s\n```\n\n' "$1" >>"$TRACE_FILE"
}

record_output() {
  local masked
  masked="$(printf '%s' "$1" | mask)"
  printf '### Output\n\n```\n%s\n```\n\n' "$masked" >>"$TRACE_FILE"
}

record_result() {
  local result="$1"
  printf '### Result: **%s**\n\n' "$result" >>"$TRACE_FILE"
  if [[ "$result" == "PASS" ]]; then
    PASS_N=$((PASS_N + 1))
  else
    FAIL_N=$((FAIL_N + 1))
  fi
}

set_checklist() {
  CHECKLIST["$1"]="$2"
}

run_capture() {
  set +e
  OUT="$(eval "$@" 2>&1)"
  RC=$?
  set -e
}

{
  printf '# Sprint 4 Trace (S4)\n\n'
  printf '| Field | Value |\n|---|---|\n'
  printf '| project | %s |\n' "$PROJECT"
  printf '| operator | %s |\n' "$OPERATOR"
  printf '| generated_at_utc | %s |\n' "$TIMESTAMP"
  printf '| note | Sweeper + failure tests mutate state; ops endpoints are read-only |\n'
  printf '\n'
} >"$TRACE_FILE"

# ── 1. Manual sweeper ──────────────────────────────────────────────────────
section "1" "Manual sweeper (sweep-now)"
record_cmd "python scripts/sweep_now.py"
run_capture "\"$PY\" scripts/sweep_now.py"
record_output "$OUT"
if (( RC == 0 )) && printf '%s' "$OUT" | grep -q "stalled_jobs_retried"; then
  record_result "PASS"
  set_checklist "manual_sweep" "PASS"
else
  record_result "FAIL"
  set_checklist "manual_sweep" "FAIL"
fi

# ── 2. /v1/health/detail ────────────────────────────────────────────────────
section "2" "GET /v1/health/detail"
record_cmd "curl -s ${COLLECTOR_API_URL}/v1/health/detail"
run_capture "curl -sf \"${COLLECTOR_API_URL}/v1/health/detail\""
record_output "$OUT"
if (( RC == 0 )) && printf '%s' "$OUT" | grep -q "live_workers"; then
  record_result "PASS"
  set_checklist "health_detail" "PASS"
else
  record_result "FAIL"
  set_checklist "health_detail" "FAIL"
  warn "API may be down — start make api (and mock/worker) before re-running trace"
fi

# ── 3. /v1/reconcile ────────────────────────────────────────────────────────
section "3" "GET /v1/reconcile"
record_cmd "curl -s '${COLLECTOR_API_URL}/v1/reconcile?minutes=0'"
run_capture "curl -sf \"${COLLECTOR_API_URL}/v1/reconcile?minutes=0\""
record_output "$OUT"
if (( RC == 0 )) && printf '%s' "$OUT" | grep -q "unloaded"; then
  record_result "PASS"
  set_checklist "reconcile" "PASS"
else
  record_result "FAIL"
  set_checklist "reconcile" "FAIL"
fi

# ── 4. /v1/dead-letter ──────────────────────────────────────────────────────
section "4" "GET /v1/dead-letter"
record_cmd "curl -s ${COLLECTOR_API_URL}/v1/dead-letter"
run_capture "curl -sf \"${COLLECTOR_API_URL}/v1/dead-letter\""
record_output "$OUT"
if (( RC == 0 )) && printf '%s' "$OUT" | grep -q '"dead"'; then
  record_result "PASS"
  set_checklist "dead_letter" "PASS"
else
  record_result "FAIL"
  set_checklist "dead_letter" "FAIL"
fi

# ── 5. Live workers + heartbeat ages ────────────────────────────────────────
section "5" "Live workers and heartbeat ages"
record_cmd "psql \$COLLECTOR_DSN -c \"SELECT id, now()-last_heartbeat AS age FROM procrastinate_workers\""
run_capture "psql \"\$COLLECTOR_DSN\" -v ON_ERROR_STOP=1 -c \"
SELECT id,
       last_heartbeat,
       now() - last_heartbeat AS heartbeat_age,
       CASE WHEN now() - last_heartbeat < interval '60 seconds' THEN 'live' ELSE 'stale' END AS state
FROM procrastinate_workers
ORDER BY id;
SELECT count(*) FILTER (WHERE now() - last_heartbeat < interval '60 seconds')::int AS live_workers
FROM procrastinate_workers;
\""
record_output "$OUT"
if (( RC == 0 )); then
  record_result "PASS"
  set_checklist "workers" "PASS"
else
  record_result "FAIL"
  set_checklist "workers" "FAIL"
fi

# ── 6. Five failure tests ───────────────────────────────────────────────────
FAILURE_TESTS=(
  test_hard_kill_recovery
  test_source_failure_retries
  test_reconcile_detects_silent_gap
  test_dead_letter
  test_sweeper_does_not_double_recover
)

section "6" "Failure tests (Sprint 4 gate)"
printf 'Each test is recorded individually. Full stack is started via scripts/12_failures.sh logic when run as `make failure-demos`; here we invoke pytest per node against a stack that must already be up (mock :8081, API :8080, maintenance worker). Sentinel workers are owned by the tests.\n\n' >>"$TRACE_FILE"

for t in "${FAILURE_TESTS[@]}"; do
  printf '### %s\n\n' "$t" >>"$TRACE_FILE"
  record_cmd "pytest tests/test_failures.py::${t} -v"
  run_capture "\"$PY\" -m pytest \"tests/test_failures.py::${t}\" -v"
  record_output "$OUT"
  if (( RC == 0 )); then
    record_result "PASS"
    set_checklist "$t" "PASS"
  else
    record_result "FAIL"
    set_checklist "$t" "FAIL"
  fi
done

# ── 7. Exit checklist ───────────────────────────────────────────────────────
section "7" "Sprint 4 exit checklist"

{
  printf '| Check | Result |\n'
  printf '|---|---|\n'
  printf '| Manual sweeper returns dict | %s |\n' "${CHECKLIST[manual_sweep]:-FAIL}"
  printf '| /v1/health/detail reachable | %s |\n' "${CHECKLIST[health_detail]:-FAIL}"
  printf '| /v1/reconcile reachable | %s |\n' "${CHECKLIST[reconcile]:-FAIL}"
  printf '| /v1/dead-letter reachable | %s |\n' "${CHECKLIST[dead_letter]:-FAIL}"
  printf '| Worker heartbeats queryable | %s |\n' "${CHECKLIST[workers]:-FAIL}"
  printf '| test_hard_kill_recovery | %s |\n' "${CHECKLIST[test_hard_kill_recovery]:-FAIL}"
  printf '| test_source_failure_retries | %s |\n' "${CHECKLIST[test_source_failure_retries]:-FAIL}"
  printf '| test_reconcile_detects_silent_gap | %s |\n' "${CHECKLIST[test_reconcile_detects_silent_gap]:-FAIL}"
  printf '| test_dead_letter | %s |\n' "${CHECKLIST[test_dead_letter]:-FAIL}"
  printf '| test_sweeper_does_not_double_recover | %s |\n' "${CHECKLIST[test_sweeper_does_not_double_recover]:-FAIL}"
  printf '\n'
  printf '**Totals:** PASS=%s FAIL=%s\n\n' "$PASS_N" "$FAIL_N"
  if (( FAIL_N == 0 )); then
    printf '**SPRINT 4 GATE: PASSED**\n'
  else
    printf '**SPRINT 4 GATE: FAILED** — see sections above\n'
  fi
} >>"$TRACE_FILE"

ok "Wrote ${TRACE_FILE} (PASS=${PASS_N} FAIL=${FAIL_N})"
if (( FAIL_N > 0 )); then
  exit 1
fi
